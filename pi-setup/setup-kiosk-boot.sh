#!/usr/bin/env bash
# =============================================================================
# HanryxVault — Kiosk Boot Setup
# Configures the Pi to:
#   1. Auto-start Docker Compose (POS server) on every boot
#   2. Auto-launch Chromium in kiosk mode pointing to /kiosk
#   3. Disable screen sleep / blanking
#   4. Boot to desktop with auto-login (no password prompt)
#
# Run once on the main Pi:
#   sudo bash ~/hanryx-vault-pos/pi-setup/setup-kiosk-boot.sh
# =============================================================================
set -e

REPO_DIR="$HOME/hanryx-vault-pos"
COMPOSE_FILE="$REPO_DIR/pi-setup/docker-compose.yml"
KIOSK_URL="http://localhost/kiosk"
CURRENT_USER="${SUDO_USER:-$(whoami)}"
HOME_DIR="/home/$CURRENT_USER"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[✓]${NC} $1"; }
info() { echo -e "${YELLOW}[→]${NC} $1"; }

echo ""
echo "  HanryxVault Kiosk Boot Setup"
echo "  ============================================================"
echo "  User  : $CURRENT_USER"
echo "  Repo  : $REPO_DIR"
echo "  URL   : $KIOSK_URL"
echo ""

# ── 1. Docker service already enabled — just make sure ─────────────────────
info "Enabling Docker to start at boot…"
systemctl enable docker
ok "Docker enabled"

# ── 2. Systemd service: Docker Compose for POS ─────────────────────────────
info "Creating hanryx-pos.service (starts Docker Compose at boot)…"
cat > /etc/systemd/system/hanryx-pos.service << EOF
[Unit]
Description=HanryxVault POS — Docker Compose
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=$CURRENT_USER
WorkingDirectory=$REPO_DIR
ExecStartPre=/usr/bin/docker compose -f $COMPOSE_FILE pull --quiet
ExecStart=/usr/bin/docker compose -f $COMPOSE_FILE up -d
ExecStop=/usr/bin/docker compose -f $COMPOSE_FILE down
TimeoutStartSec=180
Restart=no

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable hanryx-pos.service
ok "hanryx-pos.service enabled"

# ── 3. Auto-login to desktop (raspi-config) ────────────────────────────────
info "Setting Pi to boot to desktop with auto-login…"
raspi-config nonint do_boot_behaviour B4 2>/dev/null || true
ok "Boot behaviour set to desktop auto-login"

# ── 4. Install helper tools ────────────────────────────────────────────────
info "Installing Chromium and unclutter (cursor hider)…"
apt-get install -y -qq chromium-browser unclutter 2>/dev/null || \
apt-get install -y -qq chromium unclutter 2>/dev/null || true
ok "Packages ready"

# ── 5. XDG autostart entry (works on LXDE, Wayland, and labwc) ─────────────
info "Creating kiosk autostart entry…"
mkdir -p "$HOME_DIR/.config/autostart"

cat > "$HOME_DIR/.config/autostart/hanryx-kiosk.desktop" << EOF
[Desktop Entry]
Type=Application
Name=HanryxVault Kiosk
Comment=Customer-facing POS display
Exec=/bin/bash -c 'sleep 8 && DISPLAY=:0 chromium-browser --kiosk --noerrdialogs --disable-infobars --disable-session-crashed-bubble --no-first-run --disable-translate --check-for-update-interval=31536000 $KIOSK_URL'
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
EOF
chown -R "$CURRENT_USER:$CURRENT_USER" "$HOME_DIR/.config/autostart"
ok "Autostart entry created"

# ── 6. LXDE autostart backup (pre-Bookworm / X11 fallback) ─────────────────
info "Writing LXDE autostart backup…"
mkdir -p "$HOME_DIR/.config/lxsession/LXDE-pi"
LXDE_AUTO="$HOME_DIR/.config/lxsession/LXDE-pi/autostart"

# Remove any old kiosk lines first
grep -v "hanryx\|chromium.*kiosk\|unclutter\|xset.*dpms\|xset.*s off" "$LXDE_AUTO" 2>/dev/null > /tmp/lxde_auto.tmp || true
cat /tmp/lxde_auto.tmp > "$LXDE_AUTO" 2>/dev/null || true

cat >> "$LXDE_AUTO" << 'EOF'
@xset s off
@xset -dpms
@xset s noblank
@unclutter -idle 0 -root
@sleep 8 && chromium-browser --kiosk --noerrdialogs --disable-infobars --disable-session-crashed-bubble --no-first-run --disable-translate --check-for-update-interval=31536000 http://localhost/kiosk
EOF
chown "$CURRENT_USER:$CURRENT_USER" "$LXDE_AUTO"
ok "LXDE autostart backup written"

# ── 7. Disable screen sleep / blanking via config.txt ──────────────────────
info "Disabling HDMI blanking in /boot/firmware/config.txt…"
CONFIG_TXT="/boot/firmware/config.txt"
[ -f "$CONFIG_TXT" ] || CONFIG_TXT="/boot/config.txt"

if ! grep -q "blanking=1" "$CONFIG_TXT" 2>/dev/null; then
    echo "" >> "$CONFIG_TXT"
    echo "# HanryxVault — keep display on" >> "$CONFIG_TXT"
    echo "hdmi_blanking=1" >> "$CONFIG_TXT"
    # 1 = blank but don't power off, use 2 to prevent even that
fi
# Also prevent console blanking
sed -i 's/ consoleblank=[0-9]*//' /boot/firmware/cmdline.txt 2>/dev/null || \
sed -i 's/ consoleblank=[0-9]*//' /boot/cmdline.txt 2>/dev/null || true
ok "Screen blanking disabled"

# ── 8. Screensaver off via lightdm if present ──────────────────────────────
if [ -f /etc/lightdm/lightdm.conf ]; then
    if ! grep -q "xserver-command" /etc/lightdm/lightdm.conf; then
        sed -i '/^\[Seat:\*\]/a xserver-command=X -s 0 -dpms' /etc/lightdm/lightdm.conf
    fi
    ok "lightdm screensaver disabled"
fi

echo ""
echo "  ============================================================"
echo "  All done! Reboot to activate."
echo ""
echo "  On boot the Pi will:"
echo "    1. Start the POS Docker containers automatically"
echo "    2. Open Chromium in fullscreen kiosk mode at /kiosk"
echo "    3. Never sleep or blank the screen"
echo ""
echo "  Staff open a second tab/window to: http://localhost/admin"
echo "  or from any device on the LAN: http://192.168.10.1/admin"
echo ""
echo "  To reboot now:  sudo reboot"
echo ""
