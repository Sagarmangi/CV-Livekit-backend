-- Post-call analysis: what happened on the call, beyond what it cost.
--
-- Replaces an n8n flow that ran an OpenAI prompt over a Vapi webhook payload and
-- asked it to extract fourteen fields. That shape was forced by where n8n sat --
-- outside the call, holding only an opaque payload, so even the caller's phone
-- number had to be mined out of it by a language model.
--
-- The worker *is* the call, so most of those fields are observations rather than
-- inferences. Only three need a model (see agent-worker/src/worker/analysis.py):
-- call_summary, user_queries and priority. The rest are derived in code, which is
-- both free and more accurate -- a SIP attribute is exact where a model reading a
-- transcript can drop a digit.
--
-- Deliberately NOT added, because an existing column already holds the same fact
-- and a second copy is a second thing to drift:
--     conversation_id -> room_id / call_sid
--     caller_phone    -> caller_number
--     caller_name     -> lead_name
--     duration/transcript -> already stored
--
-- call_status and priority sit alongside `outcome` rather than replacing it.
-- They answer different questions: outcome is what the call was *for*
-- (qualified, transferred, spam), call_status is whether it *worked*, priority is
-- how much someone should care. A spam call hung up on deliberately is
-- outcome=spam_bot, call_status=success, priority=Low -- all three true at once.

create type call_priority as enum ('High', 'Medium', 'Low');

alter table call_logs
  -- Which of the agent's numbers was dialled (sip.trunkPhoneNumber). Read at
  -- answer time to resolve the agent and, until now, thrown away. Null for
  -- browser test calls, which dial nothing.
  add column called_number      text,

  -- Derived in code from what the session did -- no model involved.
  add column call_status        text check (call_status in ('success', 'failed', 'incomplete')),
  add column transfer_attempted boolean,
  add column callback_needed    boolean,
  add column has_error          boolean,
  add column error_message      text,

  -- Judged by the analysis LLM. Null means the analysis didn't run or couldn't
  -- be parsed -- a short transcript, a timeout, a provider outage -- which is a
  -- different fact from "it ran and found nothing", and worth being able to tell
  -- apart when deciding whether the analysis is pulling its weight.
  add column call_summary       text,
  add column user_queries       jsonb,
  add column priority           call_priority;

comment on column call_logs.call_status is
  'Whether the call worked: success | failed | incomplete. Derived, not inferred --
   failed means the session errored or a transfer didn''t connect; incomplete means
   no terminal state was reached (the caller hung up mid-conversation).';
comment on column call_logs.priority is
  'How much attention the call deserves, judged by the analysis LLM from the
   transcript alone. Independent of outcome.';
comment on column call_logs.user_queries is
  'The caller''s substantive statements, as a JSON array of strings. An empty array
   means the analysis ran and found none; null means it never ran.';
comment on column call_logs.has_error is
  'Whether the session emitted an ErrorEvent mid-call. Previously visible only in
   the worker log, which made "did that call actually work" unanswerable from the
   call record.';

-- Both are filter candidates on the calls list, and both are low-cardinality
-- enough that a plain btree over the recent-first ordering is enough.
create index idx_call_logs_priority on call_logs (priority);
create index idx_call_logs_call_status on call_logs (call_status);
