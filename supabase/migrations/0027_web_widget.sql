-- A public "web widget" call mode: a visitor on a customer's website talks to
-- an agent from the browser, with no phone number and no dashboard login.
--
-- The worker already has a browser path -- the dashboard's Test button dispatches
-- a job with {"test_agent_id": ...} as metadata and the tester joins as an
-- ordinary WebRTC participant. The widget is the same mechanics with a different
-- trust model: anyone on the public internet can reach it, so the agent is
-- addressed by an opaque key rather than its id, the feature is off until an
-- admin turns it on, the session has a hard length limit, and the call is
-- recorded as a real call rather than a test.
--
-- Two tables change.
--
-- agents gains the widget's own settings. widget_key is the only thing a
-- website ever learns about an agent: 'wk_' plus 24 URL-safe base64 characters
-- (144 bits), generated server-side, and generated for every existing row here
-- so enabling the widget later is one toggle rather than a key-minting step.
-- widget_allowed_origins is the second lock on that key -- a leaked key is still
-- useless from a site not on the list. widget_max_seconds caps what a stranger
-- can run up on the STT/LLM/TTS meter; 300s is enough for a real enquiry and
-- bounds the cost of a page left open. widget_config is presentation only
-- (theme colour, button label, position, a greeting override) and is read by
-- the embed script; the worker reads just `greeting` from it.
--
-- call_logs gains `channel`, the one fact the row couldn't state: how the call
-- arrived. is_test said "browser test or not", which was enough while browser
-- meant test. It stays, both because the dashboard filters on it and because a
-- widget call must NOT be a test -- it is a real visitor and the row is real
-- history -- so the two columns answer different questions and both get
-- written. Text with a check constraint rather than an enum, following
-- call_status (0022): a fourth channel is one migration and no type surgery.
--
-- channel_metadata holds what the channel knows and the phone columns can't
-- hold: for a widget call, the page's origin and the embed's visitor id. A
-- jsonb column rather than two text ones because the next channel will have
-- different facts again, and nullable because a phone call has nothing to put
-- here -- NULL says that where '{}' would claim an empty record was taken.

-- Needs pgcrypto's gen_random_bytes; 0001 created the extension.
create or replace function generate_widget_key()
returns text
language sql
volatile
as $$
  -- 18 random bytes encode to exactly 24 base64 characters with no padding;
  -- translate() swaps the two characters that aren't URL-safe.
  select 'wk_' || translate(encode(gen_random_bytes(18), 'base64'), '+/', '-_')
$$;

comment on function generate_widget_key() is
  'A fresh agents.widget_key: wk_ plus 24 URL-safe base64 characters (144 random bits).';

alter table agents
  add column widget_enabled         boolean not null default false,
  add column widget_key             text,
  add column widget_allowed_origins text[]  not null default '{}',
  add column widget_config          jsonb   not null default '{}'::jsonb,
  add column widget_max_seconds     integer not null default 300
    check (widget_max_seconds > 0);

-- Every existing agent gets a key now, then the column is locked down so no
-- row can ever be without one. Done as a separate update rather than a volatile
-- column default alone: a default is evaluated per row on ADD COLUMN, but
-- spelling the backfill out is what makes it obviously true when reading this.
update agents set widget_key = generate_widget_key() where widget_key is null;

alter table agents
  alter column widget_key set default generate_widget_key(),
  alter column widget_key set not null,
  add constraint agents_widget_key_unique unique (widget_key),
  add constraint agents_widget_key_format check (widget_key ~ '^wk_[A-Za-z0-9_-]{24}$');

comment on column agents.widget_enabled is
  'Whether the public web widget may start calls with this agent. Off by default,
   including for agents that predate the column: a key exists for every agent, but
   nothing answers on it until this is on AND the agent is active.';
comment on column agents.widget_key is
  'Opaque public identifier the embed script sends in the job metadata
   ({"widget_key": ...}). The only thing a website learns about the agent. Rotate by
   setting it to generate_widget_key().';
comment on column agents.widget_allowed_origins is
  'Browser origins (scheme://host[:port], no path) allowed to use the key. Empty means
   the token endpoint and worker do not check the origin -- fine while testing, not
   for a key embedded on a public site.';
comment on column agents.widget_config is
  'Presentation settings for the embed: theme colour, button label, position, and an
   optional "greeting" the agent opens widget calls with instead of its first message.';
comment on column agents.widget_max_seconds is
  'Hard cap on a widget session. The worker speaks a closing line and ends the call
   when it is reached; bounds the provider cost a stranger can run up.';

alter table call_logs
  add column channel text not null default 'phone'
    check (channel in ('phone', 'test', 'widget')),
  add column channel_metadata jsonb;

-- Every row so far was either a phone call or a dashboard test, and is_test
-- already says which.
update call_logs set channel = 'test' where is_test;

comment on column call_logs.channel is
  'How the call arrived: phone (Twilio SIP), test (dashboard Test button), widget
   (public web widget). is_test is kept and still written; a widget call is NOT a
   test -- it is a real visitor and real history.';
comment on column call_logs.channel_metadata is
  'What the channel knows beyond the phone columns. Widget: {"origin": the page''s
   origin, "visitor_id": the embed''s id for the visitor}. NULL for phone and test.';

-- "Show me the widget calls" is the first filter anyone will want from this,
-- and it is as low-cardinality as ended_by (0025) and call_status (0022).
create index idx_call_logs_channel on call_logs (channel);
