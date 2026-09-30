"""FSD Section 4.1 / FR-07: the pronunciation dictionary.

Cartesia's own pronunciation_dict_id is a pre-provisioned server-side resource
(created once via their dashboard/API, not something to spin up per agent at
call time), so instead of wiring that up per-provider, this does plain text
substitution on the words the TTS engine actually receives -- provider-agnostic,
and it's the same fix for the FSD's concrete motivating incident either way:
"Kodexo Labs" coming out as "Codexo Labs".
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterable, AsyncIterator

from .models import PronunciationEntry


def compile_pronunciation(dictionary: list[PronunciationEntry]) -> re.Pattern | None:
    if not dictionary:
        return None
    # Longest terms first so "Kodexo Labs" matches before a bare "Kodexo" entry
    # would swallow part of it.
    terms = sorted((e.term for e in dictionary), key=len, reverse=True)
    pattern = "|".join(re.escape(t) for t in terms)
    return re.compile(rf"\b(?:{pattern})\b", re.IGNORECASE)


def apply_pronunciation(text: str, dictionary: list[PronunciationEntry]) -> str:
    pattern = compile_pronunciation(dictionary)
    if pattern is None:
        return text

    say_as_by_lower = {e.term.lower(): e.say_as for e in dictionary}

    def _replace(match: re.Match) -> str:
        return say_as_by_lower.get(match.group(0).lower(), match.group(0))

    return pattern.sub(_replace, text)


async def substitute_stream(
    text: AsyncIterable[str], dictionary: list[PronunciationEntry]
) -> AsyncIterator[str]:
    """Wraps the Agent.tts_node text stream so pronunciation substitution runs
    on every chunk before it reaches the TTS engine, regardless of provider."""

    pattern = compile_pronunciation(dictionary)
    if pattern is None:
        async for chunk in text:
            yield chunk
        return

    async for chunk in text:
        yield apply_pronunciation(chunk, dictionary)


def stt_keyterms(dictionary: list[PronunciationEntry]) -> list[str]:
    """Deepgram's `keyterm` boosting -- the STT half of the same requirement,
    so the recognizer is more likely to hear "Kodexo" correctly in the first
    place rather than only fixing how it's spoken back."""

    return [entry.term for entry in dictionary]
