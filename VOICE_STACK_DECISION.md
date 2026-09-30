# Voice Stack Decision

Status: decided and implemented for STT/TTS/LLM. Telephony (Telnyx) is
recommended but not yet built. This is the reference doc for "what are we
using and why" — update it if the stack changes again rather than letting the
decision live only in chat history.

All rates below were pulled live from provider pricing pages/docs during this
decision, not from memory. Verify before scaling spend — pricing pages change.

## Final stack

| Layer | Choice | Why |
|---|---|---|
| Telephony | **Telnyx** (recommended; Twilio is what's currently wired) | ~3x cheaper inbound than Twilio at the same SIP-trunk model. **Not yet built** — see "Open items" below. |
| STT | **Deepgram Nova-3** (streaming) | True low-latency streaming with interim results — needed for real barge-in. Cheapest option that doesn't compromise this. |
| LLM | **Groq — Llama-3.3-70b-versatile** (default), **DeepSeek v4 Flash** (per-agent opt-in) | Groq: fast enough for voice (the actual reason we picked it, not lowest $/token). DeepSeek: cheaper per-token, kept as an explicit switchable option — see the dedicated section below on why it isn't the default. |
| TTS | **Deepgram Aura-1** | ~2x cheaper than Cartesia (our original pick), and reuses the Deepgram API key/plugin already required for STT — no new vendor. **Implemented and verified.** |
| Orchestration | Self-hosted LiveKit + LiveKit Agents SDK (Python) | Already built this session; not reconsidered here. |

**LLM provider is a per-agent, dashboard-editable dropdown** (`agents.llm_provider`,
migration `0005_agent_llm_provider.sql`), not a global config — the worker
reads it fresh on every call, so switching an agent between Groq and DeepSeek
needs no redeploy. Built in `agent-worker/src/worker/entrypoint.py`'s
`_build_llm()`.

## Why DeepSeek is opt-in, not the default — tested, not assumed

Ran real requests against DeepSeek's own API (`deepseek-v4-flash`) rather than
relying on the team's prior "too slow" note secondhand:

- **It reasons by default**, even on the "flash" tier: asking "what kind of
  business is Kodexo Labs, one sentence" produced 54 hidden `reasoning_content`
  tokens before a 21-token answer — 174 total tokens for a one-sentence reply.
- DeepSeek's API supports `thinking: {"type": "disabled"}` to turn this off.
  With it disabled, the same question took **39 total tokens** (4.5x fewer) —
  meaningful cost win, now wired into `_build_llm()` via `extra_body`.
- **But wall-clock time barely changed: 1.75s → 1.58s.** The slowness isn't
  the reasoning step — it's DeepSeek's own API infrastructure/network path.
  ~1.6s for one sentence is still slow for a live phone call regardless of the
  `thinking` toggle.

Conclusion: `thinking: disabled` is applied unconditionally when an agent uses
DeepSeek (free cost win, no reason not to), but it does not make DeepSeek
fast enough to be the default for a live agent. It's there for whoever wants
to trade latency for lower per-token cost on a specific agent, with eyes open.

A faster path to using the same DeepSeek model, not yet built: third-party
hosts (DeepInfra, Together) serve the open-weight DeepSeek model on their own
infrastructure, typically faster than DeepSeek's origin API for non-China
callers — see "Open items."

## Why not the other alternatives we looked at

**OpenAI gpt-4o-mini (LLM).** Cheaper per-token than Groq, but Groq is faster. Since LLM choice moves total cost by about $0.002/min either way, speed wins over the fractional saving.

**Realtime speech-to-speech (OpenAI Realtime, Gemini Live).** Explicitly ruled out by the team for cost — though the actual numbers turned out more nuanced than "always pricier": Gemini Live computed out *cheaper* than our cascaded pipeline (~$0.023/min all-in vs ~$0.017–0.035/min), and only OpenAI's flagship `gpt-realtime` was clearly expensive (~$0.064/min). Still excluded per explicit decision, and the pipeline approach keeps the pronunciation-dictionary and tuning-knob control the FSD wants, which a realtime model would give up.

**Dograh** (open-source Pipecat-based voice platform, Vapi/Retell alternative). Genuinely solid project (5.1k GitHub stars, BSD 2-Clause — fully commercial-SaaS-safe, no source-disclosure obligation). Has a built-in no-code tools framework equivalent to what we hand-built. Not adopted because: (a) it's Pipecat-based, not LiveKit-based — switching means re-platforming the worker already built and verified this session; (b) no documented multi-tenant/workspace/white-label primitives, which is the actually-hard part of the "resell to clients" goal — we'd have to build that layer ourselves regardless of foundation, so switching buys little for the one thing that would've justified it.

**Vapi.** Reference point, not a serious option (that's what we're building an alternative to). $0.05/min platform fee on top of provider costs, best case.

## Cost comparison (per minute of call time)

Assumptions used throughout: ~3 LLM turns/min, ~1,500 input + 50 output tokens/turn, agent actually speaking ~45% of call time (TTS only bills while generating audio; STT/telephony bill for full call duration).

| Stack | Telephony | STT | LLM | TTS | **Total/min** |
|---|---|---|---|---|---|
| **Chosen (Groq)** | Telnyx $0.0032 | Deepgram Nova-3 $0.0048 | Groq $0.0028 | Deepgram Aura $0.006–0.014 | **~$0.017** |
| **Chosen (DeepSeek opt-in)** | Telnyx $0.0032 | Deepgram Nova-3 $0.0048 | DeepSeek ~$0.0007 | Deepgram Aura $0.006–0.014 | **~$0.015, but ~1.6s slower per turn** |
| Original (this session's first build) | Twilio $0.0100 | Deepgram Nova-3 $0.0048 | Groq $0.0028 | Cartesia $0.013–0.017 | ~$0.031–0.035 |
| Aggressive/unverified | Telnyx $0.0032 | Groq Whisper $0.0007* | Groq $0.0028 | Deepgram Aura $0.006–0.014 | ~$0.013 |
| Realtime (Gemini Live) | Twilio $0.0100 | — | — (bundled) | — (bundled) | ~$0.023 |
| Realtime (OpenAI gpt-realtime-mini) | Twilio $0.0100 | — | — (bundled) | — (bundled) | ~$0.027 |
| Realtime (OpenAI gpt-realtime, flagship) | Twilio $0.0100 | — | — (bundled) | — (bundled) | ~$0.064 |
| Vapi (best case — assumes zero provider markup) | — | — | — | — | ~$0.08 |

\* Groq Whisper is billed per-request with a 10-second minimum and is not true continuous streaming — real barge-in quality is unverified. Not used for that reason, kept here for reference only.

**Chosen stack is ~4.7x cheaper than Vapi's best case**, and ~2x cheaper than our own first-pass build, with no compromise on streaming STT or on the tuning knobs the FSD cares about. DeepSeek saves another ~$0.002/min over Groq — small in absolute terms, and not worth the latency hit as a default; kept as an opt-in for cost-sensitive, latency-tolerant use cases.

Excluded from every row above: the self-hosted LiveKit VPS itself (~$20–40/month fixed). Amortizes to near-zero per minute at any real call volume; matters more at very low volume.

## What Deepgram Aura gives up vs. Cartesia

Documenting this so it doesn't get silently forgotten:
- **No `speed` parameter** — `conversation_settings.speech_rate` (a dashboard-exposed tuning knob) has no effect with Aura. It already had no effect with `tts_stability`/`backchannel_frequency` before this change; this adds a third unmapped knob.
- **Voice selection is a model-name string** (e.g. `aura-2-andromeda-en`) rather than a separate voice ID — the dashboard's "Voice ID" field still works, just expects Deepgram's naming instead of Cartesia's.
- Voice naturalness is generally considered a notch below Cartesia in community comparisons — not independently verified by us. Worth an actual side-by-side listening test before treating this as final if voice quality complaints come up later.

## Open items — not yet done

1. **Telnyx isn't wired up.** The dashboard's entire Numbers page (`dashboard/lib/twilio.ts`, the search/buy/attach/release flow) is Twilio-API-specific code. Recommending Telnyx on cost doesn't make it real — an equivalent `lib/telnyx.ts` and a Numbers-page rework would be new work, not a config change. Currently still running on Twilio.
2. **A faster host for DeepSeek** (DeepInfra/Together instead of DeepSeek's own API) — would likely close most of the latency gap while keeping the same model and similar cost. Not built; `_build_llm()` currently only points at DeepSeek's origin API.
3. **Prompt caching** — not implemented. Several providers discount repeated-prefix tokens (DeepInfra showed ~78% off cached input; DeepSeek's own usage response already reports `prompt_cache_hit_tokens`, so this is available today for free once the prompt is structured to stay byte-identical across turns). LLM is already <10% of total cost, so the ceiling on this saving is small in absolute terms.
4. **Cheap-model-first routing** — not implemented. Routing trivial decisions (e.g. call classification) to a smaller/cheaper model or heuristic instead of the full 70B model on every turn. Real engineering effort, real savings, doesn't require scale.
5. **Volume-negotiated contracts and self-hosted STT/TTS/LLM on owned GPUs** are the two biggest levers the larger platforms (Vapi/Retell-scale) actually use — both require real committed call volume we don't have yet. Revisit once there's meaningful production traffic; attempting either before then would raise costs, not lower them (idle GPU time costs more than pay-per-use APIs at low volume).
