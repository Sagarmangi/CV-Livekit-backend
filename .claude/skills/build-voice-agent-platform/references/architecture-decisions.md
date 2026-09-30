# Architecture decisions, and the evidence behind them

Each of these was a real fork with a real cost. The numbers are from one
production deployment (a two-core VPS, US East, inbound PSTN) — treat them as
worked examples of *how to decide*, not as constants to copy.

## Self-hosted media server vs managed

**Chose self-hosted** (LiveKit + SIP bridge + Redis, in Docker).

Managed platforms charge a per-minute platform fee on top of provider costs —
around $0.05/min, which exceeds the entire rest of the stack. Self-hosting
trades that for operating a media server: UDP port ranges, firewall rules, no
TURN, TLS you set up yourself.

What it costs you beyond money: enhanced noise cancellation is typically a
cloud-only feature. Self-hosted, there is **no input-side noise suppression at
all** — the only defences are the turn detector and the caller's handset. Know
that before promising performance on noisy lines.

## Cascaded pipeline vs speech-to-speech

**Default to the cascaded pipeline** (STT → LLM → TTS); offer realtime as a
per-agent opt-in.

Realtime is often *cheaper* and sounds more natural, so this is not a cost
decision. It is a control decision: a realtime model takes turn-taking
server-side, so endpointing, interruption gating and preemption stop applying.
It also may not emit transcripts the way the pipeline does.

**The failure mode is silence, not errors.** Any feature keyed off transcripts
— spam detection, keyword triggers — simply stops running. Audit every
transcript consumer before exposing a realtime option, and log a warning when
an agent is configured onto one.

Also verify the specific realtime model can *start* a turn. Some refuse a
client-initiated first turn, which means an inbound agent never greets and the
line opens on silence — indistinguishable from a dead worker.

## Which LLM

**Measure time-to-first-token for a one-sentence reply. Ignore everything else.**

TTS starts speaking on the first token, so total completion time is invisible
to the caller. Measured, five runs each, reasoning disabled:

```
gemini-2.5-flash        451 ms best / 499 ms median   <- chosen
gemini-3.1-flash-lite   498 / 510
deepseek (origin API)   535 / 648
gemini-3.5-flash        687 / 753
```

The newer, larger models were slower. Benchmarks and token throughput predicted
none of this.

Two corollaries:

- **Model hosting is a latency decision.** An open-weight model served from a
  datacentre near the worker beat its own origin API by hundreds of
  milliseconds. Same model, different network path.
- **Pin the major version.** Reasoning is disabled via a parameter whose name
  changes between model generations, and a model that reasons before answering
  adds dead air to every turn. A version bump is a latency change.

Make the engine a **per-agent database column**, so switching is a dropdown and
a new call, not a deploy. Keep the previous provider wired up as rollback.

## Routing in the database, not the telephony provider

Every number points at one shared trunk. Which agent answers is resolved by
looking up the dialled number at answer time.

The alternative — per-number configuration in Twilio — means reassigning a
number is an API call against a third party, permissions to manage, and a
second source of truth that drifts from your dashboard. This way the dashboard
*is* the truth, and the trunk never changes.

It also gives you bring-your-own-number: a customer's own Twilio account gets
its own trunk pointed at the same endpoint, recorded in a separate table. Their
credentials stay server-side and are used only to attach and detach.

## Configuration in the database, behaviour in code

Admins configure prompts, voices, which tools an agent has, and each tool's
name and description. They do **not** configure what a tool does.

The line matters in both directions. Tool *descriptions* must be editable —
when to transfer is genuinely agent-specific, and a description is read by the
model every turn, so it carries more weight than prose in a system prompt. Tool
*mechanics* must not be: getting a hang-up wrong produces calls that never end,
billing until the room times out.

Where a tool has non-negotiable mechanics, append them to the admin's
description rather than replacing it, so an admin writing "end after booking"
cannot accidentally drop "speak your closing line first".

## One call record, written once, at the end

Not incremental updates as the call progresses.

A call is only fully describable once it has ended: duration, outcome, cost,
who hung up. Incremental writes mean partial rows for in-flight calls that
every consumer has to special-case, and a crash leaves them partial forever.

The cost is that a crash before teardown loses the record entirely — which is
why the shutdown callback is registered before anything that can raise, and why
the write itself catches and logs rather than propagating.

## Post-call work belongs after the call

Summarisation, extraction and scoring run in the shutdown path, after the
caller has gone. Nothing is waiting, so the model chosen for the conversation
(fast, more expensive) is the wrong one here: a slower, cheaper model does the
same job for roughly a tenth of the cost.

It must never be able to take the call record with it — catch every exception,
apply a hard timeout, and write the row with NULLs when it fails.

## Derive in code what you can observe

The first version of post-call enrichment asked a model for fourteen fields.
Most of them were facts the system had watched happen: which number was
dialled, whether a transfer ran, whether the session errored, who hung up.

A model reading a transcript can drop a digit from a phone number. A SIP
attribute cannot. Only genuinely subjective fields — a summary, a priority —
need a model, and those should be labelled as model-produced everywhere they
surface.

## Opt-in over opt-out for anything that leaves the system

Slack notifications, webhooks and lead alerts default **off**, per agent.

An outbound message is not reversible. A new agent that starts broadcasting to
a shared channel before anyone decided it should is worse than one that stays
quiet — and with opt-in, silence is unambiguous rather than "either off, or
nothing happened yet".

The exception is anything that ends a call. An agent that cannot hang up holds
the line until the room times out, billing the whole time, so the hang-up tool
is opt-*out*: every agent gets it unless it has its own.

## Notify on outcomes, not events

A channel that receives every call is a channel nobody reads, and it costs you
the alerts that matter. Send only what somebody must act on: a captured lead,
and a failed transfer where a caller was promised a callback. Everything else
lives in the call log, where it is searchable and does not interrupt anyone.
