-- The caller's name, recovered from the transcript by the analysis model.
--
-- Wanted because the reliable path doesn't always exist. `lead_name` is filled
-- by the record_lead_info tool, which the model calls deliberately when the
-- caller states their name -- but that tool is opt-in per agent, and an agent
-- without it attached records no name at all even when the caller clearly gave
-- one. A real call went out with lead_name NULL while the analysis summary
-- opened "The caller, Diana, expressed interest in..." -- the name was right
-- there in the same row, just not in a field anything could read.
--
-- A SEPARATE column rather than a fallback that writes into lead_name, and the
-- distinction is the point:
--
--   lead_name   - the tool ran. The caller said it, the model chose to record
--                 it, and it went in as a deliberate act. Ground truth.
--   caller_name - inferred afterwards from a transcript that may itself have
--                 misheard the name. Usually right, occasionally "Kitexa" for
--                 "Kodexo".
--
-- Collapsing them would mean a downstream consumer could no longer tell whether
-- a name was recorded or guessed, and the guess would silently win on any agent
-- where both exist. Anything that wants one value can coalesce them itself and
-- choose which it trusts first.
--
-- analysis_model records which model produced this and the other judgements, so
-- the dashboard can attribute the field rather than assume. Not implied by
-- ANALYSIS_LLM: that setting can change, and old rows would then be relabelled
-- with a model that never saw them.

alter table call_logs
  add column caller_name    text,
  add column analysis_model text;

comment on column call_logs.caller_name is
  'Caller''s name as inferred from the transcript by the analysis model. Distinct from
   lead_name, which the record_lead_info tool records during the call and which is
   authoritative. Null when the caller never stated a name.';
comment on column call_logs.analysis_model is
  'The model that produced caller_name, call_summary, user_queries and priority
   (e.g. deepseek-v4-flash). Stored per row so the UI can attribute those fields even
   after ANALYSIS_LLM is repointed.';
