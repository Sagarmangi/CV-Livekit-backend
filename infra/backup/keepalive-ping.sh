#!/usr/bin/env bash
# Inserts one row into `keepalive` via the REST API, to keep the Supabase
# free-tier project from auto-pausing on inactivity. Run every few days via cron.
set -euo pipefail
cd "$(dirname "$0")"
set -a; source ./.env; set +a

curl -sf -X POST "${SUPABASE_URL}/rest/v1/keepalive" \
  -H "apikey: ${SUPABASE_SERVICE_ROLE_KEY}" \
  -H "Authorization: Bearer ${SUPABASE_SERVICE_ROLE_KEY}" \
  -H "Content-Type: application/json" \
  -d '{}' \
  && echo "Keepalive ping ok: $(date -Iseconds)"
