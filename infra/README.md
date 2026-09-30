# LiveKit + SIP — self-hosted infra

> Setting up a whole deployment? Follow **[../DEPLOYMENT.md](../DEPLOYMENT.md)**
> instead — it puts these steps in the right order relative to the database,
> the worker and the dashboard. This file is the detail on the LiveKit and SIP
> layer specifically.

This runs the two self-hosted pieces the FSD calls for: the LiveKit media
server and the LiveKit SIP bridge (the thing Twilio actually talks to). It's
the same `docker-compose.yml` whether you run it on this machine via Docker
Desktop to validate the setup, or on the production VPS — only the host
changes.

`deploy/deploy-backend.sh` is the VPS deploy script: it pulls `main`, migrates
the database, and restarts whatever changed. See DEPLOYMENT.md for how to
install it, and the comments in the file for why it refuses to run as root.

Redis is included because LiveKit recommends it for production and the SIP
bridge requires it for state.

## 1. Configure secrets

```bash
cp .env.example .env
```

Fill in `.env` with real values:

```bash
openssl rand -hex 16   # -> LIVEKIT_API_KEY
openssl rand -hex 32   # -> LIVEKIT_API_SECRET
openssl rand -hex 24   # -> REDIS_PASSWORD
```

These two LiveKit values aren't issued by anyone — you're inventing your own
key/secret pair, the same way you'd invent a password. The agent worker and
the dashboard backend will need this exact same pair to talk to this server.

## 2. Start it

```bash
docker compose up -d
docker compose logs -f
```

This starts three containers: `redis`, `livekit` (the media/RTC server), and
`sip` (the SIP↔WebRTC bridge). All three use `network_mode: host` — LiveKit's
own docs require this because of how many UDP ports are involved; Docker's
per-port mapping can't handle a 10,000-port range.

On the VPS, this means the containers bind directly to the host's network
interface, so make sure nothing else on the box is already using these ports.

## 3. Open these ports on the server's firewall / cloud security group

| Port | Protocol | Purpose |
|---|---|---|
| 7880 | TCP | LiveKit signaling/API |
| 7881 | TCP | LiveKit RTC (TCP fallback) |
| 50000–60000 | UDP | LiveKit RTC media |
| 5060 | UDP (+TCP if your provider needs it) | SIP signaling |
| 10000–20000 | UDP | SIP call audio (RTP) |

Twilio's SIP trunk and the caller's actual audio both depend on these being
open — a call reaching LiveKit but with no audio is almost always a firewall
gap in the 10000–20000 or 50000–60000 ranges.

## 4. Create the inbound trunk + dispatch rule (first manual test only)

Install the CLI:

```bash
# Windows
winget install LiveKit.LiveKitCLI
# Linux (on the VPS)
curl -sSL https://get.livekit.io/cli | bash
```

Point it at this server (swap in the real host once it's reachable — use
`ws://<vps-ip>:7880` for a first local test, `wss://...` once TLS is set up):

```bash
lk project add kodexo-voice --url ws://<vps-ip-or-domain>:7880 --api-key $LIVEKIT_API_KEY --api-secret $LIVEKIT_API_SECRET
```

Create the trunk and dispatch rule from the example files in `sip/` (edit the
phone number in `inbound-trunk.example.json` to the real Twilio number first):

```bash
lk sip inbound create sip/inbound-trunk.example.json
lk sip dispatch create sip/dispatch-rule.example.json
```

**Important — this manual step is only for the first connectivity test.**
Once the dashboard exists (Phase 8 / the dynamic-numbers scope addition), the
dashboard's backend creates trunks and dispatch rules automatically via the
same LiveKit server API (`api.sip.create_sip_inbound_trunk`,
`api.sip.create_sip_dispatch_rule` in the Python SDK) every time an admin buys
and attaches a new Twilio number — nobody should be hand-editing JSON files
per agent in the real system.

The dispatch rule's `roomConfig.agents[].agentName` must match the name the
Python agent worker registers itself under (Phase 4) — that's how LiveKit
knows which worker to dispatch into the room when a call comes in.

## 5. Point Twilio at it

In the Twilio Elastic SIP Trunk's **Origination URI**, set:

```
sip:<vps-public-ip-or-domain>:5060
```

Then call the Twilio number. If everything above is correct, LiveKit creates
a room and dispatches whatever agent worker is registered under the name in
the dispatch rule.

## Notes / what's deliberately left out for now

- **TURN is not enabled.** It matters most for browser/WebRTC participants
  behind restrictive NATs (e.g. a future warm-handoff feature); it's not
  required to get the core Twilio→LiveKit→worker call path working, and it
  needs its own domain + TLS certificate to set up. Add it later if warm
  handoff or a web client is built.
- **TLS/`wss://` is not set up yet.** The first connectivity test can run
  over plain `ws://`/`sip:`; add a reverse proxy or LiveKit's built-in TLS
  once this needs to be reachable securely from anywhere other than a direct
  IP test.
