-- Login allowlist for the dashboard.
--
-- The dashboard uses Supabase Auth email+password sign-in, with accounts
-- created via the Admin API rather than a public sign-up form (see
-- app/login/actions.ts) -- so on its own, Supabase Auth already limits who
-- can even attempt to log in. This table is a second, explicit layer on top:
-- the app checks it right after a successful password sign-in and signs the
-- user back out immediately if their email isn't listed here, so revoking
-- access doesn't require deleting the Auth account itself.
--
-- Deliberately not exposed to anon/authenticated: the default privileges set
-- up in 0002_grants.sql would otherwise make this list of admin emails
-- readable by anyone with a valid session, which is more exposure than an
-- internal tool needs.

create table allowed_users (
  email text primary key,
  created_at timestamptz not null default now()
);

revoke select on allowed_users from anon, authenticated;

-- Seed the account that's setting this up so the very first login works.
insert into allowed_users (email) values ('ai-automation@kodexolabs.com');
