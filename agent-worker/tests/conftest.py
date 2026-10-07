"""Shared fixtures. The worker reads config from the environment lazily, so no
provider keys are needed to import it -- but a developer's own .env must not
leak into the tests either, which is what the PRICE_* scrub below is for."""

from __future__ import annotations

import os

import pytest

from worker.models import Agent, AgentConfig


@pytest.fixture(autouse=True)
def _no_price_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("PRICE_"):
            monkeypatch.delenv(name)


@pytest.fixture
def agent_row() -> dict:
    """The minimum `agents` row Agent.from_row accepts."""
    return {"agent_id": "agent-1", "name": "Test Agent", "status": "active"}


@pytest.fixture
def config(agent_row: dict) -> AgentConfig:
    return AgentConfig(agent=Agent.from_row(agent_row))
