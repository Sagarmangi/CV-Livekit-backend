# Failure modes

Every one of these was hit in production. They share a shape: **nothing errors
visibly**. A call rings out, a message never arrives, a row is never written —
and the logs say nothing unless you made them.

Read this before building. Most of these are cheap to design out and expensive
to diagnose.

## Calls

**The phone rings and nobody answers.**
Check in this order: is the agent `active` (a draft agent is refused at the
config lookup); does the dispatch rule's agent name match the name the worker
registers under; is the number actually attached to the trunk.

That last one is the nastiest, because the number can look perfect in your
dashboard while being unrouted at the telephony provider. An unrouted number
produces **no call record at the provider either** — the call is rejected
before it is accepted, so there is no log line anywhere and the ringing the
caller hears comes from their own carrier. Reconcile your number list against
the provider's trunk periodically; a number assigned to an agent but not
attached is invisible until someone calls it.

**Call connects, no audio either way.**
A missing UDP range in the firewall — the media ports or the RTP ports. Signal
travels on TCP and works fine, so the call "connects" and is silent.

**Agent never speaks first.**
A realtime model that cannot accept a client-initiated turn. Both failures —
this and a dead worker — present identically: nobody ever speaks.

**Agent cuts itself off mid-sentence, then resumes two seconds later.**
Barge-in triggered by raw audio energy: line hiss, a television, or the agent's
own voice echoing off a speakerphone. Gate interruption on recognised *words*,
not energy.

## The worker

**A call happens and no row is written.**
Something raised before the shutdown callback was registered. Register it as
early as possible — before loading provider settings, before building the
session — and treat every line above it as a place that must not fail.

This has a nastier cousin: the row write itself failing and being swallowed. A
swallowed write plus a schema behind the code means **every** call silently
vanishes, with one log line on a box nobody is watching.

**Intermittent "bound to a different event loop" on the first DB call.**
A process-wide database client reused across jobs. The client's connection pool
binds async primitives to whichever loop first used it; a worker process is
reused and each job gets a fresh loop. Key clients by event loop, with weak
references so they die with the loop.

It presents as random flakiness because it only bites when a job lands on an
already-used process.

**Calls stop being answered after a deploy.**
Code deployed ahead of its schema. Run migrations *before* restarting, with
`set -e` so a failed migration aborts the deploy and leaves the old worker
running against the schema it was built for.

## Tools

**A successful action reports itself as an error.**
A tool that ends or transfers the call returning a string. Frameworks commonly
decide "run another LLM turn" from whether the tool returned anything, so the
model is asked to narrate an action against a session already tearing down. The
model returns an empty completion, which logs as an LLM error, marks the call
failed, and may be spoken into a room the caller has left.

**Return `None` from anything that ends or hands off the call.** Raise on
failure instead — that is the case where the model genuinely needs to know.

**A closing line is cut off mid-word.**
The tool did not wait for playout before tearing down. Wait for the speech
generated *before* the tool call in the same turn — and use the run-context's
wait, not the turn handle's, or you create a circular wait (the handle waits
for the whole turn, which includes the tool).

**An agent captures no leads however good the calls are.**
The capture tool is opt-in and was never attached. The agent looks entirely
healthy; nothing downstream can fire because nothing was ever recorded.
Surface "attached tools" wherever agent health is shown.

## Notifications

**Configured and still silent.**
Three independent gates, and only a log line distinguishes them: the webhook
URL missing at deployment level, the per-agent toggle off, or nothing worth
sending. Log each case distinctly — "not configured", "disabled for this
agent", "nothing captured" — or you cannot tell them apart from the outside.

**A value that is set but reads as unset.**
Dotenv-style loaders take the **last** occurrence of a key. Env example files
ship keys present but blank, and editors append at the end — so a value added
above an existing blank line silently loses. Write env values by *replacing*
the key's line, never appending, and warn about present-but-empty keys instead
of treating them as absent.

## Database and config

**Connection string fails with a DNS error on a host you have never seen.**
The password contains a character that must be percent-encoded. A literal `@`
makes the URI parser split there and read the rest of the password as a
hostname. Encode it, or use an alphanumeric password.

**Advisory locks silently do nothing.**
Connecting through a transaction-mode pooler. Session-scoped features — advisory
locks, `SET`, prepared statements — do not survive it. Use a session-mode or
direct connection for anything that needs them.

**A migration reports drift on a file nobody touched.**
Checksums computed over raw bytes, with the same commit checked out with
different line endings on different platforms. Normalise line endings before
hashing, and pin them in `.gitattributes`.

**The database pauses itself.**
Free tiers idle out. Every call then fails at the config lookup, before
anything is logged. A cron that touches one row prevents it — and confirm it is
actually running, because its absence is invisible until the first outage.

## Infrastructure

**Git refuses to pull: "insufficient permission for adding an object".**
The deploy script was run with `sudo` once, so objects in `.git/objects` are
root-owned and every later unprivileged pull fails. Have the script refuse to
run as root, and use `sudo` only for the specific privileged commands.

**Compose variables silently expand to empty.**
Running `docker compose -f /abs/path` from a different working directory.
Compose resolves its env file relative to the project directory, so secrets
become empty strings: the media server's key map ends up empty and it refuses
every token, with the containers up and apparently healthy.

**Everyone's replies get slower at once.**
Concurrency on a small box. Each call is typically a separate process; nothing
rejects the fifth caller, they all just degrade together. Compare
time-to-first-token on overlapping calls against isolated ones before trusting
any capacity estimate.

## Design rules that prevent most of the above

1. **Register teardown before anything that can fail.**
2. **Log the three reasons a thing didn't happen, distinctly.**
3. **Never swallow a write failure without a loud log line.**
4. **Anything opt-in should say so where the thing looks healthy.**
5. **Reconcile your config against the external system**; a number, a trunk or
   a key can be right in your database and wrong at the provider.
6. **Prefer a refusal to a half-action** — a migrator that stops on an
   unexpected state is worth more than one that guesses.
