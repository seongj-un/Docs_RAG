#!/usr/bin/env bash
# Dump the database to backups/ .
#
# pg_dump runs *inside* the db container on purpose: the client must not be
# older than the server, and the container always ships the matching version.
# Dumping from the host works until someone's local psql drifts, and then it
# fails at the worst possible moment.
#
#   scripts/db_backup.sh            # -> backups/docs_rag-<utc>.dump
#   BACKUP_DIR=/mnt/x scripts/db_backup.sh
set -euo pipefail

cd "$(dirname "$0")/.."

DB_NAME="${DB_NAME:-docs_rag}"
DB_USER="${DB_USER:-postgres}"
BACKUP_DIR="${BACKUP_DIR:-backups}"
KEEP="${KEEP:-7}"

mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$BACKUP_DIR/${DB_NAME}-${stamp}.dump"

# -Fc (custom) rather than plain SQL: compressed, and pg_restore can then be
# selective, which is what you want when recovering one table at 3am.
docker compose exec -T db pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$out"

# A dump that cannot be listed is not a backup. Checking here means the failure
# surfaces at backup time rather than at restore time.
#
# The file has to go back into the container to be checked: a custom-format
# archive is read by seeking, and a pipe cannot seek — `pg_restore --list` on
# stdin fails with "did not find magic string in file header" even when the
# dump is perfectly good. Verifying the copied-out file also covers the
# transfer, not just what pg_dump produced.
if ! docker compose cp "$out" db:/tmp/verify.dump >/dev/null 2>&1 \
   || ! docker compose exec -T db pg_restore --list /tmp/verify.dump >/dev/null 2>&1; then
    docker compose exec -T db rm -f /tmp/verify.dump >/dev/null 2>&1 || true
    echo "backup unreadable, removing: $out" >&2
    rm -f "$out"
    exit 1
fi
docker compose exec -T db rm -f /tmp/verify.dump >/dev/null 2>&1 || true

size="$(du -h "$out" | cut -f1)"
echo "$out ($size)"

# Keep the last $KEEP dumps. Retention lives with the backup so a forgotten
# cron job cannot quietly fill the disk.
ls -1t "$BACKUP_DIR/${DB_NAME}-"*.dump 2>/dev/null | tail -n "+$((KEEP + 1))" | while read -r old; do
    echo "expiring $old"
    rm -f "$old"
done
