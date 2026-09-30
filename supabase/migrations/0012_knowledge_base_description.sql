-- Lets each knowledge base entry say when the model should look it up, same
-- as a custom tool's "when to use it" field. The worker now exposes each
-- entry as its own on-demand tool (see agent-worker/src/worker/tools.py
-- build_knowledge_tools) instead of dumping every entry's full content into
-- the system prompt on every turn -- this description is what the model sees
-- to decide whether an entry is relevant, without paying for its content
-- unless it actually calls the tool.

alter table knowledge_base
  add column description text not null default '';
