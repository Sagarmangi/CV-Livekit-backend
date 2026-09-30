"""The inbound call flow -- one Agent whose instructions come from the
agent's own configured prompt, not a hardcoded script.

Deliberately minimal: the model's behavior is driven by (1) the agent's own
system prompt, exactly as typed into the dashboard, and (2) each tool's own
description, which the model already sees via the function-calling schema on
every turn regardless of what's in the prompt text. This file used to wrap
that prompt in a large hand-authored call-handling script (classification
steps, a qualifying-question order, scam/sales-pitch heuristics, an AI-
disclosure paragraph, etc.) -- that script wasn't configured by any admin, so
it silently overrode whatever they actually wrote and crowded out the tool
descriptions' own influence on tool-calling. Removed for that reason: an
admin who wants any of that back writes it into the prompt themselves.

The only things added below are data that has its own dashboard field with no
other way to reach the model (qualification questions, end-call conditions)
and two voice-call mechanics tied to actual settings rather than scripted
behavior: reply length (Voice & humanness tuning) and a one-line reminder
that end_call is the one tool every agent always has and must actually call
to hang up -- a spoken goodbye by itself doesn't end the call.

Deterministic actions (transfer, ending the call, recording data) are pushed
into tool calls rather than left to the LLM to narrate, so the parts that
matter for compliance and correctness happen in code -- the LLM only decides
*when* to call them, guided by each tool's own description.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable

from livekit.agents import Agent, ModelSettings
from livekit.agents.llm import RealtimeModel

from . import tools
from .models import AgentConfig
from .pronunciation import stt_keyterms, substitute_stream

logger = logging.getLogger("worker.flow")


def _build_instructions(config: AgentConfig) -> str:
    agent = config.agent
    parts = [
        agent.prompt or f"You are {agent.name}.",
        f"You're on a live phone call -- everything you say is spoken aloud, not read as text. "
        f"Keep replies to about {agent.conversation_settings.max_reply_sentences} sentence(s) at a time.",
        "Call end_call once there's nothing further to discuss or the caller wants to hang up -- "
        "speaking a goodbye out loud does not by itself end the call.",
    ]

    if agent.qualification_criteria:
        questions = "\n".join(
            f"- {c.question}" + (" (required)" if c.required else "")
            for c in agent.qualification_criteria
        )
        parts.append(f"Things to find out during the call, if they come up naturally:\n{questions}")

    if agent.end_call_instructions:
        parts.append(f"When to end the call:\n{agent.end_call_instructions}")

    return "\n\n".join(parts)


class InboundCallAgent(Agent):
    """One Agent handles the whole call -- see the module docstring for why
    this is a single LLM-driven flow with tool calls for consequential
    actions, rather than several Agent subclasses handed off between."""

    def __init__(self, config: AgentConfig) -> None:
        self._pronunciation_dictionary = config.agent.pronunciation_dictionary
        self._first_message_mode = config.agent.first_message_mode
        self._first_message_text = config.agent.first_message_text
        super().__init__(
            instructions=_build_instructions(config),
            tools=[
                # Passed the agent's rows so it can stand down if one of them is
                # its own end_call tool -- see tools.builtin_tools.
                *tools.builtin_tools(config.tools),
                *tools.build_agent_tools(config.tools),
                *tools.build_knowledge_tool(config.agent),
            ],
        )

    async def on_enter(self) -> None:
        if self._first_message_mode == "user_starts":
            # Say nothing -- the session still listens continuously, so it
            # responds normally once the caller speaks first.
            return

        realtime = isinstance(self.session.llm, RealtimeModel)

        if realtime and not self.session.llm.capabilities.mutable_chat_context:
            # Some Gemini Live models (anything 3.x so far) refuse a
            # client-initiated first turn: the plugin gates generate_reply on
            # mutable_chat_context and raises otherwise, so there is no way to
            # make the agent speak first. Warn loudly rather than leaving an
            # inbound call opening with silence and no explanation.
            logger.warning(
                "greeting skipped: %s doesn't support a client-initiated turn, so the agent "
                "can't speak first. Use a native-audio Gemini Live model (e.g. "
                "gemini-2.5-flash-native-audio-preview-12-2025), or set this agent's first "
                "message to \"caller speaks first\".",
                getattr(self.session.llm, "model", "this realtime model"),
            )
            return

        if self._first_message_mode == "agent_says_exact" and self._first_message_text:
            if realtime:
                # No TTS stage exists for say() to write into -- with a realtime
                # model it raises outright (supports_say is False). The nearest
                # equivalent is handing the model the exact line for this one
                # turn; it's read by the model rather than synthesized verbatim,
                # so treat it as strong instruction, not a guarantee.
                self.session.generate_reply(
                    instructions=(
                        f"Open the call by saying exactly this, word for word, and nothing "
                        f'else: "{self._first_message_text}"'
                    )
                )
                return
            # session.say() goes straight to TTS, skipping the LLM entirely --
            # this is spoken verbatim, not ad-libbed from the prompt.
            self.session.say(self._first_message_text)
            return

        # Default ("agent_generates"): nudges the model to speak first instead
        # of waiting on caller input, but doesn't tell it what to say -- no
        # `instructions` kwarg here, so the opening line comes entirely from
        # the agent's own system prompt, matching what the dashboard's First
        # Message option promises ("ad-libbed from the prompt").
        self.session.generate_reply()

    def tts_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ):
        return super().tts_node(
            substitute_stream(text, self._pronunciation_dictionary), model_settings
        )


def stt_keyterm_list(config: AgentConfig) -> list[str]:
    return stt_keyterms(config.agent.pronunciation_dictionary)
