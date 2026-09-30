"""Spam-call detection: drop robocalls and cold sales pitches on the caller's
first reply.

Configured as `tools` rows (see the 0020 migration) so admins get the library UI
they already know -- attach per agent, edit the statement list, toggle it off --
but *not* exposed to the model as callable functions. That distinction is the
whole design:

- A function tool only fires if the LLM chooses to call it. A caller who keeps
  talking can argue a model out of hanging up, and nothing records that it
  should have.
- Prompt instructions have the same problem plus a worse one: flow.py used to
  carry hardcoded scam/sales-pitch heuristics and they were deleted for silently
  overriding whatever the admin had actually written (see its module docstring).

So this runs as a plain check on the first final transcript, before the model has
a say. Two stages, cheapest first:

1. `literal_match` -- normalized containment against the admin's statements. Free
   and instant, and the reason the statement list is worth maintaining rather
   than being decoration on a prompt.
2. `classify` -- one LLM call for the semantic case, using those same statements
   as examples plus each detector's description as extra guidance. Bounded and
   fails open, because a classifier that breaks must never drop a real call.

It runs concurrently with the agent's normal reply rather than gating it, so a
legitimate caller pays nothing for the check. The cost of that choice is that a
spam caller may hear a word or two before the line drops; the alternative is
taxing every real call to look tidier on the calls we're hanging up on anyway.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from livekit.agents import JobContext
from livekit.agents.llm import ChatContext

from .llm_clients import build_utility_llm
from .models import DETECTOR_TOOL_TYPES, AgentConfig, CallOutcome
from .settings import ProviderSettings
from .state import CallState

logger = logging.getLogger("worker.spam")

# The semantic pass is racing the agent's own first reply, and its only possible
# outcome is hanging up -- so waiting a long time buys nothing. Past this, the
# call simply proceeds.
_CLASSIFY_TIMEOUT = 2.5

# Utterances too short to carry an intent. "Hello?" is not a sales pitch, and
# asking an LLM to judge it invites a coin flip on a real caller.
_MIN_CHARS = 12

_PUNCTUATION = re.compile(r"[^\w\s]")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Detector:
    """One attached detector tool, flattened out of its Supabase row."""

    kind: str  # "detect_bot_call" | "detect_sales_call"
    tool_name: str
    description: str
    statements: list[str]
    outcome: CallOutcome
    # None means "whatever _provider_choice defaults to" -- the row didn't pick.
    llm: str | None


@dataclass(frozen=True)
class Detection:
    """Why a call was dropped. Ends up in `call_logs.spam_detection`, which is
    the only record a silently-hung-up caller leaves behind -- so it names the
    detector, how it fired, and the text that triggered it."""

    outcome: CallOutcome
    detail: str


def _normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", text.lower())).strip()


def detectors_for(config: AgentConfig) -> list[Detector]:
    """The detectors actually in force for this call.

    Skips disabled rows, and skips a detector whose statement list is empty:
    with nothing to match literally and no examples to give the classifier, it
    would be asking an LLM to guess at "is this spam" with no definition of
    spam -- which is how real callers get hung up on.
    """
    found: list[Detector] = []
    for row in config.tools:
        outcome = DETECTOR_TOOL_TYPES.get(row.tool_type)
        if outcome is None:
            continue
        if not row.is_enabled:
            logger.info("detector %s is switched off; skipping", row.name)
            continue
        if not row.detector_statements:
            logger.warning(
                "detector %s has no example statements, so it can't distinguish spam from "
                "anything else -- skipping it rather than guessing",
                row.name,
            )
            continue
        found.append(
            Detector(
                kind=row.tool_type,
                tool_name=row.name,
                description=row.description or "",
                statements=row.detector_statements,
                outcome=outcome,
                llm=row.detector_llm,
            )
        )
    return found


def literal_match(transcript: str, detectors: list[Detector]) -> Detection | None:
    """First pass: does the utterance contain one of the admin's statements?

    Containment rather than equality, because these arrive as fragments of a
    longer sentence ("hi there, I'm calling about your Google business listing,
    do you have a moment"). Normalizing both sides means STT punctuation and
    casing don't decide the outcome.
    """
    haystack = _normalize(transcript)
    if not haystack:
        return None

    for detector in detectors:
        for statement in detector.statements:
            needle = _normalize(statement)
            if needle and needle in haystack:
                return Detection(
                    outcome=detector.outcome,
                    detail=f"{detector.tool_name}: matched statement {statement!r}",
                )
    return None


def _build_classifier_llm(provider_name: str, provider: ProviderSettings):
    """The classifier's LLM -- see llm_clients.build_utility_llm, which
    analysis.py shares. Gemini Flash is the default for this caller because this
    one gates a live call and latency is the binding constraint."""
    return build_utility_llm(provider_name, provider)


def _classifier_prompt(detectors: list[Detector]) -> str:
    """One prompt covering every attached detector.

    One call rather than one per detector: the categories are mutually exclusive
    from the caller's point of view, and two calls would double the cost and the
    latency to reach the same answer.
    """
    categories = []
    for detector in detectors:
        examples = "\n".join(f"    - {s}" for s in detector.statements)
        guidance = f"\n  Also treat as {detector.kind}: {detector.description}" if detector.description else ""
        categories.append(f"- {detector.kind}{guidance}\n  Examples:\n{examples}")

    labels = ", ".join(d.kind for d in detectors)
    return (
        "You classify the first thing a caller said to a company's phone receptionist.\n\n"
        f"Categories:\n" + "\n".join(categories) + "\n\n"
        f"Answer with exactly one word: {labels}, or none.\n\n"
        "Answer 'none' unless you are confident. These callers are hung up on immediately "
        "with no explanation, so a wrong answer disconnects a real customer mid-sentence. "
        "Someone asking about the company's own products or services, requesting support, "
        "or following up on existing business is always 'none', however they phrase it."
    )


async def classify(
    transcript: str, detectors: list[Detector], provider: ProviderSettings
) -> Detection | None:
    """Second pass: the semantic case the literal matcher can't reach.

    Returns None on anything unexpected -- no match, a timeout, a missing key, a
    provider outage. Failing open is deliberate and not negotiable: the worst
    outcome available here is hanging up on a paying customer because an API was
    slow.
    """
    if len(transcript.strip()) < _MIN_CHARS:
        return None

    by_kind = {d.kind: d for d in detectors}
    prompt = _classifier_prompt(detectors)

    chat_ctx = ChatContext.empty()
    chat_ctx.add_message(role="system", content=prompt)
    chat_ctx.add_message(role="user", content=transcript)

    async def run(llm) -> str:
        parts: list[str] = []
        stream = llm.chat(chat_ctx=chat_ctx)
        try:
            async for chunk in stream:
                if chunk.delta and chunk.delta.content:
                    parts.append(chunk.delta.content)
        finally:
            await stream.aclose()
        return "".join(parts)

    try:
        llm = _build_classifier_llm(_provider_choice(detectors), provider)
        answer = await asyncio.wait_for(run(llm), timeout=_CLASSIFY_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(
            "spam classifier didn't answer within %.1fs; letting the call continue",
            _CLASSIFY_TIMEOUT,
        )
        return None
    except Exception:
        logger.exception("spam classifier failed; letting the call continue")
        return None

    # Exact match against a known label, not containment: a model that answers
    # "this is not a detect_sales_call" contains the label while meaning the
    # opposite, and that mistake hangs up on a real customer. Two spellings are
    # accepted -- the whole answer, and its first word -- which covers the
    # model writing the label with spaces or with a trailing full stop, without
    # accepting a sentence that merely mentions it.
    normalized = _normalize(answer)
    candidates = [normalized.replace(" ", "_"), normalized.split(" ")[0] if normalized else ""]
    detector = next((by_kind[c] for c in candidates if c in by_kind), None)
    if detector is None:
        if normalized and normalized != "none":
            logger.info("spam classifier answered %r, which is not a label; continuing", answer[:80])
        return None

    return Detection(
        outcome=detector.outcome,
        detail=f"{detector.tool_name}: classified as {detector.kind} on {transcript[:120]!r}",
    )


def _provider_choice(detectors: list[Detector]) -> str:
    """Which LLM to classify with. Set on the detector row; Gemini otherwise.

    Both detectors are answered by one call, so there is one provider to pick and
    the first row that names one wins. Attaching two detectors that disagree
    doesn't run two classifiers -- it just means one of the two settings is
    ignored, which is better than doubling the latency of a first-reply gate to
    honour a preference nobody is likely to have set deliberately.
    """
    for detector in detectors:
        if detector.llm:
            return detector.llm
    return "gemini"


def watch_first_reply(
    ctx: JobContext,
    session,
    state: CallState,
    detectors: list[Detector],
    provider: ProviderSettings,
) -> None:
    """Classify the caller's first reply and hang up if it's spam.

    First reply only, by design: an opener is where a robocall and a cold pitch
    announce themselves, and checking every turn would run a classifier on every
    utterance of every legitimate call. A salesperson who opens with pleasantries
    gets through -- `end_call_instructions` is the backstop for those.

    Note for realtime agents: `user_input_transcribed` depends on there being a
    transcript. A gemini_live agent has no separate STT stage, so this may never
    fire there. It degrades to "no detection" rather than erroring, which is the
    right failure, but it is not protection -- see the dashboard copy.
    """
    if not detectors:
        return

    # Guards against a second transcript arriving while the first is still being
    # classified, which would run the LLM twice and could hang up twice.
    checked = False

    @session.on("user_input_transcribed")
    def _on_transcript(event) -> None:
        nonlocal checked
        if checked or not getattr(event, "is_final", False):
            return
        transcript = (getattr(event, "transcript", "") or "").strip()
        if not transcript:
            return
        checked = True
        asyncio.create_task(_check(transcript))

    async def _check(transcript: str) -> None:
        try:
            detection = literal_match(transcript, detectors) or await classify(
                transcript, detectors, provider
            )
        except Exception:
            # The handler above already marked the turn as checked, so a crash
            # here must not take the call with it -- the call is worth more than
            # the check.
            logger.exception("spam detection failed; letting the call continue")
            return

        if detection is None:
            logger.info("first reply cleared by spam detection")
            return

        # Written onto CallState rather than passed anywhere: the shutdown
        # callback reads it when it writes the call_logs row, so the drop is
        # still recorded even though the caller was given no explanation.
        state.outcome = detection.outcome
        state.spam_detection = detection.detail
        # "system", not "agent": this runs in the classifier task, outside the
        # conversation, and the model was never asked. Keeping the two apart is
        # what lets "the agent decided to hang up" be counted separately from
        # "a detector dropped the call" -- which is the number that matters when
        # reviewing whether a statement list is too broad. The reason detail is
        # already in spam_detection.
        state.claim_end("system", "spam_filter")
        logger.info("ending call as spam -- %s", detection.detail)
        ctx.delete_room()
