-- A single-purpose table for the external cron ping that keeps this project
-- from auto-pausing on Supabase's free tier. Nothing in the app reads this --
-- it exists purely to be written to on a schedule.
--
-- Deliberately not exposed to anon/authenticated, same reasoning as
-- allowed_users: no reason for this to be readable by a logged-in session.

create table keepalive (
  id bigint generated always as identity primary key,
  pinged_at timestamptz not null default now()
);

revoke select on keepalive from anon, authenticated;
