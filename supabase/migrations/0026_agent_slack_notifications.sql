-- Per-agent switch for Slack notifications, and a narrowing of what Slack is
-- for.
--
-- Two changes in one migration because they are the same decision. Until now
-- every non-test call posted a summary to Slack (entrypoint.py -> notify.
-- send_call_summary), regardless of whether anything came of it: robocalls,
-- wrong numbers, calls that dropped after four seconds. A channel that posts
-- everything is a channel nobody reads, which costs the alerts that do matter.
--
-- Slack now carries two things only: a captured lead, and the existing urgent
-- transfer-failure alert (which is not a notification so much as an unpaid
-- obligation -- someone owes that caller a callback). Everything else stays in
-- call_logs and the dashboard, where the full record already lives and nothing
-- is lost by not also being a message.
--
-- DEFAULT false, INCLUDING for the agents that already exist -- so this is a
-- deliberate behaviour change on deploy: agents currently posting to Slack go
-- quiet until someone switches them back on from the dashboard. Chosen over
-- `default true` because the alternative is worse in both directions: any new
-- agent would start broadcasting to a shared channel before anyone decided it
-- should, and an admin who never visits the toggle can't tell whether silence
-- means "off" or "no leads yet". Opt-in makes that unambiguous.
--
-- Deployment-level SLACK_WEBHOOK_URL still gates everything above this: with no
-- webhook configured the toggle does nothing, which is why this is a per-agent
-- switch rather than a replacement for that setting.

alter table agents
  add column slack_notifications_enabled boolean not null default false;

comment on column agents.slack_notifications_enabled is
  'Whether this agent posts to Slack at all. Off by default, including for agents
   created before this column existed. When on, the agent posts a lead alert for
   calls where lead details were captured (see agent-worker analysis.is_lead) and the
   urgent alert when a transfer fails -- never a summary of every call. Requires
   SLACK_WEBHOOK_URL to be set on the worker as well; this switch cannot enable
   notifications a deployment has no webhook for.';
