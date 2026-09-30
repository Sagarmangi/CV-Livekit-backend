-- Tools gain a type so everything except end_call (the one thing that must
-- always be able to hang up a call, per the product decision to keep it
-- automatic) becomes an admin-authored, optional, per-agent-attachable tool
-- -- Vapi-style Transfer Call / Record Lead Info / Record Callback Number
-- types alongside the existing webhook-based Function type, instead of
-- unconditional behavior baked into every agent regardless of whether it's
-- wanted. An agent that doesn't want lead capture no longer has a
-- record_lead_info tool pushing it toward asking for one.

alter table tools
  add column tool_type text not null default 'function',
  add column destination_number text;

alter table tools
  add constraint tools_tool_type_check
  check (tool_type in ('function', 'transfer_call', 'record_lead_info', 'record_callback_number'));

-- Departments retired in favor of one Transfer Call tool per destination --
-- each gets its own admin-editable description instead of a single generic
-- transfer_to_department tool matching against a routing-keywords directory.
-- Migrate existing rows so configured transfers keep working unattended.
insert into tools (name, description, tool_type, destination_number, is_builtin)
select
  'transfer_to_' || regexp_replace(lower(department_name), '[^a-z0-9]+', '_', 'g'),
  case
    when routing_keywords is not null and routing_keywords <> ''
      then 'Transfer the caller to ' || department_name || ' when they mention: ' || routing_keywords || '.'
    else 'Transfer the caller to ' || department_name || '.'
  end,
  'transfer_call',
  transfer_number,
  false
from departments;

insert into agent_tools (agent_id, tool_id)
select d.agent_id, t.tool_id
from departments d
join tools t
  on t.tool_type = 'transfer_call'
  and t.destination_number = d.transfer_number
  and t.name = 'transfer_to_' || regexp_replace(lower(d.department_name), '[^a-z0-9]+', '_', 'g');
