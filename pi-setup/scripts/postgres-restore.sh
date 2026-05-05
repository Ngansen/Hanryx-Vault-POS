#!/usr/bin/env bash
# =============================================================================
# postgres-restore.sh — restore a pg_dump produced by postgres-backup.sh
#
# Usage:
#   sudo bash postgres-restore.sh                          # interactive picker
#   sudo bash postgres-restore.sh /path/to/pos-...sql.zst  # explicit file
#   sudo bash postgres-restore.sh --latest                 # most recent hourly
#
# Restores into the SAME running postgres container the backup came from.
# DROPS the existing database first (the dump uses --clean --if-exists).
# Requires: a confirmation prompt unless --yes is passed.
# =============================================================================
set -u

BACKUP_ROOT="${BACKUP_ROOT:-/mnt/cards/backups/postgres}"
YES=0
FILE=""

for arg in "$@"; do
    case "$arg" in
        --latest) FILE=$(ls -1t "$BACKUP_ROOT/hourly"/pos-*.sql.zst 2>/dev/null | head -1) ;;
        --yes|-y) YES=1 ;;
        -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
        *) [ -z "$FILE" ] && FILE="$arg" ;;
    esac
done

if [ -z "$FILE" ]; then
    echo "Available backups:"
    for d in hourly daily monthly; do
        echo "  ── $d ──"
        ls -1t "$BACKUP_ROOT/$d"/pos-*.sql.zst 2>/dev/null | head -10 | sed 's/^/    /'
    done
    echo ""
    read -rp "Path to backup file (or --latest): " FILE
    [ "$FILE" = "--latest" ] && FILE=$(ls -1t "$BACKUP_ROOT/hourly"/pos-*.sql.zst | head -1)
fi

[ -f "$FILE" ] || { echo "FATAL: file not found: $FILE"; exit 2; }

PG_CONTAINER=$(docker ps --format '{{.Names}}' --filter 'name=db' \
    --filter 'status=running' | head -1)
PG_CONTAINER="${PG_CONTAINER:-$(docker ps --format '{{.Names}}' \
    | grep -E '(postgres|pgvector|^db$|hanryx.*db)' | head -1)}"
[ -n "$PG_CONTAINER" ] || { echo "FATAL: no running postgres container"; exit 3; }

PG_USER=$(docker exec "$PG_CONTAINER" sh -c 'echo $POSTGRES_USER' | tr -d '\r\n')
PG_USER="${PG_USER:-postgres}"
PG_DB=$(docker exec "$PG_CONTAINER" sh -c 'echo $POSTGRES_DB' | tr -d '\r\n')
PG_DB="${PG_DB:-hanryxvault}"

echo ""
echo "  About to restore:"
echo "    File:      $FILE  ($(numfmt --to=iec "$(stat -c%s "$FILE")"))"
echo "    Into:      container=$PG_CONTAINER  db=$PG_DB  user=$PG_USER"
echo "    Effect:    DROPS existing tables and replaces them with backup contents"
echo ""

if [ $YES -ne 1 ]; then
    read -rp "  Type the database name '$PG_DB' to confirm: " CONFIRM
    [ "$CONFIRM" = "$PG_DB" ] || { echo "Aborted."; exit 1; }
fi

echo "[→] Restoring…"
zstd -dc "$FILE" | docker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1
echo "[✓] Restore complete"
