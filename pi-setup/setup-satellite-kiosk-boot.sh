#!/usr/bin/env bash
# =============================================================================
# HanryxVault — Satellite Pi 5 Dual-Monitor Kiosk Boot Setup  (v3)
#
# What this configures:
#   Monitor 1 (HDMI-0) → /admin   — staff admin portal
#   Monitor 2 (HDMI-1) → /kiosk   — customer-facing display (Pokémon + POS)
#
# The satellite Pi does NOT run Docker — it just points Chromium at
# the main Pi's POS server over the local network.
#
# Features:
#   • Asks for main Pi's IP or hostname during setup
#   • Detects Wayland (Pi 5 Bookworm default) vs X11 automatically
#   • Hardware-accelerated video decode for smooth Pokémon playback
#   • GPU memory bump to 256 MB for dual 1080p + video
#   • Chromium watchdog — auto-restarts both windows if they crash
#   • Persistent Chromium profiles (survive crashes, keep login sessions)
#   • Logs everything to /var/log/hanryx-kiosk.log for easy debugging
#   • Disables USB autosuspend (keeps barcode scanners & receipt printers alive)
#   • Removes boot rainbow splash & quietens console output
#   • Increases swap to 512 MB for smoother multi-tab performance
#   • Handles both labwc (Wayland) and LXDE (X11) autostart paths
#   • SSH stays accessible for remote management at a show
#
# Run ONCE on the satellite Pi:
#   sudo bash ~/hanryx-vault-pos/pi-setup/setup-satellite-kiosk-boot.sh
# =============================================================================
set -euo pipefail

CURRENT_USER="${SUDO_USER:-$(whoami)}"
HOME_DIR="/home/$CURRENT_USER"
LOG_FILE="/var/log/hanryx-kiosk.log"
PROFILE_DIR_ADMIN="$HOME_DIR/.hanryx/admin-profile"
PROFILE_DIR_KIOSK="$HOME_DIR/.hanryx/kiosk-profile"
LAUNCH_SCRIPT="$HOME_DIR/.hanryx-dual-monitor.sh"
CONFIG_FILE="$HOME_DIR/.hanryx/satellite.conf"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'
RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[✓]${NC} $1"; }
info() { echo -e "${YELLOW}[→]${NC} $1"; }
note() { echo -e "${CYAN}[i]${NC} $1"; }
warn() { echo -e "${RED}[!]${NC} $1"; }

echo ""
echo -e "${BOLD}  HanryxVault — Satellite Pi 5 Dual-Monitor Setup (v3)${NC}"
echo "  ============================================================"
echo ""

# ── Ask for main Pi's IP ─────────────────────────────────────────────────────
DEFAULT_IP="192.168.86.45"
if [ -f "$CONFIG_FILE" ]; then
    SAVED_IP=$(grep "^MAIN_PI_IP=" "$CONFIG_FILE" 2>/dev/null | cut -d= -f2)
    DEFAULT_IP="${SAVED_IP:-$DEFAULT_IP}"
fi

echo -e "${CYAN}  The satellite Pi connects to the main Pi's POS server.${NC}"
echo -e "${CYAN}  The main Pi runs Docker and serves the admin + kiosk pages.${NC}"
echo ""
read -rp "  Enter the main Pi's IP address [$DEFAULT_IP]: " MAIN_PI_IP
MAIN_PI_IP="${MAIN_PI_IP:-$DEFAULT_IP}"

ADMIN_URL="http://${MAIN_PI_IP}:8080/admin"
KIOSK_URL="http://${MAIN_PI_IP}:8080/kiosk"

echo ""
echo "  User     : $CURRENT_USER"
echo "  Main Pi  : $MAIN_PI_IP"
echo "  Monitor 1: $ADMIN_URL  (staff admin)"
echo "  Monitor 2: $KIOSK_URL  (customer kiosk)"
echo "  Logs     : $LOG_FILE"
echo ""

# Save config for future re-runs
mkdir -p "$HOME_DIR/.hanryx"
cat > "$CONFIG_FILE" << EOF
MAIN_PI_IP=$MAIN_PI_IP
ADMIN_URL=$ADMIN_URL
KIOSK_URL=$KIOSK_URL
EOF
chown "$CURRENT_USER:$CURRENT_USER" "$CONFIG_FILE"
ok "Config saved → $CONFIG_FILE"

# ── 1. Auto-login to desktop ────────────────────────────────────────────────
info "Setting Pi to boot to desktop with auto-login…"
raspi-config nonint do_boot_behaviour B4 2>/dev/null || true
ok "Boot to desktop auto-login set"

# ── 2. Install packages ─────────────────────────────────────────────────────
info "Installing required packages…"
apt-get update -qq
apt-get install -y -qq \
  chromium-browser \
  unclutter \
  curl \
  2>/dev/null || \
apt-get install -y -qq \
  chromium \
  unclutter \
  curl \
  2>/dev/null || true
ok "Packages ready (chromium, unclutter, curl)"

# ── 3. Increase swap to 512 MB (smoother dual-monitor + video performance) ──
info "Setting swap to 512 MB…"
if [ -f /etc/dphys-swapfile ]; then
    sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=512/' /etc/dphys-swapfile
    dphys-swapfile setup 2>/dev/null || true
    ok "Swap set to 512 MB"
else
    note "dphys-swapfile not found — skipping swap change"
fi

# ── 4. Disable USB autosuspend (keeps scanners & receipt printers alive) ────
info "Disabling USB autosuspend…"
UDEV_RULE="/etc/udev/rules.d/99-hanryx-usb.rules"
if [ ! -f "$UDEV_RULE" ]; then
    echo 'ACTION=="add", SUBSYSTEM=="usb", TEST=="power/autosuspend", ATTR{power/autosuspend}="-1"' \
        > "$UDEV_RULE"
    udevadm control --reload-rules 2>/dev/null || true
fi
ok "USB autosuspend disabled"

# ── 5. GPU memory + Pi 5 performance tweaks in config.txt ──────────────────
info "Applying Pi 5 performance settings to config.txt…"
CONFIG_TXT="/boot/firmware/config.txt"
[ -f "$CONFIG_TXT" ] || CONFIG_TXT="/boot/config.txt"

if ! grep -q "HanryxVault" "$CONFIG_TXT" 2>/dev/null; then
    cat >> "$CONFIG_TXT" << 'CFG'

# ── HanryxVault satellite Pi 5 ──────────────────────────────────
# 256 MB GPU memory for smooth dual-4K/1080p + hardware video decode
gpu_mem=256
# Keep both HDMI ports active even if no display connected at boot
hdmi_force_hotplug:0=1
hdmi_force_hotplug:1=1
# Disable blanking — both screens stay on permanently
hdmi_blanking=0
# Quiet boot — remove boot messages from screen
quiet
# Remove the rainbow splash square on boot
disable_splash=1
CFG
    ok "config.txt updated"
else
    note "config.txt already has HanryxVault block — skipping"
fi

# Quiet console boot (remove boot text from screens)
for CMDLINE in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
    [ -f "$CMDLINE" ] || continue
    sed -i 's/ consoleblank=[0-9]*//' "$CMDLINE"
    grep -q "loglevel=3" "$CMDLINE" || \
        sed -i 's/$/ quiet loglevel=3 logo.nologo/' "$CMDLINE"
    break
done
ok "Boot console quietened"

# ── 6. Disable screensaver via lightdm (X11) ────────────────────────────────
if [ -f /etc/lightdm/lightdm.conf ]; then
    if ! grep -q "xserver-command" /etc/lightdm/lightdm.conf; then
        sed -i '/^\[Seat:\*\]/a xserver-command=X -s 0 -dpms' \
            /etc/lightdm/lightdm.conf
    fi
    ok "lightdm screensaver disabled"
fi

# ── 7. Network timeout guard ─────────────────────────────────────────────
info "Setting network-online timeout to 15 s (prevents offline boot hang)…"
mkdir -p /etc/systemd/system/systemd-networkd-wait-online.service.d
cat > /etc/systemd/system/systemd-networkd-wait-online.service.d/timeout.conf << 'EOF'
[Service]
TimeoutStartSec=15
EOF
systemctl daemon-reload
ok "Network timeout guard set (15 s max)"

# ── 8. Write the dual-monitor launcher script ────────────────────────────────
info "Writing dual-monitor launcher…"

cat > "$LAUNCH_SCRIPT" << 'LAUNCH'
#!/usr/bin/env bash
# =============================================================================
# HanryxVault — Satellite Dual-Monitor Kiosk Launcher
# Connects to the main Pi's POS server — no Docker needed on this Pi.
# =============================================================================

LOG_FILE="/var/log/hanryx-kiosk.log"
CONFIG_FILE="$HOME/.hanryx/satellite.conf"
PROFILE_ADMIN="$HOME/.hanryx/admin-profile"
PROFILE_KIOSK="$HOME/.hanryx/kiosk-profile"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

log "============================================"
log "HanryxVault satellite kiosk starting…"
log "============================================"

# ── Load config ──────────────────────────────────────────────────────────────
if [ -f "$CONFIG_FILE" ]; then
    source "$CONFIG_FILE"
else
    ADMIN_URL="http://192.168.86.45:8080/admin"
    KIOSK_URL="http://192.168.86.45:8080/kiosk"
fi
log "Admin URL: $ADMIN_URL"
log "Kiosk URL: $KIOSK_URL"

# ── Detect display server (Wayland vs X11) ───────────────────────────────────
if [ "$XDG_SESSION_TYPE" = "wayland" ] || pgrep -x labwc > /dev/null 2>&1; then
    DISPLAY_SERVER="wayland"
    log "Display server: Wayland (labwc)"
else
    DISPLAY_SERVER="x11"
    export DISPLAY="${DISPLAY:-:0}"
    log "Display server: X11 (DISPLAY=$DISPLAY)"
fi

# ── Disable screen blanking ───────────────────────────────────────────────────
if [ "$DISPLAY_SERVER" = "x11" ]; then
    xset s off    2>/dev/null || true
    xset -dpms    2>/dev/null || true
    xset s noblank 2>/dev/null || true
fi

# ── Hide mouse cursor (unclutter works on both X11 and XWayland) ─────────────
unclutter -idle 3 -root 2>/dev/null &

# ── Wait for the main Pi's POS server to respond (up to 120 s) ──────────────
log "Waiting for POS server at $ADMIN_URL …"
READY=0
for i in $(seq 1 60); do
    if curl -sf --max-time 2 "$ADMIN_URL" > /dev/null 2>&1; then
        READY=1
        break
    fi
    sleep 2
done

if [ "$READY" -eq 0 ]; then
    log "WARNING: POS server not responding after 120 s — launching anyway"
else
    log "POS server is ready"
fi

# ── Detect monitor layout ─────────────────────────────────────────────────────
if [ "$DISPLAY_SERVER" = "wayland" ]; then
    MONITOR1_W=$(wlr-randr 2>/dev/null | grep -oP '\d+x\d+' | head -1 | cut -dx -f1)
else
    MONITOR1_W=$(xrandr 2>/dev/null | grep -oP '(?<=connected )\d+x\d+' \
                   | head -1 | cut -dx -f1)
fi
MONITOR1_W=${MONITOR1_W:-1920}
MONITOR2_X=$MONITOR1_W
log "Monitor layout: Monitor1 width=${MONITOR1_W}px, Monitor2 x-offset=${MONITOR2_X}px"

# ── Build Chromium flags ──────────────────────────────────────────────────────
COMMON_FLAGS=(
    --noerrdialogs
    --disable-infobars
    --disable-session-crashed-bubble
    --no-first-run
    --disable-translate
    --disable-features=TranslateUI
    --check-for-update-interval=31536000
    --autoplay-policy=no-user-gesture-required
    --enable-gpu-rasterization
    --enable-zero-copy
    --ignore-gpu-blocklist
    --use-gl=egl
    --enable-accelerated-video-decode
    --enable-features=VaapiVideoDecoder,VaapiVideoEncoder
    --disable-background-networking
    --disable-default-apps
    --disable-extensions
    --disable-plugins
    --process-per-site
)

if [ "$DISPLAY_SERVER" = "wayland" ]; then
    COMMON_FLAGS+=(--ozone-platform=wayland)
fi

# ── Helper: find chromium binary ─────────────────────────────────────────────
CHROMIUM_BIN=$(command -v chromium-browser 2>/dev/null \
               || command -v chromium 2>/dev/null \
               || echo "chromium-browser")

# ── Launch function with watchdog ────────────────────────────────────────────
launch_with_watchdog() {
    local name="$1"
    local url="$2"
    local profile="$3"
    local pos_x="$4"

    mkdir -p "$profile"

    log "Launching $name at $url (position $pos_x,0)"

    while true; do
        "$CHROMIUM_BIN" \
            --kiosk \
            --window-position="${pos_x},0" \
            --user-data-dir="$profile" \
            "${COMMON_FLAGS[@]}" \
            "$url" \
            >> "$LOG_FILE" 2>&1

        EXIT_CODE=$?
        log "$name exited (code $EXIT_CODE) — restarting in 5 s…"
        sleep 5
    done
}

# ── Start Monitor 1: staff admin ─────────────────────────────────────────────
launch_with_watchdog "Admin (Monitor 1)" "$ADMIN_URL" \
    "$PROFILE_ADMIN" 0 &

# Small delay so Monitor 1 claims focus first
sleep 4

# ── Start Monitor 2: customer kiosk ─────────────────────────────────────────
launch_with_watchdog "Kiosk (Monitor 2)" "$KIOSK_URL" \
    "$PROFILE_KIOSK" "$MONITOR2_X" &

log "Both windows launched — watchdog running"
wait
LAUNCH

chmod +x "$LAUNCH_SCRIPT"
chown -R "$CURRENT_USER:$CURRENT_USER" "$HOME_DIR/.hanryx" "$LAUNCH_SCRIPT"
ok "Launcher written → $LAUNCH_SCRIPT"

# ── 9. XDG autostart (Wayland / GNOME / modern Pi Bookworm) ────────────────
info "Creating XDG autostart entry…"
mkdir -p "$HOME_DIR/.config/autostart"
cat > "$HOME_DIR/.config/autostart/hanryx-dual-kiosk.desktop" << EOF
[Desktop Entry]
Type=Application
Name=HanryxVault Dual Monitor
Comment=Customer kiosk + Staff admin — auto-launched at desktop login
Exec=/bin/bash -c 'sleep 8 && $LAUNCH_SCRIPT'
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
EOF
chown -R "$CURRENT_USER:$CURRENT_USER" "$HOME_DIR/.config/autostart"
ok "XDG autostart entry created"

# ── 10. labwc autostart (Pi 5 Bookworm Wayland window manager) ───────────────
info "Configuring labwc autostart (Pi 5 Wayland)…"
LABWC_DIR="$HOME_DIR/.config/labwc"
mkdir -p "$LABWC_DIR"
LABWC_AUTO="$LABWC_DIR/autostart"
grep -v "hanryx" "$LABWC_AUTO" 2>/dev/null > /tmp/labwc_auto.tmp || true
cat /tmp/labwc_auto.tmp > "$LABWC_AUTO" 2>/dev/null || true
echo "sleep 8 && $LAUNCH_SCRIPT &" >> "$LABWC_AUTO"
chown -R "$CURRENT_USER:$CURRENT_USER" "$LABWC_DIR"
ok "labwc autostart configured"

# ── 11. LXDE autostart (X11 / older Raspberry Pi OS fallback) ───────────────
info "Configuring LXDE autostart (X11 fallback)…"
mkdir -p "$HOME_DIR/.config/lxsession/LXDE-pi"
LXDE_AUTO="$HOME_DIR/.config/lxsession/LXDE-pi/autostart"
grep -v "hanryx\|chromium.*kiosk\|unclutter\|xset.*dpms\|xset.*s off" \
    "$LXDE_AUTO" 2>/dev/null > /tmp/lxde_auto.tmp || true
cat /tmp/lxde_auto.tmp > "$LXDE_AUTO" 2>/dev/null || true
cat >> "$LXDE_AUTO" << EOF
@xset s off
@xset -dpms
@xset s noblank
@sleep 8 && $LAUNCH_SCRIPT
EOF
chown "$CURRENT_USER:$CURRENT_USER" "$LXDE_AUTO"
ok "LXDE autostart configured"

# ── 12. Create log file with correct ownership ───────────────────────────────
touch "$LOG_FILE"
chown "$CURRENT_USER:$CURRENT_USER" "$LOG_FILE"
ok "Log file ready → $LOG_FILE"

# ── 13. SSH: ensure openssh-server is enabled for remote management ──────────
info "Ensuring SSH is enabled (remote management at shows)…"
systemctl enable ssh 2>/dev/null || true
ok "SSH enabled"

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  ============================================================${NC}"
echo -e "${GREEN}  All done — reboot to activate.${NC}"
echo ""
echo "  On every boot the satellite Pi will:"
echo "    1.  Wait for the main Pi's POS server to be ready"
echo "    2.  Monitor 1 (HDMI-0) → /admin   — staff admin"
echo "    3.  Monitor 2 (HDMI-1) → /kiosk   — customer screen"
echo "    4.  Auto-restart both windows if they ever crash"
echo "    5.  Never sleep or blank either screen"
echo ""
echo -e "${CYAN}  Main Pi IP:${NC} $MAIN_PI_IP"
echo -e "${CYAN}  To change later:${NC} edit $CONFIG_FILE and reboot"
echo ""
echo -e "${CYAN}  Tips:${NC}"
echo "    • Swap HDMI cables if the screens are the wrong way round"
echo "    • View live logs:  tail -f $LOG_FILE"
echo "    • SSH from your laptop: ssh $CURRENT_USER@<satellite-ip>"
echo "    • Run the launcher manually to test:"
echo "        bash $LAUNCH_SCRIPT"
echo ""
warn "GPU memory bumped to 256 MB in config.txt — required for smooth video."
echo ""
echo "  To reboot now:  sudo reboot"
echo ""
