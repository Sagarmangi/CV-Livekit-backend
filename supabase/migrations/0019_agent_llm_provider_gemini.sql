-- Adds 'gemini' -- Gemini Flash as a plain text LLM inside the existing
-- Deepgram pipeline (Flux STT -> Gemini -> Aura TTS).
--
-- This is deliberately NOT the same thing as 'gemini_live', which is a
-- speech-to-speech model that replaces the pipeline entirely and takes its
-- turn-taking decisions server-side. 'gemini' keeps every latency mechanism
-- the worker has -- Flux semantic end-of-turn, eager EOT, preemptive TTS,
-- dynamic endpointing -- and only swaps which model writes the words.
--
-- Why it becomes the default: measured time-to-first-token for a one-sentence
-- phone reply, five runs each, reasoning disabled where the API allows it:
--
--   gemini-2.5-flash        451 ms best / 499 ms median
--   gemini-3.1-flash-lite   498 ms best / 510 ms median
--   deepseek (origin)       535 ms best / 648 ms median
--   gemini-3.5-flash        687 ms best / 753 ms median
--
-- Those were taken from Pakistan; the production VPS is in Boston, which
-- shortens the hop to Google and lengthens the one to DeepSeek's origin, so
-- the gap should widen rather than close. TTFT is the number that matters
-- because TTS starts speaking on the first token -- total completion time is
-- not what a caller waits for.
--
-- The old default, 'groq', is also no longer reachable: that account's key is
-- rejected (HTTP 401), so any agent created with the previous default would
-- fail at the moment it took a call.

alter table agents drop constraint agents_llm_provider_check;

alter table agents
  add constraint agents_llm_provider_check
    check (llm_provider in ('groq', 'deepseek', 'gemini', 'gemini_live'));

alter table agents alter column llm_provider set default 'gemini';

comment on column agents.llm_provider is
  'Which conversation engine runs the call. groq/deepseek/gemini are text LLMs '
  'inside the Deepgram STT+TTS pipeline; gemini_live is speech-to-speech and '
  'replaces that pipeline entirely.';
