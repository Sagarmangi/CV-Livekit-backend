"""Call recording: LiveKit Egress to a local file, then Cloudinary.

Two hops rather than one, because Egress can only upload to S3, GCS, Azure or
AliOSS -- Cloudinary is not among them and has no S3-compatible endpoint. So
Egress writes the mixed audio to a directory shared between its container and
this worker, and once the file is finalised the worker uploads it and throws the
local copy away. `CLOUDINARY_*` was already in .env.example waiting for this.

Everything here is best-effort by design. A call that happened is worth more
than a recording of it, so no failure in this module is allowed to reach the
caller or fail the call log: a missing recording writes NULL to
`call_logs.recording_url`, exactly as before this existed, and says why in the
worker log.

Recording is opt-in (`CALL_RECORDING_ENABLED`) for a reason beyond caution about
cost. Recording an inbound PSTN call carries consent obligations that vary by
jurisdiction, and only the deployment's owner knows which apply -- so switching
it on is a deliberate act, and whoever does it is the one deciding the greeting
in flow.py says whatever their jurisdiction requires.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from livekit import api
from livekit.protocol import egress as egress_proto

from .settings import RecordingSettings, livekit_settings, recording_settings

logger = logging.getLogger("worker.recording")

# How long to wait for Egress to finalise the file after being told to stop.
# This runs inside call teardown, so it is a bounded wait rather than a generous
# one: the alternative to giving up is holding the job open, and the Slack
# summary and call_logs row behind it, on a file that may never arrive.
_FINALISE_TIMEOUT = 20.0
_POLL_INTERVAL = 0.5


@dataclass
class Recording:
    """Handle for an in-flight recording, returned by `start`."""

    egress_id: str
    room_name: str


def _http_url(ws_url: str) -> str:
    """LIVEKIT_URL is a websocket URL (the SDK wants it that way); the Twirp
    API the egress service is reached over is plain HTTP on the same host."""
    if ws_url.startswith("wss://"):
        return "https://" + ws_url[len("wss://") :]
    if ws_url.startswith("ws://"):
        return "http://" + ws_url[len("ws://") :]
    return ws_url


def _egress_filepath(settings: RecordingSettings, room_name: str) -> str:
    """Where Egress writes, expressed in *its* filesystem, not ours.

    The egress container bind-mounts the same directory this worker reads from,
    but the two see it at different paths (`/out` inside, whatever
    RECORDING_OUTPUT_DIR says outside). Getting this pair wrong is the one
    misconfiguration that produces a perfectly successful egress and no file
    the worker can find, so `local_path` below is deliberately derived from the
    filename Egress reports rather than recomputed.
    """
    return f"{settings.egress_dir.rstrip('/')}/{room_name}.ogg"


def _local_path(settings: RecordingSettings, egress_filename: str) -> str:
    return os.path.join(settings.output_dir, os.path.basename(egress_filename))


async def start(room_name: str) -> Recording | None:
    """Starts an audio-only room recording. Returns None if recording is off or
    couldn't start -- callers treat that as "no recording", not as an error.

    Audio-only on purpose: these are phone calls, so there is no video to
    composite, and `audio_only` keeps the output a single small OGG of both
    sides mixed. OGG rather than MP3 because Opus passes through without being
    re-encoded, which matters on a box that already runs the SFU, the SIP bridge
    and this worker -- Cloudinary can deliver an MP3 transcode from the same
    asset if a browser needs one.
    """
    try:
        settings = recording_settings()
    except Exception:
        # Misconfigured storage must not cost a call. `main()` already refuses
        # to boot the worker on this, so reaching here means the environment
        # changed under a running process -- log it and take the call unrecorded.
        logger.exception("recording is misconfigured; call proceeds unrecorded")
        return None

    if not settings.enabled:
        return None

    livekit = livekit_settings()
    try:
        async with api.LiveKitAPI(
            url=_http_url(livekit.url),
            api_key=livekit.api_key,
            api_secret=livekit.api_secret,
        ) as client:
            info = await client.egress.start_room_composite_egress(
                egress_proto.RoomCompositeEgressRequest(
                    room_name=room_name,
                    audio_only=True,
                    file_outputs=[
                        egress_proto.EncodedFileOutput(
                            file_type=egress_proto.EncodedFileType.OGG,
                            filepath=_egress_filepath(settings, room_name),
                            # Nothing reads the .json sidecar Egress writes next
                            # to the media, and it would be a second file to
                            # clean up out of the shared directory.
                            disable_manifest=True,
                        )
                    ],
                )
            )
    except Exception:
        # The usual cause is no egress service running -- it's a separate
        # container (see infra/docker-compose.yml) and the API accepts the
        # request only to have it time out unassigned if nothing is listening.
        logger.exception("couldn't start recording for room %s; call proceeds unrecorded", room_name)
        return None

    logger.info("recording started: egress=%s room=%s", info.egress_id, room_name)
    return Recording(egress_id=info.egress_id, room_name=room_name)


async def finish(recording: Recording | None) -> str | None:
    """Stops the egress, waits for the file, uploads it, deletes the local copy.

    Returns the Cloudinary URL, or None if any step didn't work out. Called from
    the shutdown callback, so it must not raise: whatever happens here, the
    call_logs row still gets written.
    """
    if recording is None:
        return None

    settings = recording_settings()
    try:
        info = await asyncio.wait_for(
            _stop_and_wait(recording),
            timeout=_FINALISE_TIMEOUT + 5.0,
        )
    except Exception:
        logger.exception("recording %s never finalised", recording.egress_id)
        return None

    if info is None or not info.file_results:
        logger.warning(
            "recording %s produced no file (status=%s error=%s)",
            recording.egress_id,
            egress_proto.EgressStatus.Name(info.status) if info else "unknown",
            (info.error if info else "") or "none reported",
        )
        return None

    # Checked before the file, because a failed egress still reports the
    # filename it was going to write. Blaming the bind mount for that sends
    # whoever is debugging after a mount that was never wrong -- which is
    # exactly what happened the first time this ran.
    if info.status != egress_proto.EgressStatus.EGRESS_COMPLETE:
        logger.error(
            "egress %s ended as %s (error: %s) -- `docker logs` on the egress "
            "container has the reason. A common one is that the container runs "
            "as a non-root user and can't write to the output directory.",
            recording.egress_id,
            egress_proto.EgressStatus.Name(info.status),
            info.error or "none reported",
        )
        return None

    path = _local_path(settings, info.file_results[0].filename)
    if not os.path.exists(path):
        logger.error(
            "egress reported %s complete but this worker sees nothing at %s -- "
            "check that RECORDING_OUTPUT_DIR and RECORDING_EGRESS_DIR are the two "
            "ends of the same bind mount (`docker inspect -f '{{json .Mounts}}'`)",
            info.file_results[0].filename,
            path,
        )
        return None

    url = await _upload(path, recording.room_name, settings)
    if url is None:
        # Left on disk on purpose: an upload can fail for a transient reason,
        # and a kept file can be uploaded by hand later. A deleted one can't.
        logger.error("keeping %s on disk since the upload failed", path)
        return None

    try:
        os.remove(path)
    except OSError:
        logger.warning("uploaded %s but couldn't delete the local copy", path, exc_info=True)

    logger.info("recording uploaded for room %s: %s", recording.room_name, url)
    return url


async def _stop_and_wait(recording: Recording) -> egress_proto.EgressInfo | None:
    """Stop, then poll until the file is written.

    Stopping and reading the result are separate steps because they are: the
    response to `stop_egress` reports EGRESS_ENDING, before the file has been
    muxed and closed. Uploading at that point ships a truncated recording.

    A room-composite egress also stops itself when the room closes, which is
    racing this during teardown -- so an "already stopped" failure from
    `stop_egress` is an ordinary outcome, and the polling below is what actually
    determines the result either way.
    """
    livekit = livekit_settings()
    async with api.LiveKitAPI(
        url=_http_url(livekit.url),
        api_key=livekit.api_key,
        api_secret=livekit.api_secret,
    ) as client:
        try:
            await client.egress.stop_egress(
                egress_proto.StopEgressRequest(egress_id=recording.egress_id)
            )
        except Exception:  # noqa: BLE001 -- see docstring; the room may have closed first
            logger.debug("stop_egress on %s failed", recording.egress_id, exc_info=True)

        deadline = asyncio.get_running_loop().time() + _FINALISE_TIMEOUT
        last: egress_proto.EgressInfo | None = None
        while asyncio.get_running_loop().time() < deadline:
            response = await client.egress.list_egress(
                egress_proto.ListEgressRequest(egress_id=recording.egress_id)
            )
            if response.items:
                last = response.items[0]
                if last.status in (
                    egress_proto.EgressStatus.EGRESS_COMPLETE,
                    egress_proto.EgressStatus.EGRESS_FAILED,
                    egress_proto.EgressStatus.EGRESS_ABORTED,
                    egress_proto.EgressStatus.EGRESS_LIMIT_REACHED,
                ):
                    return last
            await asyncio.sleep(_POLL_INTERVAL)

        logger.warning(
            "gave up waiting for recording %s after %.0fs (last status=%s)",
            recording.egress_id,
            _FINALISE_TIMEOUT,
            egress_proto.EgressStatus.Name(last.status) if last else "unknown",
        )
        return last


async def _upload(path: str, room_name: str, settings: RecordingSettings) -> str | None:
    """Uploads to Cloudinary and returns the delivery URL.

    `resource_type="video"` is not a mistake -- Cloudinary has no separate audio
    type and handles audio files under the video resource type. The upload is
    synchronous in Cloudinary's SDK, so it goes to a thread; this runs during
    teardown and blocking the event loop here would stall the rest of it.

    `public_id` is the room name, which is unique per call, so a retry of the
    same call overwrites rather than accumulating duplicates.
    """
    try:
        import cloudinary
        import cloudinary.uploader
    except ImportError:
        logger.error("cloudinary isn't installed -- `uv sync` in agent-worker/")
        return None

    def upload() -> str:
        cloudinary.config(
            cloud_name=settings.cloudinary_cloud_name,
            api_key=settings.cloudinary_api_key,
            api_secret=settings.cloudinary_api_secret,
            secure=True,
        )
        result = cloudinary.uploader.upload_large(
            path,
            resource_type="video",
            folder=settings.cloudinary_folder,
            public_id=room_name,
            overwrite=True,
            # Recordings of customer calls have no business being enumerable
            # from a public listing endpoint or served to anyone holding a
            # guessable URL. `authenticated` means delivery needs a signed URL,
            # which is the dashboard's job to mint when it renders the link.
            type="authenticated",
        )
        return result["secure_url"]

    try:
        return await asyncio.to_thread(upload)
    except Exception:
        logger.exception("Cloudinary upload of %s failed", path)
        return None
