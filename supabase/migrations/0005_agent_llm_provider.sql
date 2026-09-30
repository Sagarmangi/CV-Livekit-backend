-- Per-agent LLM provider switch (Groq <-> DeepSeek), changeable from the
-- dashboard at any time -- no worker redeploy needed, since the worker reads
-- this column fresh on every call. See VOICE_STACK_DECISION.md.

alter table agents
  add column llm_provider text not null default 'groq'
    check (llm_provider in ('groq', 'deepseek'));
