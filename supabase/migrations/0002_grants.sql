-- Grants for the tables created in 0001_init_schema.sql.
--
-- Table creation alone doesn't grant anything to Supabase's built-in roles —
-- CREATE TABLE only gives privileges to its owner. On Supabase Cloud, the
-- platform's own provisioning sets up default privileges for anon/
-- authenticated/service_role, so this is easy to miss locally: a fresh
-- `supabase start` applies these migrations with none of that in place, and
-- every INSERT/UPDATE/DELETE fails with "permission denied for table" even
-- though the schema looks fine.
--
-- RLS stays off (`rowsecurity = false`, the default) because every access path
-- is the dashboard's server-side service-role client — see
-- dashboard/lib/supabase.ts. `anon`/`authenticated` are granted read-only
-- here as a conservative default so a future browser-side Supabase client
-- (there isn't one yet) can't write without RLS explicitly turned back on.

grant usage on schema public to anon, authenticated, service_role;

grant select on
  agents, departments, knowledge_base, tools, call_logs
to anon, authenticated;

grant select, insert, update, delete on
  agents, departments, knowledge_base, tools, call_logs
to service_role;

-- Every table's primary key defaults via gen_random_uuid(); none use a
-- sequence, so there are no sequences to grant usage on here.

-- Applies the same grants to whatever's created next, so a future migration
-- that adds a table doesn't silently reintroduce this bug.
alter default privileges in schema public
  grant select on tables to anon, authenticated;

alter default privileges in schema public
  grant select, insert, update, delete on tables to service_role;
