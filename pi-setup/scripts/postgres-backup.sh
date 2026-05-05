#!/usr/bin/env bash
# =============================================================================
# postgres-backup.sh — automated PostgreSQL backup to /mnt/cards/backups
#
# Fixes Audit M3: until now, only the SQLite USB mirror was backed up.
# A corrupted Postgres meant losing every sale and inventory change.
#
# What it does:
#   • pg_dump the live POS database to /mnt/cards/backups/postgres/
#   • Compress with zstd (faster than gzip, better ratio)
#   • Rotate: keep 24 hourly + 14 daily + 6 monthly
#   • Verify the backup actually opens before declaring success
#   • Write status to /run/hanryx-postgres-backup.status for monitoring
#
# Triggered by: hanryx-postgres-backup.timer (hourly)
# Manual run:   sudo bash pi-setup/scripts/postgres-backup.sh
# =============================================================================
set -u

REPO_DIR="${REPO_DIR:-/home/ngansen/Hanryx-Vault-POS}"
BACKUP_ROOT="${BACKUP_ROOT:-/mnt/cards/backups/postgres}"
RETENTION_HOURLY="${RETENTION_HOURLY:-24}"
RETENTION_DAILY="${RETENTION_DAILY:-14}"
RETENTION_MONTHLY="${RETENTION_MONTHLY:-6}"
STATUS_FILE=/run/hanryx-postgres-backup.status

ts() { date +%Y%m%d-%H%M%S; }
log() { echo "[$(date -Is)] $*"; }

# ── Pre-flight: USB mounted? ────────────────────────────────────────────────
if ! mountpoint -q /mnt/cards 2>/dev/null; then
    log "FATAL: /mnt/cards is not mounted — refusing to write backups to SD card"
    echo "FAIL $(date +%s) usb-not-mounted" > "$STATUS_FILE" 2>/dev/null || true
    exit 2
fi

mkdir -p "$BACKUP_ROOT/hourly" "$BACKUP_ROOT/daily" "$BACKUP_ROOT/monthly"

# ── Find the running postgres container ────────────────────────────────────
PG_CONTAINER=$(docker ps --format '{{.Names}}' --filter 'name=db' \
    --filter 'status=running' | head -1)
if [ -z "$PG_CONTAINER" ]; then
    PG_CONTAINER=$(docker ps --format '{{.Names}}' \
        | grep -E '(postgres|pgvector|^db$|hanryx.*db)' | head -1)
fi
if [ -z "$PG_CONTAINER" ]; then
    log "FATAL: no running postgres container found"
    echo "FAIL $(date +%s) no-pg-container" > "$STATUS_FILE" 2>/dev/null || true
    exit 3
fi
log "Postgres container: $PG_CONTAINER"

# ── Discover db name + user from the container env ─────────────────────────
PG_USER=$(docker exec "$PG_CONTAINER" sh -c 'echo $POSTGRES_USER' 2>/dev/null \
    | tr -d '\r\n' || true)
PG_USER="${PG_USER:-postgres}"
PG_DB=$(docker exec "$PG_CONTAINER" sh -c 'echo $POSTGRES_DB' 2>/dev/null \
    | tr -d '\r\n' || true)
PG_DB="${PG_DB:-hanryxvault}"
log "Dumping db=$PG_DB user=$PG_USER"

# ── Dump ────────────────────────────────────────────────────────────────────
TS=$(ts)
TMP="$BACKUP_ROOT/.in-progress-${TS}.sql"
FINAL="$BACKUP_ROOT/hourly/pos-${TS}.sql.zst"

# Stream from container -> zstd -> file. -9 takes a few seconds even for 500MB
# but -19 would block writes too long; -9 is the sweet spot for hourly.
if ! docker exec "$PG_CONTAINER" pg_dump \
        -U "$PG_USER" \
        --no-owner --no-privileges --clean --if-exists \
        "$PG_DB" 2>/dev/null \
        | zstd -9 -T0 -q -o "$TMP" 2>/dev/null; then
    log "FATAL: pg_dump | zstd failed"
    rm -f "$TMP"
    echo "FAIL $(date +%s) dump-failed" > "$STATUS_FILE" 2>/dev/null || true
    exit 4
fi

# ── Verify the dump actually opens ──────────────────────────────────────────
SIZE=$(stat -c%s "$TMP" 2>/dev/null || echo 0)
if [ "$SIZE" -lt 1024 ]; then
    log "FATAL: dump file suspiciously small ($SIZE bytes)"
    rm -f "$TMP"
    echo "FAIL $(date +%s) dump-too-small" > "$STATUS_FILE" 2>/dev/null || true
    exit 5
fi
if ! zstd -t -q "$TMP" 2>/dev/null; then
    log "FATAL: zstd verification failed for $TMP"
    rm -f "$TMP"
    echo "FAIL $(date +%s) zstd-verify-failed" > "$STATUS_FILE" 2>/dev/null || true
    exit 6
fi

mv "$TMP" "$FINAL"
log "Backup written: $FINAL ($(numfmt --to=iec "$SIZE"))"

# ── Promote: copy hourly to daily/monthly on the appropriate schedule ──────
HOUR=$(date +%H)
DAY=$(date +%d)
if [ "$HOUR" = "03" ]; then
    cp "$FINAL" "$BACKUP_ROOT/daily/pos-$(date +%Y%m%d).sql.zst"
    log "Promoted to daily"
fi
if [ "$DAY" = "01" ] && [ "$HOUR" = "03" ]; then
    cp "$FINAL" "$BACKUP_ROOT/monthly/pos-$(date +%Y%m).sql.zst"
    log "Promoted to monthly"
fi

# ── Rotate ──────────────────────────────────────────────────────────────────
prune() {
    local dir="$1" keep="$2"
    ls -1t "$dir"/pos-*.sql.zst 2>/dev/null | tail -n +"$((keep+1))" | xargs -r rm -f
}
prune "$BACKUP_ROOT/hourly"  "$RETENTION_HOURLY"
prune "$BACKUP_ROOT/daily"   "$RETENTION_DAILY"
prune "$BACKUP_ROOT/monthly" "$RETENTION_MONTHLY"

# ── Free-space sanity ──────────────────────────────────────────────────────
AVAIL=$(df --output=avail -B1 /mnt/cards 2>/dev/null | tail -1 | tr -d ' ')
if [ -n "$AVAIL" ] && [ "$AVAIL" -lt 5368709120 ]; then  # < 5 GB
    log "WARN: /mnt/cards has less than 5GB free — consider increasing retention pruning"
fi

echo "OK $(date +%s) ${SIZE} ${FINAL}" > "$STATUS_FILE" 2>/dev/null || true
log "Backup complete"
exit 0
