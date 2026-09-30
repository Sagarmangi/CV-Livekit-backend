-- Tools become a shared library instead of one row per agent -- the same
-- webhook (e.g. a booking tool) is often reused across several agents, so
-- this replaces `tools.agent_id` with an `agent_tools` join table recording
-- which agent has which tool selected.

create table agent_tools (
  agent_id uuid not null references agents (agent_id) on delete cascade,
  tool_id uuid not null references tools (tool_id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (agent_id, tool_id)
);

create index idx_agent_tools_tool_id on agent_tools (tool_id);

-- Preserve every existing tool's current agent as its initial selection
-- before agent_id goes away.
insert into agent_tools (agent_id, tool_id)
select agent_id, tool_id from tools where agent_id is not null;

alter table tools drop column agent_id;
