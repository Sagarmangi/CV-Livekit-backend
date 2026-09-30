-- Lets an agent owner get the full call record (transcript, outcome, lead
-- info, recording once that exists) pushed to their own webhook the moment a
-- call ends, instead of only ever seeing it in the dashboard or Slack.

alter table agents
  add column end_call_webhook_url text;
