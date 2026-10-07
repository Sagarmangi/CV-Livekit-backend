"""Model-name resolution and per-call pricing -- see pricing.py."""

from __future__ import annotations

import logging

import pytest

from worker import pricing
from worker.pricing import _match, compute_call_cost, rates


class LLMUsage:
    type = "llm_usage"
    provider = "google"
    input_audio_tokens = 0
    output_audio_tokens = 0

    def __init__(self, model: str, input_tokens: int, output_tokens: int) -> None:
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


@pytest.fixture(autouse=True)
def _fresh_rates():
    # rates() is lru_cached; tests that touch PRICE_* need it rebuilt.
    rates.cache_clear()
    yield
    rates.cache_clear()


def test_flash_lite_resolves_to_its_own_entry_only() -> None:
    table = rates().llm_per_1m
    assert _match(table, "gemini-3.5-flash-lite") == table["gemini-3.5-flash-lite"]
    assert _match(table, "models/gemini-3.5-flash-lite") == table["gemini-3.5-flash-lite"]
    assert _match(table, "gemini-2.5-flash") == table["gemini-2.5-flash"]


@pytest.mark.parametrize("model", ["gemini-3.8-flash", "gemini-3.5-flash"])
def test_models_without_an_entry_do_not_borrow_flash_lite(model: str) -> None:
    assert _match(rates().llm_per_1m, model) is None


def test_flash_lite_call_is_priced_with_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=pricing.logger.name):
        cost = compute_call_cost([LLMUsage("gemini-3.5-flash-lite", 1078, 113)], 30, is_web=True)
    expected = 1078 / 1e6 * 0.30 + 113 / 1e6 * 2.50
    assert cost.llm_usd == pytest.approx(expected)
    assert cost.llm_usd == pytest.approx(0.000606, abs=1e-6)
    assert not any("no rate" in record.getMessage() for record in caplog.records)


def test_unpriced_model_reports_no_rate(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=pricing.logger.name):
        cost = compute_call_cost([LLMUsage("gemini-3.8-flash", 1078, 113)], 30, is_web=True)
    assert cost.llm_usd == 0
    assert cost.lines[0].rate_usd is None
    assert any("no rate configured for gemini-3.8-flash" in r.getMessage() for r in caplog.records)


def test_price_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRICE_LLM_GEMINI_35_FLASH_LITE_OUT_PER_1M", "5.00")
    rates.cache_clear()
    cost = compute_call_cost([LLMUsage("gemini-3.5-flash-lite", 0, 1_000_000)], 1, is_web=True)
    assert cost.llm_usd == pytest.approx(5.0)


def test_browser_calls_carry_no_telephony_cost() -> None:
    assert compute_call_cost([], 60, is_web=True).telephony_usd == 0
    assert compute_call_cost([], 60, is_web=False).telephony_usd > 0
