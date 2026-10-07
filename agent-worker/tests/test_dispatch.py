"""How a job's metadata selects the phone / test / widget path, and the
widget's origin allow-list -- see entrypoint._parse_dispatch and
_origin_allowed."""

from __future__ import annotations

import json

import pytest

from worker import entrypoint as ep


def test_no_metadata_is_a_phone_call() -> None:
    assert ep._parse_dispatch("").channel == "phone"


@pytest.mark.parametrize("metadata", ["not json", "[1]", json.dumps({"widget_key": "  "})])
def test_unusable_metadata_is_a_phone_call(metadata: str) -> None:
    assert ep._parse_dispatch(metadata).channel == "phone"


def test_test_agent_id_selects_test() -> None:
    d = ep._parse_dispatch(json.dumps({"test_agent_id": "abc"}))
    assert d.channel == "test"
    assert d.test_agent_id == "abc"


def test_widget_key_selects_widget_and_keeps_origin_and_visitor() -> None:
    d = ep._parse_dispatch(
        json.dumps({"widget_key": "wk_x", "origin": "https://a.com/", "visitor_id": "v1"})
    )
    assert d.channel == "widget"
    assert (d.widget_key, d.origin, d.visitor_id) == ("wk_x", "https://a.com/", "v1")


def test_test_wins_over_widget_if_both_present() -> None:
    assert ep._parse_dispatch(json.dumps({"test_agent_id": "t", "widget_key": "w"})).channel == "test"


# --- origin allow-list ------------------------------------------------------

ALLOWED = ["https://customer.com"]
DASHBOARD = "https://dash.example.com"


def test_empty_list_allows_any_origin_including_none() -> None:
    assert ep._origin_allowed(None, [])
    assert ep._origin_allowed("https://x.com", [])
    assert ep._origin_allowed("https://anything.com", [], DASHBOARD)


def test_with_a_list_a_missing_origin_is_refused() -> None:
    assert not ep._origin_allowed(None, ALLOWED)
    assert not ep._origin_allowed(None, ALLOWED, DASHBOARD)


def test_list_match_is_case_and_slash_insensitive() -> None:
    assert ep._origin_allowed("https://CUSTOMER.com/", ALLOWED)
    assert not ep._origin_allowed("https://evil.com", ALLOWED)
    assert not ep._origin_allowed("https://evil.com", ALLOWED, DASHBOARD)


def test_dashboard_origin_is_always_accepted_alongside_the_list() -> None:
    assert ep._origin_allowed(DASHBOARD, ALLOWED, DASHBOARD)
    # Path and query dropped, case ignored.
    assert ep._origin_allowed("https://DASH.example.com", ALLOWED, f"{DASHBOARD}/agents/123?x=1")
    # Port kept; trailing slash dropped.
    assert ep._origin_allowed("http://localhost:3001", ALLOWED, "http://localhost:3001/")
    assert not ep._origin_allowed("http://localhost:3000", ALLOWED, "http://localhost:3001")
    # The list still works with a dashboard URL configured, and without one.
    assert ep._origin_allowed("https://customer.com", ALLOWED, DASHBOARD)
    assert ep._origin_allowed("https://customer.com", ALLOWED, None)


def test_normalise_origin() -> None:
    assert ep._normalise_origin("HTTPS://Site.com:443/path/") == "https://site.com:443"
    assert ep._normalise_origin("site.com/") == "site.com"


# --- refused attempts are recorded ------------------------------------------


@pytest.mark.asyncio
async def test_refused_widget_call_writes_a_widget_row(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict] = []

    async def fake_insert(**kw):
        captured.append(kw)
        return "log-id"

    monkeypatch.setattr(ep, "insert_call_log", fake_insert)

    class Room:
        name = "widget-room-1"

    class Ctx:
        room = Room()

    d = ep._parse_dispatch(
        json.dumps({"widget_key": "wk_x", "origin": "https://evil.com", "visitor_id": "v9"})
    )
    await ep._log_refused_widget_call(
        Ctx(), d, agent_id="agent-1", reason="widget_origin_refused", detail="not on the list"
    )
    await ep._log_refused_widget_call(
        Ctx(), d, agent_id=None, reason="widget_key_refused", detail="key inactive"
    )

    for row in captured:
        assert row["channel"] == "widget"
        assert row["is_test"] is False
        assert row["call_status"] == "failed"
        assert row["ended_by"] == "system"
        assert row["room_id"] == "widget-room-1"
        assert row["duration_seconds"] == 0
        assert row["channel_metadata"]["origin"] == "https://evil.com"
        assert row["channel_metadata"]["visitor_id"] == "v9"
        assert row["channel_metadata"]["widget_key"] == "wk_x"
    assert captured[0]["agent_id"] == "agent-1"
    assert captured[0]["end_reason"] == "widget_origin_refused"
    assert captured[1]["agent_id"] is None
    assert captured[1]["end_reason"] == "widget_key_refused"
