#!/usr/bin/env bash
# =============================================================================
# HanryxVault — Satellite Pi 5 Dual-Monitor Kiosk Boot Setup  (v4 + mDNS)
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
#   • Auto-discovers main Pi via mDNS (hanryxvault.local) — IP changes don't break anything
#   • Branded "Connecting…" splash screen shown while main Pi boots up
#   • Splash auto-redirects to admin/kiosk once server is ready (no timeout crash)
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
echo -e "${CYAN}  If the main Pi has run its setup, it is also reachable as${NC}"
echo -e "${CYAN}  ${BOLD}hanryxvault.local${NC}${CYAN} via mDNS — enter that instead of an IP${NC}"
echo -e "${CYAN}  to survive any DHCP address changes.${NC}"
echo ""
read -rp "  Enter the main Pi's IP or hostname [$DEFAULT_IP]: " MAIN_PI_IP
MAIN_PI_IP="${MAIN_PI_IP:-$DEFAULT_IP}"

# Auto-detect via mDNS if user left blank or typed "auto"
if [[ "${MAIN_PI_IP,,}" == "auto" || "${MAIN_PI_IP,,}" == "discover" ]]; then
    note "Trying mDNS discovery for hanryxvault.local …"
    RESOLVED=$(avahi-resolve-host-name hanryxvault.local 2>/dev/null | awk '{print $2}')
    if [ -n "$RESOLVED" ]; then
        MAIN_PI_IP="hanryxvault.local"
        ok "Discovered main Pi via mDNS: $RESOLVED"
    else
        warn "mDNS discovery failed — using default $DEFAULT_IP"
        MAIN_PI_IP="$DEFAULT_IP"
    fi
fi

ADMIN_URL="http://${MAIN_PI_IP}:8080/admin"
KIOSK_URL="http://${MAIN_PI_IP}:8080/kiosk"
HEALTH_URL="http://${MAIN_PI_IP}:8080/health"

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
HEALTH_URL=$HEALTH_URL
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
  avahi-daemon \
  avahi-utils \
  libnss-mdns \
  2>/dev/null || \
apt-get install -y -qq \
  chromium \
  unclutter \
  curl \
  avahi-daemon \
  avahi-utils \
  libnss-mdns \
  2>/dev/null || true
ok "Packages ready (chromium, unclutter, curl, avahi)"

# Enable avahi so the satellite can resolve hanryxvault.local
systemctl enable avahi-daemon 2>/dev/null || true
systemctl restart avahi-daemon 2>/dev/null || true
ok "mDNS (avahi) running — hanryxvault.local resolution enabled"

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
    ADMIN_URL="http://192.168.86.45:8080/admin"
    KIOSK_URL="http://192.168.86.45:8080/kiosk"
    HEALTH_URL="http://192.168.86.45:8080/health"
fi
# Derive HEALTH_URL from KIOSK_URL if not saved (backward compat)
HEALTH_URL="${HEALTH_URL:-${KIOSK_URL%/kiosk}/health}"
log "Admin URL:  $ADMIN_URL"
log "Kiosk URL:  $KIOSK_URL"
log "Health URL: $HEALTH_URL"

# ── mDNS: try hanryxvault.local if IP unreachable ────────────────────────────
MAIN_PI_IP="${MAIN_PI_IP:-192.168.86.45}"
if ! curl -sf --max-time 1 "$HEALTH_URL" > /dev/null 2>&1; then
    MDNS_IP=$(avahi-resolve-host-name hanryxvault.local 2>/dev/null | awk '{print $2}')
    if [ -n "$MDNS_IP" ] && [ "$MDNS_IP" != "$MAIN_PI_IP" ]; then
        log "mDNS: hanryxvault.local resolved to $MDNS_IP (was $MAIN_PI_IP) — updating URLs"
        MAIN_PI_IP="$MDNS_IP"
        ADMIN_URL="http://${MDNS_IP}:8080/admin"
        KIOSK_URL="http://${MDNS_IP}:8080/kiosk"
        HEALTH_URL="http://${MDNS_IP}:8080/health"
        # Persist updated IP for next boot
        sed -i "s|^MAIN_PI_IP=.*|MAIN_PI_IP=$MDNS_IP|" "$CONFIG_FILE" 2>/dev/null || true
        sed -i "s|^ADMIN_URL=.*|ADMIN_URL=$ADMIN_URL|" "$CONFIG_FILE" 2>/dev/null || true
        sed -i "s|^KIOSK_URL=.*|KIOSK_URL=$KIOSK_URL|" "$CONFIG_FILE" 2>/dev/null || true
        sed -i "s|^HEALTH_URL=.*|HEALTH_URL=$HEALTH_URL|" "$CONFIG_FILE" 2>/dev/null || true
    fi
fi

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
            "$START_URL" \
            >> "$LOG_FILE" 2>&1

        EXIT_CODE=$?
        log "$name exited (code $EXIT_CODE) — restarting in 5 s…"
        sleep 5
    done
}

# ── Start Monitor 1: staff admin ─────────────────────────────────────────────
launch_with_watchdog "Admin (Monitor 1)" "$ADMIN_URL" \
    "$SPLASH_ADMIN" "$PROFILE_ADMIN" 0 &

# Small delay so Monitor 1 claims focus first
sleep 4

# ── Start Monitor 2: customer kiosk ─────────────────────────────────────────
launch_with_watchdog "Kiosk (Monitor 2)" "$KIOSK_URL" \
    "$SPLASH_KIOSK" "$PROFILE_KIOSK" "$MONITOR2_X" &

log "Both windows launched — watchdog + mDNS fallback active"
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
echo "    1.  Show a branded 'Connecting…' splash on both screens immediately"
echo "    2.  Resolve the main Pi via mDNS (hanryxvault.local) if IP changed"
echo "    3.  Splash auto-redirects once /health responds — no timeout crash"
echo "    4.  Monitor 1 (HDMI-0) → /admin   — staff admin"
echo "    5.  Monitor 2 (HDMI-1) → /kiosk   — customer screen"
echo "    6.  Auto-restart both windows if they crash (goes direct on restart)"
echo "    7.  Never sleep or blank either screen"
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
