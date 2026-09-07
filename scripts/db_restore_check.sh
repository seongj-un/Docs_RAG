#!/usr/bin/env bash
# Restore a dump into a scratch database and prove it came back intact.
#
# This is the rehearsal, not the recovery. It never writes to the live
# database: it restores into a throwaway one, compares row counts against the
# source, and drops it again. An untested backup is not a backup, and the
# usual way to find that out is the day you need it.
#
#   scripts/db_restore_check.sh                 # newest dump in backups/
#   scripts/db_restore_check.sh backups/x.dump
set -euo pipefail

cd "$(dirname "$0")/.."

DB_NAME="${DB_NAME:-docs_rag}"
DB_USER="${DB_USER:-postgres}"
BACKUP_DIR="${BACKUP_DIR:-backups}"
SCRATCH="${SCRATCH:-${DB_NAME}_restore_check}"

if [ "$SCRATCH" = "$DB_NAME" ]; then
    echo "refusing to restore over the live database ($DB_NAME)" >&2
    exit 2
fi

dump="${1:-$(ls -1t "$BACKUP_DIR/${DB_NAME}-"*.dump 2>/dev/null | head -1 || true)}"
if [ -z "$dump" ] || [ ! -f "$dump" ]; then
    echo "no dump found (looked in $BACKUP_DIR/)" >&2
    exit 1
fi
echo "restoring $dump into $SCRATCH"

psql_db() { docker compose exec -T db psql -U "$DB_USER" -d "$1" -tA -c "$2"; }

counts() {
    psql_db "$1" "
      SELECT 'users='      || (SELECT count(*) FROM users)
        || ' documents='   || (SELECT count(*) FROM documents)
        || ' chunks='      || (SELECT count(*) FROM chunks)
        || ' conversations='|| (SELECT count(*) FROM conversations)
        || ' schema='      || (SELECT version_num FROM alembic_version);"
}

before="$(counts "$DB_NAME" | tr -d '\r')"

# The scratch database and the copied-in dump go away however this exits —
# including a failed restore, which is exactly when leaving debris is easiest.
cleanup() {
    docker compose exec -T db dropdb -U "$DB_USER" --if-exists --force "$SCRATCH" >/dev/null 2>&1 || true
    docker compose exec -T db rm -f /tmp/restore_check.dump >/dev/null 2>&1 || true
}
trap cleanup EXIT

cleanup  # in case a previous run was killed before its trap fired
docker compose exec -T db createdb -U "$DB_USER" "$SCRATCH"
# Copied in rather than piped: a custom-format archive is read by seeking and
# a pipe cannot seek.
docker compose cp "$dump" db:/tmp/restore_check.dump >/dev/null
docker compose exec -T db pg_restore -U "$DB_USER" -d "$SCRATCH" --no-owner /tmp/restore_check.dump

after="$(counts "$SCRATCH" | tr -d '\r')"

echo "  원본: $before"
echo "  복구: $after"

if [ "$before" != "$after" ]; then
    echo "RESTORE CHECK FAILED — 복구본이 원본과 다릅니다" >&2
    exit 1
fi

# Row counts alone would pass on a table full of nulls, so also read one
# embedding back: it is the column most likely to survive a dump badly.
dim="$(psql_db "$SCRATCH" \
  "SELECT vector_dims(embedding) FROM chunks WHERE embedding IS NOT NULL LIMIT 1;" | tr -d '\r')"
echo "  임베딩 차원: ${dim:-<없음>}"
if [ -n "$dim" ] && [ "$dim" != "1024" ]; then
    echo "RESTORE CHECK FAILED — 임베딩이 온전하지 않습니다 (dims=$dim)" >&2
    exit 1
fi

echo "RESTORE CHECK OK"
