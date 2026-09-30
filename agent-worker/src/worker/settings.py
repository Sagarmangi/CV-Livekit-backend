"""Environment access, mirroring the pattern in dashboard/lib/env.ts.

Every secret is read lazily through one of these functions rather than at
import time, so a worker without, say, a Slack webhook configured still boots
and takes calls -- it just skips the notification instead of crashing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv(".env.local")
load_dotenv(".env")


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name}. "
            f"Copy agent-worker/.env.example to agent-worker/.env.local and fill it in."
        )
    return value


@dataclass(frozen=True)
class SupabaseSettings:
    url: str
    service_role_key: str


@dataclass(frozen=True)
class LiveKitSettings:
    url: str
    api_key: str
    api_secret: str
    agent_name: str
    # CPU load above which the worker tells LiveKit it can't take jobs.
    #
    # None -- the default -- means "never refuse a call because the machine is
    # busy", and it's the only defensible setting for a single worker: LiveKit's
    # load shedding exists so a *pool* can route a call to a less busy peer, and
    # with one worker there is no peer, so refusing simply loses the call.
    #
    # This is not theoretical. The framework's own production default is 0.7, and
    # a box that also runs a dashboard and Docker crosses it constantly --
    # measured on a dev machine here, load sat between 0.56 and 0.99. Every
    # dispatch arriving above the line is dropped with no error raised anywhere:
    # an inbound call just rings until the caller gives up, and the only trace is
    # a single "marking as unavailable" line in the worker log. Two out of three
    # test calls were lost this way at a 0.95 ceiling.
    #
    # Set WORKER_LOAD_THRESHOLD to a number below 1.0 to opt back into real load
    # reporting once there is a pool to shed to.
    load_threshold: float | None


@dataclass(frozen=True)
class ProviderSettings:
    """Deepgram covers both ends of the audio pipeline (STT + Aura TTS) --
    see VOICE_STACK_DECISION.md for why that beat keeping Cartesia.

    Groq, DeepSeek, and Gemini Live are all lazy-optional -- an agent only
    needs the key for whichever llm_provider it's actually set to, checked at
    the moment a call needs it (see entrypoint.py), not at worker startup. A
    worker that never runs a Groq-configured agent can leave GROQ_API_KEY
    unset entirely, same as the other two.
    """

    deepgram_api_key: str
    groq_api_key: str | None
    groq_model: str
    deepseek_api_key: str | None
    deepseek_model: str
    deepseek_base_url: str
    # Shared by both Gemini engines -- llm_provider='gemini' (Flash as a text
    # LLM in the Deepgram pipeline) and llm_provider='gemini_live'
    # (speech-to-speech). Same lazy-optional pattern as DeepSeek above.
    gemini_api_key: str | None
    # The speech-to-speech model, for llm_provider='gemini_live' only.
    gemini_model: str
    # The text model, for llm_provider='gemini' only. Separate setting because
    # the two are different model families and an agent can be switched between
    # them -- a live model name sent to the text API (or the reverse) is
    # rejected outright.
    #
    # gemini-2.5-flash is the default on measured time-to-first-token for a
    # one-sentence reply (451ms best / 499ms median, vs 535/648 for DeepSeek's
    # origin API) -- see the 0019 migration for the full comparison. It's also
    # the non-lite tier, which matters here because these agents call tools.
    gemini_llm_model: str
    # Gemini's "proactive audio": the model judges for itself whether speech was
    # addressed to it and stays quiet otherwise, so a background conversation
    # stops pulling a reply out of it. Native-audio models only, and it forces
    # the v1alpha endpoint -- off by default because a model that doesn't
    # support it rejects the session outright rather than ignoring the flag.
    gemini_proactive_audio: bool

    # Which LLM writes the post-call analysis -- the summary, the caller's
    # queries and the priority (see analysis.py). DeepSeek by default, and this
    # is the one place it's the obvious choice: the job runs after the caller has
    # hung up, so its ~1.6s per reply costs nothing, and it's roughly a tenth of
    # Gemini Flash's price for the same work. Set ANALYSIS_LLM=gemini to switch.
    analysis_llm: str

    # --- Turn taking -------------------------------------------------------
    # Which Deepgram STT to run. "flux" uses Deepgram's Flux model, which
    # decides end-of-turn from the speech itself and lets the session stop
    # waiting on a fixed silence timer -- the single biggest latency win
    # available on a phone call, where every stage is already paying for PSTN
    # and jitter-buffer delay. "nova" is the previous streaming-transcript
    # behaviour, kept as a one-variable rollback.
    stt_engine: str
    deepgram_flux_model: str
    # Confidence at which Flux calls the turn finished. Higher waits longer and
    # interrupts less; 0.7 is the value Deepgram and Vapi both settle on.
    flux_eot_threshold: float
    # Lower bar at which Flux emits a *provisional* end-of-turn, letting the
    # LLM start on a likely-complete utterance. Must sit below eot_threshold.
    flux_eager_eot_threshold: float
    # Hard ceiling on waiting for the confidence threshold after speech stops.
    flux_eot_timeout_ms: int


@dataclass(frozen=True)
class RecordingSettings:
    """Call recording -- LiveKit Egress writes a file, the worker uploads it to
    Cloudinary. See recording.py for why it takes two hops.

    Off unless CALL_RECORDING_ENABLED is set. That default is deliberate: these
    are inbound PSTN calls, and recording them carries consent obligations that
    depend on where the caller and the business are. Only the deployment's owner
    can decide that, so the switch is theirs to throw -- along with adding
    whatever disclosure their jurisdiction wants to the agent's greeting.
    """

    enabled: bool
    # The shared directory, seen from each side of the egress container's bind
    # mount: `output_dir` is this worker's path, `egress_dir` is the container's.
    # They are the same directory. Both exist because Egress is told where to
    # write in its own terms and the worker has to find the result in its own.
    output_dir: str
    egress_dir: str
    cloudinary_cloud_name: str | None
    cloudinary_api_key: str | None
    cloudinary_api_secret: str | None
    cloudinary_folder: str


@dataclass(frozen=True)
class SlackSettings:
    """Direct Slack incoming-webhook notifications -- there is no n8n hop in
    this build, per the project decision to skip n8n entirely. The worker
    posts straight to Slack and straight to Supabase after each call."""

    webhook_url: str | None
    # Where the dashboard is served, e.g. https://voiceagents.kodexolabs.co.
    # Lives here rather than in its own settings class because building a link
    # into a Slack message is the only thing the worker uses it for -- it has no
    # other reason to know the dashboard exists. Optional: with it unset the
    # lead alert simply omits its "Open call record" button, which is the one
    # part of that message that can't be assembled from the call alone.
    dashboard_base_url: str | None


@lru_cache
def supabase_settings() -> SupabaseSettings:
    return SupabaseSettings(
        url=_required("SUPABASE_URL"),
        service_role_key=_required("SUPABASE_SERVICE_ROLE_KEY"),
    )


@lru_cache
def livekit_settings() -> LiveKitSettings:
    return LiveKitSettings(
        url=_required("LIVEKIT_URL"),
        api_key=_required("LIVEKIT_API_KEY"),
        api_secret=_required("LIVEKIT_API_SECRET"),
        agent_name=os.environ.get("LIVEKIT_AGENT_NAME", "kodexo-inbound-agent"),
        load_threshold=_load_threshold(),
    )


def _load_threshold() -> float | None:
    """None when load shedding is off -- see LiveKitSettings.load_threshold."""
    raw = (os.environ.get("WORKER_LOAD_THRESHOLD") or "").strip().lower()
    if raw in ("", "off", "none", "disabled", "0"):
        return None
    # Clamped rather than trusted: the framework refuses to start on >= 1.0
    # outside dev mode, and a worker that won't boot is worse than one that
    # quietly caps a typo'd ceiling.
    return min(float(raw), 0.99)


@lru_cache
def provider_settings() -> ProviderSettings:
    return ProviderSettings(
        deepgram_api_key=_required("DEEPGRAM_API_KEY"),
        groq_api_key=os.environ.get("GROQ_API_KEY") or None,
        groq_model=os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
        deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY") or None,
        deepseek_model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        deepseek_base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        gemini_api_key=os.environ.get("GEMINI_API_KEY") or None,
        # Native-audio 2.5, not 3.x. The 3.x live models cannot accept a
        # client-initiated first turn, so an inbound agent on one of them never
        # greets the caller -- and if GEMINI_PROACTIVE_AUDIO is also set, they
        # reject the session outright and the call is silent end to end. Both
        # failures look identical from the phone: nobody speaks, ever.
        #
        # Defaulting to a 3.x model made silence the out-of-the-box behaviour
        # for the one thing this worker exists to do: answer inbound calls.
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-native-audio-latest"),
        gemini_llm_model=os.environ.get("GEMINI_LLM_MODEL", "gemini-2.5-flash"),
        gemini_proactive_audio=(os.environ.get("GEMINI_PROACTIVE_AUDIO") or "").strip().lower()
        in ("1", "true", "yes"),
        analysis_llm=(os.environ.get("ANALYSIS_LLM") or "deepseek").strip().lower(),
        stt_engine=(os.environ.get("DEEPGRAM_STT_ENGINE") or "flux").strip().lower(),
        deepgram_flux_model=os.environ.get("DEEPGRAM_FLUX_MODEL", "flux-general-en"),
        flux_eot_threshold=float(os.environ.get("DEEPGRAM_FLUX_EOT_THRESHOLD", "0.7")),
        flux_eager_eot_threshold=float(
            os.environ.get("DEEPGRAM_FLUX_EAGER_EOT_THRESHOLD", "0.5")
        ),
        flux_eot_timeout_ms=int(os.environ.get("DEEPGRAM_FLUX_EOT_TIMEOUT_MS", "600")),
    )


@lru_cache
def recording_settings() -> RecordingSettings:
    """Refuses to enable itself half-configured.

    Recording that silently doesn't happen is the worst outcome here -- the
    dashboard shows a call with no audio and nothing says why, and by then the
    call is over and unrepeatable. So a missing Cloudinary credential turns the
    feature off loudly at the first call instead of recording into a void.
    """
    enabled = (os.environ.get("CALL_RECORDING_ENABLED") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    cloud_name = os.environ.get("CLOUDINARY_CLOUD_NAME") or None
    api_key = os.environ.get("CLOUDINARY_API_KEY") or None
    api_secret = os.environ.get("CLOUDINARY_API_SECRET") or None

    if enabled and not (cloud_name and api_key and api_secret):
        raise RuntimeError(
            "CALL_RECORDING_ENABLED is set but CLOUDINARY_CLOUD_NAME / "
            "CLOUDINARY_API_KEY / CLOUDINARY_API_SECRET are not all present, so "
            "there would be nowhere to upload the recording to. Fill them in or "
            "unset CALL_RECORDING_ENABLED."
        )

    return RecordingSettings(
        enabled=enabled,
        output_dir=os.environ.get("RECORDING_OUTPUT_DIR", "/opt/kodexo/recordings"),
        egress_dir=os.environ.get("RECORDING_EGRESS_DIR", "/out"),
        cloudinary_cloud_name=cloud_name,
        cloudinary_api_key=api_key,
        cloudinary_api_secret=api_secret,
        cloudinary_folder=os.environ.get("CLOUDINARY_FOLDER", "kodexo-calls"),
    )


@lru_cache
def slack_settings() -> SlackSettings:
    return SlackSettings(
        webhook_url=os.environ.get("SLACK_WEBHOOK_URL") or None,
        dashboard_base_url=os.environ.get("DASHBOARD_BASE_URL") or None,
    )
