#!/usr/bin/env bash
#
# One command to stand the backend up on a fresh VPS, and safe to run again at
# any point:
#
#     /opt/kodexo/backend/infra/deploy/bootstrap.sh
#
# It does the ceremony -- venv, secrets, containers, schema, systemd, the deploy
# symlink -- and stops with an exact instruction whenever it needs something only
# a human can fetch. Run it, fill in what it asks for, run it again.
#
# DEPLOYMENT.md is the long way round, and it stays the reference for when this
# script is the thing that's broken. A script that hides thirty commands is
# excellent until it half-fails, so this one prints every action it takes and
# would rather refuse than half-do something.
#
# What it deliberately does NOT do:
#   * overwrite any secret or config that already exists -- re-running must
#     never invalidate a working deployment
#   * edit /etc/sudoers -- it prints the line for you to install with visudo,
#     because a malformed sudoers file locks you out of sudo entirely
#   * set up the dashboard, DNS or TLS -- separate repo, and too
#     environment-specific to guess at. Printed as a checklist at the end.
#
set -euo pipefail

REPO=/opt/kodexo/backend
WORKER="$REPO/agent-worker"
INFRA="$REPO/infra"
SERVICE=kodexo-worker

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m   %s\n' "$*"; }
skip() { printf '    --   %s\n' "$*"; }
warn() { printf '    \033[33mwarn\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mSTOP\033[0m %s\n\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Writing to a .env file
# ---------------------------------------------------------------------------
# Replaces the key's line if it is there, appends if it is not. Never just
# appends, and that is the whole point: these files ship most keys present but
# blank, python-dotenv takes the LAST occurrence, and a blank line below a
# filled one silently wins. That exact bug made a configured Slack webhook read
# as unset and cost a real lead.
set_env_var() {
  local file=$1 key=$2 value=$3
  if grep -q "^${key}=" "$file"; then
    # `|` as the delimiter: values here are URLs, full of slashes.
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >>"$file"
  fi
}

env_value() {
  local file=$1 key=$2
  [ -f "$file" ] || return 0
  # tail -1 rather than head: matches how python-dotenv resolves duplicates, so
  # this reads the same value the worker will.
  grep "^${key}=" "$file" 2>/dev/null | tail -1 | cut -d= -f2- || true
}

# ---------------------------------------------------------------------------
step "Preflight"
# ---------------------------------------------------------------------------
# Same reason the deploy script refuses: git run as root leaves .git/objects
# root-owned, and every later unprivileged pull dies with "insufficient
# permission for adding an object to repository database".
[ "$(id -u)" -ne 0 ] || die "Don't run this as root. Run it as your deploy user; it calls sudo itself where needed."

for cmd in git python3 docker openssl curl; do
  command -v "$cmd" >/dev/null || die "$cmd is not installed."
done
docker compose version >/dev/null 2>&1 || die "The docker compose plugin is missing (docker-compose v1 won't do)."
[ -d "$REPO/.git" ] || die "Expected the repo at $REPO. Clone it there first -- see DEPLOYMENT.md step 1."
sudo -n true 2>/dev/null || warn "passwordless sudo isn't set up yet; you'll be prompted, and the deploy script will need it later"
ok "user $(whoami), repo at $REPO"

CORES=$(nproc)
if [ "$CORES" -lt 2 ]; then
  warn "$CORES core(s). Two is the practical floor and it is already tight."
else
  ok "$CORES cores"
fi

# ---------------------------------------------------------------------------
step "Python environment"
# ---------------------------------------------------------------------------
if [ -x "$WORKER/.venv/bin/python" ]; then
  skip "venv already exists"
else
  python3 -m venv "$WORKER/.venv"
  ok "created $WORKER/.venv"
fi
"$WORKER/.venv/bin/pip" install -e "$WORKER" --quiet --upgrade
ok "worker package installed"

# ---------------------------------------------------------------------------
step "LiveKit and Redis secrets (infra/.env)"
# ---------------------------------------------------------------------------
# Invented here, not issued by anyone -- self-hosted LiveKit's `keys:` map is
# built from them (see docker-compose.yml). Generated once and then propagated
# to the worker below, because the same pair has to appear in infra/.env,
# agent-worker/.env.local and dashboard/.env.local. Keeping those three in step
# by hand is the classic "worker registers but never gets dispatched" bug.
if [ -f "$INFRA/.env" ]; then
  skip "infra/.env exists -- leaving its secrets alone"
else
  cp "$INFRA/.env.example" "$INFRA/.env"
  set_env_var "$INFRA/.env" LIVEKIT_API_KEY "$(openssl rand -hex 16)"
  set_env_var "$INFRA/.env" LIVEKIT_API_SECRET "$(openssl rand -hex 32)"
  set_env_var "$INFRA/.env" REDIS_PASSWORD "$(openssl rand -hex 24)"
  chmod 600 "$INFRA/.env"
  ok "generated infra/.env"
fi

LK_KEY=$(env_value "$INFRA/.env" LIVEKIT_API_KEY)
LK_SECRET=$(env_value "$INFRA/.env" LIVEKIT_API_SECRET)
[ -n "$LK_KEY" ] && [ -n "$LK_SECRET" ] || die "infra/.env has no LIVEKIT_API_KEY/SECRET. Fill them in, or delete the file and re-run to generate a fresh pair."

# ---------------------------------------------------------------------------
step "Worker config (agent-worker/.env.local)"
# ---------------------------------------------------------------------------
if [ -f "$WORKER/.env.local" ]; then
  skip ".env.local exists -- only syncing the LiveKit values"
else
  cp "$WORKER/.env.example" "$WORKER/.env.local"
  chmod 600 "$WORKER/.env.local"
  ok "created .env.local from the example"
fi

set_env_var "$WORKER/.env.local" LIVEKIT_URL "ws://localhost:7880"
set_env_var "$WORKER/.env.local" LIVEKIT_API_KEY "$LK_KEY"
set_env_var "$WORKER/.env.local" LIVEKIT_API_SECRET "$LK_SECRET"
ok "LiveKit values synced from infra/.env"

# The credentials gate. Everything above this line the script can do alone;
# nothing below it can proceed without values only you can fetch.
MISSING=()
for key in SUPABASE_URL SUPABASE_SERVICE_ROLE_KEY SUPABASE_DB_URL DEEPGRAM_API_KEY; do
  [ -n "$(env_value "$WORKER/.env.local" "$key")" ] || MISSING+=("$key")
done
if [ -z "$(env_value "$WORKER/.env.local" GEMINI_API_KEY)" ] &&
   [ -z "$(env_value "$WORKER/.env.local" DEEPSEEK_API_KEY)" ]; then
  MISSING+=("GEMINI_API_KEY or DEEPSEEK_API_KEY")
fi

if [ ${#MISSING[@]} -gt 0 ]; then
  printf '\n\033[33mNeeds your credentials before it can go further.\033[0m\n'
  printf 'Edit %s and set:\n\n' "$WORKER/.env.local"
  for key in "${MISSING[@]}"; do printf '    %s\n' "$key"; done
  cat <<'NOTE'

Where they come from:
    SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY   Supabase -> Project Settings -> API
    SUPABASE_DB_URL                            Supabase -> Settings -> Database ->
                                               Connection string -> URI
    DEEPGRAM_API_KEY                           console.deepgram.com
    GEMINI_API_KEY                             aistudio.google.com/apikey

Two things about SUPABASE_DB_URL that waste an afternoon otherwise:
    * Percent-encode the password. Supabase shows the raw one, and a literal
      "@" makes the parser read the rest of it as a hostname -- the error is
      then a DNS failure on a host you have never seen.
          @ -> %40    $ -> %24    + -> %2B
      Easiest cure: reset the database password to letters and digits only.
    * Use port 5432, not 6543. The migrator takes a session-scoped advisory
      lock, which the transaction pooler drops between statements.

Also worth setting now: ADMIN_EMAIL (your address, so you can sign in to the
dashboard on a fresh database).

Then run this script again -- it picks up where it stopped.
NOTE
  exit 2
fi
ok "all required credentials present"

# ---------------------------------------------------------------------------
step "LiveKit, SIP and Redis containers"
# ---------------------------------------------------------------------------
# From infra/, not with an absolute -f. docker-compose.yml interpolates the
# three secrets from infra/.env, and Compose resolves that relative to the
# project directory -- run it from elsewhere and they expand to empty strings,
# so LiveKit's `keys:` map becomes `: ` and refuses every token, silently.
cd "$INFRA"
mkdir -p recordings
docker compose up -d
ok "containers started"
docker compose ps --format '    {{.Service}}\t{{.Status}}' 2>/dev/null || true

# ---------------------------------------------------------------------------
step "Database schema"
# ---------------------------------------------------------------------------
cd "$WORKER"
# Reads SUPABASE_DB_URL and ADMIN_EMAIL from .env.local itself. Detects a fresh
# database from one already built by hand and refuses the latter rather than
# applying 0001 on top of live data -- see the --adopt note it prints.
.venv/bin/python -m worker.migrate

# ---------------------------------------------------------------------------
step "systemd service"
# ---------------------------------------------------------------------------
# WorkingDirectory is load-bearing, not cosmetic: the worker calls
# load_dotenv(".env.local") with a relative path, so a wrong working directory
# means it reads no config at all and every variable looks unset.
UNIT=/etc/systemd/system/$SERVICE.service
if [ -f "$UNIT" ]; then
  skip "$UNIT exists -- leaving it alone"
else
  sudo tee "$UNIT" >/dev/null <<UNITFILE
[Unit]
Description=Kodexo voice agent worker
After=network-online.target docker.service

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$WORKER
ExecStart=$WORKER/.venv/bin/python -m worker.entrypoint start
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNITFILE
  sudo systemctl daemon-reload
  sudo systemctl enable "$SERVICE" >/dev/null
  ok "installed $UNIT"
fi
sudo systemctl restart "$SERVICE"
ok "$SERVICE restarted"

# ---------------------------------------------------------------------------
step "Deploy script"
# ---------------------------------------------------------------------------
# A symlink, so `git pull` updates the script itself rather than leaving a stale
# copy that disagrees with the repo.
sudo mkdir -p /opt/kodexo/deploy
sudo ln -sfn "$INFRA/deploy/deploy-backend.sh" /opt/kodexo/deploy/deploy-backend.sh
ok "/opt/kodexo/deploy/deploy-backend.sh -> repo copy"

# ---------------------------------------------------------------------------
step "Verifying"
# ---------------------------------------------------------------------------
FAILED=0
sleep 3

if [ "$(cd "$INFRA" && docker compose ps --services --filter status=running | wc -l)" -ge 3 ]; then
  ok "containers running"
else
  warn "fewer than 3 containers running -- see: cd $INFRA && docker compose logs"
  FAILED=1
fi

if systemctl is-active --quiet "$SERVICE"; then
  ok "$SERVICE is active"
else
  warn "$SERVICE is not active -- see: journalctl -u $SERVICE -n 40"
  FAILED=1
fi

if cd "$WORKER" && .venv/bin/python -m worker.migrate --dry-run 2>/dev/null | grep -q 'pending: 0'; then
  ok "schema up to date"
else
  warn "schema may not be current -- run: .venv/bin/python -m worker.migrate --dry-run"
  FAILED=1
fi

# ---------------------------------------------------------------------------
printf '\n\033[1m%s\033[0m\n' "Backend is up. What's left needs a human:"
# ---------------------------------------------------------------------------
cat <<CHECKLIST

  1. Firewall / cloud security group -- open these, or a call connects with
     no audio and nothing explains why:
         7880/tcp  7881/tcp  50000-60000/udp  5060/udp  10000-20000/udp

  2. Passwordless sudo for the deploy script, so deploys don't prompt:
         sudo visudo -f /etc/sudoers.d/kodexo-deploy
     one line:
         $(whoami) ALL=(root) NOPASSWD: /bin/systemctl restart $SERVICE, /usr/bin/docker compose *

  3. LiveKit SIP trunk + dispatch rule -- infra/README.md step 4. The rule's
     agentName must match LIVEKIT_AGENT_NAME in .env.local, or LiveKit
     dispatches your calls to nobody.

  4. Twilio: set the Elastic SIP Trunk's Origination URI to
         sip:$(curl -s -m 5 ifconfig.me 2>/dev/null || echo '<this-vps-ip>'):5060

  5. Supabase Auth user matching ADMIN_EMAIL (Authentication -> Users -> Add
     user). allowed_users is only an allowlist; it does not create the login.

  6. Dashboard (separate repo -- needs Node, nginx and a domain):
         git clone https://github.com/aiautomationkodexo/livekit_frontend.git /opt/kodexo/dashboard
         cd /opt/kodexo/dashboard && cp .env.example .env.local   # then fill in
         npm ci && npm run build && npm run start -- -p 3001
     LIVEKIT_API_KEY / LIVEKIT_API_SECRET must be the same pair as above.
     nginx config: infra/nginx/.

  7. In the dashboard, per agent: set status ACTIVE, attach the tools it needs
     (record_lead_info is opt-in -- without it no lead is ever captured, so no
     lead alert can fire), and turn on Slack notifications if you want them.

Then call the number and watch:  journalctl -u $SERVICE -f
Long-form reference, and the troubleshooting table:  DEPLOYMENT.md
CHECKLIST

exit $FAILED
