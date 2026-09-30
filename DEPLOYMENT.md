# Deploying the voice agent on a VPS

## The short way

```bash
sudo mkdir -p /opt/kodexo && sudo chown -R "$USER:$USER" /opt/kodexo
git clone https://github.com/alihamza-kodexo/livekit_backend.git /opt/kodexo/backend
/opt/kodexo/backend/infra/deploy/bootstrap.sh
```

`bootstrap.sh` does everything mechanical — venv, LiveKit and Redis secrets,
containers, schema, systemd, the deploy symlink — and **stops with an exact
instruction** the moment it needs a credential only you can fetch. Fill that
in, run it again; it picks up where it left off and is safe to re-run at any
point. It never overwrites a secret or config that already exists.

It finishes by verifying what it built (containers up, service active, schema
current) and printing the handful of things no script can do: firewall rules,
the Twilio Origination URI, the SIP dispatch rule, the Supabase Auth user, the
dashboard, and the per-agent settings.

**The rest of this document is the long way round.** Follow it when the script
fails, or when you want to know what it is actually doing — a script that hides
thirty commands is excellent right up until it half-works.

---

# The long way

Start to finish, in the order things actually have to happen. Every step says
how to tell it worked, because most of the failures here are silent — a call
that rings out, a Slack message that never arrives, a database write that is
logged and swallowed.

Two documents sit alongside this one: `infra/README.md` covers the LiveKit and
SIP layer in more depth, and `README.md` covers database setup on its own.

**The system is four pieces:**

| Piece | What it is | Where it runs |
|---|---|---|
| Supabase | Postgres — schema and all call data | hosted (supabase.com) |
| `infra/` | LiveKit media server, SIP bridge, Redis, egress | Docker, on the VPS |
| `agent-worker/` | Python worker that answers calls | systemd, on the VPS |
| `dashboard/` | Next.js admin UI ([livekit_frontend](https://github.com/aiautomationkodexo/livekit_frontend)) | Node behind nginx, on the VPS |

---

## 0. What you need first

- A VPS with a public IP. **Two cores is the practical floor**, and it is
  already tight: on the current box the SFU, SIP bridge, worker and dashboard
  share two cores at a measured load of 0.56–0.99. Expect roughly 2–4
  concurrent calls before reply latency degrades. Four cores if you can.
- A Supabase project (free tier works).
- A Twilio account with one Elastic SIP Trunk and at least one number.
- API keys: Deepgram (STT + TTS), and Gemini and/or DeepSeek (the LLM).
- Optionally: a Slack incoming webhook, a Cloudinary account (call recording),
  Backblaze B2 (backups).

Pick a deploy user — this guide calls it `deploy`. **Do not do any of this as
root.** Step 4 explains why in detail.

---

## 1. Clone the repos

```bash
sudo mkdir -p /opt/kodexo && sudo chown -R "$USER:$USER" /opt/kodexo
cd /opt/kodexo
git clone https://github.com/alihamza-kodexo/livekit_backend.git backend
git clone https://github.com/aiautomationkodexo/livekit_frontend.git dashboard
```

The dashboard is a separate repo on purpose. `backend/.gitignore` excludes
`/dashboard/`, so the two never fight over the same files.

**Check:** `ls /opt/kodexo/backend/agent-worker` shows `src`, `pyproject.toml`.

---

## 2. Set up the database

This is one command now — there is no pasting migration files into the SQL
editor.

```bash
cd /opt/kodexo/backend/agent-worker
python3 -m venv .venv
.venv/bin/pip install -e . --quiet
cp .env.example .env.local
```

Fill in `.env.local`. For this step only two keys matter:

- `SUPABASE_DB_URL` — Supabase → Project Settings → Database → Connection
  string → **URI**
- `ADMIN_EMAIL` — your email, so you can sign in to the dashboard

> **Two traps, both of which have already cost hours.**
>
> **Percent-encode the password.** Supabase shows the raw one. A literal `@`
> in it makes the URI parser split there and read the rest of your password as
> a hostname, so the error is a DNS failure on a host you have never seen:
> `@`→`%40`, `$`→`%24`, `+`→`%2B`. Easiest cure: reset the database password to
> letters and digits only.
>
> **Use port 5432, not 6543.** The migrator takes a session-scoped
> `pg_advisory_lock`; the transaction pooler on 6543 drops it between
> statements.

Then:

```bash
.venv/bin/python -m worker.migrate --dry-run   # look before you leap
.venv/bin/python -m worker.migrate
```

**Check:** the last lines read `applied 26 migration(s)` then `done`, and
`--dry-run` afterwards reports `pending: 0`.

If it says *"already has an `agents` table but no schema_migrations"*, the
database was built by hand before the migrator existed. Adopt it instead —
this records the migrations without running them:

```bash
.venv/bin/python -m worker.migrate --adopt
```

**One thing to do by hand:** migrations 0003/0004 seed Kodexo's own addresses
into `allowed_users`. For a deployment that isn't ours, remove them:

```sql
delete from allowed_users
where email like '%@kodexolabs.com' or email = 'ai-automation@gmail.com';
```

Then create your Supabase Auth user (Authentication → Users → Add user) with
the same address as `ADMIN_EMAIL`. `allowed_users` is only an allowlist — it
does not create the login.

---

## 3. Start LiveKit, SIP and Redis

```bash
cd /opt/kodexo/backend/infra
cp .env.example .env
openssl rand -hex 16   # -> LIVEKIT_API_KEY
openssl rand -hex 32   # -> LIVEKIT_API_SECRET
openssl rand -hex 24   # -> REDIS_PASSWORD
docker compose up -d
```

You invent the LiveKit key/secret — nobody issues them. **Keep them; the worker
and the dashboard both need the same pair.**

Open these ports on the firewall and the cloud security group:

| Port | Protocol | Purpose |
|---|---|---|
| 7880 | TCP | LiveKit signaling/API |
| 7881 | TCP | LiveKit RTC (TCP fallback) |
| 50000–60000 | UDP | LiveKit RTC media |
| 5060 | UDP | SIP signaling |
| 10000–20000 | UDP | SIP call audio (RTP) |

A call that connects but has **no audio** is almost always a missing UDP range.
See `infra/README.md` for the SIP trunk and dispatch rule, and note that TLS
and TURN are deliberately not set up yet.

**Check:** `docker compose ps` shows `redis`, `livekit` and `sip` all `Up`.

Leave `egress` off unless you need call recording — it runs a headless Chrome
**per call**, which on two cores will cost you reply latency before it costs
you anything else.

---

## 4. Install the deploy script

```bash
mkdir -p /opt/kodexo/deploy
ln -sf /opt/kodexo/backend/infra/deploy/deploy-backend.sh \
       /opt/kodexo/deploy/deploy-backend.sh
```

A symlink, so `git pull` updates the script itself.

> **Never run it with `sudo`.** The two privileged steps inside call `sudo -n`
> themselves; git is meant to run as you. Running the whole thing as root makes
> git write objects into `.git/objects` owned by root, and every later
> unprivileged pull dies with *"insufficient permission for adding an object to
> repository database"* — which reads like a git bug and is really this. The
> script now refuses to start as root for exactly that reason. If it has
> already happened: `sudo chown -R "$USER:$USER" /opt/kodexo/backend`.

The deploy user needs passwordless sudo for just two commands:

```bash
sudo visudo -f /etc/sudoers.d/kodexo-deploy
```

```
deploy ALL=(root) NOPASSWD: /bin/systemctl restart kodexo-worker, /usr/bin/docker compose *
```

---

## 5. Finish the worker's config and start it

Fill in the rest of `agent-worker/.env.local`:

- `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`
- `LIVEKIT_URL=ws://localhost:7880`, plus the key/secret from step 3
- `LIVEKIT_AGENT_NAME` — must match `roomConfig.agents[].agentName` in the
  dispatch rule, or LiveKit dispatches the call to nobody
- `DEEPGRAM_API_KEY`, and `GEMINI_API_KEY` and/or `DEEPSEEK_API_KEY`
- `SLACK_WEBHOOK_URL` and `DASHBOARD_BASE_URL` if you want lead alerts

Leave `WORKER_LOAD_THRESHOLD` blank. With a single worker there is nobody to
hand a shed call to, so refusing one means the phone just rings out.

Create the service:

```bash
sudo tee /etc/systemd/system/kodexo-worker.service >/dev/null <<'UNIT'
[Unit]
Description=Kodexo voice agent worker
After=network-online.target docker.service

[Service]
Type=simple
User=deploy
WorkingDirectory=/opt/kodexo/backend/agent-worker
ExecStart=/opt/kodexo/backend/agent-worker/.venv/bin/python -m worker.entrypoint start
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now kodexo-worker
```

`WorkingDirectory` is **not** cosmetic. The worker loads config with
`load_dotenv(".env.local")` — a relative path — so if the working directory is
wrong it reads no config at all and every key looks unset.

**Check:**

```bash
systemctl status kodexo-worker --no-pager
journalctl -u kodexo-worker -n 30
```

You want `registered worker` in the log and no traceback.

---

## 6. Start the dashboard

```bash
cd /opt/kodexo/dashboard
cp .env.example .env.local     # then fill it in
npm ci && npm run build
npm run start -- -p 3001 &     # use pm2 or a systemd unit for real
```

It needs the Supabase URL, service-role **and** anon keys, the Twilio SID/token
and trunk SID, and the same LiveKit key/secret pair from step 3.

Put nginx in front of it — `infra/nginx/` has a working config. Note
`client_max_body_size 2m` there, which is the ceiling on anything uploaded
through the dashboard.

**Check:** the login page loads over your domain and your `ADMIN_EMAIL`
signs in.

---

## 7. Point Twilio at LiveKit, and make a call

Set the Elastic SIP Trunk's **Origination URI** to `sip:<vps-ip>:5060`, then
attach a number to an agent from the dashboard's Numbers page.

Then, before you dial, in the dashboard:

1. Set the agent's status to **active** — a draft agent will not answer.
2. Attach the tools it needs. **`record_lead_info` is opt-in**, and an agent
   without it captures no lead details at all — so no lead ever reaches Slack,
   however good the call was.
3. Turn on **Slack notifications** per agent if you want alerts; it is off by
   default.

Now call the number and watch:

```bash
journalctl -u kodexo-worker -f
```

**Check, in order:** the agent greets you · `call end claimed: ended_by=…` when
you hang up · a row on the dashboard's Calls page · a Slack message if the call
captured a name or a need.

---

## Deploying a change, from here on

```bash
/opt/kodexo/deploy/deploy-backend.sh
```

It pulls `main`, and if `agent-worker/` or `supabase/` changed it installs,
**migrates, then** restarts the worker. That order is the whole point: `set -e`
means a failed migration aborts before the restart, leaving the old worker
running against the schema it was built for. New code against an old schema
fails inside `insert_call_log`, which logs and swallows the error — so calls
are lost with nothing but a line in the worker log.

The dashboard deploys separately: `git pull && npm ci && npm run build`, then
restart its process.

---

## When something is wrong

| Symptom | Where to look |
|---|---|
| Phone rings out, nothing answers | agent is `draft`; or `LIVEKIT_AGENT_NAME` ≠ the dispatch rule's `agentName` |
| Call connects, no audio either way | UDP 10000–20000 or 50000–60000 closed |
| Agent never speaks first | a `gemini_live` 3.x model — it cannot start a turn. Use a native-audio model |
| Every env var "unset" | `WorkingDirectory` wrong, or a duplicate empty key later in `.env.local` |
| `SLACK_WEBHOOK_URL not set` but it is set | duplicate empty `SLACK_WEBHOOK_URL=` **below** the real one — dotenv takes the last |
| No Slack message on a good call | agent toggle off; or no `record_lead_info` tool; or no lead details captured |
| Calls happen but no rows in `call_logs` | schema behind the code — run the migrator, then check `journalctl` for `failed to write call_logs row` |
| `insufficient permission … .git/objects` | the deploy script was run with sudo once. `chown -R` the checkout |
| Migrator: `failed to resolve host '…'` | password not percent-encoded — the parser split on a literal `@` |
| Replies getting slower under load | concurrent calls on two cores. Compare `ttft` in the turn-metrics log lines |

Useful commands:

```bash
journalctl -u kodexo-worker -f                        # live worker log
journalctl -u kodexo-worker | grep -iE 'slack|lead'   # why no alert
.venv/bin/python -m worker.migrate --dry-run          # schema drift
docker compose -f /opt/kodexo/backend/infra/docker-compose.yml ps
nproc; free -m; uptime                                # is the box coping
```

---

## Don't forget

- **Backups.** `infra/backup/backup-db.sh` dumps Postgres to Backblaze B2 daily
  via cron. On free-tier Supabase it is the only copy of your call history.
  Confirm it actually runs — a backup script nobody has watched succeed is not
  a backup.
- **The keepalive ping.** Free-tier projects pause when idle;
  `infra/backup/keepalive-ping.sh` on a cron keeps it awake. A paused project
  means every call fails at the config lookup.
- **Recording consent.** `CALL_RECORDING_ENABLED` is off by default, and that
  is a decision rather than an omission — recording inbound PSTN calls carries
  obligations that depend on the caller's jurisdiction and yours.
- **Rotate credentials that have been pasted into a chat, a ticket or a
  screenshot.** Especially the database password and the Slack webhook.
