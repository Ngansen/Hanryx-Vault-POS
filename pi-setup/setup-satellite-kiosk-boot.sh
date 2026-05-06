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
# Defaults: Tailscale mode, Main Pi at 100.125.5.34
# Override defaults by setting env vars before running:
#   MAIN_PI_HOST=100.x.x.x  USE_TAILSCALE=y  TAILSCALE_AUTH_KEY=tskey-auth-...
#   sudo -E bash pi-setup/setup-satellite-kiosk-boot.sh
DEFAULT_HOST="${MAIN_PI_HOST:-100.125.5.34}"
USE_TAILSCALE="y"
if [ -f "$CONFIG_FILE" ]; then
    SAVED_HOST=$(grep "^MAIN_PI_TS_HOST=" "$CONFIG_FILE" 2>/dev/null | cut -d= -f2 || true)
    DEFAULT_HOST="${SAVED_HOST:-$DEFAULT_HOST}"
    SAVED_TS_MODE=$(grep "^USE_TAILSCALE=" "$CONFIG_FILE" 2>/dev/null | cut -d= -f2 || true)
    USE_TAILSCALE="${SAVED_TS_MODE:-y}"
fi

echo -e "${CYAN}  Network layout:${NC}"
echo -e "${CYAN}    Main Pi (home)   = Docker stack + POS server${NC}"
echo -e "${CYAN}    Satellite (here) = dual-monitor kiosk + nginx proxy for the tablet${NC}"
echo -e "${CYAN}    Tablet (shop)    = connects to THIS Pi locally — routed to Main Pi${NC}"
echo ""
echo -e "${CYAN}  How does this satellite connect to the Main Pi?${NC}"
echo -e "${CYAN}    1) Tailscale VPN  — works anywhere, home ↔ shop  [DEFAULT]${NC}"
echo -e "${CYAN}    2) Direct LAN IP  — both Pis must be on the same network${NC}"
echo ""
read -rp "  Choose [1/2] (default: 1 — Tailscale): " CONN_MODE
CONN_MODE="${CONN_MODE:-1}"

if [ "$CONN_MODE" = "2" ]; then
    USE_TAILSCALE="n"
    echo ""
    echo -e "${CYAN}  Enter the Main Pi's LAN IP address (e.g. 192.168.1.100):${NC}"
    read -rp "  Main Pi LAN IP [$DEFAULT_HOST]: " MAIN_PI_TS_HOST
    MAIN_PI_TS_HOST="${MAIN_PI_TS_HOST:-$DEFAULT_HOST}"
else
    USE_TAILSCALE="y"
    echo ""
    echo -e "${CYAN}  Enter the Main Pi's Tailscale IP (100.x.x.x) or hostname:${NC}"
    read -rp "  Main Pi Tailscale IP [$DEFAULT_HOST]: " MAIN_PI_TS_HOST
    MAIN_PI_TS_HOST="${MAIN_PI_TS_HOST:-$DEFAULT_HOST}"
fi

ADMIN_URL="http://${MAIN_PI_TS_HOST}:8080/admin"
KIOSK_URL="http://${MAIN_PI_TS_HOST}:8080/kiosk"
HEALTH_URL="http://${MAIN_PI_TS_HOST}:8080/health"
# Tablet hits satellite Pi on local LAN — nginx proxies to Main Pi via Tailscale
TABLET_PROXY_URL="http://localhost:8080"

echo ""

# ── Ask which physical HDMI port should show which screen ────────────────────
SAVED_SWAP=$(grep "^SWAP_SCREENS=" "$CONFIG_FILE" 2>/dev/null | cut -d= -f2 || true)
SWAP_SCREENS="${SAVED_SWAP:-n}"
echo -e "${CYAN}  Which monitor should show the CUSTOMER KIOSK screen?${NC}"
echo -e "${CYAN}    1) Right monitor / HDMI port 2  (default)${NC}"
echo -e "${CYAN}    2) Left  monitor / HDMI port 1  (swap)${NC}"
echo ""
read -rp "  Choose [1/2] (current: $([ "$SWAP_SCREENS" = "y" ] && echo "2 — swapped" || echo "1 — normal")): " SCR_MODE
SCR_MODE="${SCR_MODE:-}"
if [ "$SCR_MODE" = "2" ]; then
    SWAP_SCREENS="y"
elif [ "$SCR_MODE" = "1" ]; then
    SWAP_SCREENS="n"
fi

echo ""
echo "  User          : $CURRENT_USER"
echo "  Main Pi (TS)  : $MAIN_PI_TS_HOST"
echo "  Monitor 1     : $ADMIN_URL  (staff admin)"
echo "  Monitor 2     : $KIOSK_URL  (customer kiosk)"
echo "  Screen swap   : $SWAP_SCREENS  (y = kiosk on left/HDMI-1)"
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
SWAP_SCREENS=$SWAP_SCREENS
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
# wlr-randr: needed for output detection under labwc/Wayland (xrandr does not work)
apt-get install -y -qq wlr-randr 2>/dev/null || true
ok "Packages ready (nginx, chromium, unclutter, wlr-randr, curl, avahi)"

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

    # Prefer key passed via environment (sudo -E): TAILSCALE_AUTH_KEY=tskey-auth-...
    # If not set, prompt interactively.
    if [ -n "${TAILSCALE_AUTH_KEY:-}" ]; then
        TS_AUTH_KEY="$TAILSCALE_AUTH_KEY"
        ok "Using Tailscale auth key from environment (TAILSCALE_AUTH_KEY)"
    else
        echo -e "${CYAN}  Pass TAILSCALE_AUTH_KEY=tskey-auth-... as an env var to skip this prompt.${NC}"
        echo -e "${CYAN}  Or press Enter to authenticate interactively via browser link.${NC}"
        echo ""
        read -rp "  Paste your Tailscale auth key (or Enter for browser auth): " TS_AUTH_KEY
    fi

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
upstream mainpi {
    server ${MAIN_PI_TS_HOST}:8080;
    keepalive 8;
}

server {
    listen 8080;
    server_name _;

    # Gzip — compress JSON/HTML/CSS/JS on the fly (huge win over LAN)
    gzip             on;
    gzip_types       text/plain text/css application/json application/javascript text/xml;
    gzip_min_length  256;
    gzip_vary        on;

    # Long timeout for SSE (Server-Sent Events) streams
    proxy_read_timeout    600s;
    proxy_send_timeout    300s;
    proxy_connect_timeout  10s;

    # SSE streams — no buffering so events arrive instantly
    location ~ ^/(kiosk/stream|scan/stream) {
        proxy_pass         http://mainpi;
        proxy_http_version 1.1;
        proxy_set_header   Connection '';
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_buffering    off;
        proxy_cache        off;
        chunked_transfer_encoding on;
        # Disable nginx response timeout for long-lived SSE connections
        proxy_read_timeout 86400s;
    }

    # All other routes — with keepalive and sensible buffering
    location / {
        proxy_pass         http://mainpi;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade \$http_upgrade;
        proxy_set_header   Connection '';
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
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
CONNECTION_MODE=$([ "$USE_TAILSCALE" = "y" ] && echo "Tailscale VPN" || echo "Direct LAN")
ok "nginx proxy active — tablet → port 8080 → ${MAIN_PI_TS_HOST}:8080 via ${CONNECTION_MODE}"

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

# ── 3. Increase swap to 1024 MB (smoother dual-monitor + video + Chromium) ──
info "Setting swap to 1024 MB…"
if [ -f /etc/dphys-swapfile ]; then
    sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=1024/' /etc/dphys-swapfile
    dphys-swapfile setup 2>/dev/null || true
    ok "Swap set to 1024 MB"
else
    note "dphys-swapfile not found — skipping swap change"
fi

# ── 3b. Enable zram for compressed swap (Pi 5 — fast RAM swap) ───────────
info "Enabling zram compressed swap…"
if ! lsmod | grep -q zram 2>/dev/null; then
    modprobe zram num_devices=1 2>/dev/null || true
fi
if [ -b /dev/zram0 ] && ! swapon --show | grep -q zram 2>/dev/null; then
    echo lz4 > /sys/block/zram0/comp_algorithm 2>/dev/null || true
    echo 512M > /sys/block/zram0/disksize 2>/dev/null || true
    mkswap /dev/zram0 2>/dev/null && swapon -p 5 /dev/zram0 2>/dev/null || true
fi
# Persist across reboots
if ! grep -q "zram" /etc/modules 2>/dev/null; then
    echo "zram" >> /etc/modules 2>/dev/null || true
fi
ok "zram compressed swap enabled (512 MB, lz4)"

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

# Remove any previous HanryxVault block so re-running setup actually updates it
if grep -q "HanryxVault satellite" "$CONFIG_TXT" 2>/dev/null; then
    info "Removing previous HanryxVault config.txt block…"
    # Delete from the HanryxVault marker through the end-marker (or EOF if no end-marker)
    sed -i '/# ── HanryxVault satellite/,/# ── HanryxVault end ──/d' "$CONFIG_TXT"
fi

cat >> "$CONFIG_TXT" << 'CFG'

# ── HanryxVault satellite Pi 5 ──────────────────────────────────
# 256 MB GPU memory for smooth dual-display + hardware video decode
gpu_mem=256
# Keep both HDMI ports active even if no display connected at boot
hdmi_force_hotplug:0=1
hdmi_force_hotplug:1=1
# Disable blanking — both screens stay on permanently
hdmi_blanking=0
# IMPORTANT: do NOT force a resolution here.
# Each screen negotiates its native resolution via EDID — required for small
# (5", 7") displays that cannot accept 1080p. Forcing a mode causes the screen
# to flash on briefly then go black if the panel cannot handle that mode.
# Quiet boot — remove boot messages from screen
quiet
disable_splash=1
arm_boost=1
# ── HanryxVault end ──
CFG
ok "config.txt updated (resolution auto-negotiated per screen)"

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
# HanryxVault — Satellite Dual-Monitor Kiosk Launcher  (v6.2 XWayland fix)
# =============================================================================
# Pi 5 Bookworm runs labwc (Wayland compositor). This launcher:
#   • Uses XWayland (--ozone-platform=x11) — survives labwc/greeter session swaps
#   • Detects outputs with wlr-randr (xrandr does not work under labwc)
#   • Places each Chromium window on the right monitor via labwc rc.xml rules
#     (--window-position is ignored under Wayland; you MUST use compositor rules)
#   • Logs every step loudly so we can see exactly where it fails if it does
#   • Single instance via flock — only one autostart path is configured (labwc)
# =============================================================================

set -u
LOG_FILE="/var/log/hanryx-kiosk.log"
log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG_FILE" 2>/dev/null; }

# ── Single-instance guard ────────────────────────────────────────────────────
LOCK_FILE="/tmp/hanryx-kiosk-launcher.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "Another launcher instance is already running — exiting (PID $$)"
    exit 0
fi

log "============================================"
log "HanryxVault satellite kiosk v6.2 starting (XWayland mode)"
log "PID=$$  USER=$(id -un)  PPID=$PPID"
log "Initial env: WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-unset}  XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-unset}  DISPLAY=${DISPLAY:-unset}"
log "============================================"

# ── Wayland environment (always native Wayland on Pi 5 labwc) ───────────────
# A wayland-N file existing is NOT enough — stale sockets from prior compositor
# runs persist after crash/reboot. We must actively probe each socket with a
# real client (wlr-randr) and use only the one that the live compositor accepts.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

probe_wayland() {
    local name="$1"
    WAYLAND_DISPLAY="$name" timeout 3 wlr-randr >/dev/null 2>&1
}

find_live_wayland() {
    # Prefer the WAYLAND_DISPLAY inherited from labwc autostart if it works
    if [ -n "${WAYLAND_DISPLAY:-}" ] && probe_wayland "$WAYLAND_DISPLAY"; then
        echo "$WAYLAND_DISPLAY"
        return 0
    fi
    # Otherwise try every wayland-* socket in XDG_RUNTIME_DIR
    for sock in "$XDG_RUNTIME_DIR"/wayland-*; do
        [ -S "$sock" ] || continue
        case "$sock" in *.lock) continue ;; esac
        local n="$(basename "$sock")"
        if probe_wayland "$n"; then
            echo "$n"
            return 0
        fi
    done
    return 1
}

LIVE_WD=""
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    if LIVE_WD="$(find_live_wayland)" && [ -n "$LIVE_WD" ]; then
        break
    fi
    log "No live Wayland compositor yet (attempt $attempt) — waiting 2s…"
    log "  sockets present: $(ls "$XDG_RUNTIME_DIR"/wayland-* 2>/dev/null | tr '\n' ' ')"
    sleep 2
    LIVE_WD=""
done

if [ -z "$LIVE_WD" ]; then
    log "FATAL: no Wayland compositor responded after 30s."
    log "       Available sockets: $(ls "$XDG_RUNTIME_DIR"/wayland-* 2>/dev/null | tr '\n' ' ')"
    log "       The launcher must run inside a labwc session, not from a system service."
    sleep 30
    exit 1
fi

export WAYLAND_DISPLAY="$LIVE_WD"
log "Live Wayland compositor on $WAYLAND_DISPLAY (XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR)"

# ── Load config ──────────────────────────────────────────────────────────────
CONFIG_FILE="$HOME/.hanryx/satellite.conf"
if [ -f "$CONFIG_FILE" ]; then
    source "$CONFIG_FILE"
    log "Loaded config from $CONFIG_FILE"
fi
MAIN_PI_TS_HOST="${MAIN_PI_TS_HOST:-hanryxvault}"
ADMIN_URL="${ADMIN_URL:-http://${MAIN_PI_TS_HOST}:8080/admin}"
KIOSK_URL="${KIOSK_URL:-http://${MAIN_PI_TS_HOST}:8080/kiosk}"
HEALTH_URL="${HEALTH_URL:-http://${MAIN_PI_TS_HOST}:8080/health}"
log "Admin URL : $ADMIN_URL"
log "Kiosk URL : $KIOSK_URL"
log "Health URL: $HEALTH_URL"

PROFILE_ADMIN="$HOME/.hanryx/admin-profile"
PROFILE_KIOSK="$HOME/.hanryx/kiosk-profile"
SPLASH_KIOSK="/tmp/hvault-splash-kiosk.html"
SPLASH_ADMIN="/tmp/hvault-splash-admin.html"
mkdir -p "$PROFILE_ADMIN" "$PROFILE_KIOSK"

# ── Quick non-blocking connectivity check ────────────────────────────────────
if curl -sf --max-time 3 "$HEALTH_URL" > /dev/null 2>&1; then
    log "Main Pi reachable immediately"
else
    log "Main Pi not yet reachable — splash will retry automatically"
    [ "${USE_TAILSCALE:-n}" = "y" ] && systemctl restart tailscaled 2>/dev/null &
fi

# ── Detect Wayland outputs via wlr-randr ────────────────────────────────────
log "Probing outputs with wlr-randr…"
WLR_OUT=$(wlr-randr 2>&1 || true)
log "wlr-randr output ($(echo "$WLR_OUT" | wc -l) lines):"
echo "$WLR_OUT" | while IFS= read -r l; do log "  | $l"; done

declare -A OUT_W
CUR=""
while IFS= read -r line; do
    if [[ "$line" =~ ^([A-Za-z][A-Za-z0-9_-]+) ]]; then
        CUR="${BASH_REMATCH[1]}"
    elif [[ -n "$CUR" && "$line" =~ ^[[:space:]]+([0-9]+)x([0-9]+)[[:space:]]+px ]]; then
        # Only take the FIRST resolution line (the active mode)
        if [ -z "${OUT_W[$CUR]:-}" ]; then
            OUT_W[$CUR]="${BASH_REMATCH[1]}"
        fi
    fi
done <<< "$WLR_OUT"

for o in "${!OUT_W[@]}"; do
    log "Detected output: $o  (${OUT_W[$o]}px wide)"
done

# ── Decide which output is admin (large) vs kiosk (small) ────────────────────
WL_ADMIN=""; WL_KIOSK=""
if [ -n "${ADMIN_OUTPUT:-}" ] && [ -n "${KIOSK_OUTPUT:-}" ]; then
    WL_ADMIN="$ADMIN_OUTPUT"
    WL_KIOSK="$KIOSK_OUTPUT"
    log "Manual override from satellite.conf: Admin=$WL_ADMIN  Kiosk=$WL_KIOSK"
elif [ "${#OUT_W[@]}" -ge 2 ]; then
    MAX=0; MIN=999999
    for o in "${!OUT_W[@]}"; do
        w="${OUT_W[$o]:-0}"
        if [ "$w" -gt "$MAX" ]; then MAX="$w"; WL_ADMIN="$o"; fi
        if [ "$w" -lt "$MIN" ]; then MIN="$w"; WL_KIOSK="$o"; fi
    done
    if [ "$MAX" -eq "$MIN" ]; then
        mapfile -t OUTS < <(printf '%s\n' "${!OUT_W[@]}" | sort)
        if [ "${SWAP_SCREENS:-n}" = "y" ]; then
            WL_ADMIN="${OUTS[1]}"; WL_KIOSK="${OUTS[0]}"
        else
            WL_ADMIN="${OUTS[0]}"; WL_KIOSK="${OUTS[1]}"
        fi
        log "Equal sizes — SWAP_SCREENS=${SWAP_SCREENS:-n}: Admin=$WL_ADMIN  Kiosk=$WL_KIOSK"
    else
        log "Auto-assigned by width: Admin(${MAX}px)=$WL_ADMIN  Kiosk(${MIN}px)=$WL_KIOSK"
    fi
elif [ "${#OUT_W[@]}" -eq 1 ]; then
    only="${!OUT_W[*]}"
    log "WARN: only 1 output detected ($only) — both windows will share it"
    WL_ADMIN="$only"; WL_KIOSK="$only"
else
    log "WARN: no outputs detected — windows will use compositor defaults"
fi

# ── Write labwc rc.xml window rules so each app_id lands on right output ────
LABWC_RC="$HOME/.config/labwc/rc.xml"
mkdir -p "$HOME/.config/labwc"
cat > "$LABWC_RC" << RCEOF
<?xml version="1.0" encoding="UTF-8"?>
<labwc_config>
  <windowRules>
    <windowRule identifier="hvault-admin" matchType="exact">
      <action name="MoveToOutput"><output>${WL_ADMIN}</output></action>
    </windowRule>
    <windowRule identifier="hvault-kiosk" matchType="exact">
      <action name="MoveToOutput"><output>${WL_KIOSK}</output></action>
    </windowRule>
  </windowRules>
</labwc_config>
RCEOF
log "labwc rc.xml written → Admin→${WL_ADMIN:-default}  Kiosk→${WL_KIOSK:-default}"
labwcctl --reconfigure 2>/dev/null || killall -USR1 labwc 2>/dev/null || true

# ── Hide mouse cursor ───────────────────────────────────────────────────────
unclutter --timeout 3 2>/dev/null &

# ── Splash pages ────────────────────────────────────────────────────────────
write_splash() {
    local target_url="$1" label="$2" out_file="$3"
    cat > "$out_file" << HTMLEOF
<!DOCTYPE html><html><head><meta charset="utf-8">
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
  .dot:nth-child(2){animation-delay:.2s}.dot:nth-child(3){animation-delay:.4s}
  @keyframes bounce{0%,80%,100%{transform:scale(0.6);opacity:.4}40%{transform:scale(1);opacity:1}}
  .status{font-size:13px;color:#333;min-height:20px;letter-spacing:.5px}
  .label{position:fixed;bottom:20px;right:24px;font-size:11px;color:#222;letter-spacing:1px}
</style></head><body>
<div class="logo">🃏</div><h1>HanryxVault</h1><p class="sub">Trading Card Shop</p>
<div class="dot-wrap"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>
<p class="status" id="st">Connecting to POS server…</p><div class="label">${label}</div>
<script>
var TARGET="${target_url}",HEALTH="${HEALTH_URL}",attempts=0;
function check(){attempts++;document.getElementById('st').textContent='Connecting to POS server… (attempt '+attempts+')';
  fetch(HEALTH,{cache:'no-store',signal:AbortSignal.timeout(2000)}).then(function(r){if(r.ok){window.location.replace(TARGET);}else{retry();}}).catch(function(){retry();});}
function retry(){setTimeout(check,2000);}check();
</script></body></html>
HTMLEOF
}
write_splash "$KIOSK_URL" "KIOSK DISPLAY" "$SPLASH_KIOSK"
write_splash "$ADMIN_URL" "ADMIN PORTAL"  "$SPLASH_ADMIN"
log "Splash pages written"

# ── Find chromium binary (prefer Pi OS wrapper, NOT raw binary) ─────────────
# /usr/bin/chromium and chromium-browser are wrapper scripts that set up
# the correct env (XDG, GTK, fontconfig). The raw /usr/lib/chromium/chromium
# binary skips that setup and fails to connect to Wayland/XWayland on Pi 5.
if   [ -x /usr/bin/chromium-browser ];  then CHROMIUM_BIN=/usr/bin/chromium-browser
elif [ -x /usr/bin/chromium ];          then CHROMIUM_BIN=/usr/bin/chromium
elif [ -x /usr/lib/chromium/chromium ]; then CHROMIUM_BIN=/usr/lib/chromium/chromium
else                                         CHROMIUM_BIN=chromium
fi
log "Chromium binary: $CHROMIUM_BIN"

# ── Ensure DISPLAY is set for XWayland (labwc auto-starts XWayland) ─────────
# Pi OS Chromium runs as an X11 client through XWayland — NOT native Wayland.
# labwc exports DISPLAY into its session env; if we lost it (e.g. systemd
# respawn), default to :0 which is XWayland's typical socket.
export DISPLAY="${DISPLAY:-:0}"
log "DISPLAY=$DISPLAY  (XWayland via labwc)"

# ── Common Chromium flags (XWayland — Pi OS supported path) ─────────────────
COMMON_FLAGS=(
    --kiosk
    --no-first-run
    --noerrdialogs
    --disable-infobars
    --disable-session-crashed-bubble
    --disable-translate
    --disable-features=Translate,TranslateUI
    --check-for-update-interval=31536000
    --autoplay-policy=no-user-gesture-required
    --disable-background-networking
    --disable-default-apps
    --disable-extensions
    --disable-sync
    --disable-breakpad
    --disable-component-update
    --no-process-singleton
    --ozone-platform=x11
    --enable-features=VaapiVideoDecoder
    --disable-dev-shm-usage
    --allow-file-access-from-files
    --disable-web-security
    # Skip the GNOME keyring "Choose password for new keyring" dialog that
    # blocks chromium on first run when no logind session created the keyring.
    --password-store=basic
    --use-mock-keychain
)

# ── Kill any stale chromium / locks ─────────────────────────────────────────
pkill -f 'chromium' 2>/dev/null || true
sleep 1
rm -f "$PROFILE_ADMIN"/Singleton* "$PROFILE_KIOSK"/Singleton* 2>/dev/null

# ── Launch function with watchdog ───────────────────────────────────────────
launch_window() {
    local name="$1" url="$2" splash="$3" profile="$4" app_id="$5"
    local first_run=1 quick_crashes=0 using_fallback=0
    local backoff=5 max_backoff=60
    local FALLBACK_FLAGS=()
    for f in "${COMMON_FLAGS[@]}"; do
        case "$f" in
            --enable-features=VaapiVideoDecoder) FALLBACK_FLAGS+=() ;;
            *) FALLBACK_FLAGS+=("$f") ;;
        esac
    done
    FALLBACK_FLAGS+=(--disable-gpu --use-gl=swiftshader)

    log "[$name] launch loop starting (app_id=$app_id, profile=$profile)"
    while true; do
        if [ "$first_run" -eq 1 ]; then
            START_URL="file://${splash}"
            first_run=0
        else
            START_URL="$url"
        fi
        if [ "$using_fallback" -eq 1 ]; then
            FLAGS=("${FALLBACK_FLAGS[@]}")
            mode="fallback(--disable-gpu)"
        else
            FLAGS=("${COMMON_FLAGS[@]}")
            mode="ANGLE swiftshader"
        fi
        log "[$name] → $START_URL  ($mode)  backoff=${backoff}s"
        rm -f "$profile"/Singleton* 2>/dev/null
        START=$(date +%s)
        "$CHROMIUM_BIN" \
            "${FLAGS[@]}" \
            --user-data-dir="$profile" \
            --class="$app_id" \
            "$START_URL" >> "$LOG_FILE" 2>&1
        EXIT=$?
        ELAPSED=$(( $(date +%s) - START ))
        # Check if it forked into a child (chromium often does)
        sleep 1
        if pgrep -f -- "user-data-dir=${profile}" > /dev/null 2>&1; then
            log "[$name] launcher exited code=$EXIT after ${ELAPSED}s — child still alive, waiting"
            # Stable run — reset crash budget + backoff
            quick_crashes=0
            backoff=5
            while pgrep -f -- "user-data-dir=${profile}" > /dev/null 2>&1; do
                sleep 3
            done
            log "[$name] child process ended — restarting in ${backoff}s"
        else
            if [ "$ELAPSED" -lt 4 ]; then
                quick_crashes=$(( quick_crashes + 1 ))
                log "[$name] crashed in ${ELAPSED}s (count=$quick_crashes, mode=$mode)"
                # Exponential backoff capped at max_backoff
                backoff=$(( backoff * 2 ))
                if [ "$backoff" -gt "$max_backoff" ]; then backoff=$max_backoff; fi
                # First time we hit 2 quick crashes on ANGLE → drop to fallback
                if [ "$using_fallback" -eq 0 ] && [ "$quick_crashes" -ge 2 ]; then
                    log "[$name] ANGLE unstable — switching to --disable-gpu fallback"
                    using_fallback=1
                    quick_crashes=0
                    backoff=5
                # If even fallback keeps crashing, log loudly every 5 attempts
                elif [ "$using_fallback" -eq 1 ] && [ $(( quick_crashes % 5 )) -eq 0 ]; then
                    log "[$name] WARN: fallback mode also crashing repeatedly ($quick_crashes total). Check display, GPU, or chromium install."
                fi
            else
                # Ran for >=4s before exit → not a crash loop; reset budget
                log "[$name] exited code=$EXIT after ${ELAPSED}s — restarting in ${backoff}s"
                quick_crashes=0
                backoff=5
            fi
        fi
        sleep "$backoff"
    done
}

# ── Start both windows ──────────────────────────────────────────────────────
launch_window "Admin" "$ADMIN_URL" "$SPLASH_ADMIN" "$PROFILE_ADMIN" "hvault-admin" &
sleep 4
launch_window "Kiosk" "$KIOSK_URL" "$SPLASH_KIOSK" "$PROFILE_KIOSK" "hvault-kiosk" &

CONNECTION_MODE=$([ -n "${USE_TAILSCALE:-}" ] && [ "$USE_TAILSCALE" = "y" ] && echo "Tailscale" || echo "LAN")
log "Both windows launched — $CONNECTION_MODE tunnel active"

# ── Heartbeat: register satellite with main Pi every 60 s ──────────────────
HEARTBEAT_PID_FILE="/tmp/hanryx-heartbeat.pid"
if [ -f "$HEARTBEAT_PID_FILE" ]; then
    OLD_PID=$(cat "$HEARTBEAT_PID_FILE" 2>/dev/null)
    kill "$OLD_PID" 2>/dev/null || true
fi
OWN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
OWN_TS_IP=$(tailscale ip -4 2>/dev/null || echo "unknown")
(
    while true; do
        UPTIME=$(uptime -p 2>/dev/null || uptime | sed 's/.*up //' | cut -d, -f1)
        CHROMIUM_OK=$(pgrep -f 'chromium' > /dev/null 2>&1 && echo true || echo false)
        TS_STATUS=$(tailscale status --json 2>/dev/null | python3 -c \
            "import sys,json; d=json.load(sys.stdin); print('connected' if d.get('BackendState')=='Running' else 'disconnected')" \
            2>/dev/null || echo "unknown")
        curl -sf --max-time 5 -X POST "${HEALTH_URL%/health}/satellite/heartbeat" \
             -H "Content-Type: application/json" \
             -d "{\"ip\":\"$OWN_IP\",\"ts_ip\":\"$OWN_TS_IP\",\"uptime\":\"$UPTIME\",\"chromium_ok\":$CHROMIUM_OK,\"tailscale\":\"$TS_STATUS\",\"version\":\"v6.2\"}" \
             > /dev/null 2>&1 || true
        sleep 60
    done
) &
echo $! > "$HEARTBEAT_PID_FILE"
log "Heartbeat loop started (PID $!)"

wait
LAUNCH

chmod +x "$LAUNCH_SCRIPT"
chown -R "$CURRENT_USER:$CURRENT_USER" "$HOME_DIR/.hanryx" "$LAUNCH_SCRIPT"
ok "Launcher written → $LAUNCH_SCRIPT"

# ── 9. Autostart — labwc only (single entry, no race) ───────────────────────
# Pi 5 Bookworm uses labwc (Wayland). Multiple autostart paths cause race
# conditions where two launchers fight for the SingletonLock and both die.
# We register ONLY in labwc autostart and explicitly REMOVE the XDG and
# LXDE entries that previous setup runs may have left behind.
info "Configuring labwc autostart (single entry)…"
LABWC_DIR="$HOME_DIR/.config/labwc"
mkdir -p "$LABWC_DIR"
LABWC_AUTO="$LABWC_DIR/autostart"
grep -v "hanryx" "$LABWC_AUTO" 2>/dev/null > /tmp/labwc_auto.tmp || true
cat /tmp/labwc_auto.tmp > "$LABWC_AUTO" 2>/dev/null || true
echo "sleep 5 && $LAUNCH_SCRIPT &" >> "$LABWC_AUTO"
chown -R "$CURRENT_USER:$CURRENT_USER" "$LABWC_DIR"
ok "labwc autostart configured → $LABWC_AUTO"

info "Removing legacy XDG/LXDE autostart entries (avoid races)…"
rm -f "$HOME_DIR/.config/autostart/hanryx-dual-kiosk.desktop" 2>/dev/null || true
LXDE_AUTO="$HOME_DIR/.config/lxsession/LXDE-pi/autostart"
if [ -f "$LXDE_AUTO" ]; then
    grep -v "hanryx" "$LXDE_AUTO" > /tmp/lxde_auto.tmp 2>/dev/null || true
    cat /tmp/lxde_auto.tmp > "$LXDE_AUTO" 2>/dev/null || true
fi
ok "Legacy autostart entries removed"

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
        echo "[watchdog] Server back online — killing Chromium so launcher restart loop reconnects" | tee -a "\$LOG"; \\
        pkill -f chromium 2>/dev/null; \\
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
