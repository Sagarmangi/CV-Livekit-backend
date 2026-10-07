"""The widget session cap -- entrypoint._end_widget_call_at_limit -- driven
with fakes for the session and job context, since the real ones need a
LiveKit room."""

from __future__ import annotations

import asyncio

import pytest

from worker import entrypoint as ep
from worker.models import AgentConfig
from worker.state import CallState


class _Handle:
    async def wait_for_playout(self) -> None:
        pass


class FakeSession:
    def __init__(self, realtime: bool) -> None:
        # isinstance(session.llm, RealtimeModel) is the branch selector; an
        # uninitialised instance is enough to satisfy it.
        self.llm = ep.RealtimeModel.__new__(ep.RealtimeModel) if realtime else object()
        self.calls: list[tuple] = []

    def interrupt(self, *, force: bool = False) -> asyncio.Future:
        self.calls.append(("interrupt", force))
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    def say(self, text: str, *, allow_interruptions: bool = True) -> _Handle:
        self.calls.append(("say", text, allow_interruptions))
        return _Handle()

    def generate_reply(self, *, instructions: str, allow_interruptions: bool = True) -> _Handle:
        self.calls.append(("generate_reply", instructions, allow_interruptions))
        return _Handle()


class FakeCtx:
    def __init__(self) -> None:
        self.deleted = False

    async def delete_room(self) -> None:
        self.deleted = True


async def _run(config: AgentConfig, *, realtime: bool, pre_claimed: bool = False):
    state = CallState(config=config, room_name="r", channel="widget")
    if pre_claimed:
        state.claim_end("caller", "caller_hung_up")
    session, ctx = FakeSession(realtime), FakeCtx()
    await ep._end_widget_call_at_limit(ctx, session, state, max_seconds=0)
    return state, session, ctx


@pytest.mark.asyncio
async def test_pipeline_session_says_closing_line_uninterruptibly_then_deletes_room(config) -> None:
    state, session, ctx = await _run(config, realtime=False)
    assert ctx.deleted
    assert (state.ended_by, state.end_reason) == ("system", "widget_time_limit")
    assert session.calls[0] == ("interrupt", True)
    kind, text, allow_interruptions = session.calls[1]
    assert kind == "say"
    assert text == ep.widget_settings().closing_line
    assert allow_interruptions is False


@pytest.mark.asyncio
async def test_realtime_session_uses_generate_reply(config) -> None:
    state, session, ctx = await _run(config, realtime=True)
    assert ctx.deleted
    kind, instructions, allow_interruptions = session.calls[1]
    assert kind == "generate_reply"
    assert ep.widget_settings().closing_line in instructions
    assert allow_interruptions is False


@pytest.mark.asyncio
async def test_does_nothing_if_the_call_is_already_ending(config) -> None:
    state, session, ctx = await _run(config, realtime=False, pre_claimed=True)
    assert not ctx.deleted
    assert session.calls == []
    # The earlier claim stands -- first writer wins.
    assert state.ended_by == "caller"
