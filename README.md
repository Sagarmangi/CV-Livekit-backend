# Kodexo Voice Agent -- Backend

Self-hosted, LiveKit-based inbound voice-AI platform: the Python (LiveKit
Agents SDK) worker that answers real calls via Twilio SIP trunking, the
self-hosted LiveKit + SIP infra it runs on, and the database schema both this
worker and the dashboard depend on.

The admin dashboard (Next.js) lives in a separate repo: [livekit_frontend](https://github.com/aiautomationkodexo/livekit_frontend).

- `agent-worker/` -- Python LiveKit agent worker (STT -> LLM -> TTS call flow)
- `infra/` -- self-hosted LiveKit + SIP bridge (docker-compose), deploy and backup scripts
- `supabase/` -- database schema migrations

## Setting this up

On a fresh VPS:

```bash
sudo mkdir -p /opt/kodexo && sudo chown -R "$USER:$USER" /opt/kodexo
git clone https://github.com/alihamza-kodexo/livekit_backend.git /opt/kodexo/backend
/opt/kodexo/backend/infra/deploy/bootstrap.sh
```

It does everything mechanical and stops with an exact instruction whenever it
needs a credential only you can fetch. Re-run it after filling those in -- it
resumes, and never overwrites config that already exists.

**[DEPLOYMENT.md](DEPLOYMENT.md)** is the long way round: every step by hand, a
way to verify each one, and a troubleshooting table for the failures that are
silent. Use it when the script fails, or to see what it is doing.

`infra/README.md` goes deeper on LiveKit and SIP. The section below covers the
database on its own.

## Database setup

One command, whether the database is empty or already half-built:

```bash
cd agent-worker
python -m worker.migrate
```

It needs `SUPABASE_DB_URL` -- the Postgres connection string from Supabase ->
Project Settings -> Database -> Connection string -> URI. Not the REST URL and
service-role key the worker uses for everything else: those go through
PostgREST, which cannot create tables.

`python -m worker.migrate --dry-run` reports what would run and changes nothing.

### Standing up a new environment

Set `SUPABASE_DB_URL` and `ADMIN_EMAIL` on a fresh Supabase project, then run
the command above. All 26 migrations apply in order in one pass -- there is no
file-by-file pasting into the SQL editor.

`ADMIN_EMAIL` matters: migrations 0003/0004 seed Kodexo's own addresses into
`allowed_users`, so without it nobody at the new deployment can sign in to the
dashboard. Those two seeded addresses also arrive with the migrations -- for a
deployment that isn't ours, remove them afterwards:

```sql
delete from allowed_users where email like '%@kodexolabs.com' or email = 'ai-automation@gmail.com';
```

The schema is all this sets up. Still manual, because no migration can do
them: Supabase Auth users, the LiveKit SIP trunk and dispatch rule (see
`infra/`), the free-tier keepalive cron (`infra/backup/keepalive-ping.sh`), and
provider API keys -- though those can be entered from the dashboard's
Integrations page once it is running, rather than put in `.env.local`.

### An existing database

A database built by hand before this runner existed has the schema but no
`schema_migrations` table. The runner detects that and refuses rather than
applying `0001` on top of live data. Adopt it into tracking instead -- this
writes only the bookkeeping table and runs no DDL:

```bash
python -m worker.migrate --adopt
```

### Deploying a schema change

Run the migration **before** restarting the worker. New worker code against an
old schema fails inside `insert_call_log`, which logs and swallows the error --
so whole call records are lost with nothing but a line in the worker log to
show for it. Adding `python -m worker.migrate` to the deploy script ahead of
the `systemctl restart` removes that risk entirely.
