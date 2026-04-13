#!/usr/bin/env bash
# =============================================================================
# HanryxVault — Satellite Pi 5 Dual-Monitor Kiosk Boot Setup  (v2)
#
# What this configures:
#   Monitor 1 (HDMI-0) → /kiosk    — customer-facing display (Pokémon + POS)
#   Monitor 2 (HDMI-1) → /admin    — staff admin portal
#
# Improvements over v1:
#   • Detects Wayland (Pi 5 Bookworm default) vs X11 automatically
#   • Hardware-accelerated video decode for smooth Pokémon playback
#   • GPU memory bump to 256 MB for dual 1080p + video
#   • Chromium watchdog — auto-restarts both windows if they crash
#   • Auto git-pull on boot so card show always runs latest code
#   • Persistent Chromium profiles (survive crashes, keep login sessions)
#   • Logs everything to /var/log/hanryx-kiosk.log for easy debugging
#   • Disables USB autosuspend (keeps barcode scanners & receipt printers alive)
#   • Removes boot rainbow splash & quietens console output
#   • Increases swap to 512 MB for smoother multi-tab performance
#   • Handles both labwc (Wayland) and LXDE (X11) autostart paths
#   • labwc autostart support for Pi 5 Bookworm
#   • SSH stays accessible for remote management at a show
#
# Run ONCE on the satellite Pi:
#   sudo bash ~/hanryx-vault-pos/pi-setup/setup-satellite-kiosk-boot.sh
# =============================================================================
set -euo pipefail

# ── Config — edit these if your setup differs ──────────────────────────────
REPO_DIR="$HOME/hanryx-vault-pos"
COMPOSE_FILE="$REPO_DIR/pi-setup/docker-compose.yml"
KIOSK_URL="http://localhost/kiosk"
ADMIN_URL="http://localhost/admin"
LOG_FILE="/var/log/hanryx-kiosk.log"
PROFILE_DIR_KIOSK="$HOME/.hanryx/kiosk-profile"
PROFILE_DIR_ADMIN="$HOME/.hanryx/admin-profile"
LAUNCH_SCRIPT="$HOME/.hanryx-dual-monitor.sh"
# ---------------------------------------------------------------------------

CURRENT_USER="${SUDO_USER:-$(whoami)}"
HOME_DIR="/home/$CURRENT_USER"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'
RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[✓]${NC} $1"; }
info() { echo -e "${YELLOW}[→]${NC} $1"; }
note() { echo -e "${CYAN}[i]${NC} $1"; }
warn() { echo -e "${RED}[!]${NC} $1"; }

echo ""
echo -e "${BOLD}  HanryxVault — Satellite Pi 5 Dual-Monitor Setup (v2)${NC}"
echo "  ============================================================"
echo "  User     : $CURRENT_USER"
echo "  Repo     : $REPO_DIR"
echo "  Monitor 1: $KIOSK_URL  (customer kiosk)"
echo "  Monitor 2: $ADMIN_URL  (staff admin)"
echo "  Logs     : $LOG_FILE"
echo ""

# ── 1. Enable Docker ────────────────────────────────────────────────────────
info "Enabling Docker to start at boot…"
systemctl enable docker
ok "Docker enabled"

# ── 2. Systemd service: Docker Compose ─────────────────────────────────────
info "Creating hanryx-pos.service (Docker Compose)…"
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
# Pull latest images if internet is available; ignore failure (works offline)
ExecStartPre=-/usr/bin/docker compose -f $COMPOSE_FILE pull --quiet
ExecStart=/usr/bin/docker compose -f $COMPOSE_FILE up -d
ExecStop=/usr/bin/docker compose -f $COMPOSE_FILE down
TimeoutStartSec=180
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable hanryx-pos.service
ok "hanryx-pos.service enabled"

# ── 3. Auto-login to desktop ────────────────────────────────────────────────
info "Setting Pi to boot to desktop with auto-login…"
raspi-config nonint do_boot_behaviour B4 2>/dev/null || true
ok "Boot to desktop auto-login set"

# ── 4. Install packages ─────────────────────────────────────────────────────
info "Installing required packages…"
apt-get update -qq
apt-get install -y -qq \
  chromium-browser \
  unclutter \
  curl \
  git \
  2>/dev/null || \
apt-get install -y -qq \
  chromium \
  unclutter \
  curl \
  git \
  2>/dev/null || true
ok "Packages ready (chromium, unclutter, curl, git)"

# ── 5. Increase swap to 512 MB (smoother dual-monitor + video performance) ──
info "Setting swap to 512 MB…"
if [ -f /etc/dphys-swapfile ]; then
    sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=512/' /etc/dphys-swapfile
    dphys-swapfile setup 2>/dev/null || true
    ok "Swap set to 512 MB"
else
    note "dphys-swapfile not found — skipping swap change"
fi

# ── 6. Disable USB autosuspend (keeps scanners & receipt printers alive) ────
info "Disabling USB autosuspend…"
UDEV_RULE="/etc/udev/rules.d/99-hanryx-usb.rules"
if [ ! -f "$UDEV_RULE" ]; then
    echo 'ACTION=="add", SUBSYSTEM=="usb", TEST=="power/autosuspend", ATTR{power/autosuspend}="-1"' \
        > "$UDEV_RULE"
    udevadm control --reload-rules 2>/dev/null || true
fi
ok "USB autosuspend disabled"

# ── 7. GPU memory + Pi 5 performance tweaks in config.txt ──────────────────
info "Applying Pi 5 performance settings to config.txt…"
CONFIG_TXT="/boot/firmware/config.txt"
[ -f "$CONFIG_TXT" ] || CONFIG_TXT="/boot/config.txt"

# Only add our block once
if ! grep -q "HanryxVault" "$CONFIG_TXT" 2>/dev/null; then
    cat >> "$CONFIG_TXT" << 'CFG'

# ── HanryxVault satellite Pi 5 ──────────────────────────────────
# 256 MB GPU memory for smooth dual-4K/1080p + hardware video decode
gpu_mem=256
# Keep both HDMI ports active even if no display connected at boot
hdmi_force_hotplug:0=1
hdmi_force_hotplug:1=1
# Disable blanking — both screens stay on permanently
hdmi_blanking=1
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
    # Remove consoleblank and add quiet + loglevel=3 if not present
    sed -i 's/ consoleblank=[0-9]*//' "$CMDLINE"
    grep -q "loglevel=3" "$CMDLINE" || \
        sed -i 's/$/ quiet loglevel=3 logo.nologo/' "$CMDLINE"
    break
done
ok "Boot console quietened"

# ── 8. Disable screensaver via lightdm (X11) ────────────────────────────────
if [ -f /etc/lightdm/lightdm.conf ]; then
    if ! grep -q "xserver-command" /etc/lightdm/lightdm.conf; then
        sed -i '/^\[Seat:\*\]/a xserver-command=X -s 0 -dpms' \
            /etc/lightdm/lightdm.conf
    fi
    ok "lightdm screensaver disabled"
fi

# ── 9. Write the dual-monitor launcher script ────────────────────────────────
info "Writing dual-monitor launcher…"
mkdir -p "$HOME_DIR/.hanryx"

cat > "$LAUNCH_SCRIPT" << 'LAUNCH'
#!/usr/bin/env bash
# =============================================================================
# HanryxVault — Dual-Monitor Kiosk Launcher (runs every boot at desktop login)
# =============================================================================

KIOSK_URL="http://localhost/kiosk"
ADMIN_URL="http://localhost/admin"
LOG_FILE="/var/log/hanryx-kiosk.log"
REPO_DIR="$HOME/hanryx-vault-pos"
PROFILE_KIOSK="$HOME/.hanryx/kiosk-profile"
PROFILE_ADMIN="$HOME/.hanryx/admin-profile"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

log "============================================"
log "HanryxVault satellite kiosk starting…"
log "============================================"

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

# ── Auto-update: git pull latest code (non-blocking, best-effort) ────────────
log "Checking for updates…"
(
    cd "$REPO_DIR" 2>/dev/null || exit 0
    git fetch --quiet origin main 2>/dev/null || true
    git merge --ff-only --quiet origin/main 2>/dev/null || true
    log "Git pull complete"
) &

# ── Wait for the POS server to respond (up to 90 s) ─────────────────────────
log "Waiting for POS server…"
READY=0
for i in $(seq 1 45); do
    if curl -sf --max-time 2 "$KIOSK_URL" > /dev/null 2>&1; then
        READY=1
        break
    fi
    sleep 2
done

if [ "$READY" -eq 0 ]; then
    log "WARNING: POS server not responding after 90 s — launching anyway"
else
    log "POS server is ready"
fi

# ── Detect monitor layout ─────────────────────────────────────────────────────
# Pi 5 dual HDMI: monitors appear side-by-side in the extended desktop.
# We detect the width of the primary monitor so monitor 2 starts right of it.
if [ "$DISPLAY_SERVER" = "wayland" ]; then
    # wlr-randr gives us geometry on Wayland
    MONITOR1_W=$(wlr-randr 2>/dev/null | grep -oP '\d+x\d+' | head -1 | cut -dx -f1)
else
    MONITOR1_W=$(xrandr 2>/dev/null | grep -oP '(?<=connected )\d+x\d+' \
                   | head -1 | cut -dx -f1)
fi
MONITOR1_W=${MONITOR1_W:-1920}
MONITOR2_X=$MONITOR1_W
log "Monitor layout: Monitor1 width=${MONITOR1_W}px, Monitor2 x-offset=${MONITOR2_X}px"

# ── Build Chromium flags ──────────────────────────────────────────────────────
# Pi 5-specific: enable hardware video decode (H.264/VP9 via V4L2)
# This makes YouTube Pokémon episodes play smoothly without maxing the CPU.
COMMON_FLAGS=(
    --noerrdialogs
    --disable-infobars
    --disable-session-crashed-bubble
    --no-first-run
    --disable-translate
    --disable-features=TranslateUI
    --check-for-update-interval=31536000
    --autoplay-policy=no-user-gesture-required
    # Hardware acceleration (Pi 5 — V4L2 codec)
    --enable-gpu-rasterization
    --enable-zero-copy
    --ignore-gpu-blocklist
    --use-gl=egl
    --enable-accelerated-video-decode
    --enable-features=VaapiVideoDecoder,VaapiVideoEncoder
    # Memory / performance
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
# The watchdog re-opens Chromium if the window is closed or crashes.
# At a card show this is critical — no one wants a blank screen.

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

# ── Start Monitor 1: customer kiosk ─────────────────────────────────────────
launch_with_watchdog "Kiosk (Monitor 1)" "$KIOSK_URL" \
    "$PROFILE_KIOSK" 0 &

# Small delay so Monitor 1 claims focus first
sleep 4

# ── Start Monitor 2: staff admin ────────────────────────────────────────────
launch_with_watchdog "Admin (Monitor 2)" "$ADMIN_URL" \
    "$PROFILE_ADMIN" "$MONITOR2_X" &

log "Both windows launched — watchdog running"
wait
LAUNCH

chmod +x "$LAUNCH_SCRIPT"
chown -R "$CURRENT_USER:$CURRENT_USER" "$HOME_DIR/.hanryx" "$LAUNCH_SCRIPT"
ok "Launcher written → $LAUNCH_SCRIPT"

# ── 10. XDG autostart (Wayland / GNOME / modern Pi Bookworm) ────────────────
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

# ── 11. labwc autostart (Pi 5 Bookworm Wayland window manager) ───────────────
info "Configuring labwc autostart (Pi 5 Wayland)…"
LABWC_DIR="$HOME_DIR/.config/labwc"
mkdir -p "$LABWC_DIR"
LABWC_AUTO="$LABWC_DIR/autostart"
# Remove any old hanryx entry
grep -v "hanryx" "$LABWC_AUTO" 2>/dev/null > /tmp/labwc_auto.tmp || true
cat /tmp/labwc_auto.tmp > "$LABWC_AUTO" 2>/dev/null || true
echo "sleep 8 && $LAUNCH_SCRIPT &" >> "$LABWC_AUTO"
chown -R "$CURRENT_USER:$CURRENT_USER" "$LABWC_DIR"
ok "labwc autostart configured"

# ── 12. LXDE autostart (X11 / older Raspberry Pi OS fallback) ───────────────
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

# ── 13. Create log file with correct ownership ───────────────────────────────
touch "$LOG_FILE"
chown "$CURRENT_USER:$CURRENT_USER" "$LOG_FILE"
ok "Log file ready → $LOG_FILE"

# ── 14. SSH: ensure openssh-server is enabled for remote management ──────────
info "Ensuring SSH is enabled (remote management at shows)…"
systemctl enable ssh 2>/dev/null || true
ok "SSH enabled"

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  ============================================================${NC}"
echo -e "${GREEN}  All done — reboot to activate.${NC}"
echo ""
echo "  On every boot the satellite Pi will:"
echo "    1.  Pull latest code from GitHub (if internet available)"
echo "    2.  Start POS Docker containers"
echo "    3.  Wait for the server to be ready"
echo "    4.  Monitor 1 (HDMI-0) → /kiosk   — customer screen"
echo "    5.  Monitor 2 (HDMI-1) → /admin   — staff admin"
echo "    6.  Auto-restart both windows if they ever crash"
echo "    7.  Never sleep or blank either screen"
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
