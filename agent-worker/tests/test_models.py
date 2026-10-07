"""Row -> dataclass mapping for the 0027 widget columns, and the channel
flags on CallState."""

from __future__ import annotations

import pytest

from worker.models import Agent, AgentConfig
from worker.state import CallState


def test_row_from_before_0027_reads_as_widget_off(agent_row: dict) -> None:
    agent = Agent.from_row(agent_row)
    assert agent.widget_enabled is False
    assert agent.widget_key is None
    assert agent.widget_allowed_origins == []
    assert agent.widget_config == {}
    assert agent.widget_max_seconds == 300
    assert agent.widget_greeting is None


def test_widget_columns_are_read(agent_row: dict) -> None:
    agent = Agent.from_row(
        {
            **agent_row,
            "widget_enabled": True,
            "widget_key": "wk_1",
            "widget_allowed_origins": ["https://a.com"],
            "widget_config": {"greeting": " Hi there ", "theme": "#fff"},
            "widget_max_seconds": 120,
        }
    )
    assert agent.widget_enabled and agent.widget_key == "wk_1"
    assert agent.widget_allowed_origins == ["https://a.com"]
    assert agent.widget_greeting == "Hi there"
    assert agent.widget_max_seconds == 120


def test_garbage_widget_config_is_ignored(agent_row: dict) -> None:
    assert Agent.from_row({**agent_row, "widget_config": "garbage"}).widget_config == {}


@pytest.mark.parametrize(
    ("channel", "is_web"), [("phone", False), ("test", True), ("widget", True)]
)
def test_is_web_follows_channel(config: AgentConfig, channel: str, is_web: bool) -> None:
    assert CallState(config=config, room_name="r", channel=channel).is_web is is_web
