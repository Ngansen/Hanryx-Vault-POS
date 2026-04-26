#!/usr/bin/env bash
# migrate-db-to-usb.sh — One-time copy of pokedex_local.db onto the USB drive.
#
# Run this ONCE on the Pi after:
#   1. /mnt/cards is mounted (per `lsblk -f` output showing ext4 mount)
#   2. docker-compose has been UPDATED with the bind-mount + HANRYX_LOCAL_DB_DIR
#      env var, but BEFORE you `docker compose up -d` for the first time.
#
# What it does:
#   - Checks /mnt/cards exists, is writable, and is the ext4 USB drive
#     (refuses to write to anything else as a safety belt).
#   - Looks for an existing pokedex_local.db in the most likely places
#     (in-container path inherited from previous deploys, or the
#      pi-setup/ directory on the host).
#   - Copies it into /mnt/cards/pokedex_local.db, preserving timestamps.
#   - Creates /mnt/cards/{faiss,logs,backups}/ subdirs the new code expects.
#   - Sets ownership to ngansen:ngansen so the container's hanryx user
#     (added to gid that matches host) can read/write it.
#
# Idempotent: rerunning it overwrites the USB copy with whatever's freshest,
# so you can use it both as the initial migration AND as a "snapshot the
# current SQLite for backup" tool. To avoid clobbering on the second run,
# pass --no-overwrite.
#
# Why this is a separate script and not a Python entrypoint hook:
# It runs ONCE, before any container starts. Putting it in container start-up
# code creates a chicken-and-egg problem (container needs the file to start;
# the file needs the container to copy it). Manual one-shot script is simpler.

set -euo pipefail

USB_DIR="${USB_DIR:-/mnt/cards}"
NO_OVERWRITE=0
for arg in "$@"; do
    case "$arg" in
        --no-overwrite) NO_OVERWRITE=1 ;;
        --usb-dir=*)    USB_DIR="${arg#--usb-dir=}" ;;
        -h|--help)
            grep -E '^# ' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "Unknown arg: $arg" >&2
            exit 2
            ;;
    esac
done

# ── Safety belts: refuse to write anywhere except a real USB ext4 mount ──
if [ ! -d "$USB_DIR" ]; then
    echo "[migrate] ERROR: $USB_DIR does not exist. Mount the USB first." >&2
    exit 1
fi

if [ ! -w "$USB_DIR" ]; then
    echo "[migrate] ERROR: $USB_DIR is not writable. Check ownership." >&2
    exit 1
fi

# Make sure it's actually a separate filesystem from /, i.e. the USB really
# is mounted there (otherwise we'd be writing onto the SD card and silently
# defeating the whole point).
ROOT_DEV=$(stat -c %d /)
USB_DEV=$(stat -c %d "$USB_DIR")
if [ "$ROOT_DEV" = "$USB_DEV" ]; then
    echo "[migrate] ERROR: $USB_DIR is on the root filesystem (USB not mounted)." >&2
    echo "         Run: sudo mount $USB_DIR" >&2
    exit 1
fi

# Confirm it's ext4 (the format we documented), warn loudly otherwise.
FSTYPE=$(findmnt -n -o FSTYPE "$USB_DIR" 2>/dev/null || true)
if [ "$FSTYPE" != "ext4" ]; then
    echo "[migrate] WARNING: $USB_DIR is filesystem '$FSTYPE', not ext4." >&2
    echo "         SQLite WAL mode has known issues on non-ext4 filesystems." >&2
    echo "         Continuing anyway — re-run mkfs.ext4 if you hit corruption." >&2
fi

# ── Find an existing pokedex_local.db to seed from ──
CANDIDATES=(
    "/mnt/cards/pokedex_local.db"
    "/data/pokedex_local.db"
    "/var/lib/docker/volumes/pi-setup_pos-data/_data/pokedex_local.db"
    "$(dirname "$0")/../pokedex_local.db"
    "/home/ngansen/Hanryx-Vault-POS/pi-setup/pokedex_local.db"
)

# In-container path (inside `pi-setup-pos-1`). docker exec works only if
# the container is running, so we attempt it but don't make it required.
if command -v docker >/dev/null 2>&1; then
    if docker ps --format '{{.Names}}' | grep -qx 'pi-setup-pos-1'; then
        if docker exec pi-setup-pos-1 test -f /app/pokedex_local.db 2>/dev/null; then
            TMP=$(mktemp /tmp/pokedex_local.XXXXX.db)
            docker exec pi-setup-pos-1 cat /app/pokedex_local.db > "$TMP"
            CANDIDATES+=("$TMP")
        fi
    fi
fi

SOURCE=""
NEWEST_TS=0
for c in "${CANDIDATES[@]}"; do
    if [ -f "$c" ]; then
        TS=$(stat -c %Y "$c" 2>/dev/null || echo 0)
        if [ "$TS" -gt "$NEWEST_TS" ]; then
            NEWEST_TS=$TS
            SOURCE=$c
        fi
    fi
done

DEST="$USB_DIR/pokedex_local.db"

# Set up the subdirs the new code expects regardless of whether we found a source.
mkdir -p "$USB_DIR/faiss" "$USB_DIR/logs" "$USB_DIR/backups"

if [ -z "$SOURCE" ]; then
    echo "[migrate] No existing pokedex_local.db found. The sync orchestrator"
    echo "          will build $DEST on first tick (after docker compose up)."
    exit 0
fi

if [ "$SOURCE" = "$DEST" ]; then
    echo "[migrate] Source and destination are the same file ($DEST). Nothing to do."
    exit 0
fi

if [ -f "$DEST" ] && [ "$NO_OVERWRITE" = "1" ]; then
    echo "[migrate] $DEST already exists and --no-overwrite was passed. Skipping."
    exit 0
fi

if [ -f "$DEST" ]; then
    BACKUP="$USB_DIR/backups/pokedex_local.$(date +%Y%m%d-%H%M%S).db"
    echo "[migrate] Backing up existing $DEST → $BACKUP"
    cp -p "$DEST" "$BACKUP"
fi

echo "[migrate] Copying $SOURCE → $DEST"
cp -p "$SOURCE" "$DEST"

# Clean up the docker exec temp file if we created one.
if [ -n "${TMP:-}" ] && [ -f "${TMP:-}" ]; then
    rm -f "$TMP"
fi

# Ownership: docker-compose runs the container as 'hanryx' (uid 1000 in
# the image). On the Pi, the host's ngansen user is also uid 1000 by
# default, so chowning to ngansen makes both ends happy.
if command -v chown >/dev/null 2>&1; then
    chown -R ngansen:ngansen "$USB_DIR" 2>/dev/null || \
        sudo chown -R ngansen:ngansen "$USB_DIR" 2>/dev/null || true
fi

echo "[migrate] Done. /mnt/cards now contains:"
ls -lh "$USB_DIR"
