#!/usr/bin/env bash
# =============================================================================
# HanryxVault — Main Pi Kiosk Boot Setup  (v3 — offline-safe + mDNS)
#
# Configures the Pi to:
#   1. Auto-start Docker Compose (POS server) on every boot — works offline
#   2. Auto-launch Chromium in kiosk mode (/kiosk) with watchdog
#   3. Disable screen sleep / blanking
#   4. Boot to desktop with auto-login (no password prompt)
#   5. Best-effort git pull (skipped when offline)
#   6. Hardware-accelerated video decode for smooth Pokémon playback
#   7. Network timeout guard — never hangs waiting for internet
#   8. Persistent Chromium profile (survives crashes, keeps login session)
#   9. Logs everything to /var/log/hanryx-kiosk.log
#  10. mDNS via avahi — reachable as hanryxvault.local on the LAN
#  11. Optional static IP guard — prevents DHCP IP changes
#
# Run once on the main Pi:
#   sudo bash ~/hanryx-vault-pos/pi-setup/setup-kiosk-boot.sh
# =============================================================================
set -euo pipefail

REPO_DIR="$HOME/hanryx-vault-pos"
COMPOSE_FILE="$REPO_DIR/pi-setup/docker-compose.yml"
KIOSK_URL="http://localhost:8080/kiosk"
LOG_FILE="/var/log/hanryx-kiosk.log"
PROFILE_DIR="$HOME/.hanryx/kiosk-profile"
LAUNCH_SCRIPT="$HOME/.hanryx-kiosk-launcher.sh"
CURRENT_USER="${SUDO_USER:-$(whoami)}"
HOME_DIR="/home/$CURRENT_USER"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'
RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[✓]${NC} $1"; }
info() { echo -e "${YELLOW}[→]${NC} $1"; }
note() { echo -e "${CYAN}[i]${NC} $1"; }
warn() { echo -e "${RED}[!]${NC} $1"; }

echo ""
echo -e "${BOLD}  HanryxVault — Main Pi Kiosk Setup (v2 — offline-safe)${NC}"
echo "  ============================================================"
echo "  User  : $CURRENT_USER"
echo "  Repo  : $REPO_DIR"
echo "  URL   : $KIOSK_URL"
echo "  Logs  : $LOG_FILE"
echo ""

# ── 1. Docker service already enabled — just make sure ─────────────────────
info "Enabling Docker to start at boot…"
systemctl enable docker
ok "Docker enabled"

# ── 2. Network timeout guard ──────────────────────────────────────────────
# Prevent boot from hanging forever waiting for network at trade shows
info "Setting network-online timeout to 15 s (prevents offline boot hang)…"
mkdir -p /etc/systemd/system/systemd-networkd-wait-online.service.d
cat > /etc/systemd/system/systemd-networkd-wait-online.service.d/timeout.conf << 'EOF'
[Service]
TimeoutStartSec=15
EOF
systemctl daemon-reload
ok "Network timeout guard set (15 s max)"

# ── 3. Systemd service: Docker Compose for POS ─────────────────────────────
info "Creating hanryx-pos.service (starts Docker Compose at boot)…"
cat > /etc/systemd/system/hanryx-pos.service << EOF
[Unit]
Description=HanryxVault POS — Docker Compose
Requires=docker.service
After=docker.service
# Wants (not Requires) network — starts even with no internet
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=$CURRENT_USER
WorkingDirectory=$REPO_DIR
# The - prefix means "ignore failure" — pull skipped when offline
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
ok "hanryx-pos.service enabled (offline-safe)"

# ── 4. Auto-login to desktop (raspi-config) ────────────────────────────────
info "Setting Pi to boot to desktop with auto-login…"
raspi-config nonint do_boot_behaviour B4 2>/dev/null || true
ok "Boot behaviour set to desktop auto-login"

# ── 5. Install helper tools ────────────────────────────────────────────────
info "Installing required packages…"
apt-get update -qq 2>/dev/null || true
apt-get install -y -qq \
  chromium-browser \
  unclutter \
  curl \
  git \
  avahi-daemon \
  avahi-utils \
  libnss-mdns \
  2>/dev/null || \
apt-get install -y -qq \
  chromium \
  unclutter \
  curl \
  git \
  avahi-daemon \
  avahi-utils \
  libnss-mdns \
  2>/dev/null || true
ok "Packages ready (chromium, unclutter, curl, git, avahi)"

# ── 5b. mDNS hostname — used only on home LAN ──────────────────────────────
info "Configuring hostname (hanryxvault)…"
hostnamectl set-hostname hanryxvault 2>/dev/null || hostname hanryxvault 2>/dev/null || true
echo "hanryxvault" > /etc/hostname 2>/dev/null || true
AVAHI_CONF="/etc/avahi/avahi-daemon.conf"
if [ -f "$AVAHI_CONF" ]; then
    sed -i 's/^#\?host-name=.*/host-name=hanryxvault/' "$AVAHI_CONF" 2>/dev/null || true
fi
systemctl enable avahi-daemon 2>/dev/null || true
systemctl restart avahi-daemon 2>/dev/null || true
ok "Hostname set: hanryxvault (reachable as hanryxvault.local on home LAN)"

# ── 5c. Tailscale — remote access from satellite Pi & tablet at the shop ────
info "Installing Tailscale (satellite Pi + tablet reach this Pi from the shop)…"
if ! command -v tailscale &>/dev/null; then
    curl -fsSL https://tailscale.com/install.sh | sh
    ok "Tailscale installed"
else
    ok "Tailscale already installed ($(tailscale version | head -1))"
fi

echo ""
echo -e "${CYAN}  ── Tailscale authentication ─────────────────────────────${NC}"
echo -e "${CYAN}  The Main Pi must join the same Tailscale network as the satellite Pi.${NC}"
echo -e "${CYAN}  Get an auth key from: https://login.tailscale.com/admin/settings/keys${NC}"
echo ""
read -rp "  Paste your Tailscale auth key (or press Enter to authenticate interactively): " TS_AUTH_KEY

if [ -n "$TS_AUTH_KEY" ]; then
    tailscale up --authkey="$TS_AUTH_KEY" --hostname="hanryxvault" 2>/dev/null || \
    tailscale up --authkey="$TS_AUTH_KEY" 2>/dev/null || true
    ok "Tailscale connected with auth key"
else
    tailscale up --hostname="hanryxvault" 2>/dev/null &
    TS_PID=$!
    echo ""
    note "Follow the link above to approve this device in the Tailscale admin panel."
    read -rp "  Press Enter once approved… "
    wait $TS_PID 2>/dev/null || true
fi

TS_IP=$(tailscale ip -4 2>/dev/null || echo "not connected yet")
systemctl enable tailscaled 2>/dev/null || true
ok "Tailscale active — Main Pi Tailscale IP: $TS_IP"
echo ""
note "Satellite Pi should connect as hostname 'hanryxvault-sat' in Tailscale."
note "The satellite Pi's nginx will proxy the tablet to this Pi via Tailscale."

# ── 5d. Static IP guard (optional — prevents DHCP from changing IP) ────────
info "Checking current IP address…"
CURRENT_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
note "Current IP: ${CURRENT_IP:-unknown}"
echo ""
read -rp "  Lock this IP as static via dhcpcd? [y/N]: " LOCK_IP
if [[ "${LOCK_IP,,}" == "y" ]]; then
    IFACE=$(ip route show default 2>/dev/null | awk '/default/ {print $5}' | head -1)
    GATEWAY=$(ip route show default 2>/dev/null | awk '/default/ {print $3}' | head -1)
    DHCPCD="/etc/dhcpcd.conf"
    if grep -q "HanryxVault static IP" "$DHCPCD" 2>/dev/null; then
        note "Static IP already configured in dhcpcd.conf — skipping"
    else
        cat >> "$DHCPCD" << EOF

# ── HanryxVault static IP ─────────────────────────────────────────
interface ${IFACE:-eth0}
static ip_address=${CURRENT_IP}/24
static routers=${GATEWAY:-192.168.86.1}
static domain_name_servers=8.8.8.8 8.8.4.4
EOF
        ok "Static IP ${CURRENT_IP} locked for interface ${IFACE} in dhcpcd.conf"
    fi
else
    note "Skipping static IP — using mDNS (hanryxvault.local) for satellite discovery"
fi

# ── 6. Increase swap to 512 MB (smoother video + multi-tab) ──────────────
info "Setting swap to 512 MB…"
if [ -f /etc/dphys-swapfile ]; then
    sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=512/' /etc/dphys-swapfile
    dphys-swapfile setup 2>/dev/null || true
    ok "Swap set to 512 MB"
else
    note "dphys-swapfile not found — skipping swap change"
fi

# ── 7. Disable USB autosuspend (keeps scanners & receipt printers alive) ──
info "Disabling USB autosuspend…"
UDEV_RULE="/etc/udev/rules.d/99-hanryx-usb.rules"
if [ ! -f "$UDEV_RULE" ]; then
    echo 'ACTION=="add", SUBSYSTEM=="usb", TEST=="power/autosuspend", ATTR{power/autosuspend}="-1"' \
        > "$UDEV_RULE"
    udevadm control --reload-rules 2>/dev/null || true
fi
ok "USB autosuspend disabled"

# ── 8. GPU memory + Pi 5 performance tweaks ──────────────────────────────
info "Applying Pi 5 performance settings to config.txt…"
CONFIG_TXT="/boot/firmware/config.txt"
[ -f "$CONFIG_TXT" ] || CONFIG_TXT="/boot/config.txt"

if ! grep -q "HanryxVault" "$CONFIG_TXT" 2>/dev/null; then
    cat >> "$CONFIG_TXT" << 'CFG'

# ── HanryxVault main Pi ─────────────────────────────────────────
# 256 MB GPU memory for smooth 1080p + hardware video decode
gpu_mem=256
# Keep HDMI active even if no display connected at boot
hdmi_force_hotplug:0=1
# Disable blanking — screen stays on permanently
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

# Quiet console boot
for CMDLINE in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
    [ -f "$CMDLINE" ] || continue
    sed -i 's/ consoleblank=[0-9]*//' "$CMDLINE"
    grep -q "loglevel=3" "$CMDLINE" || \
        sed -i 's/$/ quiet loglevel=3 logo.nologo/' "$CMDLINE"
    break
done
ok "Boot console quietened"

# ── 9. Screensaver off via lightdm (X11) ──────────────────────────────────
if [ -f /etc/lightdm/lightdm.conf ]; then
    if ! grep -q "xserver-command" /etc/lightdm/lightdm.conf; then
        sed -i '/^\[Seat:\*\]/a xserver-command=X -s 0 -dpms' /etc/lightdm/lightdm.conf
    fi
    ok "lightdm screensaver disabled"
fi

# ── 10. Write the kiosk launcher script with watchdog ─────────────────────
info "Writing kiosk launcher with watchdog…"
mkdir -p "$HOME_DIR/.hanryx"

cat > "$LAUNCH_SCRIPT" << 'LAUNCH'
#!/usr/bin/env bash
# =============================================================================
# HanryxVault — Kiosk Launcher with Watchdog (runs every boot at desktop login)
# =============================================================================

KIOSK_URL="http://localhost:8080/kiosk"
LOG_FILE="/var/log/hanryx-kiosk.log"
REPO_DIR="$HOME/hanryx-vault-pos"
PROFILE_DIR="$HOME/.hanryx/kiosk-profile"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

log "============================================"
log "HanryxVault kiosk starting…"
log "============================================"

# ── Detect display server (Wayland vs X11) ────────────────────────────────
if [ "$XDG_SESSION_TYPE" = "wayland" ] || pgrep -x labwc > /dev/null 2>&1; then
    DISPLAY_SERVER="wayland"
    log "Display server: Wayland (labwc)"
else
    DISPLAY_SERVER="x11"
    export DISPLAY="${DISPLAY:-:0}"
    log "Display server: X11 (DISPLAY=$DISPLAY)"
fi

# ── Disable screen blanking ──────────────────────────────────────────────
if [ "$DISPLAY_SERVER" = "x11" ]; then
    xset s off    2>/dev/null || true
    xset -dpms    2>/dev/null || true
    xset s noblank 2>/dev/null || true
fi

# ── Hide mouse cursor ────────────────────────────────────────────────────
unclutter -idle 3 -root 2>/dev/null &

# ── Network check (non-blocking) ─────────────────────────────────────────
if ping -c 1 -W 3 8.8.8.8 > /dev/null 2>&1; then
    log "Network: ONLINE"
    NETWORK_STATUS="online"
else
    log "Network: OFFLINE — running on cached data"
    NETWORK_STATUS="offline"
fi

# ── Auto-update: git pull (best-effort, non-blocking, offline-safe) ──────
if [ "$NETWORK_STATUS" = "online" ]; then
    log "Checking for updates…"
    (
        cd "$REPO_DIR" 2>/dev/null || exit 0
        git fetch --quiet origin main 2>/dev/null || true
        git merge --ff-only --quiet origin/main 2>/dev/null || true
        log "Git pull complete"
    ) &
else
    log "Skipping git pull (offline)"
fi

# ── Wait for the POS server to respond via /health (up to 90 s) ──────────
HEALTH_URL="http://localhost:8080/health"
log "Waiting for POS server at $HEALTH_URL …"
READY=0
for i in $(seq 1 45); do
    if curl -sf --max-time 2 "$HEALTH_URL" > /dev/null 2>&1; then
        READY=1
        log "POS server is ready (attempt $i)"
        break
    fi
    [ $((i % 5)) -eq 0 ] && log "Still waiting… attempt $i/45"
    sleep 2
done

if [ "$READY" -eq 0 ]; then
    log "WARNING: POS server not responding after 90 s — launching anyway"
fi

# ── Chromium flags — Pi 5 hardware acceleration + kiosk hardening ────────
CHROMIUM_BIN=$(command -v chromium-browser 2>/dev/null \
               || command -v chromium 2>/dev/null \
               || echo "chromium-browser")

CHROMIUM_FLAGS=(
    --kiosk
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
    CHROMIUM_FLAGS+=(--ozone-platform=wayland)
fi

mkdir -p "$PROFILE_DIR"

# ── Watchdog: auto-restart Chromium if it crashes ────────────────────────
log "Launching kiosk with watchdog…"
while true; do
    "$CHROMIUM_BIN" \
        --user-data-dir="$PROFILE_DIR" \
        "${CHROMIUM_FLAGS[@]}" \
        "$KIOSK_URL" \
        >> "$LOG_FILE" 2>&1

    EXIT_CODE=$?
    log "Kiosk exited (code $EXIT_CODE) — restarting in 5 s…"
    sleep 5
done
LAUNCH

chmod +x "$LAUNCH_SCRIPT"
chown -R "$CURRENT_USER:$CURRENT_USER" "$HOME_DIR/.hanryx" "$LAUNCH_SCRIPT"
ok "Launcher written → $LAUNCH_SCRIPT"

# ── 11. XDG autostart (Wayland / GNOME / modern Pi Bookworm) ─────────────
info "Creating XDG autostart entry…"
mkdir -p "$HOME_DIR/.config/autostart"
cat > "$HOME_DIR/.config/autostart/hanryx-kiosk.desktop" << EOF
[Desktop Entry]
Type=Application
Name=HanryxVault Kiosk
Comment=Customer-facing POS display — auto-launched at desktop login
Exec=/bin/bash -c 'sleep 8 && $LAUNCH_SCRIPT'
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
EOF
chown -R "$CURRENT_USER:$CURRENT_USER" "$HOME_DIR/.config/autostart"
ok "XDG autostart entry created"

# ── 12. labwc autostart (Pi 5 Bookworm Wayland window manager) ───────────
info "Configuring labwc autostart (Pi 5 Wayland)…"
LABWC_DIR="$HOME_DIR/.config/labwc"
mkdir -p "$LABWC_DIR"
LABWC_AUTO="$LABWC_DIR/autostart"
grep -v "hanryx" "$LABWC_AUTO" 2>/dev/null > /tmp/labwc_auto.tmp || true
cat /tmp/labwc_auto.tmp > "$LABWC_AUTO" 2>/dev/null || true
echo "sleep 8 && $LAUNCH_SCRIPT &" >> "$LABWC_AUTO"
chown -R "$CURRENT_USER:$CURRENT_USER" "$LABWC_DIR"
ok "labwc autostart configured"

# ── 13. LXDE autostart backup (pre-Bookworm / X11 fallback) ──────────────
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

# ── 14. Create log file with correct ownership ───────────────────────────
touch "$LOG_FILE"
chown "$CURRENT_USER:$CURRENT_USER" "$LOG_FILE"
ok "Log file ready → $LOG_FILE"

# ── 15. SSH: ensure openssh-server is enabled for remote management ──────
info "Ensuring SSH is enabled…"
systemctl enable ssh 2>/dev/null || true
ok "SSH enabled"

# ── Done ─────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  ============================================================${NC}"
echo -e "${GREEN}  All done — reboot to activate.${NC}"
echo ""
echo "  On every boot the Pi will:"
echo "    1.  Pull latest code from GitHub (if internet available)"
echo "    2.  Start POS Docker containers (works fully offline)"
echo "    3.  Wait for the server to be ready"
echo "    4.  Open Chromium in kiosk mode at /kiosk"
echo "    5.  Auto-restart Chromium if it ever crashes (watchdog)"
echo "    6.  Never sleep or blank the screen"
echo ""
echo -e "${CYAN}  Offline resilience:${NC}"
echo "    • Docker pull is best-effort — skipped when offline"
echo "    • Git pull is best-effort — skipped when offline"
echo "    • Network timeout capped at 15 s — boot never hangs"
echo "    • All data (cards, prices, images) cached in local DB"
echo "    • Kiosk & admin work entirely from local PostgreSQL + Redis"
echo ""
echo -e "${CYAN}  Tips:${NC}"
echo "    • View live logs:  tail -f $LOG_FILE"
echo "    • SSH from laptop: ssh $CURRENT_USER@<pi-ip>"
echo "    • Run launcher manually:  bash $LAUNCH_SCRIPT"
echo "    • Staff admin: http://localhost:8080/admin"
echo ""
warn "GPU memory bumped to 256 MB in config.txt — required for smooth video."
echo ""
echo "  To reboot now:  sudo reboot"
echo ""
