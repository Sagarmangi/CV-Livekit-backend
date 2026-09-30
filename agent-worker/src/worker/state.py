"""Per-call state shared across every Agent phase and tool via AgentSession.userdata.

One instance is created per LiveKit job (per call) and threaded through the
whole session -- this is what lets the qualification data gathered in one
Agent phase still be there when a later phase (or the post-call logger) needs it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .models import AgentConfig, CallOutcome, EndedBy

logger = logging.getLogger("worker.state")


@dataclass
class CallState:
    config: AgentConfig
    room_name: str
    call_sid: str | None = None
    caller_number: str | None = None
    # The number the caller dialled (sip.trunkPhoneNumber). Read at answer time
    # to resolve which agent owns the call, and kept because it's also the only
    # record of *which* of an agent's numbers was rung -- worth having once an
    # agent fronts more than one. None for a browser test, which dials nothing.
    called_number: str | None = None
    # True for a dashboard "test this agent" session (see entrypoint.py) --
    # skips call_logs/Slack so test runs don't pollute real call history.
    is_test: bool = False

    # Filled in by the record_lead_info built-in tool as the qualification
    # conversation progresses (see tools.py). Keys match qualification_criteria
    # keys, plus the fixed lead_name/lead_company/lead_need fields.
    qualification_answers: dict[str, Any] = field(default_factory=dict)
    lead_name: str | None = None
    lead_company: str | None = None
    lead_need: str | None = None

    # Set once the call flow reaches a terminal state, so the shutdown
    # callback in entrypoint.py knows what to write to call_logs.
    outcome: CallOutcome | None = None
    matched_department: str | None = None
    transfer_failed_callback_number: str | None = None

    # Whether the session broke mid-call, and the first reason it gave. Set by
    # entrypoint's ErrorEvent handler, which until now only reported the failure
    # to the caller and the log -- so "did that call actually work" was a
    # question the call record couldn't answer. The *first* error is kept rather
    # than the last: a failing session tends to emit a cascade, and the one that
    # started it is the useful one.
    has_error: bool = False
    error_message: str | None = None

    # Which spam detector fired and why (see spam.py). Set alongside a
    # spam_bot/spam_sales outcome, and worth more than the outcome alone: those
    # calls are hung up on with no explanation to the caller, so this is the only
    # evidence available when deciding whether a statement list is too broad.
    spam_detection: str | None = None

    # Rotates through the "are you AI?" deflection lines (FSD Section 4.5-ish
    # human-likeness requirement) so no line repeats within a single call.
    ai_deflection_index: int = 0

    # Who ended the call and why -- see claim_end below, and migration 0025 for
    # why these are two fields rather than one. Both stay None on a call that
    # ended in some way nothing thought to claim, which the row records as NULL
    # rather than guessing.
    ended_by: EndedBy | None = None
    end_reason: str | None = None

    def claim_end(self, actor: EndedBy, reason: str) -> None:
        """Record who ended the call. The first claim wins; later ones are
        ignored.

        That rule is the whole reason this is a method rather than two
        assignments. Ending a call from our side deletes the room, and deleting
        the room disconnects the caller -- so entrypoint's
        `participant_disconnected` handler fires on *every* clean hang-up, a
        moment after the agent's own claim. Last-writer-wins would therefore
        label every successful end_call as a caller hangup, which is precisely
        backwards.

        Same convention as `has_error` above: the first thing to happen is the
        thing that explains the call.
        """
        if self.ended_by is not None:
            logger.debug(
                "call end already claimed by %s (%s); ignoring later claim %s (%s)",
                self.ended_by,
                self.end_reason,
                actor,
                reason,
            )
            return
        self.ended_by = actor
        self.end_reason = reason
        logger.info("call end claimed: ended_by=%s reason=%s", actor, reason)
