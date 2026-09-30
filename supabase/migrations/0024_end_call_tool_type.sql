-- `end_call` as a configurable tool type.
--
-- Hanging up was the one behaviour with no admin control over its description.
-- *When* to end a call is genuinely agent-specific -- "end once they've booked a
-- slot", "end after two refusals" -- and the only lever was
-- agents.end_call_instructions, which is prose in the system prompt. A tool
-- description is different in kind: the model reads it on every turn as part of
-- the function schema, so a condition written there carries more weight than the
-- same sentence buried in a prompt it has been drifting away from for twenty
-- turns.
--
-- Unlike every other type in this constraint, end_call keeps a builtin fallback.
-- The others were made opt-in because an agent that doesn't do lead capture
-- shouldn't be nudged to ask for a name. That reasoning does not transfer: an
-- agent which cannot hang up does not end its calls politely, it holds the line
-- until LiveKit's room timeout expires and telephony bills every minute of it.
-- So an agent with no end_call row still gets the default tool
-- (agent-worker tools.builtin_tools), and attaching a row replaces it rather
-- than enabling it.
--
-- The admin's description is appended to the built-in one rather than replacing
-- it, because the default explains the mechanics -- speak the closing line
-- first, pass an outcome -- which someone writing "end after booking" has no
-- reason to restate and every reason not to accidentally drop. See
-- tools.build_end_call_tool.

alter table tools
  drop constraint tools_tool_type_check;

alter table tools
  add constraint tools_tool_type_check
  check (tool_type in (
    'function',
    'transfer_call',
    'record_lead_info',
    'record_callback_number',
    'detect_bot_call',
    'detect_sales_call',
    'end_call'
  ));
