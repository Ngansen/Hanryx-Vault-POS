#!/usr/bin/env bash
# =============================================================================
# setup-satellite-hostname-fix.sh — fix the hostname collision (Audit #1)
#
# The original setup-satellite-kiosk-boot.sh sets the satellite Pi's hostname
# to "hanryxvault" — exactly the same as the main Pi. That collision causes
# mDNS confusion, Tailscale double-naming, and SSH known_hosts churn.
#
# This script renames the satellite to "hanryxvault-sat" in all the places
# that matter:
#   • /etc/hostname
#   • hostnamectl (system bus)
#   • /etc/hosts
#   • avahi-daemon (mDNS) — restart so it re-publishes the new name
#   • Tailscale — set --hostname=hanryxvault-sat (next tailscale up)
#
# Idempotent. Safe to re-run.
#
# Usage (on the SATELLITE Pi):
#   sudo bash ~/Hanryx-Vault-POS/pi-setup/setup-satellite-hostname-fix.sh
# =============================================================================
set -euo pipefail

NEW_NAME="${1:-hanryxvault-sat}"

if [ "$EUID" -ne 0 ]; then
    echo "Re-running with sudo…"
    exec sudo -E bash "$0" "$@"
fi

CURRENT=$(hostname)
echo ""
echo "  Renaming satellite Pi: '$CURRENT' → '$NEW_NAME'"
echo ""

if [ "$CURRENT" = "$NEW_NAME" ]; then
    echo "[i] Hostname already $NEW_NAME — nothing to do."
    exit 0
fi

# ── 1. /etc/hostname + hostnamectl ──────────────────────────────────────────
echo "$NEW_NAME" > /etc/hostname
hostnamectl set-hostname "$NEW_NAME"
echo "[✓] /etc/hostname + hostnamectl updated"

# ── 2. /etc/hosts ──────────────────────────────────────────────────────────
sed -i "/127\.0\.1\.1/d" /etc/hosts
echo "127.0.1.1  $NEW_NAME ${NEW_NAME}.local" >> /etc/hosts
echo "[✓] /etc/hosts updated"

# ── 3. avahi (mDNS) ────────────────────────────────────────────────────────
if systemctl is-active avahi-daemon >/dev/null 2>&1; then
    systemctl restart avahi-daemon
    echo "[✓] avahi-daemon restarted — now advertising ${NEW_NAME}.local"
else
    echo "[i] avahi not running — skipped"
fi

# ── 4. Tailscale ───────────────────────────────────────────────────────────
if command -v tailscale >/dev/null 2>&1; then
    if tailscale status >/dev/null 2>&1; then
        echo "[→] Updating Tailscale node name to $NEW_NAME…"
        tailscale set --hostname="$NEW_NAME" 2>/dev/null \
            || tailscale up --reset --hostname="$NEW_NAME" 2>/dev/null \
            || echo "[!] Tailscale rename failed — run manually: sudo tailscale up --reset --hostname=$NEW_NAME"
        echo "[✓] Tailscale node renamed"
    else
        echo "[i] Tailscale not authenticated — name will apply on next 'tailscale up'"
    fi
else
    echo "[i] Tailscale not installed — skipped"
fi

# ── 5. Update satellite.conf if it has stale references ────────────────────
for u in "ngansen" "pi"; do
    CONF="/home/$u/.hanryx/satellite.conf"
    if [ -f "$CONF" ] && grep -q "^MAIN_PI_TS_HOST=hanryxvault\$" "$CONF"; then
        echo "[i] satellite.conf has MAIN_PI_TS_HOST=hanryxvault — that's the MAIN Pi name (correct), leaving it alone"
    fi
done

# ── 6. Done ─────────────────────────────────────────────────────────────────
echo ""
echo "  ============================================================"
echo "  Hostname fix complete."
echo ""
echo "  • This Pi is now: $NEW_NAME"
echo "  • mDNS:           ${NEW_NAME}.local"
echo "  • Tailscale:      $NEW_NAME (use 'tailscale status' to confirm)"
echo ""
echo "  Recommended next steps:"
echo "    1. From your laptop: ssh ${SUDO_USER:-pi}@${NEW_NAME}.local"
echo "       (the old name 'hanryxvault.local' will now point to the MAIN Pi only)"
echo "    2. Reboot to ensure all services re-register with the new name:"
echo "       sudo reboot"
echo ""
