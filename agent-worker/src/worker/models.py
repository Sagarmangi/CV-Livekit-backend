"""Row shapes for the Supabase schema in supabase/migrations/0001_init_schema.sql.

Mirrors dashboard/lib/types.ts -- keep the two in sync if the schema changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

AgentStatus = Literal["active", "paused", "draft"]

# Switchable per-agent from the dashboard, no worker redeploy needed -- see
# VOICE_STACK_DECISION.md.
#
# The first three are text LLMs inside the same Deepgram STT+TTS pipeline, so
# every turn-taking mechanism the worker has applies to all of them:
#   "gemini"   -- Gemini Flash. The default and the fastest measured
#                 time-to-first-token; see the 0019 migration for the numbers.
#   "deepseek" -- cheaper per token, slower to first token. Its base URL is
#                 configurable, which is the supported way to serve the same
#                 open-weight model from a faster host.
#   "groq"     -- kept for rollback; this account's key is currently rejected.
#
# "gemini_live" is a different shape entirely: a speech-to-speech realtime
# model that replaces the whole STT+LLM+TTS pipeline, not just the LLM leg. It
# also takes every turn-taking decision server-side, so Flux end-of-turn,
# preemptive TTS and the endpointing settings do not apply to it -- see
# entrypoint.py's session-building branch before enabling it on a live agent.
LLMProvider = Literal["groq", "deepseek", "gemini", "gemini_live"]

# How the agent opens a call -- see flow.py's on_enter.
FirstMessageMode = Literal["agent_generates", "agent_says_exact", "user_starts"]

CallOutcome = Literal[
    "qualified",
    "department_transfer",
    "not_qualified",
    "transfer_failed",
    "dropped",
    # Set by spam.py, not by the model. Kept separate from "dropped" -- which
    # means "the agent judged this call not worth continuing", from any cause --
    # because the whole point of detecting these is to be able to count them and
    # review what was hung up on. See the 0020 migration.
    "spam_bot",
    "spam_sales",
]

# Who ended the call -- see CallState.claim_end and the 0025 migration. Separate
# from CallOutcome, which says what the call was *for*: an agent hanging up on a
# robocall and an agent hanging up on a qualified lead are the same actor and
# different outcomes.
#
# "telephony" is not a caller hangup and must never be recorded as one: the line
# failed (SIP trunk, media, timeout) rather than anyone choosing to end the call.
EndedBy = Literal["agent", "caller", "system", "telephony", "unknown"]


@dataclass(frozen=True)
class QualificationCriterion:
    key: str
    question: str
    required: bool

    @staticmethod
    def from_row(row: dict[str, Any]) -> "QualificationCriterion":
        return QualificationCriterion(
            key=row["key"], question=row["question"], required=bool(row.get("required"))
        )


@dataclass(frozen=True)
class PronunciationEntry:
    term: str
    say_as: str

    @staticmethod
    def from_row(row: dict[str, Any]) -> "PronunciationEntry":
        return PronunciationEntry(term=row["term"], say_as=row["say_as"])


# Mirrors CONVERSATION_SETTING_DEFAULTS in dashboard/lib/types.ts -- what an
# unset field resolves to. Keep the numbers identical in both places: the
# dashboard shows these as placeholder text, so a drift here makes that text
# a lie about what actually happens on a call.
CONVERSATION_SETTING_DEFAULTS: dict[str, float] = {
    "temperature": 0.7,
    "max_reply_sentences": 2,
    "tts_stability": 0.6,
    "speech_rate": 1.0,
    # 300ms, not 500: this is now a floor under Flux's own end-of-turn decision
    # rather than the whole decision (see entrypoint._build_stt), and 500ms of
    # dead air was landing on top of PSTN transit on every phone turn.
    "vad_threshold_ms": 300,
    "interruption_sensitivity": 0.5,
    "backchannel_frequency": 0.2,
}


@dataclass(frozen=True)
class ConversationSettings:
    temperature: float
    max_reply_sentences: int
    tts_stability: float
    speech_rate: float
    vad_threshold_ms: float
    interruption_sensitivity: float
    backchannel_frequency: float

    @staticmethod
    def from_row(row: dict[str, Any]) -> "ConversationSettings":
        merged = {**CONVERSATION_SETTING_DEFAULTS, **(row or {})}
        return ConversationSettings(
            temperature=float(merged["temperature"]),
            max_reply_sentences=int(merged["max_reply_sentences"]),
            tts_stability=float(merged["tts_stability"]),
            speech_rate=float(merged["speech_rate"]),
            vad_threshold_ms=float(merged["vad_threshold_ms"]),
            interruption_sensitivity=float(merged["interruption_sensitivity"]),
            backchannel_frequency=float(merged["backchannel_frequency"]),
        )


@dataclass(frozen=True)
class Agent:
    agent_id: str
    name: str
    twilio_number: str | None
    status: AgentStatus
    prompt: str
    first_message_mode: FirstMessageMode
    # Only used when first_message_mode is "agent_says_exact".
    first_message_text: str | None
    qualification_criteria: list[QualificationCriterion]
    stt_provider: str
    tts_provider: str
    llm_provider: LLMProvider
    # Deepgram Aura TTS model name. Only reaches a call on the STT/LLM/TTS
    # pipeline -- gemini_live has no separate TTS stage to apply it to.
    voice_id: str | None
    # Gemini Live prebuilt voice name (Puck, Kore, Sulafat, ...). Separate column
    # from voice_id because the two engines' voice namespaces don't overlap and
    # an agent can be switched between them -- see the 0018 migration.
    gemini_voice: str | None
    pronunciation_dictionary: list[PronunciationEntry]
    conversation_settings: ConversationSettings
    # Null means "use flow.py's default end-of-call guidance". Doesn't change what
    # end_call *does* (still just hangs up) -- only the conditions the model is
    # told to watch for before calling it.
    end_call_instructions: str | None
    # Free-form reference text for off-topic questions, exposed to the model as
    # one on-demand tool (see tools.build_knowledge_tool) rather than
    # concatenated into every turn's prompt -- knowledge_base_description is
    # what the model actually sees to decide whether to call it.
    knowledge_base_content: str
    knowledge_base_description: str
    # Posted the full call record (transcript, outcome, lead info, etc.) once
    # the call ends -- see notify.send_end_call_webhook. Null means "don't send".
    end_call_webhook_url: str | None
    # Whether this agent posts to Slack at all. Off by default, including for
    # agents that predate the column -- see the 0026 migration on why opt-in.
    # Gates the lead alert and the transfer-failure alert alike; the worker's
    # SLACK_WEBHOOK_URL still has to be set for either to go anywhere.
    slack_notifications_enabled: bool

    @staticmethod
    def from_row(row: dict[str, Any]) -> "Agent":
        return Agent(
            agent_id=row["agent_id"],
            name=row["name"],
            twilio_number=row.get("twilio_number"),
            status=row["status"],
            prompt=row.get("prompt") or "",
            first_message_mode=row.get("first_message_mode") or "agent_generates",  # type: ignore[assignment]
            first_message_text=row.get("first_message_text"),
            qualification_criteria=[
                QualificationCriterion.from_row(r) for r in row.get("qualification_criteria") or []
            ],
            stt_provider=row.get("stt_provider") or "deepgram",
            tts_provider=row.get("tts_provider") or "deepgram",
            llm_provider=row.get("llm_provider") or "groq",  # type: ignore[assignment]
            voice_id=row.get("voice_id"),
            gemini_voice=row.get("gemini_voice"),
            pronunciation_dictionary=[
                PronunciationEntry.from_row(r) for r in row.get("pronunciation_dictionary") or []
            ],
            conversation_settings=ConversationSettings.from_row(row.get("conversation_settings") or {}),
            end_call_instructions=row.get("end_call_instructions"),
            knowledge_base_content=row.get("knowledge_base_content") or "",
            knowledge_base_description=row.get("knowledge_base_description") or "",
            end_call_webhook_url=row.get("end_call_webhook_url"),
            # Absent key reads as off, matching the column default -- a worker
            # running against a database without 0026 stays quiet rather than
            # broadcasting on a setting nobody chose.
            slack_notifications_enabled=bool(row.get("slack_notifications_enabled")),
        )


ToolType = Literal[
    "function",
    "transfer_call",
    "record_lead_info",
    "record_callback_number",
    # Hanging up. Unlike every other type here, this one has a builtin fallback:
    # an agent with no end_call row attached still gets the default tool, because
    # an agent that *cannot* hang up doesn't end its calls -- it holds the line
    # until LiveKit's room timeout expires, billing telephony the whole time.
    # Attaching a row replaces the default description with an admin's own.
    "end_call",
    # The two detector types are the odd ones out: they are configuration rows,
    # not callable functions. build_agent_tools deliberately doesn't build them,
    # because handing the model a "hang up on spam" function would put the
    # decision back where it can be argued with -- spam.py runs them against the
    # first transcript instead. See the 0020 migration.
    "detect_bot_call",
    "detect_sales_call",
]

# Which detector types exist, and the CallOutcome each one writes when it fires.
DETECTOR_TOOL_TYPES: dict[str, CallOutcome] = {
    "detect_bot_call": "spam_bot",
    "detect_sales_call": "spam_sales",
}


@dataclass(frozen=True)
class Tool:
    """A tool definition lives independently of any agent -- the same one
    (e.g. a booking webhook, or a transfer to Sales) is often reused across
    agents, so `tools` rows are global and an `agent_tools` join table
    records which agent has which selected. See dashboard/lib/queries.ts
    listAgentTools.

    `tool_type` picks the underlying behavior (see tools.py's build_agent_tools):
    "function" calls webhook_url with the model's arguments; the other three
    are native Python behavior (transfer, lead capture, callback capture)
    that used to be unconditional on every agent -- now they're admin-created
    rows like any other tool, opt-in per agent, with an admin-written
    description instead of a fixed one."""

    tool_id: str
    name: str
    description: str
    tool_type: ToolType
    parameter_schema: dict[str, Any]
    webhook_url: str | None
    destination_number: str | None
    is_builtin: bool
    # Off switches the tool off for every agent at once, without unpicking
    # agent_tools row by row. Applies to all types, not just detectors.
    is_enabled: bool
    # Detector types only. Example statements an admin maintains: matched
    # literally first (free, no LLM) and then used as few-shot examples for the
    # semantic pass -- see spam.py.
    detector_statements: list[str]
    # Which LLM judges the semantic pass. None means "use the agent's own
    # llm_provider", which is the sane default: one fewer thing to configure,
    # and the key is already known to be present.
    detector_llm: str | None

    @staticmethod
    def from_row(row: dict[str, Any]) -> "Tool":
        # Statements are jsonb, so a hand-edited row can contain anything --
        # coerced to strings and stripped of blanks here rather than letting an
        # empty entry match every utterance, which is what a bare "" would do to
        # the containment check in spam.literal_match.
        raw_statements = row.get("detector_statements") or []
        statements = [
            text.strip()
            for text in (str(value) for value in raw_statements if value is not None)
            if text.strip()
        ]

        return Tool(
            tool_id=row["tool_id"],
            name=row["name"],
            description=row["description"],
            tool_type=row.get("tool_type") or "function",
            parameter_schema=row.get("parameter_schema") or {},
            webhook_url=row.get("webhook_url"),
            destination_number=row.get("destination_number"),
            is_builtin=bool(row.get("is_builtin")),
            # Defaults to enabled for a row written before the column existed.
            is_enabled=bool(row.get("is_enabled", True)),
            detector_statements=statements,
            detector_llm=row.get("detector_llm"),
        )


@dataclass
class AgentConfig:
    """Everything the worker needs to run one call, loaded once per job."""

    agent: Agent
    tools: list[Tool] = field(default_factory=list)
