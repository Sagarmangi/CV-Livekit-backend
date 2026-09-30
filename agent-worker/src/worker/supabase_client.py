"""Async Supabase access for the worker: resolve an agent by dialed number,
load its full config, log the finished call, and record run-time errors.

Mirrors dashboard/lib/queries.ts, but read-heavy and scoped to what a single
call needs rather than what an admin screen needs.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from typing import Any

from supabase import AsyncClient, acreate_client

from .models import (
    Agent,
    AgentConfig,
    CallOutcome,
    Tool,
)
from .pricing import CallCost
from .settings import supabase_settings

logger = logging.getLogger("worker.supabase")

# Keyed by event loop, not a single module-level instance.
#
# The client owns an httpx connection pool, and httpx's HTTP/2 machinery binds
# asyncio primitives (anyio Events, futures) to whichever loop first used the
# connection. A worker process is reused across jobs and every job runs on its
# own event loop, so a process-wide singleton hands job N+1 a pool bound to job
# N's dead loop. The result is a bare
#
#     RuntimeError: <asyncio.locks.Event ...> is bound to a different event loop
#
# raised from the very first Supabase call in the job -- loading the agent
# config -- so the agent never joins and the caller sits on "Connecting" with no
# other symptom. It presents as random flakiness because it only bites when a
# job lands on an already-used process.
#
# Weak keys so an entry disappears with its loop instead of accumulating one
# dead client per job for the life of the process.
_clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, AsyncClient] = (
    weakref.WeakKeyDictionary()
)


async def get_client() -> AsyncClient:
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None:
        settings = supabase_settings()
        client = await acreate_client(settings.url, settings.service_role_key)
        _clients[loop] = client
    return client


async def load_agent_config_by_number(dialed_number: str) -> AgentConfig | None:
    """The routing lookup Project Plan v2 describes: which agent owns a call
    is decided here, by the number Twilio/LiveKit says was dialed -- not by
    any Twilio-side per-number configuration."""

    client = await get_client()
    result = (
        await client.table("agents")
        .select("*")
        .eq("twilio_number", dialed_number)
        .maybe_single()
        .execute()
    )
    if result is None or result.data is None:
        return None

    return await _load_config_for_agent_row(client, result.data)


async def load_agent_config_by_id(agent_id: str) -> AgentConfig | None:
    """Used for local/console testing, where there's no real SIP call to read
    a dialed number from."""

    client = await get_client()
    result = (
        await client.table("agents").select("*").eq("agent_id", agent_id).maybe_single().execute()
    )
    if result is None or result.data is None:
        return None

    return await _load_config_for_agent_row(client, result.data)


async def _load_config_for_agent_row(client: AsyncClient, agent_row: dict[str, Any]) -> AgentConfig:
    agent = Agent.from_row(agent_row)

    # Tools are a global library now (see 0014_global_tools.sql) -- this joins
    # through agent_tools to whichever ones this agent has selected. Every
    # tool_type (including transfer_call, which replaced the old departments
    # directory -- see 0016_tool_types_and_transfer_call.sql) comes through
    # here identically.
    agent_tools_result = (
        await client.table("agent_tools")
        .select("tools(*)")
        .eq("agent_id", agent.agent_id)
        .execute()
    )

    return AgentConfig(
        agent=agent,
        tools=[
            Tool.from_row(row["tools"]) for row in agent_tools_result.data if row.get("tools")
        ],
    )


async def insert_call_log(
    *,
    call_sid: str | None,
    room_id: str | None,
    agent_id: str | None,
    caller_number: str | None,
    transcript: str | None,
    # None whenever recording is off, or on for a call whose egress or upload
    # didn't work out -- see recording.py. The column has always been nullable
    # and every row written before recording existed has it NULL.
    recording_url: str | None,
    duration_seconds: int | None,
    outcome: CallOutcome | None,
    matched_department: str | None,
    # Which spam detector ended the call and why -- see spam.py. Null on every
    # call that wasn't dropped as spam. Paired with a spam_bot/spam_sales
    # outcome, and the only trace of a hangup the caller was given no
    # explanation for, so it's what a false positive is found from.
    spam_detection: str | None = None,
    lead_name: str | None,
    lead_company: str | None,
    lead_need: str | None,
    is_test: bool = False,
    # What the call cost, split by the thing that charges for it -- see
    # pricing.py. None means it couldn't be computed; the columns stay NULL
    # rather than recording a call as free.
    cost: CallCost | None = None,
    # --- Post-call analysis (see analysis.py) -------------------------------
    # The number the caller dialled, and four facts derived in code from what
    # the session did. None of these needs a model; they're recorded because the
    # call record couldn't previously answer "did that call actually work".
    called_number: str | None = None,
    call_status: str | None = None,
    transfer_attempted: bool | None = None,
    callback_needed: bool | None = None,
    has_error: bool | None = None,
    error_message: str | None = None,
    # Who ended the call and why -- observed during it, not inferred afterwards.
    # Both None on a call that ended in a way nothing claimed, which the row
    # keeps as NULL: "we didn't record this" is a different fact from any of the
    # actors, and the wrong one to invent. See the 0025 migration.
    ended_by: str | None = None,
    end_reason: str | None = None,
    # The three an LLM judged. All None/empty when the analysis didn't run --
    # a short transcript, a timeout, a provider outage. NULL says "not analysed",
    # which is the truth; a default would claim a judgement nobody made.
    call_summary: str | None = None,
    user_queries: list[str] | None = None,
    priority: str | None = None,
    # The caller's name as the analysis model heard it -- a separate column from
    # lead_name, which the record_lead_info tool fills deliberately during the
    # call. An inference must not overwrite a recorded fact, and the dashboard
    # shows which is which.
    caller_name: str | None = None,
    analysis_model: str | None = None,
) -> str | None:
    """Writes the post-call record directly to Supabase. There is no n8n hop
    in this build -- see settings.SlackSettings -- so this and
    notify.py::send_lead_alert are the entire Phase 6 replacement.

    Returns the new row's call_log_id, so the Slack lead alert can link to the
    call record instead of restating it -- and None when the write failed, which
    is why the caller must treat the link as optional rather than assuming an id
    always comes back."""

    # Rounded to the microdollar the column stores. A three-minute call costs
    # around $0.07, so the interesting digits are the fourth and fifth -- these
    # cannot be rounded to cents without every row reading $0.00.
    cost_fields: dict[str, Any] = (
        {
            "cost_stt_usd": round(cost.stt_usd, 6),
            "cost_llm_usd": round(cost.llm_usd, 6),
            "cost_tts_usd": round(cost.tts_usd, 6),
            "cost_telephony_usd": round(cost.telephony_usd, 6),
            "cost_total_usd": round(cost.total_usd, 6),
            "cost_breakdown": cost.breakdown(),
        }
        if cost is not None
        else {}
    )

    client = await get_client()
    try:
        result = await client.table("call_logs").insert(
            {
                "call_sid": call_sid,
                "room_id": room_id,
                "agent_id": agent_id,
                "caller_number": caller_number,
                "transcript": transcript,
                "recording_url": recording_url,
                "duration_seconds": duration_seconds,
                "outcome": outcome,
                "matched_department": matched_department,
                "spam_detection": spam_detection,
                "lead_name": lead_name,
                "lead_company": lead_company,
                "lead_need": lead_need,
                "is_test": is_test,
                "called_number": called_number,
                "call_status": call_status,
                "transfer_attempted": transfer_attempted,
                "callback_needed": callback_needed,
                "has_error": has_error,
                "error_message": error_message,
                "ended_by": ended_by,
                "end_reason": end_reason,
                "call_summary": call_summary,
                # Written even when empty, and the distinction matters: [] means
                # the analysis ran and found nothing substantive the caller said,
                # NULL means it never ran. Those are different facts about a call.
                "user_queries": user_queries,
                "priority": priority,
                "caller_name": caller_name,
                "analysis_model": analysis_model,
                **cost_fields,
            }
        ).execute()
    except Exception:
        # A failed write shouldn't crash call teardown -- the call already
        # happened. Logged loudly so it's caught in worker monitoring instead.
        logger.exception("failed to write call_logs row for call_sid=%s", call_sid)
        return None

    # Defensive rather than indexing straight in: this id is only used to build
    # a convenience link, and a client that returns the row in an unexpected
    # shape must not be the thing that takes down call teardown.
    rows = getattr(result, "data", None) or []
    if rows and isinstance(rows[0], dict):
        return rows[0].get("call_log_id")
    return None
