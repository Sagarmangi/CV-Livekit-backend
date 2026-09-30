"""end_call (the one tool every agent always has -- something has to be able
to hang up regardless of configuration) plus the generic framework that turns
a Supabase `tools` row into a callable LLM function tool. Transfer, lead
capture, and callback capture used to be unconditional builtins here too;
they're now admin-created rows like a webhook Function tool, opt-in per
agent -- see build_agent_tools' dispatch by `tool_type` below. Each still
runs fixed Python behavior (there's no webhook for these three), just wrapped
with whatever name/description the admin gave the row instead of a hardcoded
one, and only present at all if attached.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from livekit import api, rtc
from livekit.agents import RunContext, get_job_context
from livekit.agents.llm import RawFunctionTool, ToolError, function_tool

from .models import DETECTOR_TOOL_TYPES, Agent, Tool
from .state import CallState

logger = logging.getLogger("worker.tools")


# The mechanics of hanging up, shared by the builtin below and by any admin
# -configured `end_call` tool row. Only the description differs between them --
# what it *does* is not something an admin should be able to change, since
# getting it wrong means either a call that never ends or one cut off mid-word.
_END_CALL_DESCRIPTION = (
    "End the call. Speak your closing line and call this in the SAME turn -- it waits for your "
    "line to finish playing before hanging up, so the caller hears all of it. Do not save this "
    "for a later turn: once you finish speaking the turn is over and you will not get another "
    "one unless the caller speaks again, so a goodbye without this call just leaves them on a "
    "silent line. Use outcome='not_qualified' for a normal close, 'dropped' for a "
    "scam/fake-verification call you're ending immediately, or leave outcome unset if it was "
    "already set by a transfer."
)


async def _hang_up(context: RunContext[CallState], outcome: str | None = None) -> None:
    """Returns None, and that is the whole point rather than an oversight.

    The SDK decides whether to run another LLM turn after a tool from whether
    that tool returned anything: `reply_required=fnc_out is not None` in
    voice/generation.py. Returning a string -- this used to return "Call ending."
    -- therefore asked the model to comment on the result *after* the room had
    already been deleted underneath it. Gemini, called against a session being
    torn down, answers with an empty completion:

        APIStatusError('no response generated', body='finish reason: STOP')

    which surfaces as an llm_error and gets narrated to the caller as "there was
    an issue ending the call" -- immediately after the goodbye, on a call that
    hung up perfectly well. A race, so it didn't fail every time.

    There is nothing to tell the model here anyway: the call is over, and any
    reply it produced would be spoken into a room that no longer exists.
    """
    state = context.userdata
    if outcome and state.outcome is None:
        state.outcome = outcome  # type: ignore[assignment]

    # Claimed here rather than after the room is gone, and before the playout
    # wait below rather than after it: everything past this point can raise, and
    # a hang-up the agent definitely asked for must not be recorded as anything
    # else because the closing line stumbled. The caller-disconnect handler in
    # entrypoint.py fires a moment from now as a consequence of delete_room, and
    # claim_end's first-writer-wins rule is what stops it relabelling this.
    state.claim_end("agent", "end_call_tool")

    # Whether the model reached for this at all is the first question asked every
    # time a call fails to end, and it used to be unanswerable from the log: a
    # hang-up that raised looked identical to one the model never requested.
    logger.info("hanging up: tool=%s outcome=%s", context.function_call.name, state.outcome)

    # Let the closing line finish playing before the room disconnects under it.
    #
    # It must be RunContext.wait_for_playout, never SpeechHandle.wait_for_playout:
    # the handle waits for the *entire* turn, which includes this tool, so calling
    # it from in here is a circular wait. The SDK guards against exactly that and
    # raises RuntimeError (speech_handle.py:186-199) rather than deadlocking -- and
    # a tool that raises never reaches delete_room, so the call simply carried on.
    # The framework then fed the error back to the model, which apologised and
    # asked the caller to repeat themselves right after they'd said goodbye. This
    # is also why hanging up from spam.py always worked: that path calls
    # delete_room from the classifier task, not from inside a tool.
    #
    # RunContext's version waits only for the speech generated *before* this call
    # in the same step, which is precisely the closing line. Interruption resolves
    # it too (_mark_done marks the generation done), so a caller talking over the
    # goodbye can't wedge the hang-up.
    try:
        await context.wait_for_playout()
    except Exception:
        # Never let this stop the hang-up: the cost of not ending a call is a line
        # held open and billed until LiveKit's room timeout, which is far worse
        # than clipping the last word of a goodbye.
        logger.exception("waiting for the closing line failed; hanging up anyway")

    job_ctx = get_job_context()
    await job_ctx.delete_room()


@function_tool(name="end_call", description=_END_CALL_DESCRIPTION)
async def end_call(context: RunContext[CallState], outcome: str | None = None) -> None:
    await _hang_up(context, outcome)


def build_end_call_tool(tool_row: Tool) -> RawFunctionTool:
    """A `tool_type: "end_call"` row -- the same hang-up behaviour as the builtin,
    under an admin's own name and description.

    The point of allowing this is that *when* to hang up is genuinely
    agent-specific ("end once they've booked a slot", "end after two refusals"),
    and until now the only way to influence it was the agent's end-call
    instructions field, which is prose in the system prompt rather than something
    attached to the tool the model actually calls. A tool description is read by
    the model on every turn as part of the function schema, so conditions written
    here carry more weight than the same words buried in a prompt.

    The admin's description is appended to the default rather than replacing it.
    The default explains the *mechanics* -- speak your closing line first, pass an
    outcome -- which an admin writing "end after booking" has no reason to
    restate and every reason not to accidentally drop. Getting that half wrong
    produces calls cut off mid-sentence.
    """

    async def _end(raw_arguments: dict[str, Any], context: RunContext[CallState]) -> None:
        await _hang_up(context, raw_arguments.get("outcome"))

    _end.__name__ = tool_row.name

    return function_tool(
        _end,
        raw_schema={
            "name": tool_row.name,
            "description": f"{tool_row.description}\n\n{_END_CALL_DESCRIPTION}",
            "parameters": {
                "type": "object",
                "properties": {"outcome": {"type": "string"}},
            },
        },
    )


def _find_sip_participant(job_ctx: Any):
    for participant in job_ctx.room.remote_participants.values():
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            return participant
    return None


def builtin_tools(tool_rows: list[Tool] | None = None) -> list:
    """The tools every agent gets regardless of configuration -- just hanging up.

    Suppressed when the agent has its own enabled `end_call` row, so the model
    isn't handed two tools that do the same thing under different names and left
    to pick. Otherwise the builtin stands: an agent that cannot hang up doesn't
    end its calls, it holds the line until LiveKit's room timeout expires while
    telephony bills for every minute. That failure is bad enough that end_call is
    the one type which stays opt-*out* rather than opt-in.
    """
    configured = any(
        row.tool_type == "end_call" and row.is_enabled for row in (tool_rows or [])
    )
    if configured:
        logger.info("agent has its own end_call tool; not adding the builtin")
        return []
    return [end_call]


def build_custom_tool(tool_row: Tool) -> RawFunctionTool:
    """Turns a `tool_type: "function"` row into a live function tool that
    calls its configured webhook. This is the entire mechanism Function tools
    need -- there's no per-tool Python code, which is the point of the
    framework."""

    if not tool_row.webhook_url:
        raise ValueError(f"Function tool '{tool_row.name}' has no webhook_url configured")

    async def _call_webhook(raw_arguments: dict[str, Any], context: RunContext[CallState]) -> str:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(tool_row.webhook_url, json=raw_arguments)
                response.raise_for_status()
                # Tools are free-form webhooks; the LLM can work with either
                # a JSON body or plain text back, so don't force a shape here.
                try:
                    return str(response.json())
                except ValueError:
                    return response.text
        except httpx.HTTPError as e:
            logger.warning("Function tool '%s' webhook call failed: %s", tool_row.name, e)
            raise ToolError(
                f"The {tool_row.name} tool is temporarily unavailable. "
                "Apologize and continue without it, or offer a callback."
            ) from e

    _call_webhook.__name__ = tool_row.name

    return function_tool(
        _call_webhook,
        raw_schema={
            "name": tool_row.name,
            "description": tool_row.description,
            "parameters": tool_row.parameter_schema or {"type": "object", "properties": {}},
        },
    )


def _as_transfer_uri(destination: str) -> str:
    """LiveKit's SIP transfer takes a URI, not a phone number.

    The dashboard asks an admin for a destination *number*, and E.164 is what
    they sensibly type -- but `transfer_sip_participant` rejects a bare number
    with `transfer_to must be a valid SIP(s) or TEL URI`, a 400 that surfaces
    only at the moment a real caller asks to be transferred. Every transfer
    failed this way: the caller heard the agent apologise and ask for a
    callback number instead, which reads as the agent being broken rather than
    as a configuration problem.

    Anything already carrying a scheme is passed through untouched, so an admin
    who deliberately enters a `sip:` address to reach an internal extension
    still gets exactly what they typed.
    """
    value = destination.strip()
    if value.lower().startswith(("tel:", "sip:", "sips:")):
        return value
    # Strip the separators people type in phone numbers; `tel:` wants digits
    # (with an optional leading +), not "+1 (737) 271-0090".
    compact = re.sub(r"[\s()\-.]", "", value)
    return f"tel:{compact}"


# How long to let the destination ring before giving up on a transfer.
#
# The SDK's default is 30s (livekit/api/_dial_timeout.py), and that is not enough
# for a person to reach a phone: on a US-to-UK transfer the callee picked up at
# 16:23:49 having been rung from roughly 16:23:15, by which point the 30s window
# had expired, the tool had raised, and the agent had already told the caller
# "the team is currently unavailable" -- on a transfer that then went through and
# ran for 25 seconds. The call was logged transfer_failed while the two of them
# were talking. A timeout shorter than a human's reaction time turns every slow
# answer into a phantom failure.
#
# A longer window costs nothing when nobody answers: the caller hears ringing
# either way, and ringing is what people expect a transfer to sound like.
_TRANSFER_RINGING_TIMEOUT = 45


def build_transfer_call_tool(tool_row: Tool) -> RawFunctionTool:
    """A `tool_type: "transfer_call"` row -- SIP-transfers to the fixed
    number the admin set on this specific tool. One tool per destination
    (like Vapi's Transfer Call), each with its own admin-written description
    driving when the model reaches for it, rather than one generic tool
    matched against a routing-keywords directory."""

    if not tool_row.destination_number:
        raise ValueError(f"Transfer call tool '{tool_row.name}' has no destination_number configured")

    async def _transfer(raw_arguments: dict[str, Any], context: RunContext[CallState]) -> None:
        state = context.userdata
        job_ctx = get_job_context()
        participant = _find_sip_participant(job_ctx)
        if participant is None:
            raise ToolError("No SIP participant found in the room to transfer.")

        transfer_to = _as_transfer_uri(tool_row.destination_number)

        # Built by hand rather than via job_ctx.transfer_sip_participant, which
        # exposes neither of the two settings that decide whether a transfer is
        # survivable for the person on the phone. See the constants above.
        request = api.TransferSIPParticipantRequest(
            room_name=job_ctx.room.name,
            participant_identity=participant.identity,
            transfer_to=transfer_to,
            play_dialtone=True,
        )
        request.ringing_timeout.seconds = _TRANSFER_RINGING_TIMEOUT

        # Let the "I'm transferring you now" line finish before the caller is
        # REFERed away -- the tool description tells the model to say it first,
        # but nothing was waiting for it to actually play, so a slow TTS could
        # be cut off mid-sentence by the transfer it was announcing.
        #
        # RunContext.wait_for_playout, never SpeechHandle's: the handle waits for
        # the whole turn, which includes this tool, so calling it from in here is
        # a circular wait the SDK raises on. Same reasoning as _hang_up.
        try:
            await context.wait_for_playout()
        except Exception:
            # Never block the transfer on this. A clipped last word is a far
            # better outcome than a caller who is never handed over.
            logger.exception("waiting for the pre-transfer line failed; transferring anyway")

        try:
            await job_ctx.api.sip.transfer_sip_participant(request)
        except Exception as e:
            # Logs the URI actually sent, not the row's raw value -- when a
            # transfer is rejected, what went on the wire is the thing in
            # question.
            logger.exception("SIP transfer to %s failed", transfer_to)
            state.outcome = "transfer_failed"
            state.matched_department = tool_row.name
            raise ToolError(
                "The transfer failed. Apologize, explain a callback is coming, and ask for the "
                "best number to reach them at -- then record it if you have a way to."
            ) from e

        state.outcome = "department_transfer"
        state.matched_department = tool_row.name
        # The agent ended this leg of the call, even though nobody hung up: the
        # caller leaves the room because we handed them somewhere else. Recorded
        # as such so a transfer isn't counted among the calls people abandoned.
        # Which destination is already in matched_department.
        state.claim_end("agent", "transferred")
        logger.info("transferred to %s via %s", tool_row.name, transfer_to)

        # Returns None, and that is the whole point -- the same trap end_call
        # fell into (see _hang_up's docstring).
        #
        # The SDK decides whether to run another LLM turn from whether the tool
        # returned anything: `reply_required=fnc_out is not None` in
        # voice/generation.py. Returning "Transferred." therefore asked the model
        # to comment on a transfer that had already moved the caller off this
        # room. Called against a session being torn down, Gemini answers with an
        # empty completion:
        #
        #     APIStatusError('no response generated', body='finish reason: STOP')
        #
        # which surfaces as an llm_error, sets has_error on the call, and gets
        # the whole thing logged call_status='failed' -- on a transfer that
        # worked. It is also spoken into a room the caller has already left.
        #
        # There is nothing to tell the model here anyway: the caller is gone.
        # A failed transfer is the case where the model does need to know, and
        # that path raises ToolError above.
        return None

    _transfer.__name__ = tool_row.name

    return function_tool(
        _transfer,
        raw_schema={
            "name": tool_row.name,
            "description": (
                f"{tool_row.description} The transfer itself happens silently -- tell the caller "
                "you're transferring them before calling this, not after."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    )


def build_record_lead_info_tool(tool_row: Tool) -> RawFunctionTool:
    """A `tool_type: "record_lead_info"` row -- same fixed behavior (writes
    onto CallState, ends up in call_logs) as the old unconditional builtin,
    just opt-in per agent with an admin-written description now instead of a
    fixed one, so an agent that isn't doing lead qualification doesn't have
    this nudging it to ask for a name/company/need it has no use for."""

    async def _record(raw_arguments: dict[str, Any], context: RunContext[CallState]) -> str:
        state = context.userdata
        lead_name = raw_arguments.get("lead_name")
        lead_company = raw_arguments.get("lead_company")
        lead_need = raw_arguments.get("lead_need")
        qualification_answers = raw_arguments.get("qualification_answers")
        if lead_name:
            state.lead_name = lead_name
        if lead_company:
            state.lead_company = lead_company
        if lead_need:
            state.lead_need = lead_need
        if qualification_answers:
            state.qualification_answers.update(qualification_answers)
        return "Recorded."

    _record.__name__ = tool_row.name

    return function_tool(
        _record,
        raw_schema={
            "name": tool_row.name,
            "description": tool_row.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "lead_name": {"type": "string"},
                    "lead_company": {"type": "string"},
                    "lead_need": {"type": "string"},
                    "qualification_answers": {"type": "object"},
                },
            },
        },
    )


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def build_record_callback_number_tool(tool_row: Tool) -> RawFunctionTool:
    """A `tool_type: "record_callback_number"` row -- same fixed behavior
    (feeds the transfer-failed Slack alert) as the old unconditional builtin,
    opt-in per agent now.

    `callback_number` is deliberately optional. The single most common answer to
    "what's the best number to reach you at" is "use this one" / "the number I'm
    calling from", and the model has no way to honour that: the caller's number
    lives on CallState for logging and is never put in the prompt. With the
    argument required, that exchange ended with the agent asking again or saying
    it couldn't record anything -- and the number was lost on exactly the calls
    (failed transfers) where it mattered most. Omitted or non-numeric now falls
    back to the caller ID we already have.

    Returns the number it stored so the model can read it back for confirmation,
    which is the only check available against a misheard digit.
    """

    async def _record(raw_arguments: dict[str, Any], context: RunContext[CallState]) -> str:
        state = context.userdata
        given = str(raw_arguments.get("callback_number") or "").strip()

        # Anything without a plausible run of digits is the model relaying "this
        # number" rather than a number, so prefer the caller ID over storing prose.
        if len(_digits(given)) < 7:
            if not state.caller_number:
                raise ToolError(
                    "No number to record -- there's no caller ID on this call. Ask them to "
                    "read the digits out."
                )
            given = state.caller_number

        state.transfer_failed_callback_number = given
        logger.info("recorded callback number for %s", state.room_name)
        return f"Recorded {given}. Read it back to confirm it's right."

    _record.__name__ = tool_row.name

    return function_tool(
        _record,
        raw_schema={
            "name": tool_row.name,
            "description": tool_row.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "callback_number": {
                        "type": "string",
                        "description": (
                            "The number in full. Omit it if the caller says to use the number "
                            "they're calling from -- their caller ID is used instead."
                        ),
                    }
                },
            },
        },
    )


_TOOL_BUILDERS = {
    "function": build_custom_tool,
    "transfer_call": build_transfer_call_tool,
    "record_lead_info": build_record_lead_info_tool,
    "record_callback_number": build_record_callback_number_tool,
    # Also has a builtin fallback -- see builtin_tools(), which stands down when
    # an enabled row of this type is attached so the model isn't offered two.
    "end_call": build_end_call_tool,
}


def build_agent_tools(tool_rows: list[Tool]) -> list:
    """Every attached, enabled tool -- dispatches on `tool_type` to the matching
    builder above.

    Two kinds of row produce nothing here, and neither is an error:

    - Anything switched off (`is_enabled` false). One flag turns a tool off for
      every agent at once, which is otherwise a matter of unpicking `agent_tools`
      row by row.
    - The `detect_*` types, which are configuration for spam.py rather than
      functions. Handing the model a "hang up on spam" tool would put that
      decision back somewhere a caller can talk it out of -- see spam.py.
    """

    tools = []
    for row in tool_rows:
        if not row.is_enabled:
            logger.info("tool %s is switched off; not offering it to the model", row.name)
            continue
        if row.tool_type in DETECTOR_TOOL_TYPES:
            continue
        builder = _TOOL_BUILDERS.get(row.tool_type)
        if builder is None:
            logger.error("unknown tool_type %r for tool row %s", row.tool_type, row.tool_id)
            continue
        try:
            tools.append(builder(row))
        except ValueError:
            logger.exception("skipping malformed tool row %s", row.tool_id)
    return tools


def build_knowledge_tool(agent: Agent) -> list[RawFunctionTool]:
    """A single on-demand tool wrapping the agent's whole knowledge base --
    there's one knowledge base per agent, not several documents to pick
    between, so one tool is enough. Its content is only sent to the model
    (and only costs tokens) on the calls where it's actually invoked, instead
    of being concatenated into every turn's prompt regardless of whether it's
    ever needed. Returns an empty list if no content is configured, so an
    agent with nothing to look up doesn't get an empty/useless tool."""

    if not agent.knowledge_base_content.strip():
        return []

    async def _search_knowledge_base(
        raw_arguments: dict[str, Any], context: RunContext[CallState]
    ) -> str:
        return agent.knowledge_base_content

    return [
        function_tool(
            _search_knowledge_base,
            raw_schema={
                "name": "search_knowledge_base",
                "description": agent.knowledge_base_description
                or "Reference material for off-topic caller questions.",
                "parameters": {"type": "object", "properties": {}},
            },
        )
    ]
