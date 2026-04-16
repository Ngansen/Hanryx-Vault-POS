#!/usr/bin/env bash
# =============================================================================
# HanryxVault — Satellite Pi 5 Dual-Monitor Kiosk Boot Setup  (v5 + Tailscale)
#
# Network topology:
#   Main Pi   (home)  — Flask POS server, Docker stack, database, Tailscale
#   Satellite Pi (shop) — THIS Pi: dual-monitor kiosk + nginx proxy for tablet
#   Tablet    (shop)  — Expo POS app, connects to satellite Pi on local LAN
#
# What this configures:
#   Monitor 1 (HDMI-0) → /admin   — staff admin portal (via Tailscale)
#   Monitor 2 (HDMI-1) → /kiosk   — customer-facing display (via Tailscale)
#   nginx on port 8080 → proxies tablet API calls over Tailscale to Main Pi
#
# Features:
#   • Installs Tailscale — satellite Pi joins the same network as the Main Pi
#   • Asks for Main Pi's Tailscale hostname during setup
#   • nginx reverse proxy — tablet hits satellite Pi locally, routed to Main Pi
#   • Satellite Pi advertised as hanryxvault.local for tablet discovery
#   • Branded "Connecting…" splash screen checks Tailscale reachability
#   • Detects Wayland (Pi 5 Bookworm default) vs X11 automatically
#   • Hardware-accelerated video decode for smooth Pokémon playback
#   • GPU memory bump to 256 MB for dual 1080p + video
#   • Chromium watchdog — auto-restarts both windows if they crash
#   • Persistent Chromium profiles (survive crashes, keep login sessions)
#   • Logs everything to /var/log/hanryx-kiosk.log for easy debugging
#   • Disables USB autosuspend (keeps scanners & receipt printers alive)
#   • Removes boot rainbow splash & quietens console output
#   • Increases swap to 512 MB for smoother multi-tab performance
#   • Handles both labwc (Wayland) and LXDE (X11) autostart paths
#   • SSH stays accessible for remote management
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

# ── Ask for Main Pi connection details ────────────────────────────────────────
DEFAULT_HOST="hanryxvault"
USE_TAILSCALE="n"
if [ -f "$CONFIG_FILE" ]; then
    SAVED_HOST=$(grep "^MAIN_PI_TS_HOST=" "$CONFIG_FILE" 2>/dev/null | cut -d= -f2)
    DEFAULT_HOST="${SAVED_HOST:-$DEFAULT_HOST}"
    SAVED_TS_MODE=$(grep "^USE_TAILSCALE=" "$CONFIG_FILE" 2>/dev/null | cut -d= -f2)
    USE_TAILSCALE="${SAVED_TS_MODE:-n}"
fi

echo -e "${CYAN}  Network layout:${NC}"
echo -e "${CYAN}    Main Pi (home)   = Docker stack + POS server${NC}"
echo -e "${CYAN}    Satellite (here) = dual-monitor kiosk + nginx proxy for the tablet${NC}"
echo -e "${CYAN}    Tablet (shop)    = connects to THIS Pi locally — routed to Main Pi${NC}"
echo ""
echo -e "${CYAN}  How does this satellite connect to the Main Pi?${NC}"
echo -e "${CYAN}    1) Direct LAN IP  — both Pis on the same network (simplest)${NC}"
echo -e "${CYAN}    2) Tailscale VPN  — Pis on different networks (home ↔ shop)${NC}"
echo ""
read -rp "  Choose [1/2] (default: 1 — direct LAN): " CONN_MODE
CONN_MODE="${CONN_MODE:-1}"

if [ "$CONN_MODE" = "2" ]; then
    USE_TAILSCALE="y"
    echo ""
    echo -e "${CYAN}  Enter the Main Pi's Tailscale hostname or IP (100.x.x.x):${NC}"
    read -rp "  Main Pi Tailscale hostname [$DEFAULT_HOST]: " MAIN_PI_TS_HOST
    MAIN_PI_TS_HOST="${MAIN_PI_TS_HOST:-$DEFAULT_HOST}"
else
    USE_TAILSCALE="n"
    echo ""
    echo -e "${CYAN}  Enter the Main Pi's LAN IP address (e.g. 192.168.1.100):${NC}"
    read -rp "  Main Pi LAN IP [$DEFAULT_HOST]: " MAIN_PI_TS_HOST
    MAIN_PI_TS_HOST="${MAIN_PI_TS_HOST:-$DEFAULT_HOST}"
fi

ADMIN_URL="http://${MAIN_PI_TS_HOST}:8080/admin"
KIOSK_URL="http://${MAIN_PI_TS_HOST}:8080/kiosk"
HEALTH_URL="http://${MAIN_PI_TS_HOST}:8080/health"
# Tablet hits satellite Pi on local LAN — nginx proxies to Main Pi via Tailscale
TABLET_PROXY_URL="http://localhost:8080"

echo ""
echo "  User          : $CURRENT_USER"
echo "  Main Pi (TS)  : $MAIN_PI_TS_HOST"
echo "  Monitor 1     : $ADMIN_URL  (staff admin)"
echo "  Monitor 2     : $KIOSK_URL  (customer kiosk)"
echo "  Tablet proxy  : port 8080 → $MAIN_PI_TS_HOST:8080"
echo "  Connection    : $([ "$USE_TAILSCALE" = "y" ] && echo "Tailscale VPN" || echo "Direct LAN")"
echo "  Logs          : $LOG_FILE"
echo ""

# Save config for future re-runs
mkdir -p "$HOME_DIR/.hanryx"
cat > "$CONFIG_FILE" << EOF
MAIN_PI_TS_HOST=$MAIN_PI_TS_HOST
ADMIN_URL=$ADMIN_URL
KIOSK_URL=$KIOSK_URL
HEALTH_URL=$HEALTH_URL
USE_TAILSCALE=$USE_TAILSCALE
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
  nginx \
  curl \
  avahi-daemon \
  avahi-utils \
  libnss-mdns \
  2>/dev/null || true
# Chromium: try both package names
apt-get install -y -qq chromium-browser unclutter 2>/dev/null || \
apt-get install -y -qq chromium         unclutter 2>/dev/null || true
ok "Packages ready (nginx, chromium, unclutter, curl, avahi)"

# ── Tailscale (optional) ────────────────────────────────────────────────────
if [ "$USE_TAILSCALE" = "y" ]; then
    info "Installing Tailscale…"
    if ! command -v tailscale &>/dev/null; then
        curl -fsSL https://tailscale.com/install.sh | sh
        ok "Tailscale installed"
    else
        ok "Tailscale already installed ($(tailscale version | head -1))"
    fi

    echo ""
    echo -e "${CYAN}  ── Tailscale authentication ─────────────────────────────${NC}"
    echo -e "${CYAN}  You need to connect this satellite Pi to your Tailscale network.${NC}"
    echo -e "${CYAN}  Options:${NC}"
    echo -e "${CYAN}    A) Auth key (recommended — paste from Tailscale admin panel)${NC}"
    echo -e "${CYAN}    B) Interactive — browser link printed for you to approve${NC}"
    echo ""
    read -rp "  Paste your Tailscale auth key (or press Enter to authenticate interactively): " TS_AUTH_KEY

    if [ -n "$TS_AUTH_KEY" ]; then
        tailscale up --authkey="$TS_AUTH_KEY" --hostname="hanryxvault-sat" 2>/dev/null || \
        tailscale up --authkey="$TS_AUTH_KEY" 2>/dev/null || true
        ok "Tailscale connected with auth key"
    else
        tailscale up --hostname="hanryxvault-sat" 2>/dev/null &
        TS_PID=$!
        echo ""
        note "Follow the link above to authorise this Pi in the Tailscale admin panel."
        read -rp "  Press Enter once you've approved the device in Tailscale… "
        wait $TS_PID 2>/dev/null || true
    fi

    TS_IP=$(tailscale ip -4 2>/dev/null || echo "not connected yet")
    ok "Tailscale IP: $TS_IP"
    systemctl enable tailscaled 2>/dev/null || true
else
    note "Tailscale skipped — using direct LAN connection to $MAIN_PI_TS_HOST"
fi

# ── nginx proxy — tablet LAN → Tailscale → Main Pi ──────────────────────────
info "Configuring nginx proxy (tablet traffic → Main Pi via Tailscale)…"
cat > /etc/nginx/sites-available/hanryxvault-proxy << NGINX
# HanryxVault satellite nginx proxy
# Tablet hits http://hanryxvault.local:8080/ → forwarded to Main Pi via Tailscale
server {
    listen 8080;
    server_name _;

    # Long timeout for SSE (Server-Sent Events) streams
    proxy_read_timeout    300s;
    proxy_send_timeout    300s;
    proxy_connect_timeout  10s;

    location / {
        proxy_pass         http://${MAIN_PI_TS_HOST}:8080;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade \$http_upgrade;
        proxy_set_header   Connection '';
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        # Required for SSE — disable buffering so events reach the client instantly
        proxy_buffering    off;
        proxy_cache        off;
        chunked_transfer_encoding on;
    }
}
NGINX

# Activate site
ln -sf /etc/nginx/sites-available/hanryxvault-proxy \
       /etc/nginx/sites-enabled/hanryxvault-proxy 2>/dev/null || true
rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true
nginx -t && systemctl enable nginx && systemctl restart nginx
ok "nginx proxy active — tablet → port 8080 → ${MAIN_PI_TS_HOST}:8080 via Tailscale"

# ── avahi — advertise this satellite Pi as hanryxvault.local on the shop LAN ─
info "Configuring avahi mDNS (satellite Pi = hanryxvault.local on shop LAN)…"
hostnamectl set-hostname hanryxvault 2>/dev/null || \
    echo "hanryxvault" > /etc/hostname
# Update /etc/hosts to match
sed -i '/127\.0\.1\.1/d' /etc/hosts
echo "127.0.1.1  hanryxvault hanryxvault.local" >> /etc/hosts
systemctl enable avahi-daemon 2>/dev/null || true
systemctl restart avahi-daemon 2>/dev/null || true
ok "Satellite Pi is now hanryxvault.local on the shop LAN (tablet can find it automatically)"

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
# HanryxVault — Satellite Dual-Monitor Kiosk Launcher  (v4 + mDNS + splash)
# Connects to the main Pi's POS server — no Docker needed on this Pi.
# =============================================================================

LOG_FILE="/var/log/hanryx-kiosk.log"
CONFIG_FILE="$HOME/.hanryx/satellite.conf"
PROFILE_ADMIN="$HOME/.hanryx/admin-profile"
PROFILE_KIOSK="$HOME/.hanryx/kiosk-profile"
SPLASH_KIOSK="/tmp/hvault-splash-kiosk.html"
SPLASH_ADMIN="/tmp/hvault-splash-admin.html"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

log "============================================"
log "HanryxVault satellite kiosk starting…"
log "============================================"

# ── Load config ──────────────────────────────────────────────────────────────
if [ -f "$CONFIG_FILE" ]; then
    source "$CONFIG_FILE"
else
    MAIN_PI_TS_HOST="hanryxvault"
    ADMIN_URL="http://hanryxvault:8080/admin"
    KIOSK_URL="http://hanryxvault:8080/kiosk"
    HEALTH_URL="http://hanryxvault:8080/health"
fi
HEALTH_URL="${HEALTH_URL:-${KIOSK_URL%/kiosk}/health}"
log "Main Pi (Tailscale): $MAIN_PI_TS_HOST"
log "Admin URL:  $ADMIN_URL"
log "Kiosk URL:  $KIOSK_URL"
log "Health URL: $HEALTH_URL"

# ── Wait for Main Pi to be reachable before doing anything ────────────────────
log "Waiting for Main Pi (${MAIN_PI_TS_HOST}) to become reachable…"
WAIT_COUNT=0
until curl -sf --max-time 3 "$HEALTH_URL" > /dev/null 2>&1; do
    WAIT_COUNT=$((WAIT_COUNT + 1))
    if [ "$WAIT_COUNT" -eq 1 ]; then
        log "Main Pi not reachable yet — waiting for connection…"
    fi
    if [ "$WAIT_COUNT" -ge 5 ]; then
        if [ "${USE_TAILSCALE:-n}" = "y" ]; then
            systemctl restart tailscaled 2>/dev/null || true
            log "Restarted tailscaled — retrying…"
        else
            log "Still waiting for Main Pi on LAN ($MAIN_PI_TS_HOST)…"
        fi
        sleep 5
        WAIT_COUNT=0
    fi
    sleep 3
done
log "Main Pi reachable — launching kiosk"

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

# ── Hide mouse cursor ─────────────────────────────────────────────────────────
unclutter -idle 3 -root 2>/dev/null &

# ── Write branded splash pages ───────────────────────────────────────────────
# Each splash polls /health and auto-navigates to the real URL when ready.
write_splash() {
    local target_url="$1"
    local label="$2"
    local out_file="$3"
    cat > "$out_file" << HTMLEOF
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HanryxVault — Connecting…</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:#0a0a0a;color:#fff;font-family:'Segoe UI',sans-serif;
       display:flex;flex-direction:column;align-items:center;justify-content:center;
       height:100vh;overflow:hidden}
  .logo{font-size:72px;margin-bottom:28px;animation:pulse 2s ease-in-out infinite}
  @keyframes pulse{0%,100%{opacity:.7;transform:scale(1)}50%{opacity:1;transform:scale(1.05)}}
  h1{font-size:36px;font-weight:900;color:#facc15;letter-spacing:2px;margin-bottom:8px}
  .sub{font-size:15px;color:#555;margin-bottom:48px;letter-spacing:1px}
  .dot-wrap{display:flex;gap:12px;margin-bottom:36px}
  .dot{width:12px;height:12px;border-radius:50%;background:#facc15;
       animation:bounce 1.4s ease-in-out infinite}
  .dot:nth-child(2){animation-delay:.2s}
  .dot:nth-child(3){animation-delay:.4s}
  @keyframes bounce{0%,80%,100%{transform:scale(0.6);opacity:.4}40%{transform:scale(1);opacity:1}}
  .status{font-size:13px;color:#333;min-height:20px;letter-spacing:.5px}
  .label{position:fixed;bottom:20px;right:24px;font-size:11px;color:#222;letter-spacing:1px}
</style>
</head>
<body>
<div class="logo">🃏</div>
<h1>HanryxVault</h1>
<p class="sub">Trading Card Shop</p>
<div class="dot-wrap"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>
<p class="status" id="st">Connecting to POS server…</p>
<div class="label">${label}</div>
<script>
var TARGET = "${target_url}";
var HEALTH = "${HEALTH_URL}";
var attempts = 0;
function check() {
  attempts++;
  document.getElementById('st').textContent =
    'Connecting to POS server… (attempt ' + attempts + ')';
  fetch(HEALTH, {cache:'no-store', signal: AbortSignal.timeout(2000)})
    .then(function(r){ if(r.ok){ window.location.replace(TARGET); } else { retry(); } })
    .catch(function(){ retry(); });
}
function retry() { setTimeout(check, 2000); }
check();
</script>
</body>
</html>
HTMLEOF
}

write_splash "$KIOSK_URL" "KIOSK DISPLAY" "$SPLASH_KIOSK"
write_splash "$ADMIN_URL" "ADMIN PORTAL"  "$SPLASH_ADMIN"
log "Splash pages written"

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
    # Allow file:// to fetch http:// (for splash page health polling)
    --allow-file-access-from-files
    --disable-web-security
)

if [ "$DISPLAY_SERVER" = "wayland" ]; then
    COMMON_FLAGS+=(--ozone-platform=wayland)
fi

# ── Helper: find chromium binary ─────────────────────────────────────────────
CHROMIUM_BIN=$(command -v chromium-browser 2>/dev/null \
               || command -v chromium 2>/dev/null \
               || echo "chromium-browser")

# ── Launch function with watchdog ────────────────────────────────────────────
# On first launch: open the splash page (which auto-redirects to real URL).
# On restart (crash recovery): go straight to real URL (server already up).
launch_with_watchdog() {
    local name="$1"
    local real_url="$2"
    local splash_file="$3"
    local profile="$4"
    local pos_x="$5"
    local extra_flags=("${@:6}")   # optional extra flags per-window
    local first_run=1

    mkdir -p "$profile"
    log "Launching $name (position ${pos_x},0)"

    while true; do
        if [ "$first_run" -eq 1 ]; then
            START_URL="file://${splash_file}"
            first_run=0
        else
            # After a crash, server is likely still running — go direct
            START_URL="$real_url"
        fi

        log "$name → $START_URL"
        "$CHROMIUM_BIN" \
            --kiosk \
            --window-position="${pos_x},0" \
            --user-data-dir="$profile" \
            "${COMMON_FLAGS[@]}" \
            "${extra_flags[@]}" \
            "$START_URL" \
            >> "$LOG_FILE" 2>&1

        EXIT_CODE=$?
        log "$name exited (code $EXIT_CODE) — restarting in 5 s…"
        sleep 5
    done
}

# ── Start Monitor 1: staff admin  (restore session on crash so you stay logged in)
launch_with_watchdog "Admin (Monitor 1)" "$ADMIN_URL" \
    "$SPLASH_ADMIN" "$PROFILE_ADMIN" 0 \
    "--restore-last-session" "--password-store=basic" &

# Small delay so Monitor 1 claims focus first
sleep 4

# ── Start Monitor 2: customer kiosk ─────────────────────────────────────────
launch_with_watchdog "Kiosk (Monitor 2)" "$KIOSK_URL" \
    "$SPLASH_KIOSK" "$PROFILE_KIOSK" "$MONITOR2_X" &

log "Both windows launched — Tailscale tunnel active"

# ── Heartbeat: register satellite with main Pi every 60 s ──────────────────
OWN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
OWN_TS_IP=$(tailscale ip -4 2>/dev/null || echo "unknown")
(
  while true; do
    UPTIME=$(uptime -p 2>/dev/null || uptime | sed 's/.*up //' | cut -d, -f1)
    CHROMIUM_OK=$(pgrep -x chromium-browser > /dev/null 2>&1 \
                  || pgrep -x chromium > /dev/null 2>&1; echo $?)
    CHROMIUM_OK=$([[ "$CHROMIUM_OK" -eq 0 ]] && echo true || echo false)
    TS_STATUS=$(tailscale status --json 2>/dev/null | python3 -c \
        "import sys,json; d=json.load(sys.stdin); print('connected' if d.get('BackendState')=='Running' else 'disconnected')" \
        2>/dev/null || echo "unknown")
    curl -sf --max-time 5 -X POST "${HEALTH_URL%/health}/satellite/heartbeat" \
         -H "Content-Type: application/json" \
         -d "{\"ip\":\"$OWN_IP\",\"ts_ip\":\"$OWN_TS_IP\",\"uptime\":\"$UPTIME\",\"chromium_ok\":$CHROMIUM_OK,\"tailscale\":\"$TS_STATUS\",\"version\":\"v5\"}" \
         > /dev/null 2>&1 || true
    sleep 60
  done
) &
log "Heartbeat loop started — pinging main Pi via Tailscale every 60 s"

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

# ── 14. Network watchdog service — restarts Chromium if server goes MIA ──────
info "Installing network watchdog service…"
cat > /etc/systemd/system/hanryx-watchdog.service << EOF
[Unit]
Description=HanryxVault Satellite Network Watchdog
After=network-online.target

[Service]
Type=simple
User=$CURRENT_USER
Environment=HOME=/home/$CURRENT_USER
ExecStart=/bin/bash -c '\\
  LOG="/var/log/hanryx-kiosk.log"; \\
  CONF="$CONFIG_FILE"; \\
  FAIL=0; \\
  while true; do \\
    source "\$CONF" 2>/dev/null || true; \\
    HURL="\${HEALTH_URL:-http://hanryxvault:8080/health}"; \\
    if curl -sf --max-time 3 "\$HURL" > /dev/null 2>&1; then \\
      if [ "\$FAIL" -ge 3 ]; then \\
        echo "[watchdog] Server back online — restarting Chromium" | tee -a "\$LOG"; \\
        pkill -x chromium-browser 2>/dev/null; pkill -x chromium 2>/dev/null; \\
        sleep 3; \\
        bash $LAUNCH_SCRIPT & \\
      fi; \\
      FAIL=0; \\
    else \\
      FAIL=\$((FAIL+1)); \\
      echo "[watchdog] Health check failed #\$FAIL" | tee -a "\$LOG"; \\
      if [ "\$FAIL" -eq 3 ]; then \\
        echo "[watchdog] 3 consecutive failures — Chromium will reload on recovery" | tee -a "\$LOG"; \\
      fi; \\
    fi; \\
    sleep 30; \\
  done'
Restart=always
RestartSec=10
StandardOutput=append:/var/log/hanryx-kiosk.log
StandardError=append:/var/log/hanryx-kiosk.log

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable hanryx-watchdog.service
ok "Network watchdog service installed and enabled"
note "Watchdog pings /health every 30 s and reloads Chromium if server comes back after ≥3 failures"

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  ============================================================${NC}"
echo -e "${GREEN}  All done — reboot to activate.${NC}"
echo ""
echo "  Network layout after reboot:"
echo "    Main Pi (home)   → Tailscale host: $MAIN_PI_TS_HOST  (port 8080)"
echo "    Satellite (here) → hanryxvault.local on shop LAN, Tailscale: hanryxvault-sat"
echo "    Tablet           → connect to http://hanryxvault.local:8080/ (auto-proxied)"
echo ""
echo "  On every boot the satellite Pi will:"
echo "    1.  Wait for Tailscale tunnel to Main Pi before launching Chromium"
echo "    2.  Show branded 'Connecting…' splash on both screens while waiting"
echo "    3.  Monitor 1 (HDMI-0) → /admin  (directly via Tailscale)"
echo "    4.  Monitor 2 (HDMI-1) → /kiosk  (directly via Tailscale)"
echo "    5.  nginx on port 8080 proxies all tablet traffic to Main Pi via Tailscale"
echo "    6.  Auto-restart both Chromium windows if they crash"
echo "    7.  Never sleep or blank either screen"
echo "    8.  Send heartbeat to Main Pi every 60 s (visible on System page)"
echo "    9.  Network watchdog re-triggers Chromium if Tailscale recovers from outage"
echo "   10.  Admin session restored after Chromium crash (stays logged in)"
echo ""
echo -e "${CYAN}  Main Pi Tailscale hostname:${NC} $MAIN_PI_TS_HOST"
echo -e "${CYAN}  To change later:${NC} edit $CONFIG_FILE then: sudo systemctl restart nginx && reboot"
echo ""
echo -e "${CYAN}  Tips:${NC}"
echo "    • Check Tailscale connection:   tailscale status"
echo "    • Check nginx proxy:            curl http://localhost:8080/health"
echo "    • Tablet finds this Pi as:      http://hanryxvault.local:8080/"
echo "    • View live logs:               tail -f $LOG_FILE"
echo "    • SSH from your laptop:         ssh $CURRENT_USER@hanryxvault-sat"
echo "    • Swap HDMI cables if the screens are the wrong way round"
echo "    • Run the launcher manually:    bash $LAUNCH_SCRIPT"
echo ""
warn "GPU memory bumped to 256 MB in config.txt — required for smooth video."
echo ""
echo "  If Tailscale isn't connected yet, run:  tailscale up"
echo "  To reboot now:  sudo reboot"
echo ""
