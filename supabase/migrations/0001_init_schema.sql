-- Kodexo Voice Agent — initial schema
-- Matches FSD Section 6 (KL-FSD-VOICE-006 v6) plus the v2 Project Plan scope
-- addition: the `tools` table for the dashboard-configurable tools framework.

create extension if not exists "pgcrypto";

create or replace function set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

-- ---------------------------------------------------------------------------
-- agents
-- ---------------------------------------------------------------------------
create type agent_status as enum ('active', 'paused', 'draft');

create table agents (
  agent_id uuid primary key default gen_random_uuid(),
  name text not null,                          -- persona name
  twilio_number text unique,                   -- e164, e.g. +15105550100; null until a number is attached
  status agent_status not null default 'draft',
  prompt text not null default '',
  qualification_criteria jsonb not null default '[]'::jsonb,
  stt_provider text not null default 'deepgram',
  tts_provider text not null default 'cartesia',
  voice_id text,
  pronunciation_dictionary jsonb not null default '[]'::jsonb,
  -- conversation_settings holds the Section 4.3 humanness parameters:
  -- temperature, max_reply_sentences, tts_stability, speech_rate,
  -- vad_threshold_ms, interruption_sensitivity, backchannel_frequency
  conversation_settings jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create trigger agents_set_updated_at
  before update on agents
  for each row execute function set_updated_at();

create index idx_agents_twilio_number on agents (twilio_number);

-- ---------------------------------------------------------------------------
-- departments — one or more per agent, the transfer/routing directory
-- ---------------------------------------------------------------------------
create table departments (
  department_id uuid primary key default gen_random_uuid(),
  agent_id uuid not null references agents (agent_id) on delete cascade,
  department_name text not null,               -- e.g. Sales, HR, Support
  transfer_number text not null,                -- e164
  routing_keywords text,                        -- free text used to match caller intent
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create trigger departments_set_updated_at
  before update on departments
  for each row execute function set_updated_at();

create index idx_departments_agent_id on departments (agent_id);

-- ---------------------------------------------------------------------------
-- knowledge_base — reference answers per agent (FSD Section 3.5)
-- ---------------------------------------------------------------------------
create table knowledge_base (
  kb_id uuid primary key default gen_random_uuid(),
  agent_id uuid not null references agents (agent_id) on delete cascade,
  title text not null,
  content text not null,
  updated_at timestamptz not null default now()
);

create trigger knowledge_base_set_updated_at
  before update on knowledge_base
  for each row execute function set_updated_at();

create index idx_knowledge_base_agent_id on knowledge_base (agent_id);

-- ---------------------------------------------------------------------------
-- tools — Scope Addition (Project Plan v2): dashboard-configurable,
-- Vapi-style tool definitions assignable to any agent.
-- ---------------------------------------------------------------------------
create table tools (
  tool_id uuid primary key default gen_random_uuid(),
  agent_id uuid not null references agents (agent_id) on delete cascade,
  name text not null,                           -- tool name exposed to the LLM's function-calling interface
  description text not null,                    -- tells the LLM when to use it
  parameter_schema jsonb not null default '{}'::jsonb,  -- JSON Schema for the tool's arguments
  webhook_url text,                             -- n8n webhook this tool calls; null for built-in tools (transfer, call-tracking)
  is_builtin boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create trigger tools_set_updated_at
  before update on tools
  for each row execute function set_updated_at();

create index idx_tools_agent_id on tools (agent_id);

-- ---------------------------------------------------------------------------
-- call_logs
-- ---------------------------------------------------------------------------
create type call_outcome as enum (
  'qualified',
  'department_transfer',
  'not_qualified',
  'transfer_failed',
  'dropped'
);

create table call_logs (
  call_log_id uuid primary key default gen_random_uuid(),
  call_sid text,                                -- Twilio call SID
  room_id text,                                 -- LiveKit room name/id
  agent_id uuid references agents (agent_id) on delete set null,
  caller_number text,
  transcript text,
  recording_url text,
  duration_seconds integer,
  outcome call_outcome,
  matched_department text,
  lead_name text,
  lead_company text,
  lead_need text,
  created_at timestamptz not null default now()
);

create index idx_call_logs_agent_id on call_logs (agent_id);
create index idx_call_logs_created_at on call_logs (created_at desc);
create index idx_call_logs_outcome on call_logs (outcome);
