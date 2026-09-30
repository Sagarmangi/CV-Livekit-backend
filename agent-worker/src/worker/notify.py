"""Direct Slack notifications -- the Phase 6 replacement for what the FSD
originally routed through n8n (Sections 5.3.1/5.3.2). Since this build skips
n8n entirely, the worker posts to the Slack incoming-webhook URL itself once a
call has ended.

Two messages, not three, and deliberately not one per call:

  send_lead_alert            -- the call captured lead details (analysis.is_lead)
  send_transfer_failure_alert -- a transfer failed and a callback is owed

The per-call summary that used to fire on every non-test call is gone. See the
0026 migration for the reasoning; the short version is that a channel carrying
every robocall and four-second hangup is a channel nobody reads, and it cost
the alerts that mattered. Whether an agent posts at all is its own toggle
(`agents.slack_notifications_enabled`, off by default), checked by the caller
in entrypoint.py rather than in here.

NOTE: the exact message copy/format here is a reasonable best-effort, not a
transcription of FSD Section 5.3.1/5.3.2 -- I don't have that section's exact
wording. Treat these two message builders as the thing to check against the
real FSD text before launch, not as already-verified copy.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from .models import Agent, CallOutcome
from .settings import slack_settings

if TYPE_CHECKING:
    # Under TYPE_CHECKING only: annotations are strings here (see the __future__
    # import), so the dataclass is never needed at runtime -- and importing
    # analysis for real would drag the whole utility-LLM stack in with it.
    from .analysis import CallAnalysis

logger = logging.getLogger("worker.notify")

# Slack's own per-element ceilings. A block that exceeds one of these makes the
# *whole* message fail with invalid_blocks rather than arriving trimmed, so
# every free-text field below is cut to fit before it is sent.
_SECTION_LIMIT = 2900  # Slack's limit is 3000; leave room for the bold label
_HEADER_LIMIT = 150


def _truncate(value: str | None, limit: int) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# Matches the emoji vocabulary the team's existing n8n lead alert already uses,
# so the two sources of leads read the same way in the channel.
_PRIORITY_EMOJI = {"High": "🔥", "Medium": "⚡", "Low": "🧊"}
_PRIORITY_COLOR = {"High": "#d72b3f", "Medium": "warning", "Low": "good"}


async def _post_to_slack(payload: dict) -> None:
    settings = slack_settings()
    if not settings.webhook_url:
        logger.info("SLACK_WEBHOOK_URL not set; skipping Slack notification")
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(settings.webhook_url, json=payload)
            response.raise_for_status()
    except Exception:
        logger.exception("failed to post Slack notification")


async def send_lead_alert(
    *,
    agent: Agent,
    caller_number: str | None,
    called_number: str | None,
    outcome: CallOutcome | None,
    duration_seconds: int,
    matched_department: str | None,
    lead_name: str | None,
    lead_company: str | None,
    lead_need: str | None,
    qualification_answers: dict[str, Any] | None = None,
    # The post-call analysis, already computed by the time entrypoint reaches
    # this (see _log_and_notify's ordering) -- so the summary, the caller's own
    # questions and the priority cost nothing extra to include. Leaving them out
    # was the difference between an alert someone can act on and one that only
    # says a lead happened.
    analysis: CallAnalysis | None = None,
    # Links the message to the full record -- transcript, recording, cost. None
    # when the insert failed or DASHBOARD_BASE_URL isn't configured, in which
    # case the button is simply omitted.
    call_log_id: str | None = None,
    ended_by: str | None = None,
) -> None:
    """Posted when a call captured lead details -- see analysis.is_lead for what
    counts, and the 0026 migration for why this replaced the summary that used
    to fire on every single call.

    Whether it fires at all is the caller's decision to make, not this
    function's: entrypoint checks the agent's toggle and `is_lead` before
    getting here. This only formats.

    Block Kit inside a coloured attachment, rather than either alone. Blocks
    because the interesting content is now long-form prose -- the call summary
    and the caller's own questions -- which an attachment `field` renders in a
    cramped half-width column and truncates badly. The attachment wrapper is
    kept purely for the coloured bar down the left, which Block Kit has no way
    to produce on its own and which is how priority reads at a glance in a busy
    channel.
    """

    minutes, seconds = divmod(duration_seconds, 60)
    priority = (analysis.priority if analysis else None) or None
    emoji = _PRIORITY_EMOJI.get(priority or "", "✅")

    # The agent's name is in the header, and repeated in `text` -- the latter is
    # what Slack shows in notifications and sidebar previews, where blocks are
    # not rendered at all. Without it a push notification would say nothing
    # about which agent, or which lead, it was for.
    who = lead_name or (analysis.caller_name if analysis else None) or "Unknown caller"
    fallback = f"{emoji} New lead for {agent.name}: {who}"

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": _truncate(f"{emoji} New lead — {agent.name}", _HEADER_LIMIT),
                "emoji": True,
            },
        }
    ]

    # Who called. `caller_name` is the analysis model's guess and is only used
    # when the record_lead_info tool captured nothing -- and it is labelled as a
    # guess, because a name someone typed into a CRM off the back of this should
    # not silently be a transcription artefact.
    if lead_name:
        headline = f"*{lead_name}*"
    elif analysis and analysis.caller_name:
        headline = f"*{analysis.caller_name}* (name inferred from the transcript, not confirmed)"
    else:
        headline = "*Name not captured*"
    if lead_company:
        headline += f" — {lead_company}"
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": headline}})

    facts = [
        f"*Phone*\n{caller_number or 'unknown'}",
        f"*Duration*\n{minutes}m {seconds}s",
        f"*Priority*\n{priority or 'not judged'}",
        f"*Outcome*\n{(outcome or 'not set').replace('_', ' ')}",
    ]
    if called_number:
        facts.append(f"*They dialled*\n{called_number}")
    if matched_department:
        facts.append(f"*Transferred to*\n{matched_department}")
    blocks.append(
        {
            "type": "section",
            # Slack renders at most 10 fields per section, two per row.
            "fields": [{"type": "mrkdwn", "text": f} for f in facts[:10]],
        }
    )

    if lead_need:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*What they need*\n{_truncate(lead_need, _SECTION_LIMIT)}",
                },
            }
        )

    if analysis and analysis.call_summary:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Call summary*\n{_truncate(analysis.call_summary, _SECTION_LIMIT)}",
                },
            }
        )

    if analysis and analysis.user_queries:
        # The caller's own words, which is the detail a salesperson actually
        # wants before ringing back -- the summary paraphrases, this doesn't.
        asked = "\n".join(f"• {q}" for q in analysis.user_queries)
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*What they asked*\n{_truncate(asked, _SECTION_LIMIT)}",
                },
            }
        )

    if qualification_answers:
        # Whatever the agent's own qualification_criteria captured. Keys are
        # admin-defined per agent, so they're printed as configured rather than
        # mapped to anything this function pretends to know about.
        answers = "\n".join(
            f"• *{key.replace('_', ' ')}:* {value}"
            for key, value in qualification_answers.items()
            if value not in (None, "")
        )
        if answers:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Qualification*\n{_truncate(answers, _SECTION_LIMIT)}",
                    },
                }
            )

    settings = slack_settings()
    if call_log_id and settings.dashboard_base_url:
        # The escape hatch for everything deliberately not in this message: the
        # full transcript, the recording, the cost breakdown. Cheaper than
        # trying to fit any of it into Slack.
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Open call record", "emoji": True},
                        "url": f"{settings.dashboard_base_url.rstrip('/')}/calls/{call_log_id}",
                    }
                ],
            }
        )

    footnotes = [f"Ended by: {ended_by or 'not recorded'}"]
    if analysis and analysis.model:
        # Attribute the inferred fields, the same way the dashboard does. The
        # summary and priority above are a model's reading of the call, and
        # anyone acting on them should be able to see that.
        footnotes.append(f"Summary/priority by {analysis.model}")
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": " · ".join(footnotes)}],
        }
    )

    await _post_to_slack(
        {
            "text": _truncate(fallback, _HEADER_LIMIT),
            "attachments": [
                {"color": _PRIORITY_COLOR.get(priority or "", "good"), "blocks": blocks}
            ],
        }
    )


async def send_end_call_webhook(
    *,
    agent: Agent,
    call_sid: str | None,
    room_id: str,
    caller_number: str | None,
    transcript: str,
    recording_url: str | None,
    duration_seconds: int,
    outcome: CallOutcome | None,
    matched_department: str | None,
    lead_name: str | None,
    lead_company: str | None,
    lead_need: str | None,
    qualification_answers: dict[str, Any],
    is_test: bool,
    # Who ended the call and why -- see CallState.claim_end. Sent because a CRM
    # syncing these records cares a great deal whether the caller abandoned the
    # call or the agent finished it, and `outcome` alone doesn't say: a caller
    # who hangs up mid-qualification leaves it null, exactly like a call that
    # broke. Both None on a call that ended in a way nothing claimed.
    ended_by: str | None = None,
    end_reason: str | None = None,
) -> None:
    """Posts the full call record to the agent's own configured webhook
    (Prompt & qualification tab), for whoever wants call data outside Slack/
    the dashboard -- e.g. a CRM sync in n8n. Distinct from Slack: this fires
    even for dashboard test calls, matching how a custom tool call already
    isn't sandboxed there either -- see agent-worker's tools.py.

    recording_url is a Cloudinary link when CALL_RECORDING_ENABLED is on and the
    egress and upload both worked; None otherwise, which includes every call on a
    deployment that leaves recording off. See recording.py. It's an
    `authenticated` Cloudinary asset, so a receiving workflow can't fetch it with
    the bare URL -- it needs a signed one.
    """
    if not agent.end_call_webhook_url:
        return

    payload = {
        "agent_id": agent.agent_id,
        "agent_name": agent.name,
        "call_sid": call_sid,
        "room_id": room_id,
        "caller_number": caller_number,
        "transcript": transcript,
        "recording_url": recording_url,
        "duration_seconds": duration_seconds,
        "outcome": outcome,
        "matched_department": matched_department,
        "lead_name": lead_name,
        "lead_company": lead_company,
        "lead_need": lead_need,
        "qualification_answers": qualification_answers,
        "is_test": is_test,
        "ended_by": ended_by,
        "end_reason": end_reason,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(agent.end_call_webhook_url, json=payload)
            response.raise_for_status()
    except Exception:
        logger.exception("failed to post end-call webhook for agent %s", agent.agent_id)


async def send_transfer_failure_alert(
    *,
    agent: Agent,
    caller_number: str | None,
    department_name: str,
    callback_number: str | None,
) -> None:
    """FSD 5.3.2 equivalent: the urgent @channel alert when a transfer fails
    and the caller was told a callback is coming -- someone has to actually
    make that callback."""

    lines = [
        "@channel :rotating_light: *Transfer failed -- callback owed*",
        f"*Agent:* {agent.name}",
        f"*Caller:* {caller_number or 'unknown'}",
        f"*Wanted:* {department_name}",
        f"*Callback number given:* {callback_number or 'caller did not provide one'}",
    ]
    await _post_to_slack({"text": "\n".join(lines)})
