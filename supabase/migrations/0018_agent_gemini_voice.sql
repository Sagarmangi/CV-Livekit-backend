-- Gemini Live picks its voice from its own fixed set of prebuilt names
-- (Puck, Kore, Sulafat, ...), which has nothing in common with a Deepgram Aura
-- model name like 'aura-2-thalia-en'.
--
-- Kept in its own column rather than reusing voice_id: the two engines are
-- switchable per agent from the dashboard, and sharing one column would mean
-- flipping the engine either silently invalidates the stored voice or throws
-- away the other engine's choice. With both columns the worker just reads
-- whichever one matches the engine it's building.

alter table agents add column gemini_voice text;

comment on column agents.gemini_voice is
  'Gemini Live prebuilt voice name, used only when llm_provider = ''gemini_live''. Null means the plugin default (Puck).';

comment on column agents.voice_id is
  'Deepgram Aura TTS model name, used only when llm_provider is groq or deepseek. Null means the worker default.';
