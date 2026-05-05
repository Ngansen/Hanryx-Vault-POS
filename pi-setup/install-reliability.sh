#!/usr/bin/env bash
# =============================================================================
# install-reliability.sh — install the HanryxVault self-heal stack
#
# Idempotent. Run once on EACH Pi (main + satellite).
#
# What it installs:
#   • /usr/local/bin/hanryx-recover  → wrapper for pi-setup/recover.sh
#   • systemd unit  hanryx-heal.service   (runs recover.sh --quiet)
#   • systemd timer hanryx-heal.timer     (every 2 min, 30s after boot)
#   • systemd unit  hanryx-boot.service   (blocks until healthy at boot)
#   • Persists current WiFi creds + adds a documented "phone-hotspot" fallback
#
# Run:
#   sudo bash ~/Hanryx-Vault-POS/pi-setup/install-reliability.sh
# =============================================================================
set -euo pipefail

REPO_DIR="${REPO_DIR:-/home/ngansen/Hanryx-Vault-POS}"
SYSD_SRC="$REPO_DIR/pi-setup/systemd"

if [ "$EUID" -ne 0 ]; then
    echo "Re-running with sudo…"
    exec sudo -E bash "$0" "$@"
fi

echo "[→] Installing recover.sh wrapper at /usr/local/bin/hanryx-recover"
cat > /usr/local/bin/hanryx-recover <<EOF
#!/usr/bin/env bash
exec /bin/bash $REPO_DIR/pi-setup/recover.sh "\$@"
EOF
chmod +x /usr/local/bin/hanryx-recover
chmod +x "$REPO_DIR/pi-setup/recover.sh"
echo "[✓] you can now run 'hanryx-recover' from anywhere"

echo "[→] Installing systemd units"
install -m 0644 "$SYSD_SRC/hanryx-heal.service" /etc/systemd/system/hanryx-heal.service
install -m 0644 "$SYSD_SRC/hanryx-heal.timer"   /etc/systemd/system/hanryx-heal.timer
install -m 0644 "$SYSD_SRC/hanryx-boot.service" /etc/systemd/system/hanryx-boot.service

# Postgres backup units (only on Pis that run docker — installer is idempotent
# either way; the service has ConditionPathIsMountPoint=/mnt/cards so it
# silently skips on satellites without the USB drive).
if [ -f "$SYSD_SRC/hanryx-postgres-backup.service" ]; then
    install -m 0644 "$SYSD_SRC/hanryx-postgres-backup.service" /etc/systemd/system/hanryx-postgres-backup.service
    install -m 0644 "$SYSD_SRC/hanryx-postgres-backup.timer"   /etc/systemd/system/hanryx-postgres-backup.timer
fi
chmod +x "$REPO_DIR/pi-setup/scripts/postgres-backup.sh" 2>/dev/null || true
chmod +x "$REPO_DIR/pi-setup/scripts/postgres-restore.sh" 2>/dev/null || true
chmod +x "$REPO_DIR/pi-setup/scripts/preflight-usb-check.sh" 2>/dev/null || true

systemctl daemon-reload

echo "[→] Enabling timers + boot service"
systemctl enable --now hanryx-heal.timer
systemctl enable --now hanryx-boot.service
if [ -f /etc/systemd/system/hanryx-postgres-backup.timer ] && command -v docker >/dev/null 2>&1; then
    systemctl enable --now hanryx-postgres-backup.timer
    echo "[✓] postgres backup timer enabled (hourly)"
fi

echo ""
echo "[i] Status:"
systemctl status --no-pager hanryx-heal.timer | head -10 || true
echo ""
systemctl list-timers --no-pager | grep -E 'NEXT|hanryx' || true

echo ""
echo "[✓] Reliability stack installed"
echo ""
echo "  Manual recovery:           hanryx-recover"
echo "  Quiet recovery (one line): hanryx-recover --quiet"
echo "  Just fix kiosks:           hanryx-recover --kiosk-only"
echo "  Watch the heal log:        tail -f /var/log/hanryx-recover.log"
echo "  See last status:           cat /run/hanryx-status"
echo ""
