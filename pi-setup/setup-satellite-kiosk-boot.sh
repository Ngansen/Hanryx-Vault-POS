#!/usr/bin/env bash
# =============================================================================
# HanryxVault — Satellite Pi Dual-Monitor Kiosk Boot Setup
# Configures the satellite Pi to:
#   1. Auto-start Docker Compose (POS server) on every boot
#   2. Monitor 1 (HDMI-0): /kiosk fullscreen — customer-facing display
#   3. Monitor 2 (HDMI-1): /admin fullscreen — staff admin portal
#   4. Disable screen sleep / blanking on both screens
#   5. Boot to desktop with auto-login (no password prompt)
#
# Run once on the SATELLITE Pi only:
#   sudo bash ~/hanryx-vault-pos/pi-setup/setup-satellite-kiosk-boot.sh
# =============================================================================
set -e

REPO_DIR="$HOME/hanryx-vault-pos"
COMPOSE_FILE="$REPO_DIR/pi-setup/docker-compose.yml"
KIOSK_URL="http://localhost/kiosk"
ADMIN_URL="http://localhost/admin"
CURRENT_USER="${SUDO_USER:-$(whoami)}"
HOME_DIR="/home/$CURRENT_USER"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[✓]${NC} $1"; }
info() { echo -e "${YELLOW}[→]${NC} $1"; }
note() { echo -e "${CYAN}[i]${NC} $1"; }

echo ""
echo "  HanryxVault — Satellite Pi Dual-Monitor Kiosk Boot"
echo "  ============================================================"
echo "  User     : $CURRENT_USER"
echo "  Repo     : $REPO_DIR"
echo "  Monitor 1: $KIOSK_URL  (customer kiosk)"
echo "  Monitor 2: $ADMIN_URL  (staff admin)"
echo ""

# ── 1. Enable Docker at boot ────────────────────────────────────────────────
info "Enabling Docker to start at boot…"
systemctl enable docker
ok "Docker enabled"

# ── 2. Systemd service: Docker Compose for POS ─────────────────────────────
info "Creating hanryx-pos.service…"
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

# ── 3. Auto-login to desktop ────────────────────────────────────────────────
info "Setting Pi to boot to desktop with auto-login…"
raspi-config nonint do_boot_behaviour B4 2>/dev/null || true
ok "Boot behaviour set to desktop auto-login"

# ── 4. Install helper tools ─────────────────────────────────────────────────
info "Installing Chromium and unclutter…"
apt-get install -y -qq chromium-browser unclutter 2>/dev/null || \
apt-get install -y -qq chromium unclutter 2>/dev/null || true
ok "Packages ready"

# ── 5. Write the dual-monitor launch script ─────────────────────────────────
info "Writing dual-monitor launch script…"
LAUNCH_SCRIPT="$HOME_DIR/.hanryx-dual-monitor.sh"

cat > "$LAUNCH_SCRIPT" << 'LAUNCH'
#!/usr/bin/env bash
# HanryxVault dual-monitor kiosk launcher
# Runs at desktop login — waits for server, then opens two Chromium windows.

KIOSK_URL="http://localhost/kiosk"
ADMIN_URL="http://localhost/admin"
COMMON_FLAGS="--noerrdialogs --disable-infobars --disable-session-crashed-bubble \
  --no-first-run --disable-translate --check-for-update-interval=31536000 \
  --disable-features=TranslateUI --autoplay-policy=no-user-gesture-required"

# Hide the mouse cursor
unclutter -idle 2 -root &

# Disable screen blanking
xset s off
xset -dpms
xset s noblank

# Wait for the POS server to be ready (up to 60 s)
echo "[hanryx] Waiting for POS server…"
for i in $(seq 1 30); do
  curl -sf http://localhost/kiosk > /dev/null 2>&1 && break
  sleep 2
done
echo "[hanryx] Server ready."

# Detect the X offset of the second monitor using xrandr
# On Pi 5 with two HDMI ports this is typically 1920 (first monitor width)
MONITOR1_W=$(xrandr 2>/dev/null | grep -oP '(?<=connected )\d+x\d+' | head -1 | cut -dx -f1)
MONITOR1_W=${MONITOR1_W:-1920}
MONITOR2_X=$MONITOR1_W

# ── Monitor 1: customer kiosk (HDMI-0) ──────────────────────────────────────
chromium-browser \
  --kiosk \
  --window-position=0,0 \
  --user-data-dir=/tmp/hanryx-kiosk \
  $COMMON_FLAGS \
  "$KIOSK_URL" &

sleep 3

# ── Monitor 2: staff admin (HDMI-1) ─────────────────────────────────────────
chromium-browser \
  --kiosk \
  --window-position=${MONITOR2_X},0 \
  --user-data-dir=/tmp/hanryx-admin \
  $COMMON_FLAGS \
  "$ADMIN_URL" &

wait
LAUNCH

chmod +x "$LAUNCH_SCRIPT"
chown "$CURRENT_USER:$CURRENT_USER" "$LAUNCH_SCRIPT"
ok "Dual-monitor launch script written → $LAUNCH_SCRIPT"

# ── 6. XDG autostart entry ──────────────────────────────────────────────────
info "Creating autostart entry…"
mkdir -p "$HOME_DIR/.config/autostart"

cat > "$HOME_DIR/.config/autostart/hanryx-dual-kiosk.desktop" << EOF
[Desktop Entry]
Type=Application
Name=HanryxVault Dual Monitor
Comment=Customer kiosk + Staff admin on two screens
Exec=/bin/bash -c 'sleep 8 && DISPLAY=:0 $LAUNCH_SCRIPT'
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
EOF
chown -R "$CURRENT_USER:$CURRENT_USER" "$HOME_DIR/.config/autostart"
ok "XDG autostart entry created"

# ── 7. LXDE autostart backup (X11 / pre-Bookworm fallback) ─────────────────
info "Writing LXDE autostart backup…"
mkdir -p "$HOME_DIR/.config/lxsession/LXDE-pi"
LXDE_AUTO="$HOME_DIR/.config/lxsession/LXDE-pi/autostart"

grep -v "hanryx\|chromium\|unclutter\|xset.*dpms\|xset.*s off" \
  "$LXDE_AUTO" 2>/dev/null > /tmp/lxde_auto.tmp || true
cat /tmp/lxde_auto.tmp > "$LXDE_AUTO" 2>/dev/null || true

cat >> "$LXDE_AUTO" << EOF
@xset s off
@xset -dpms
@xset s noblank
@sleep 8 && $LAUNCH_SCRIPT
EOF
chown "$CURRENT_USER:$CURRENT_USER" "$LXDE_AUTO"
ok "LXDE autostart backup written"

# ── 8. Disable HDMI blanking ────────────────────────────────────────────────
info "Disabling HDMI blanking…"
CONFIG_TXT="/boot/firmware/config.txt"
[ -f "$CONFIG_TXT" ] || CONFIG_TXT="/boot/config.txt"

if ! grep -q "hdmi_blanking" "$CONFIG_TXT" 2>/dev/null; then
    echo "" >> "$CONFIG_TXT"
    echo "# HanryxVault — keep both displays on" >> "$CONFIG_TXT"
    echo "hdmi_blanking=1" >> "$CONFIG_TXT"
fi
sed -i 's/ consoleblank=[0-9]*//' /boot/firmware/cmdline.txt 2>/dev/null || \
sed -i 's/ consoleblank=[0-9]*//' /boot/cmdline.txt 2>/dev/null || true
ok "Screen blanking disabled"

# ── 9. Screensaver off via lightdm if present ───────────────────────────────
if [ -f /etc/lightdm/lightdm.conf ]; then
    if ! grep -q "xserver-command" /etc/lightdm/lightdm.conf; then
        sed -i '/^\[Seat:\*\]/a xserver-command=X -s 0 -dpms' /etc/lightdm/lightdm.conf
    fi
    ok "lightdm screensaver disabled"
fi

echo ""
echo "  ============================================================"
echo "  Done! Reboot to activate."
echo ""
echo "  On boot the satellite Pi will:"
echo "    1. Start the POS Docker containers"
echo "    2. Monitor 1 (HDMI-0) → $KIOSK_URL  — customer screen"
echo "    3. Monitor 2 (HDMI-1) → $ADMIN_URL  — staff admin"
echo "    4. Never sleep or blank either screen"
echo ""
note "If the admin screen appears on the wrong monitor, swap the HDMI cables."
note "The launch script detects monitor width automatically via xrandr."
echo ""
echo "  To reboot now:  sudo reboot"
echo ""
