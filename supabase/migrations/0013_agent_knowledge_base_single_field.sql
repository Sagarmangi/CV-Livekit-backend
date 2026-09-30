-- Replaces the multi-row `knowledge_base` table (one form per FAQ entry) with
-- a single free-text field per agent -- there's only ever one knowledge base
-- per agent in practice, so this matches that instead of forcing entries.
-- Exposed to the model as one on-demand tool (see agent-worker/src/worker/
-- tools.py build_knowledge_tool) described by knowledge_base_description, so
-- its content is only paid for in tokens on the calls that actually need it.
--
-- The old `knowledge_base` table is left in place, unused, rather than
-- dropped -- it only ever held one throwaway test row in production.

alter table agents
  add column knowledge_base_content text not null default '',
  add column knowledge_base_description text not null default '';
