-- Paste this whole file into the Supabase SQL Editor
-- (https://supabase.com/dashboard/project/nfmzyozabwyxbknmcbyt/sql/new) and
-- run it once. It's 0001_init_schema.sql + 0002_grants.sql concatenated —
-- schema creation, then the role grants Supabase Cloud doesn't always set up
-- automatically for a project provisioned outside the dashboard's own flow.
--
-- This file is just a convenience copy for pasting; the two migration files
-- above remain the source of truth for `supabase db push` later.

-- ============================================================================
-- 0001_init_schema.sql
-- ============================================================================

create extension if not exists "pgcrypto";

create or replace function set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create type agent_status as enum ('active', 'paused', 'draft');

create table agents (
  agent_id uuid primary key default gen_random_uuid(),
  name text not null,
  twilio_number text unique,
  status agent_status not null default 'draft',
  prompt text not null default '',
  qualification_criteria jsonb not null default '[]'::jsonb,
  stt_provider text not null default 'deepgram',
  tts_provider text not null default 'cartesia',
  voice_id text,
  pronunciation_dictionary jsonb not null default '[]'::jsonb,
  conversation_settings jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create trigger agents_set_updated_at
  before update on agents
  for each row execute function set_updated_at();

create index idx_agents_twilio_number on agents (twilio_number);

create table departments (
  department_id uuid primary key default gen_random_uuid(),
  agent_id uuid not null references agents (agent_id) on delete cascade,
  department_name text not null,
  transfer_number text not null,
  routing_keywords text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create trigger departments_set_updated_at
  before update on departments
  for each row execute function set_updated_at();

create index idx_departments_agent_id on departments (agent_id);

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

create table tools (
  tool_id uuid primary key default gen_random_uuid(),
  agent_id uuid not null references agents (agent_id) on delete cascade,
  name text not null,
  description text not null,
  parameter_schema jsonb not null default '{}'::jsonb,
  webhook_url text,
  is_builtin boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create trigger tools_set_updated_at
  before update on tools
  for each row execute function set_updated_at();

create index idx_tools_agent_id on tools (agent_id);

create type call_outcome as enum (
  'qualified',
  'department_transfer',
  'not_qualified',
  'transfer_failed',
  'dropped'
);

create table call_logs (
  call_log_id uuid primary key default gen_random_uuid(),
  call_sid text,
  room_id text,
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

-- ============================================================================
-- 0002_grants.sql
-- ============================================================================

grant usage on schema public to anon, authenticated, service_role;

grant select on
  agents, departments, knowledge_base, tools, call_logs
to anon, authenticated;

grant select, insert, update, delete on
  agents, departments, knowledge_base, tools, call_logs
to service_role;

alter default privileges in schema public
  grant select on tables to anon, authenticated;

alter default privileges in schema public
  grant select, insert, update, delete on tables to service_role;
