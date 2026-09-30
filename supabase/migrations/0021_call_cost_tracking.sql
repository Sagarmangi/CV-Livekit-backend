-- Per-call cost, split by the thing that charges for it.
--
-- "What did that call cost?" was unanswerable from this schema: duration was
-- the only usage number stored, and duration alone can't price a call. Two
-- three-minute calls differ by a factor of three depending on how much the
-- agent actually said, because text-to-speech bills per character and is the
-- largest line item -- roughly 65% of the total.
--
-- Four components, stored separately rather than as one number, because they
-- answer different questions. Telephony is the one that isn't ours: Twilio
-- bills it directly, the worker never sees the real figure, and it's estimated
-- from a configured per-minute rate (see agent-worker/src/worker/pricing.py).
-- Keeping it in its own column is what lets a report exclude it and show the
-- part we actually control.
--
-- cost_breakdown carries the usage counters and the rate applied to each, so a
-- row can explain its own total years later. That matters because the totals
-- are frozen at write time deliberately: provider prices change, and a call
-- that cost $0.07 in August has to still say $0.07 after Deepgram reprices,
-- or the historical numbers quietly rewrite themselves. The counters are the
-- immutable facts; re-pricing them is always possible, but it's an explicit
-- act rather than a side effect of a vendor's pricing page changing.
--
-- Every column is nullable. Rows written before this migration have no usage
-- data and can never be priced -- NULL says that honestly, where 0 would claim
-- the call was free.

alter table call_logs
  add column cost_stt_usd       numeric(12, 6),
  add column cost_llm_usd       numeric(12, 6),
  add column cost_tts_usd       numeric(12, 6),
  add column cost_telephony_usd numeric(12, 6),
  add column cost_total_usd     numeric(12, 6),
  add column cost_breakdown     jsonb;

comment on column call_logs.cost_stt_usd is
  'Speech-to-text cost. Deepgram, billed per minute of audio processed.';
comment on column call_logs.cost_llm_usd is
  'Conversation engine cost, billed on the provider''s own reported token counts.';
comment on column call_logs.cost_tts_usd is
  'Text-to-speech cost. Deepgram Aura, billed per character. Usually the largest line.';
comment on column call_logs.cost_telephony_usd is
  'Estimated carrier cost: configured per-minute rate x duration. NOT a Twilio-reported
   figure -- the worker has no Twilio credentials. Zero for browser test calls, which
   never touch the carrier.';
comment on column call_logs.cost_total_usd is
  'Sum of the four components, frozen at the rates in effect when the call ended.';
comment on column call_logs.cost_breakdown is
  'Per-component audit trail: provider, model, billed quantity, unit, and the rate
   applied. A rate of null means no rate was configured for that model, so its cost
   is missing from the total rather than wrong -- see pricing.py.';

-- Sorting and summing by spend is the point of storing this, and the calls list
-- is already paginated over created_at.
create index idx_call_logs_cost_total on call_logs (cost_total_usd desc nulls last);
