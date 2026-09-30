-- Adds Gemini Live as a third selectable conversation engine, alongside the
-- Groq/DeepSeek pipeline modes added in 0005. Unlike those two, this one
-- replaces the whole STT+LLM+TTS pipeline with a single speech-to-speech
-- model -- see VOICE_STACK_DECISION.md for what that gives up (pronunciation
-- substitution, most tuning knobs) in exchange for lower cost.

alter table agents drop constraint agents_llm_provider_check;

alter table agents
  add constraint agents_llm_provider_check
    check (llm_provider in ('groq', 'deepseek', 'gemini_live'));
