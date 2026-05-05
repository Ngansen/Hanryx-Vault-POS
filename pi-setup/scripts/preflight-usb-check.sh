#!/usr/bin/env bash
# =============================================================================
# preflight-usb-check.sh — verify /mnt/cards is real before docker starts
#
# Fixes Audit M5: the postgres bind-mount points at ${DB_DATA_DIR:-/mnt/cards/postgres-data}.
# If the USB drive failed to mount at boot, that path is just an empty dir
# on the SD card. Postgres starts, sees no data, initialises a NEW empty
# cluster on the SD card, and your live data is invisible. Sales recorded
# during this window are written to the wrong place.
#
# Run as a systemd ExecStartPre on the main hanryxvault.service (or any
# compose-up wrapper) to refuse to start when the USB isn't mounted.
#
# Exit codes:
#   0  → USB mounted, safe to proceed
#   1  → USB NOT mounted; remediation hints printed
#   2  → USB mounted but appears to be the wrong drive (no postgres-data subdir
#        AND no marker file from a previous correct mount)
# =============================================================================
set -u

MOUNT="${MOUNT:-/mnt/cards}"
MARKER="$MOUNT/.hanryx-vault-usb"
PG_DIR="$MOUNT/postgres-data"

echo "[preflight] checking $MOUNT…"

if ! mountpoint -q "$MOUNT" 2>/dev/null; then
    echo "[preflight] FAIL: $MOUNT is not a mount point"
    echo "[preflight]   • Check 'lsblk' — your USB-NVMe dock should show as /dev/sda"
    echo "[preflight]   • Check 'dmesg | tail -30' for USB errors"
    echo "[preflight]   • Try: sudo mount /mnt/cards"
    echo "[preflight]   • If new disk, run: sudo bash pi-setup/usb_resilience_setup.sh"
    exit 1
fi

# Mounted — now confirm it's THE drive, not some other USB stick
if [ ! -f "$MARKER" ] && [ ! -d "$PG_DIR" ] && [ ! -d "$MOUNT/cards-master" ]; then
    echo "[preflight] WARN: $MOUNT is mounted but contains no HanryxVault data"
    echo "[preflight]   • This may be a different USB drive than expected"
    echo "[preflight]   • Expected to find one of: $MARKER, $PG_DIR, $MOUNT/cards-master"
    echo "[preflight]   • Contents of $MOUNT:"
    ls -la "$MOUNT" 2>/dev/null | head -10 | sed 's/^/[preflight]     /'
    exit 2
fi

# All good — drop the marker if it doesn't exist yet
if [ ! -f "$MARKER" ]; then
    echo "HanryxVault USB drive — created $(date -Is) on $(hostname)" > "$MARKER" 2>/dev/null || true
fi

# Sanity: writable?
if ! touch "$MOUNT/.hanryx-write-test" 2>/dev/null; then
    echo "[preflight] FAIL: $MOUNT is mounted READ-ONLY (USB error?)"
    echo "[preflight]   • dmesg may show 'Remounting filesystem read-only'"
    echo "[preflight]   • Try: sudo mount -o remount,rw $MOUNT"
    exit 1
fi
rm -f "$MOUNT/.hanryx-write-test"

# Free space
AVAIL_GB=$(df --output=avail -BG "$MOUNT" 2>/dev/null | tail -1 | tr -d 'G ')
if [ -n "$AVAIL_GB" ] && [ "$AVAIL_GB" -lt 10 ]; then
    echo "[preflight] WARN: only ${AVAIL_GB}GB free on $MOUNT"
fi

echo "[preflight] OK: $MOUNT mounted and writable (${AVAIL_GB:-?}GB free)"
exit 0
