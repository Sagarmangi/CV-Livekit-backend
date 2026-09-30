-- Who ended the call, and why. Neither was recorded anywhere.
--
-- The existing columns each answer a different question and none of them
-- answers this one: `outcome` is what the call was *for*, `call_status` is
-- whether it *worked*, `priority` is how much to care. A caller who hung up
-- mid-sentence and an agent that said goodbye and called end_call produced
-- rows distinguishable only by `outcome` being NULL on the first -- which
-- call_status then reports as 'incomplete'. That is an inference drawn from an
-- absence, not a record of what happened, and it says nothing at all about the
-- calls where the line simply dropped.
--
-- Nothing new has to be detected to fill these in. Every signal already exists
-- at runtime and was being discarded: LiveKit reports
-- `participant.disconnect_reason` on the disconnect event (CLIENT_INITIATED vs
-- ROOM_DELETED vs SIP_TRUNK_FAILURE and friends), `ctx.shutdown()` already
-- carries a reason string the shutdown callback never accepted, and both
-- deliberate hang-up paths -- tools._hang_up and spam.py -- know exactly why
-- they fired.
--
-- TWO columns rather than one, because actor and cause are independent. "The
-- agent hung up" and "because a transfer completed" are different facts, and
-- collapsing them would force a combinatorial enum that grows every time a new
-- reason appears.
--
-- Text with a check constraint rather than an enum, following call_status
-- (0022) rather than call_priority: adding an actor later is then one migration
-- and no type surgery. `end_reason` is left unconstrained on purpose -- it is a
-- slug vocabulary that will grow, and a check constraint on it would turn every
-- new reason into a schema change.
--
-- Deliberately NOT added:
--     ended_at  -> created_at plus duration_seconds already give it
--     the detail behind a reason -> spam_detection and error_message hold it
--                                   already, and a second copy is a second
--                                   thing to drift (see 0022's list)

alter table call_logs
  -- The actor. 'system' covers anything neither party chose -- the spam filter
  -- dropping the call, the worker shutting down under it. 'telephony' is the
  -- line itself failing, which is not the caller hanging up and should never be
  -- counted as one.
  add column ended_by   text check (ended_by in ('agent', 'caller', 'system', 'telephony', 'unknown')),

  -- Why, as a short slug: end_call_tool, transferred, caller_hung_up,
  -- spam_filter, worker_shutdown, sip_trunk_failure, connection_timeout, ...
  add column end_reason text;

comment on column call_logs.ended_by is
  'Who ended the call: agent (called end_call, or transferred away) | caller (hung up) |
   system (spam filter, worker shutdown) | telephony (the line failed) | unknown.
   Claimed first-writer-wins during the call -- deleting the room also fires the
   caller-disconnected event, so without that rule every clean agent hang-up would be
   overwritten as a caller hang-up. NULL on rows written before this was recorded.';
comment on column call_logs.end_reason is
  'Why the call ended, as a short slug (end_call_tool, transferred, caller_hung_up,
   spam_filter, worker_shutdown, sip_trunk_failure, ...). Intentionally not constrained:
   the vocabulary grows, and the human-readable detail already lives in spam_detection
   and error_message.';

-- Filtering the calls list by who hung up is the first thing anyone will want
-- from this ("show me the calls people abandoned"), and it is low-cardinality
-- enough for a plain btree over the recent-first ordering -- same reasoning as
-- the priority/call_status indexes in 0022.
create index idx_call_logs_ended_by on call_logs (ended_by);
