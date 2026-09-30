-- Spam-call detection: two new tool types that drop robocalls and cold sales
-- pitches on the caller's first reply.
--
-- These are tool *rows* for configuration and per-agent opt-in only -- unlike
-- every other type, they are not exposed to the model as callable functions.
-- The worker runs them as a deterministic check on the first transcript (see
-- agent-worker/src/worker/spam.py), because a tool the LLM chooses to call is
-- exactly the non-determinism this feature exists to avoid: a caller who keeps
-- talking can argue a model out of hanging up.
--
-- The same reasoning is why this isn't prompt text. flow.py used to carry
-- hardcoded scam/sales-pitch heuristics and they were deleted for silently
-- overriding whatever an admin had written -- see its module docstring.

-- Statement list + which LLM judges the semantic match. Dedicated columns per
-- tool type, following destination_number for transfer_call rather than
-- overloading parameter_schema (which describes arguments *the model* sends,
-- and these tools take none).
--
-- detector_llm null means "use the agent's own llm_provider".
alter table tools
  add column detector_statements jsonb not null default '[]'::jsonb,
  add column detector_llm text;

alter table tools
  add constraint tools_detector_llm_check
  check (detector_llm is null or detector_llm in ('gemini', 'deepseek'));

-- The on/off toggle, deliberately general to every tool type rather than
-- detector-only: until now the only way to switch a tool off was to detach it
-- from each agent one at a time, and a kill switch for a misbehaving webhook is
-- worth just as much as one for a detector.
--
-- end_call is unaffected -- it isn't a tools row, because something must always
-- be able to hang up regardless of configuration (see 0016).
alter table tools
  add column is_enabled boolean not null default true;

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
    'detect_sales_call'
  ));

-- Separate outcomes rather than reusing 'dropped': the point of detecting these
-- is to be able to count them and to review what was dropped, and 'dropped'
-- already means "the agent ended a call it judged bad" from any cause.
--
-- ALTER TYPE ... ADD VALUE cannot be used in the same transaction that adds it,
-- so nothing below may reference these values. Run this file on its own.
alter type call_outcome add value if not exists 'spam_bot';
alter type call_outcome add value if not exists 'spam_sales';

-- Which detector fired and what matched.
--
-- Not optional bookkeeping: detection hangs up silently, so a false positive is
-- a real customer cut off mid-sentence with no trace of why. This column is the
-- only way to find out that a statement list is too broad.
alter table call_logs
  add column spam_detection text;

-- Seeded so the feature is usable without an admin authoring it from nothing,
-- and marked is_builtin like the transfer rows migrated in 0016. Still opt-in:
-- neither row is attached to any agent here, so no existing call changes
-- behaviour until someone attaches one.
--
-- The starter statements are deliberately narrow. Detection ends calls with no
-- warning, so the failure mode of a too-broad list is worse than the failure
-- mode of a too-narrow one -- widen them from real call_logs.spam_detection
-- evidence, not from imagination.
insert into tools (name, description, tool_type, detector_statements, is_builtin)
values (
  'detect_bot_call',
  'Detects automated systems -- answering machines, IVR menus, voicemail greetings and '
    || 'robocall recordings -- on the caller''s first reply, and ends the call. Edit this '
    || 'description to describe any other automated pattern worth catching; it is passed '
    || 'to the classifier alongside the statement list.',
  'detect_bot_call',
  jsonb_build_array(
    'press one to speak to a representative',
    'your call is important to us',
    'please leave a message after the tone',
    'this call is being recorded for quality assurance',
    'the person you are calling is not available',
    'if you would like to be removed from our list'
  ),
  true
),
(
  'detect_sales_call',
  'Detects someone cold-selling to us -- agencies, SEO and listing pitches, lead-gen and '
    || 'software vendors -- on the caller''s first reply, and ends the call. Edit this '
    || 'description to name the pitches you keep receiving; it is passed to the classifier '
    || 'alongside the statement list.',
  'detect_sales_call',
  jsonb_build_array(
    'i am calling about your google business listing',
    'we can help you rank higher on google',
    'i wanted to talk to you about your website',
    'we help companies like yours generate more leads',
    'is the owner or decision maker available',
    'i am calling from a digital marketing agency'
  ),
  true
);
