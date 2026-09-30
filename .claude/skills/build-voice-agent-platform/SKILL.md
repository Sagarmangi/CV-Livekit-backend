---
name: build-voice-agent-platform
description: Build or rebuild a complete self-hosted inbound voice-AI agent platform end to end — Twilio SIP trunking, self-hosted LiveKit + SIP bridge, a Python agent worker (STT/LLM/TTS or speech-to-speech), a Postgres/Supabase schema, and a Next.js admin dashboard for managing agents, phone numbers, tools and call logs. Use when recreating this system somewhere new, standing up a second deployment or environment, onboarding to how the whole thing fits together, adding a major subsystem (call recording, cost tracking, post-call analysis, spam detection), or answering "how does a call actually get from the phone to the agent and back".
---

# Building the voice agent platform

A caller dials a phone number; an AI agent answers, holds a conversation,
captures a lead or transfers them, hangs up, and the whole call is logged,
priced and summarised. Everything is self-hosted apart from the model APIs.

Read `references/architecture-decisions.md` before choosing anything — most of
the non-obvious choices here were made against measurements, and the reasoning
matters more than the specific vendor. `references/failure-modes.md` is the
list of things that break silently; it is worth reading *before* you build, not
after. `references/data-model.md` is the schema.

## The four pieces

| Piece | Does | Runs on |
|---|---|---|
| **Database** (Postgres/Supabase) | Agent config, tools, call logs. The only shared state. | Hosted |
| **Media infra** (LiveKit + SIP bridge + Redis) | Terminates SIP, bridges to WebRTC, hosts the room | Docker, one VPS |
| **Agent worker** (Python) | Answers the call, runs the conversation, writes the record | systemd, same VPS |
| **Dashboard** (Next.js) | Admin UI: agents, numbers, tools, call logs | Node + nginx, same VPS |

The worker and the dashboard share *no* code — they share the database. Both
hold a copy of the row shapes (Python dataclasses and TypeScript types); keep
them in step deliberately, because nothing enforces it.

## How a call actually flows

```
caller dials
  → Twilio number, attached to an Elastic SIP Trunk
  → trunk Origination URI: sip:<vps-ip>:5060
  → LiveKit SIP bridge accepts the INVITE, creates a room
  → dispatch rule names an agent → LiveKit dispatches to the worker
      registered under that name
  → worker reads sip.trunkPhoneNumber from the SIP participant
  → looks that number up in the DB to decide WHICH agent config to run
  → builds the session (STT+LLM+TTS, or a realtime model) and greets
  → conversation turns; tools fire for lead capture, transfer, hangup
  → call ends (agent hangs up / caller hangs up / transfer / spam drop)
  → shutdown: finalise recording, run analysis, price the call,
      write one call_logs row, POST the webhook, notify Slack
```

**Routing lives in the database, not in Twilio.** Twilio points every number at
the same trunk; which agent answers is decided by looking up the dialled number.
That is what makes the dashboard able to reassign a number without touching
Twilio.

## Build order

Each phase is usable on its own and testable before the next. Do not reorder —
later phases assume earlier ones.

### 1. Database first

Everything else reads config from here. Create the schema as **numbered,
forward-only migration files** plus a runner that tracks what it has applied —
not a single `schema.sql`, which becomes a second definition that drifts.

The runner needs: a `schema_migrations` table, one transaction per file, an
advisory lock so concurrent workers can't both migrate, and a content checksum
that normalises line endings. See `references/data-model.md`.

**Verify:** the runner reports every migration applied, and re-running is a
no-op.

### 2. Media infra

Redis, the LiveKit server and the SIP bridge, in Docker with `network_mode:
host` — the UDP port ranges are too wide for per-port mapping.

You invent the LiveKit API key/secret yourself (`openssl rand`); they are not
issued by anyone. The **same pair** must appear in the infra env, the worker
env and the dashboard env. Mismatched copies are the classic "worker registers
but never gets dispatched".

Open: 7880/tcp, 7881/tcp, 50000–60000/udp, 5060/udp, 10000–20000/udp. A call
that connects with no audio is almost always a missing UDP range.

**Verify:** all containers up; the LiveKit API answers on 7880.

### 3. Agent worker — the core

A long-running process that registers with LiveKit and receives a job per call.
Structure it as small modules with one job each:

```
entrypoint.py   job lifecycle: resolve agent, build session, teardown
flow.py         the Agent subclass: instructions, greeting behaviour
tools.py        DB tool rows -> callable function tools
state.py        per-call state threaded through the session
models.py       row shapes (mirror of the dashboard's types)
settings.py     env -> typed settings, required vs optional
supabase_client.py  config load + the single call_logs write
```

Critical structural rules:

- **Register the shutdown callback early**, before anything that can raise.
  Anything failing before it produces a call that happened with no record.
- **One row per call, written once**, in the shutdown path — not incrementally.
- **Per-call state object**, not globals: one process can handle many jobs.
- **A DB client per event loop.** A process-wide client hands job N+1 a
  connection pool bound to job N's dead loop, which fails in a way that looks
  like random flakiness.

**Verify:** a browser or SIP test call is answered and a `call_logs` row appears.

### 4. Telephony

Buy a number, create one shared Elastic SIP Trunk, set its Origination URI to
the VPS, attach numbers to the trunk. Create the LiveKit inbound trunk and
dispatch rule — by hand once to prove the path, then from the dashboard.

The dispatch rule's agent name **must** match the name the worker registers
under, or LiveKit dispatches calls to nobody.

**Verify:** a real phone call reaches the agent.

### 5. Dashboard

Next.js, server-side only — the service-role key never reaches the browser.
Pages: Agents (prompt, voice, tools, knowledge base), Phone numbers
(buy/attach/assign), Tools (the shared library), Call logs, Integrations.

Auth: Supabase Auth plus an allowlist table. There is no public sign-up.

**Verify:** you can create an agent, attach a number, and see a call appear.

### 6. Make it fast

Only now. A slow agent that works beats a fast one that doesn't. This is its
own skill — see `faster-voice-agents`.

### 7. Everything after the call

Post-call analysis, cost tracking, recording, webhooks, Slack alerts. All of it
belongs in the shutdown path where nothing is waiting, so use cheap models and
generous timeouts. Every one of these must fail soft: the call record matters
more than any enrichment of it.

## What you need before starting

**Accounts:** Twilio (with an Elastic SIP Trunk), Supabase or any Postgres,
an STT/TTS provider, an LLM provider. Optionally Slack, object storage for
recordings, and offsite backup.

**A VPS with a public IP.** Two cores is the practical floor and it is already
tight once the media server, SIP bridge, worker and dashboard share it —
budget roughly 2–4 concurrent calls per two cores. Four cores if the calls
matter.

**Decisions to make deliberately**, each covered in
`references/architecture-decisions.md`: cascaded pipeline vs speech-to-speech;
which LLM (measure time-to-first-token, not throughput); self-hosted vs managed
media server; how much of the agent is configurable by admins vs fixed in code.

## The principles that shaped this

**Configuration in the database, behaviour in code.** Admins change prompts,
voices, tool descriptions and which tools an agent has. They do not change what
a tool *does*. Getting that line wrong means either a rigid product or one
where an admin can break calls.

**Observed beats inferred.** Anything the system can watch — who hung up,
whether a transfer ran, what a tool captured — is recorded from the event, not
guessed from a transcript afterwards. Reserve the LLM for genuinely subjective
fields, and label those as model-produced wherever they surface.

**Fail soft after the call, fail loud before it.** A missing API key should
stop the worker booting. A post-call summary that times out should leave a NULL
and move on.

**Every silent failure gets a log line and a column.** Most failures here are
invisible: a call that rings out, a message that never arrives, a write that is
swallowed. If something can fail quietly, make it leave a trace — that is the
difference between a bug you fix in ten minutes and one that takes three test
calls to reproduce.
