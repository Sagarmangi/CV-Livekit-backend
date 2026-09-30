-- Numbers connected from a customer's own Twilio account (Account SID + Auth
-- Token), as opposed to numbers bought on the platform's own Twilio account
-- (TWILIO_ACCOUNT_SID in dashboard/.env.local), which live entirely in Twilio
-- and are listed live via the API with no local row needed.
--
-- Connecting a number creates a brand-new Elastic SIP trunk *inside the
-- customer's account* pointed at the same LiveKit SIP origination URI as the
-- platform's shared trunk, then moves their number onto it -- Twilio requires
-- a trunk and the number it carries to live in the same account, so an
-- outside number can never be attached to the platform's own trunk. This
-- table is the only record of that connection; Twilio has no cross-account
-- listing to rediscover it from.
--
-- Deliberately not exposed to anon/authenticated: the default privileges set
-- up in 0002_grants.sql would otherwise make every stored Auth Token readable
-- by anyone with a valid session, same reasoning as allowed_users.

create table external_numbers (
  external_number_id uuid primary key default gen_random_uuid(),
  phone_number text not null unique,
  friendly_name text not null default '',
  account_sid text not null,
  auth_token text not null,
  number_sid text not null,
  trunk_sid text not null,
  created_at timestamptz not null default now()
);

revoke select on external_numbers from anon, authenticated;
