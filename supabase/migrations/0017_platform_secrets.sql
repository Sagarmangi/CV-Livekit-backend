-- Provider credentials become editable from the dashboard's Integrations page
-- instead of being env-only. A row here overrides the matching environment
-- variable for the dashboard process; anything without a row keeps falling back
-- to .env.local, so an install that never touches this page behaves exactly as
-- before.
--
-- Deliberately NOT the place for Supabase's own credentials (SUPABASE_URL,
-- SUPABASE_SERVICE_ROLE_KEY, SUPABASE_ANON_KEY): they're what it takes to read
-- this table, so storing them in it would be circular. Those stay env-only and
-- the UI shows them as read-only.

create table platform_secrets (
  -- The environment variable name this overrides, e.g. 'DEEPGRAM_API_KEY'.
  name text primary key,
  value text not null,
  updated_at timestamptz not null default now(),
  -- Email of the admin who last changed it. Plain text rather than a FK to
  -- allowed_users so history survives that account being removed.
  updated_by text
);

-- No policies, so nothing but the service-role key can read or write this --
-- same posture as allowed_users (see 0003). Every access happens from a Server
-- Action or Server Component using the service-role client.
alter table platform_secrets enable row level security;

comment on table platform_secrets is
  'Runtime overrides for provider credentials, managed from the dashboard''s Integrations page. Values are stored as given -- treat this table as secret material.';
