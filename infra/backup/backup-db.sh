#!/usr/bin/env bash
# Daily Supabase backup -> Backblaze B2. Run via cron on the VPS.
# Requires: docker (for a version-matched pg_dump client) or a local pg_dump
# matching the server's major version, plus the AWS CLI configured against B2.
set -euo pipefail
cd "$(dirname "$0")"
set -a; source ./.env; set +a

DATE="$(date +%F)"
OUT="/tmp/kodexo-backup-${DATE}.sql.gz"

# public schema only: Supabase's own internal schemas (auth, storage, realtime,
# etc.) aren't this app's data and dumping them through the pooler is where
# pg_dump previously hung -- --schema=public avoids that entirely.
docker run --rm postgres:17-alpine \
  pg_dump "${SUPABASE_DB_URL}?sslmode=require" --schema=public --no-owner --no-privileges \
  | gzip > "$OUT"

aws s3 cp "$OUT" "s3://${B2_BUCKET}/" --endpoint-url "https://${B2_ENDPOINT}"

# Prune anything older than BACKUP_RETENTION_DAYS, locally and in the bucket.
find /tmp -maxdepth 1 -name 'kodexo-backup-*.sql.gz' -mtime +"$BACKUP_RETENTION_DAYS" -delete

CUTOFF="$(date -d "-${BACKUP_RETENTION_DAYS} days" +%F)"
aws s3api list-objects-v2 --bucket "$B2_BUCKET" --endpoint-url "https://${B2_ENDPOINT}" \
  --query "Contents[?LastModified<'${CUTOFF}'].Key" --output text \
  | tr '\t' '\n' \
  | while read -r key; do
      [ -n "$key" ] && aws s3 rm "s3://${B2_BUCKET}/${key}" --endpoint-url "https://${B2_ENDPOINT}"
    done

echo "Backup complete: ${OUT} -> s3://${B2_BUCKET}/"
