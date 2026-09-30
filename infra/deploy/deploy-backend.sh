#!/usr/bin/env bash
#
# Pull main and restart whatever changed. Run on the VPS as the deploy user:
#
#     /opt/kodexo/deploy/deploy-backend.sh
#
# This lives in the repo so its failure modes are visible to anyone reading the
# code. Install it by symlinking, so a `git pull` updates it too:
#
#     ln -sf /opt/kodexo/backend/infra/deploy/deploy-backend.sh \
#            /opt/kodexo/deploy/deploy-backend.sh
#
set -euo pipefail

REPO=/opt/kodexo/backend
WORKER="$REPO/agent-worker"

# NEVER run this with sudo.
#
# The two privileged actions below use `sudo -n` for themselves; git is meant to
# run as the deploy user. Running the whole script as root makes git write
# objects into .git/objects owned by root, and every later unprivileged pull
# then dies with "insufficient permission for adding an object to repository
# database" -- which reads like a git bug and is really this. Recovering means
# chown -R'ing the checkout back.
if [ "$(id -u)" -eq 0 ]; then
  echo "Refusing to run as root. Run as the deploy user; the two steps that" >&2
  echo "need privileges call sudo -n themselves. See the comment in this file." >&2
  exit 1
fi

cd "$REPO"

BEFORE=$(git rev-parse HEAD)
git fetch origin main
git reset --hard origin/main
AFTER=$(git rev-parse HEAD)

if [ "$BEFORE" = "$AFTER" ]; then
  echo "Already at $(git rev-parse --short HEAD); nothing fetched."
else
  echo "Updated $(git rev-parse --short "$BEFORE") -> $(git rev-parse --short "$AFTER")"
fi

CHANGED=$(git diff --name-only "$BEFORE" "$AFTER")

# `supabase/` counts as well as `agent-worker/`, and that is the point. A
# schema-only commit used to change nothing here, so the migration was left for
# somebody to remember -- which is how the database ended up behind the code
# twice, with insert_call_log logging and swallowing the error and whole call
# records disappearing.
if echo "$CHANGED" | grep -qE '^(agent-worker|supabase)/'; then
  echo "agent-worker or supabase changed; installing and migrating..."
  cd "$WORKER"
  .venv/bin/pip install -e . --quiet

  # BEFORE the restart, deliberately. `set -e` means a failed migration aborts
  # the deploy here, leaving the old worker running against the schema it was
  # built for. New code against an old schema is the worse outcome: it fails
  # inside insert_call_log, which logs and swallows, so calls are lost silently.
  #
  # Needs SUPABASE_DB_URL in agent-worker/.env.local -- see that file's
  # .env.example, and mind the URL-encoding note there.
  .venv/bin/python -m worker.migrate

  sudo -n systemctl restart kodexo-worker
  echo "worker restarted"
fi

if echo "$CHANGED" | grep -q '^infra/'; then
  # Recreates the LiveKit/SIP containers, which drops anyone mid-call.
  echo "infra changed; recreating containers (this interrupts active calls)..."

  # `cd` into infra rather than pointing -f at it from wherever the previous
  # branch left the working directory. docker-compose.yml interpolates
  # ${LIVEKIT_API_KEY}, ${LIVEKIT_API_SECRET} and ${REDIS_PASSWORD} from
  # infra/.env, and Compose resolves that file relative to the project
  # directory. Run it from the wrong directory and those expand to empty
  # strings: Redis comes up with `--requirepass ""`, LiveKit's `keys:` map
  # becomes `: ` and the server refuses every token. Nothing warns; the
  # containers just start and no call can authenticate.
  cd "$REPO/infra"
  sudo -n docker compose up -d
fi

echo "Backend deployed: $(git rev-parse --short HEAD)"
