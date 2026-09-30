"""Post-call analysis: the three things about a call only a model can tell us.

This replaces an n8n flow that ran an OpenAI prompt over a Vapi webhook payload
and asked it to extract fourteen fields. That design was forced by where n8n sat
-- outside the call, holding nothing but an opaque payload, so even the caller's
phone number had to be mined out of it by a language model.

This worker *is* the call, so most of those fields are facts rather than
inferences and are read directly:

    conversation_id     -> call_logs.room_id / call_sid
    caller_phone        -> sip.phoneNumber, captured at answer time
    called_number       -> sip.trunkPhoneNumber, likewise
    caller_name         -> lead_name, from the record_lead_info tool
    duration_seconds    -> measured
    transcript          -> the session's own history
    transfer_attempted  -> whether the transfer tool ran
    callback_needed     -> whether a callback number was recorded
    has_error           -> whether the session emitted an ErrorEvent
    call_status         -> derived from the above, in `call_status()`

Asking a model to re-derive any of those would be slower, cost money, and be
*less* accurate -- a SIP attribute is exact where a model reading a transcript
can drop a digit from a phone number. Never infer what you can observe.

That leaves three genuine judgements, which is all this module asks for:
`user_queries`, `call_summary` and `priority`.

Runs in the shutdown callback, after the caller has hung up. Nothing is waiting
on it, which is what makes DeepSeek the right model here: it was rejected as the
conversation engine because ~1.6s per reply is unusable mid-call, but off the
call path that cost is irrelevant and it is roughly a tenth the price of the
alternatives -- about $0.0007 for a three-minute call.

Fails open, on the same principle as spam.py: a timeout, a missing key or a
provider outage leaves the analysis fields NULL and the call_logs row is still
written. Losing a summary is a nuisance; losing the record of a call is not.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from livekit.agents.llm import ChatContext

from .llm_clients import build_utility_llm
from .settings import ProviderSettings
from .state import CallState

logger = logging.getLogger("worker.analysis")

Priority = Literal["High", "Medium", "Low"]
CallStatus = Literal["success", "failed", "incomplete"]

# Generous compared with the spam classifier's 2.5s, because the trade is
# different: that one gates a live call, this one only delays a shutdown
# callback that the caller has already walked away from. Still bounded -- the
# call_logs row and the Slack alert queue behind it.
_TIMEOUT = 20.0

# Below this there is nothing to summarise. A caller who hung up during the
# greeting produces a transcript of one line, and asking a model to find the
# "priority" of that invites it to invent one.
_MIN_TRANSCRIPT_CHARS = 80

_PRIORITIES: set[str] = {"High", "Medium", "Low"}

_PROMPT = """\
You are reviewing a finished phone call to a company's AI receptionist.

Return ONLY a JSON object, with no markdown fence and no commentary:

{
  "caller_name": "..." | null,
  "user_queries": ["..."],
  "call_summary": "...",
  "priority": "High" | "Medium" | "Low"
}

caller_name: the caller's own first name, if they actually said it. Null if they
did not -- do not infer one from a company name, an email address, or the
agent's own name. If they corrected themselves ("Tim, my name is Diana"), take
the corrected one. Give the name alone, no title and no surname unless they gave
one. This is transcribed speech, so spelling may be approximate; return it as
transcribed rather than normalising it to a name you think it resembles.

user_queries: the caller's substantive statements and requests, cleaned up and
normalised. Exclude bare confirmations ("yes", "okay", "hello") unless one is
the actual answer to a question. Use an automated call's own description if
there is no human -- e.g. ["Automated verification message"] or
["Spam/marketing call"]. Empty array if the caller said nothing of substance.

call_summary: under 150 words, covering what the caller wanted and what
resolution they got. Mention a transfer or an arranged callback if either
happened. Write plainly; no preamble.

priority:
  High   - an existing customer with an issue, a development or project
           enquiry, anything affecting the caller's operations, business-critical.
  Medium - general business enquiries, new service questions, follow-ups,
           account questions.
  Low    - general information requests, spam and marketing calls, automated
           verification codes, anything not urgent.

Judge only from what was said. Do not guess at phone numbers or company names --
those are captured by tools during the call, and a guess here would compete with
a known value. caller_name is the one exception, and only when the caller stated
it plainly."""


@dataclass
class CallAnalysis:
    """The model's three judgements. Every field has a safe default, so a
    partial or malformed answer degrades field by field rather than discarding
    the lot."""

    user_queries: list[str] = field(default_factory=list)
    call_summary: str | None = None
    priority: Priority | None = None
    # The caller's name as the model heard it. Deliberately NOT written to
    # lead_name: that column is filled by the record_lead_info tool, which the
    # model calls on purpose when the caller states their name, and it is
    # therefore ground truth. This is an inference from a transcript that may
    # itself have misheard the name. Keeping them apart means an agent with the
    # tool attached still gets the reliable value, and one without it gets
    # something rather than nothing -- without either silently standing in for
    # the other.
    caller_name: str | None = None
    # Which model produced the three judgements above, so the dashboard can say
    # where a field came from instead of assuming. Not hardcoded, because
    # ANALYSIS_LLM can be pointed at Gemini and a label that then read
    # "DeepSeek" would be a lie.
    model: str | None = None


def call_status(state: CallState, duration_seconds: int | None) -> CallStatus:
    """Derived, not asked of a model -- every input here is already known.

    - "failed": the session itself broke (see entrypoint's ErrorEvent handler),
      or a transfer was attempted and didn't connect. Something went wrong that
      the caller experienced.
    - "incomplete": the call ended without reaching any terminal state. No tool
      set an outcome, which means nobody said goodbye -- the caller hung up
      mid-conversation. Also covers a call too short to have been anything.
    - "success": ran to a conclusion, including deliberately hanging up on a
      robocall. A spam call correctly identified and dropped is the system
      working, not failing.
    """
    if state.has_error or state.outcome == "transfer_failed":
        return "failed"
    if state.outcome is None:
        return "incomplete"
    if duration_seconds is not None and duration_seconds < 5:
        return "incomplete"
    return "success"


def transfer_attempted(state: CallState) -> bool:
    """A transfer either ran or it didn't -- the tool records which. True for a
    failed attempt too: the question is whether it was tried."""
    return state.outcome in ("department_transfer", "transfer_failed") or bool(
        state.matched_department
    )


def callback_needed(state: CallState) -> bool:
    """A callback number is only ever recorded by the record_callback_number
    tool, which the agent calls when it has arranged one."""
    return bool(state.transfer_failed_callback_number)


# Outcomes that disqualify a call from being a lead no matter what was captured.
# Only the spam ones: a robocall that recited a company name is not a lead, and
# these calls are hung up on mid-sentence anyway (see spam.py).
_NOT_A_LEAD = ("spam_bot", "spam_sales")


def is_lead(state: CallState) -> bool:
    """Whether this call produced a lead worth telling someone about.

    Keyed on what was *captured*, not on the outcome the model chose. The
    record_lead_info tool only runs when the agent deliberately calls it, so a
    name, company or need in state is evidence a human gave real details --
    which is a firmer thing than `outcome == "qualified"`, a label the model
    can simply forget to set. A call that captured details and ended without an
    outcome is still a lead; the details are sitting right there.

    The exception is spam, which is excluded on outcome regardless of what was
    captured -- see _NOT_A_LEAD.

    Any one of the three fields is enough on purpose. A caller who gives a name
    and a need but no company has not given less of a lead, and requiring all
    three would drop real ones for a formatting reason.
    """
    if state.outcome in _NOT_A_LEAD:
        return False
    return bool(state.lead_name or state.lead_company or state.lead_need)


def _parse(raw: str) -> CallAnalysis:
    """Coerce the model's answer into the dataclass, field by field.

    Uses `json_repair` rather than `json.loads` because a model asked for bare
    JSON still occasionally wraps it in a fence or leaves a trailing comma, and
    losing an entire analysis to a stray character would be a poor trade.

    Every field is validated independently. A model that gets `priority` wrong
    shouldn't cost us the summary it got right.
    """
    text = (raw or "").strip()
    if not text:
        return CallAnalysis()

    data: Any
    try:
        data = json.loads(text)
    except ValueError:
        try:
            from json_repair import repair_json

            data = json.loads(repair_json(text))
        except Exception:  # noqa: BLE001
            logger.warning("call analysis wasn't parseable JSON: %r", text[:200])
            return CallAnalysis()

    if not isinstance(data, dict):
        logger.warning("call analysis wasn't a JSON object: %r", text[:200])
        return CallAnalysis()

    out = CallAnalysis()

    queries = data.get("user_queries")
    if isinstance(queries, list):
        out.user_queries = [
            cleaned
            for cleaned in (str(q).strip() for q in queries if q is not None)
            if cleaned
        ]

    summary = data.get("call_summary")
    if isinstance(summary, str) and summary.strip():
        out.call_summary = summary.strip()

    name = data.get("caller_name")
    if isinstance(name, str):
        cleaned = name.strip().strip(".,")
        # A model told to answer null sometimes writes the word instead, and
        # storing "null" or "unknown" as somebody's name is worse than storing
        # nothing. Length-capped because a name field is not a place for a
        # sentence -- if the model explains itself here, drop it.
        if cleaned.lower() in ("null", "none", "unknown", "n/a", "not provided", ""):
            cleaned = ""
        if cleaned and len(cleaned) <= 60:
            out.caller_name = cleaned
        elif cleaned:
            logger.info("caller_name was %d chars, which isn't a name; dropped", len(cleaned))

    priority = data.get("priority")
    if isinstance(priority, str):
        # Title-cased so "high" and "HIGH" are both accepted; anything that
        # isn't one of the three is dropped rather than stored as a value the
        # enum column would reject anyway.
        candidate = priority.strip().title()
        if candidate in _PRIORITIES:
            out.priority = candidate  # type: ignore[assignment]
        elif priority.strip():
            logger.info("call analysis returned priority %r, which isn't valid", priority[:40])

    return out


async def analyse(
    transcript: str, provider: ProviderSettings, usage: Any | None = None
) -> CallAnalysis:
    """Ask the model for the three judgements. Never raises.

    `usage` is the call's `ModelUsageCollector`. Passed in so this LLM's own
    tokens land in the same bucket as the conversation's: it is a separate client
    rather than part of the AgentSession, so its metrics never reach the session's
    handler on their own. It's about $0.0007 a call -- immaterial next to $0.098,
    but a cost figure that silently omits one of its components is the wrong
    number, and the omission would be invisible.
    """
    if len(transcript.strip()) < _MIN_TRANSCRIPT_CHARS:
        logger.info("transcript too short to analyse (%d chars); skipping", len(transcript.strip()))
        return CallAnalysis()

    chat_ctx = ChatContext.empty()
    chat_ctx.add_message(role="system", content=_PROMPT)
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
        llm = build_utility_llm(provider.analysis_llm, provider)
        if usage is not None:
            # Registered before the request so the one call it makes is counted.
            # Guarded because a metrics listener has no business being able to
            # lose the analysis it was only meant to measure.
            def _on_metrics(metrics) -> None:
                try:
                    usage.collect(metrics)
                except Exception:  # noqa: BLE001
                    logger.debug("couldn't record analysis LLM usage", exc_info=True)

            llm.on("metrics_collected", _on_metrics)
        raw = await asyncio.wait_for(run(llm), timeout=_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(
            "call analysis didn't answer within %.0fs; the call is logged without it", _TIMEOUT
        )
        return CallAnalysis()
    except Exception:
        logger.exception("call analysis failed; the call is logged without it")
        return CallAnalysis()

    result = _parse(raw)
    # Recorded from the client that actually answered, not from the setting, so
    # the label reflects what ran even if the setting changes later.
    result.model = getattr(llm, "model", None) or provider.analysis_llm
    logger.info(
        "call analysis (%s): caller_name=%s priority=%s queries=%d summary=%s",
        result.model,
        result.caller_name or "-",
        result.priority or "-",
        len(result.user_queries),
        f"{len(result.call_summary)} chars" if result.call_summary else "-",
    )
    return result
