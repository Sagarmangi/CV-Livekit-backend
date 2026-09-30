-- Per-agent override for when the built-in `end_call` tool should fire.
-- Null means "use the worker's default guidance" (baked into
-- agent-worker/src/worker/flow.py's INSTRUCTIONS_TEMPLATE) -- this doesn't
-- change what end_call *does* (it still just hangs up), only the conditions
-- the model is told to watch for before calling it.

alter table agents add column end_call_instructions text;
