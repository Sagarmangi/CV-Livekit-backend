"""What a call cost, from what it actually used.

The SDK already meters every billable quantity -- seconds of audio recognised,
tokens in and out, characters synthesised -- and reports them per
(provider, model) through `ModelUsageCollector`. This turns those counters into
money, so the dashboard can show a per-call figure instead of leaving "what is
this costing us" to a spreadsheet built on averages.

Two of the three provider numbers are near-exact: the token counts come from
the model provider's own usage response, not from our estimate, and audio
duration is metered on the stream we send. The third has a known bias worth
stating plainly -- see `_tts_cost`.

Telephony is different in kind from the other three. Twilio bills the account
directly, and this worker holds no Twilio credentials (see settings.py -- only
the dashboard does), so there is nothing to read. It is estimated from a
configured per-minute rate, kept in its own field, and labelled as an estimate
everywhere it surfaces. Anyone reconciling against a real invoice needs to know
which of these four numbers is a measurement and which is an assumption.

Rates default to the published pay-as-you-go prices verified 2026-08-13 and are
overridable per deployment by environment variable -- a rate hardcoded in a
release is a rate that goes stale the first time a provider reprices.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

logger = logging.getLogger("worker.pricing")


def _rate(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r isn't a number; using the default %s", name, raw, default)
        return default


@dataclass(frozen=True)
class Rates:
    """Published pay-as-you-go rates, verified 2026-08-13.

    Matched against the model name by substring rather than equality, because
    the names carry variants we don't want to enumerate: every Aura-2 voice is
    a separate model string (`aura-2-helena-en`, `aura-2-andromeda-en`, ...)
    but they all bill identically.
    """

    # Deepgram STT, $/minute of audio. Flux is what runs; nova-3 is the
    # FallbackAdapter's second choice and bills differently, which is exactly
    # why usage is collected per model rather than in one bucket.
    stt_per_min: dict[str, float] = field(
        default_factory=lambda: {
            "flux": _rate("PRICE_STT_FLUX_PER_MIN", 0.0065),
            "nova": _rate("PRICE_STT_NOVA_PER_MIN", 0.0048),
        }
    )
    # Deepgram Aura, $/1000 characters. Aura-2 is double Aura-1.
    tts_per_1k_chars: dict[str, float] = field(
        default_factory=lambda: {
            "aura-2": _rate("PRICE_TTS_AURA2_PER_1K", 0.030),
            "aura-1": _rate("PRICE_TTS_AURA1_PER_1K", 0.015),
        }
    )
    # $/1M tokens, (input, output). Cached input is billed separately where the
    # provider reports it -- see `_llm_cost`.
    llm_per_1m: dict[str, tuple[float, float]] = field(
        default_factory=lambda: {
            "gemini-2.5-flash": (
                _rate("PRICE_LLM_GEMINI_FLASH_IN_PER_1M", 0.30),
                _rate("PRICE_LLM_GEMINI_FLASH_OUT_PER_1M", 2.50),
            ),
            "llama-3.3-70b": (
                _rate("PRICE_LLM_GROQ_IN_PER_1M", 0.59),
                _rate("PRICE_LLM_GROQ_OUT_PER_1M", 0.79),
            ),
            "deepseek": (
                _rate("PRICE_LLM_DEEPSEEK_IN_PER_1M", 0.28),
                _rate("PRICE_LLM_DEEPSEEK_OUT_PER_1M", 0.42),
            ),
        }
    )
    # Google bills Live API audio separately from text, and far higher. Only
    # reached by a gemini_live agent.
    llm_audio_per_1m: dict[str, tuple[float, float]] = field(
        default_factory=lambda: {
            "native-audio": (
                _rate("PRICE_LLM_GEMINI_LIVE_AUDIO_IN_PER_1M", 3.00),
                _rate("PRICE_LLM_GEMINI_LIVE_AUDIO_OUT_PER_1M", 12.00),
            ),
        }
    )
    # Inbound PSTN, $/minute. An assumption, not a reading -- see module docstring.
    telephony_per_min: float = field(
        default_factory=lambda: _rate("PRICE_TELEPHONY_PER_MIN", 0.0085)
    )


@lru_cache
def rates() -> Rates:
    return Rates()


def _match(table: dict[str, Any], model: str) -> Any | None:
    """Longest matching key wins, so "aura-2" can't be shadowed by a broader
    entry added later."""
    hit: Any | None = None
    best = -1
    lowered = (model or "").lower()
    for key, value in table.items():
        if key in lowered and len(key) > best:
            hit, best = value, len(key)
    return hit


@dataclass
class LineItem:
    """One priced row of the breakdown, and the arithmetic behind it.

    `rate_usd is None` means no rate was configured for this model. The line is
    kept anyway, at zero cost, so the total is visibly incomplete rather than
    quietly wrong -- a missing rate should look like a gap, not like a bargain.
    """

    component: str  # stt | llm | tts | telephony
    provider: str
    model: str
    quantity: float
    unit: str
    rate_usd: float | None
    cost_usd: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "provider": self.provider,
            "model": self.model,
            "quantity": round(self.quantity, 4),
            "unit": self.unit,
            "rate_usd": self.rate_usd,
            "cost_usd": round(self.cost_usd, 6),
            **({"unpriced": True} if self.rate_usd is None else {}),
        }


@dataclass
class CallCost:
    stt_usd: float = 0.0
    llm_usd: float = 0.0
    tts_usd: float = 0.0
    telephony_usd: float = 0.0
    lines: list[LineItem] = field(default_factory=list)

    @property
    def total_usd(self) -> float:
        return self.stt_usd + self.llm_usd + self.tts_usd + self.telephony_usd

    def breakdown(self) -> dict[str, Any]:
        return {
            "lines": [line.as_dict() for line in self.lines],
            # Recorded so a row stays auditable after the rates change under it.
            "priced_at_rates": "2026-08-13 published pay-as-you-go, env-overridable",
            "telephony_is_estimated": True,
        }


def _stt_cost(usage: Any, out: CallCost) -> None:
    minutes = (getattr(usage, "audio_duration", 0.0) or 0.0) / 60.0
    if minutes <= 0:
        return
    rate = _match(rates().stt_per_min, usage.model)
    cost = minutes * rate if rate is not None else 0.0
    out.stt_usd += cost
    out.lines.append(
        LineItem("stt", usage.provider, usage.model, minutes, "minutes", rate, cost)
    )


def _tts_cost(usage: Any, out: CallCost) -> None:
    """Characters synthesised, at the per-1k rate for that Aura generation.

    Known to run slightly high. The SDK reports `characters_count` as the length
    of the whole text pushed into a segment, but the Deepgram plugin streams it
    word by word and stops when the agent is interrupted -- so on a barged-in
    turn Deepgram received, and billed for, fewer characters than are counted
    here. There is no way to recover how many words made it, so this
    over-reports rather than guessing; erring high is the safer direction for a
    number people budget against. Fewer false interruptions (see
    entrypoint._turn_handling_from_settings) directly shrinks the error.
    """
    chars = getattr(usage, "characters_count", 0) or 0
    if chars <= 0:
        return
    rate = _match(rates().tts_per_1k_chars, usage.model)
    cost = (chars / 1000.0) * rate if rate is not None else 0.0
    out.tts_usd += cost
    out.lines.append(
        LineItem("tts", usage.provider, usage.model, float(chars), "characters", rate, cost)
    )


def _llm_cost(usage: Any, out: CallCost) -> None:
    """Text and audio tokens priced separately, because Google charges roughly
    ten times more for audio and a gemini_live agent bills entirely in it."""
    audio_in = getattr(usage, "input_audio_tokens", 0) or 0
    audio_out = getattr(usage, "output_audio_tokens", 0) or 0
    total_in = getattr(usage, "input_tokens", 0) or 0
    total_out = getattr(usage, "output_tokens", 0) or 0

    # Whatever isn't audio is billed at the text rate. Derived by subtraction
    # rather than read from input_text_tokens, which plain text LLMs leave at 0
    # while still reporting a real input_tokens.
    text_in = max(0, total_in - audio_in)
    text_out = max(0, total_out - audio_out)

    if text_in or text_out:
        pair = _match(rates().llm_per_1m, usage.model)
        rate_in, rate_out = pair if pair else (None, None)
        cost = 0.0
        if rate_in is not None:
            cost = (text_in / 1_000_000.0) * rate_in + (text_out / 1_000_000.0) * rate_out
        out.llm_usd += cost
        out.lines.append(
            LineItem(
                "llm",
                usage.provider,
                usage.model,
                float(text_in + text_out),
                f"tokens ({text_in} in / {text_out} out)",
                rate_in,
                cost,
            )
        )

    if audio_in or audio_out:
        pair = _match(rates().llm_audio_per_1m, usage.model)
        rate_in, rate_out = pair if pair else (None, None)
        cost = 0.0
        if rate_in is not None:
            cost = (audio_in / 1_000_000.0) * rate_in + (audio_out / 1_000_000.0) * rate_out
        out.llm_usd += cost
        out.lines.append(
            LineItem(
                "llm",
                usage.provider,
                usage.model,
                float(audio_in + audio_out),
                f"audio tokens ({audio_in} in / {audio_out} out)",
                rate_in,
                cost,
            )
        )


def compute_call_cost(
    model_usage: list[Any], duration_seconds: int | None, *, is_test: bool
) -> CallCost:
    """Price one call from `ModelUsageCollector.flatten()`.

    `is_test` zeroes telephony: a dashboard test session is a browser joining a
    LiveKit room, so no carrier is involved and charging it a per-minute PSTN
    rate would overstate every test call in the log.
    """
    cost = CallCost()

    for usage in model_usage:
        kind = getattr(usage, "type", "")
        try:
            if kind == "stt_usage":
                _stt_cost(usage, cost)
            elif kind == "tts_usage":
                _tts_cost(usage, cost)
            elif kind in ("llm_usage", "realtime_usage"):
                _llm_cost(usage, cost)
            # interruption/eot usage are LiveKit inference models, unused on a
            # self-hosted deployment and unpriced -- skipped rather than
            # reported as free.
        except Exception:  # noqa: BLE001 -- costing must never fail a call teardown
            logger.exception("couldn't price %s usage for model %r", kind, getattr(usage, "model", "?"))

    if not is_test and duration_seconds:
        minutes = duration_seconds / 60.0
        rate = rates().telephony_per_min
        cost.telephony_usd = minutes * rate
        cost.lines.append(
            LineItem("telephony", "twilio", "inbound-pstn", minutes, "minutes", rate, cost.telephony_usd)
        )

    unpriced = [line.model for line in cost.lines if line.rate_usd is None]
    if unpriced:
        logger.warning(
            "no rate configured for %s -- their cost is missing from this call's total, "
            "not zero. Add a PRICE_* override in pricing.py.",
            ", ".join(sorted(set(unpriced))),
        )

    return cost
