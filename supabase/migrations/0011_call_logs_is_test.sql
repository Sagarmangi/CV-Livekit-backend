-- Marks rows written from a dashboard "Test agent" browser session (no phone
-- number or Twilio call involved) as distinct from real inbound calls, so the
-- Call logs page can tell them apart instead of either hiding them entirely
-- or mixing them in indistinguishably from real customer calls.

alter table call_logs
  add column is_test boolean not null default false;
