-- How the agent opens a call, per FSD-style call-flow customization requests:
-- either it ad-libs a greeting from the prompt (today's only behavior), says
-- an exact fixed line, or stays silent until the caller speaks first.

alter table agents
  add column first_message_mode text not null default 'agent_generates'
    check (first_message_mode in ('agent_generates', 'agent_says_exact', 'user_starts'));

alter table agents add column first_message_text text;
