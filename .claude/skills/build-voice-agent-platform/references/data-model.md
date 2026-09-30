# Data model

Postgres. The only shared state between the worker and the dashboard, and the
reason neither needs to know the other exists.

RLS is deliberately **off**: every access is server-side with a service-role
key, and the dashboard has no public surface. If you ever expose a client-side
query path, this decision has to be revisited before you do.

## Tables

### `agents` — one row per configured agent

The whole personality and behaviour of an agent, editable without a deploy.

```
agent_id                    uuid pk
name                        text
twilio_number               text unique      -- the routing key; indexed
status                      agent_status     -- active | paused | draft
prompt                      text             -- system prompt
qualification_criteria      jsonb            -- [{key, question, required}]
first_message_mode          text             -- agent_generates | agent_says_exact | user_starts
first_message_text          text
llm_provider                text             -- which engine runs the call
voice_id                    text             -- pipeline TTS voice
gemini_voice                text             -- realtime voice (separate namespace)
pronunciation_dictionary    jsonb            -- [{term, say_as}]
conversation_settings       jsonb            -- temperature, max sentences, VAD ms, ...
end_call_instructions       text
knowledge_base_content      text
knowledge_base_description  text
end_call_webhook_url        text
slack_notifications_enabled boolean default false
created_at / updated_at     timestamptz
```

Notes worth keeping:

- **`twilio_number` is the routing key.** A call resolves its agent by the
  number that was dialled. Unique and indexed; everything else is detail.
- **Two voice columns, not one.** A pipeline TTS voice and a realtime model's
  voice are different namespaces, and an agent can be switched between engines.
  One column would silently carry a meaningless value across the switch.
- **`conversation_settings` as jsonb** with defaults resolved in code, mirrored
  in both the worker and the dashboard. Drift makes the dashboard's placeholder
  text a lie about what actually happens on a call.
- **Knowledge base as one free-text field plus a description**, exposed to the
  model as a single on-demand tool. The description is what the model reads to
  decide whether to look it up, so its content costs tokens only when used.

### `tools` + `agent_tools` — a shared library, not per-agent rows

```
tools
  tool_id              uuid pk
  name                 text          -- the function name the model sees
  description          text          -- when to use it; read every turn
  tool_type            text          -- see below
  parameter_schema     jsonb
  webhook_url          text          -- 'function' type only
  destination_number   text          -- 'transfer_call' only
  detector_statements  jsonb         -- detector types only
  detector_llm         text
  is_builtin           boolean
  is_enabled           boolean

agent_tools
  (agent_id, tool_id) pk
```

`tool_type` decides which Python builder turns the row into a callable:

| type | behaviour |
|---|---|
| `function` | POSTs its arguments to `webhook_url`. The only one that runs your code. |
| `transfer_call` | SIP-transfers to `destination_number` |
| `record_lead_info` | writes name/company/need onto call state |
| `record_callback_number` | captures a number after a failed transfer |
| `end_call` | hangs up, under an admin's own name |
| `detect_bot_call` / `detect_sales_call` | classifier, not a model-callable tool |

Making tools a **library with a join table** rather than rows owned by an agent
means one tool definition is reused across agents and fixed in one place. It
also makes "which agents can transfer" a query rather than an audit.

**The description is the whole interface.** The model reads it every turn as
part of the function schema, so conditions written there carry far more weight
than the same words in a system prompt.

### `call_logs` — one row per call, written once

```
call_log_id        uuid pk
call_sid / room_id / agent_id / caller_number / called_number
transcript / recording_url / duration_seconds
is_test            boolean       -- dashboard test sessions, excluded from alerts

-- what the call was FOR
outcome            call_outcome  -- qualified | department_transfer | not_qualified
                                 -- | transfer_failed | dropped | spam_bot | spam_sales
matched_department text
lead_name / lead_company / lead_need
spam_detection     text          -- which detector fired and why

-- whether it WORKED (derived in code, no model)
call_status        text          -- success | failed | incomplete
transfer_attempted / callback_needed / has_error / error_message
ended_by           text          -- agent | caller | system | telephony | unknown
end_reason         text          -- short slug

-- what it COST
cost_stt_usd / cost_llm_usd / cost_tts_usd / cost_telephony_usd
cost_total_usd / cost_breakdown jsonb

-- what a model JUDGED (nullable = never ran)
call_summary / user_queries jsonb / priority call_priority
caller_name / analysis_model
```

The grouping is the point. `outcome`, `call_status`, `ended_by` and `priority`
answer four different questions and must not be collapsed: a robocall hung up
on deliberately is `spam_bot` / `success` / `system` / `Low` — all true at once.

Other rules that earned their place:

- **Observed vs inferred are separate columns.** `lead_name` is captured by a
  tool the model deliberately called; `caller_name` is the analysis model's
  guess from a transcript. Collapsing them means a guess can silently overwrite
  a fact, and nothing downstream can tell which it got. Store `analysis_model`
  so the UI can attribute the inferred fields.
- **NULL means "never ran"**, and it is a different fact from empty. An empty
  `user_queries` array means the analysis ran and found nothing.
- **Cost is frozen at the rates in effect when the call ended.** Three of the
  four components are metered; telephony is usually an estimate, so label it as
  one everywhere it appears.

### Supporting tables

```
allowed_users      email pk           -- dashboard login allowlist
external_numbers   customers' own Twilio accounts, each with its own trunk
platform_secrets   provider API keys, editable from the UI, overriding env
keepalive          a row a cron touches, so a free-tier DB never idles out
schema_migrations  filename, sha256, applied_at, adopted
```

`allowed_users` is an allowlist, **not** an account: the auth provider still
has to have the user. Seeding it with your own address in a migration is
convenient and becomes a bug the first time someone else deploys — take the
admin email as an input at setup time instead.

`platform_secrets` exists so rotating a provider key is a UI action rather than
an SSH session and a restart.

## Migration discipline

- Numbered, forward-only, one concern per file.
- **A prose header explaining why**, including what was deliberately *not*
  added. These comments end up being the real ADR log, and they are worth more
  than the DDL underneath them.
- Zero-pad the numbers so filename sort equals apply order.
- Assume they will run on a database that already has data. Additive columns
  are nullable or defaulted; a rename is a new column plus a backfill.
- Never edit a migration after it has run anywhere. Write another one.
