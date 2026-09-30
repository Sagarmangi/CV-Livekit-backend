---
name: faster-voice-agents
description: Architecture and latency engineering for real-time voice AI agents (LiveKit, Twilio SIP, STT/LLM/TTS pipelines). Use when building or debugging a voice agent, choosing between a cascaded pipeline and a speech-to-speech model, picking or swapping an STT/LLM/TTS provider, tuning turn-taking, endpointing, interruption or barge-in, or investigating dead air, slow replies, an agent that talks over the caller or cuts itself off, or one that answers but says nothing.
---

# Making voice agents fast

A phone call is not a chat box. A caller hears silence as a fault at roughly
800ms and starts talking over the agent at about 1.2s. Every design decision
below exists to protect that budget.

## The only number that matters: time to first token

**TTS starts speaking on the first token, not the last.** Total completion time
is irrelevant to the caller — they hear the reply begin as soon as the first
token arrives and the rest streams behind the audio already playing.

So when choosing an LLM, measure TTFT for a one-sentence reply and ignore
tokens/sec and benchmark scores. A model that is 30% faster overall but 200ms
slower to first token is *worse* on a phone call.

Measured on this stack (five runs each, one-sentence phone reply, reasoning
disabled where the API allows):

```
gemini-2.5-flash        451 ms best / 499 ms median   <- chosen
gemini-3.1-flash-lite   498 / 510
deepseek (origin API)   535 / 648
gemini-3.5-flash        687 / 753
```

The newer, larger model is the slower one. Test, don't assume.

## Where the caller's wait actually goes

```
caller stops speaking
   │
   ├─ end-of-turn detection    ← biggest and most controllable cost
   ├─ LLM time-to-first-token  ← model + host choice
   ├─ TTS time-to-first-byte   ← streaming synthesis, not file generation
   └─ network / PSTN transit   ← ~500 ms floor, unfixable
                                  (jitter buffers + carrier hops)
```

Roughly 500ms is gone before you write a line of code. Budget accordingly: the
controllable portion is what's left, and end-of-turn detection is usually the
largest slice of it.

## Two architectures, and when each wins

**Cascaded pipeline — STT → LLM → TTS.** Three vendors, three hops, but every
stage is inspectable and tunable. You control turn-taking, you can swap one
component, you can apply a pronunciation dictionary, and you can log per-stage
metrics. Default to this.

**Speech-to-speech realtime** (Gemini Live, OpenAI Realtime). One hop, often
cheaper, more natural prosody — but it decides turn-taking *server-side*, so
every endpointing, interruption and preemption control you built stops
applying. You also lose the STT transcript that downstream features depend on.

The trap: features silently stop working rather than erroring. On this stack,
spam detection keys off `user_input_transcribed`, which a realtime model may
never emit — so the detector simply never runs. Audit every feature that
consumes transcripts before offering a realtime option.

Also verify the specific model can *start* a turn. Some realtime models refuse
a client-initiated first turn, which means an inbound agent never greets the
caller and the line opens on silence — indistinguishable from a dead agent.

## Turn detection: the biggest win available

A fixed silence timer ("wait 700ms of quiet, then reply") is the naive default
and the single largest source of dead air. It cannot distinguish a thinking
pause from a finished sentence, so you pay the full timeout on every turn.

**Use semantic end-of-turn detection** — a model that decides from the speech
itself whether the turn is complete. Deepgram Flux does this; so do dedicated
turn-detector models. Typical controls:

- `eot_threshold` (e.g. 0.7) — confidence to call the turn finished. Higher
  waits longer and interrupts less.
- `eager_eot_threshold` (e.g. 0.5) — a lower bar that lets the **LLM start
  early** while the detector keeps listening. This is free latency: if the
  caller turns out to still be speaking, the speculative generation is
  discarded.
- `eot_timeout_ms` (e.g. 600) — ceiling on waiting for the threshold.

**Keep a fallback.** Wrap the semantic recogniser in a fallback adapter with a
conventional streaming STT behind it, so an unavailable account feature or a
dropped socket degrades instead of ending the call. Log when it switches —
otherwise you get a mysteriously slower agent with no explanation.

## Endpointing floor and ceiling

Two separate numbers, and both defaults are usually wrong:

- **Floor** (min delay): how long to wait after speech stops. 300ms is a
  reasonable phone default. 500ms+ lands on top of PSTN transit and is audible.
- **Ceiling** (max delay): the SDK default may be 3s. State it explicitly —
  1.5s is a sane cap. An unstated ceiling is a three-second silence waiting to
  happen on a noisy line.

## Preemptive generation: take the LLM, skip the TTS

**LLM preemption on.** Start generating on the eager end-of-turn signal. Wasted
work is cheap and the latency saving is real.

**Preemptive TTS is a judgement call, not a default.** It puts audio synthesis
ahead of turn confirmation, so with eager EOT firing often and retries
configured, a single turn can run several speculative generations and throw
most away. On a shared 2-core box also running the media server and SIP bridge,
that CPU competes with pushing audio out on time — and blowing the audio
deadline costs more than the preemption saves. Enable it when the worker has a
box to itself.

## Interruption and barge-in

Raw VAD energy as the interrupt trigger means line hiss, a television, or the
agent's own voice echoing off a speakerphone stops the reply. The agent then
resumes ~2s later, leaving a hole mid-sentence — which callers report as "it
keeps cutting itself off".

**Require recognised words, not energy.** A minimum of 2 words routes barge-in
through the transcript, where noise produces nothing. The cost is that a
one-word interjection no longer interrupts; that is the right trade on a phone.

Log false interruptions explicitly. A run of them means the gate is too loose,
and without the log the complaint is unfalsifiable.

## Audio transport

- **DTX off.** Discontinuous transmission stops sending during silence — cheaper,
  but it starves the far side's adaptive jitter buffer of a steady stream, so it
  keeps a larger safety margin, and that margin is added delay on *every* reply.
- **RED on.** Redundant payloads mean a lost packet doesn't force the buffer to
  grow either.

Both matter far more on a SIP leg than a browser's local WebRTC connection.

## Keep slow work off the call

- **Post-call analysis** (summaries, extraction, scoring) belongs in the
  shutdown path, after the caller has hung up. Nothing is waiting, so use the
  cheapest adequate model — often 10x cheaper than the conversation model for
  the same job.
- **Mid-call classifiers** (spam, intent) must be bounded and fail open: a hard
  timeout of ~2.5s with `asyncio.wait_for`, and on timeout let the call
  continue. The call is worth more than the check.
- **Disable reasoning on the conversation model.** `thinking_budget=0` or the
  provider's equivalent. A model that reasons before answering adds dead air to
  every single turn. Note that model families change this parameter's name
  between versions — pinning a major version is a latency decision, not just a
  compatibility one.

## Tool calls are pure dead air

Every function call the model makes costs an **extra LLM round trip**: the model
emits the call, the tool runs, then a second turn generates the spoken reply.
That is roughly +500ms on top of the tool's own runtime, and most frameworks
have no filler mechanism — nothing says "one moment" while it runs.

Consequences worth designing around:

- A tool that makes a network call is a second or more of silence. Budget a
  ~300–500ms ceiling for anything on the conversational path.
- Data the agent *usually* needs is cheaper inlined in the system prompt than
  fetched by a tool, despite the token cost, because the prompt costs no round
  trip. Reserve on-demand tools for data that is rarely needed or too large.
- If a tool ends the call or transfers it, **return `None`**, not a string.
  Frameworks commonly decide "run another LLM turn" from whether the tool
  returned anything — so returning a value asks the model to narrate an action
  against a session that is already tearing down. That surfaces as an empty
  completion, logs as an error on a call that worked, and can be spoken into a
  room the caller has already left.

## Model hosting is a latency decision

An open-weight model served from a datacentre near your worker can beat the
same model on its origin API by hundreds of milliseconds — the difference is
network path and infrastructure, not the model. If a model is otherwise right
but slow, check whether a closer host exists before rejecting it.

Equally: put the worker near the telephony edge, and prefer providers with a
presence in that region.

## Measure, don't guess

Log per turn, and tag by transport (SIP vs web — they are not comparable):

- `eou` — total end-of-turn delay
- `ttft` — LLM first token
- `ttfb` — TTS first byte
- the endpointing delay actually waited, separately from the configured floor

Without the last one you cannot tell a slow model from a slow turn detector,
and you will tune the wrong thing.

Latency also degrades under **concurrency**, invisibly. Each concurrent call is
typically a separate process with its own STT sockets and audio work. On a
small box, replies get slower for everyone at once rather than any call being
rejected — so compare TTFT on overlapping calls against isolated ones before
believing a capacity number.

## Checklist for a new agent

1. Semantic end-of-turn detection, with a conventional STT as fallback
2. Endpointing floor ~300ms, ceiling stated explicitly (~1.5s)
3. LLM chosen on measured TTFT; reasoning disabled; version pinned
4. LLM preemption on; preemptive TTS only with CPU headroom
5. Interruption gated on recognised words, not energy
6. DTX off, RED on
7. Streaming TTS, never file-based synthesis
8. Classifiers bounded and fail-open; analysis moved after hangup
9. Tools return `None` when they end or hand off the call
10. Per-turn `eou`/`ttft`/`ttfb` logged and actually read
