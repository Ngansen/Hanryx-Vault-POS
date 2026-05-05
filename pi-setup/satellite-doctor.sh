#!/usr/bin/env bash
# =============================================================================
# satellite-doctor.sh — read-only diagnostic for the satellite Pi
#
# Run this on the SATELLITE Pi to dump the full state of the kiosk system
# without changing anything. Output is intentionally verbose — paste it back
# and we can pinpoint exactly which of the SATELLITE_AUDIT.md issues are
# active in your specific setup.
#
# Usage:
#   bash ~/Hanryx-Vault-POS/pi-setup/satellite-doctor.sh
#
# Exit code is always 0 — diagnostic only, never fails.
# =============================================================================
set -u

C='\033[0;36m'; G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[1m'; N='\033[0m'
section() { echo ""; echo -e "${B}${C}── $1 ──${N}"; }
ok()      { echo -e "${G}[✓]${N} $1"; }
warn()    { echo -e "${Y}[!]${N} $1"; }
bad()     { echo -e "${R}[✗]${N} $1"; }
info()    { echo "    $1"; }

CURRENT_USER="${SUDO_USER:-$(whoami)}"
HOME_DIR=$(getent passwd "$CURRENT_USER" | cut -d: -f6)
CONFIG_FILE="$HOME_DIR/.hanryx/satellite.conf"
LOG_FILE="/var/log/hanryx-kiosk.log"
LAUNCH_SCRIPT="$HOME_DIR/.hanryx-dual-monitor.sh"

echo ""
echo -e "${B}  HanryxVault — Satellite Diagnostic Report${N}"
echo "  $(date -Is) — host $(hostname) — user $CURRENT_USER"

# ── 1. Identity ─────────────────────────────────────────────────────────────
section "1. Identity & network"
echo "    hostname:     $(hostname)"
echo "    hostname.local: $(hostname).local"
if [ "$(hostname)" = "hanryxvault" ]; then
    bad "Hostname is 'hanryxvault' — collides with main Pi (Audit issue #1)"
elif [ "$(hostname)" = "hanryxvault-sat" ]; then
    ok "Hostname distinct from main Pi"
else
    warn "Hostname is '$(hostname)' — not the recommended 'hanryxvault-sat'"
fi
echo "    LAN IPs:      $(hostname -I 2>/dev/null)"
if command -v tailscale >/dev/null 2>&1; then
    TS_IP=$(tailscale ip -4 2>/dev/null | head -1 || echo "not connected")
    echo "    tailscale IP: $TS_IP"
    TS_HOST=$(tailscale status --self --peers=false 2>/dev/null | awk 'NR==1{print $2}')
    echo "    tailscale name: ${TS_HOST:-unknown}"
    if [ "${TS_HOST:-}" = "hanryxvault" ]; then
        bad "Tailscale name collides with main Pi"
    fi
else
    warn "tailscale not installed"
fi

# ── 2. Display environment ──────────────────────────────────────────────────
section "2. Display environment"
echo "    DISPLAY:          ${DISPLAY:-unset}"
echo "    WAYLAND_DISPLAY:  ${WAYLAND_DISPLAY:-unset}"
echo "    XDG_RUNTIME_DIR:  ${XDG_RUNTIME_DIR:-unset}"
echo "    Compositor:       $(pgrep -af 'labwc|wayfire|sway|kwin|gnome-shell' | head -1 || echo 'none running')"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
echo "    Wayland sockets in $RUNTIME_DIR:"
ls "$RUNTIME_DIR"/wayland-* 2>/dev/null | sed 's/^/      /' || echo "      (none)"

# ── 3. wlr-randr output ─────────────────────────────────────────────────────
section "3. wlr-randr — what the compositor sees"
if command -v wlr-randr >/dev/null 2>&1; then
    # Need to be inside an active Wayland session
    SOCK=$(ls "$RUNTIME_DIR"/wayland-* 2>/dev/null | grep -v lock | head -1)
    if [ -n "$SOCK" ]; then
        WAYLAND_DISPLAY=$(basename "$SOCK") wlr-randr 2>&1 | sed 's/^/    /'
    else
        warn "no Wayland socket — can't query wlr-randr"
        info "(this is normal if you ssh'd in headless; run on the Pi's local terminal)"
    fi
else
    warn "wlr-randr not installed — cannot detect outputs"
fi

# ── 4. Active windows ───────────────────────────────────────────────────────
section "4. Chromium processes"
CHROMIUM_PIDS=$(pgrep -af 'chromium' | grep -v grep || true)
if [ -n "$CHROMIUM_PIDS" ]; then
    echo "$CHROMIUM_PIDS" | sed 's/^/    /'
    ADMIN_RUNNING=$(echo "$CHROMIUM_PIDS" | grep -c 'admin-profile' || true)
    KIOSK_RUNNING=$(echo "$CHROMIUM_PIDS" | grep -c 'kiosk-profile' || true)
    [ "$ADMIN_RUNNING" -gt 0 ] && ok "admin chromium running"  || bad "admin chromium NOT running"
    [ "$KIOSK_RUNNING" -gt 0 ] && ok "kiosk chromium running"  || bad "kiosk chromium NOT running"
else
    bad "no chromium processes running"
fi

# ── 5. Configuration ────────────────────────────────────────────────────────
section "5. Configuration"
if [ -f "$CONFIG_FILE" ]; then
    ok "config exists at $CONFIG_FILE"
    echo "    contents:"
    sed 's/^/      /' "$CONFIG_FILE"
    grep -q "^ADMIN_OUTPUT=" "$CONFIG_FILE" 2>/dev/null \
        && ok "ADMIN_OUTPUT is pinned in config" \
        || warn "ADMIN_OUTPUT not pinned — relies on auto-detection (Audit #2)"
    grep -q "^KIOSK_OUTPUT=" "$CONFIG_FILE" 2>/dev/null \
        && ok "KIOSK_OUTPUT is pinned in config" \
        || warn "KIOSK_OUTPUT not pinned — relies on auto-detection (Audit #2)"
else
    bad "no config at $CONFIG_FILE"
fi

# ── 6. Autostart ────────────────────────────────────────────────────────────
section "6. Autostart paths"
LABWC_AUTO="$HOME_DIR/.config/labwc/autostart"
LXDE_AUTO="$HOME_DIR/.config/lxsession/LXDE-pi/autostart"
XDG_AUTO="$HOME_DIR/.config/autostart/hanryx-dual-kiosk.desktop"
ROOT_AUTO="$HOME_DIR/.config/autostart/hanryx-grafana-kiosk.desktop"

for path in "$LABWC_AUTO" "$LXDE_AUTO" "$XDG_AUTO" "$ROOT_AUTO"; do
    if [ -f "$path" ]; then
        if grep -qi 'hanryx' "$path" 2>/dev/null; then
            warn "$path mentions 'hanryx' — may be a duplicate launch source (Audit #9)"
            grep -i hanryx "$path" | sed 's/^/      /'
        else
            info "$path exists (no hanryx entries)"
        fi
    fi
done

# ── 7. labwc rc.xml ─────────────────────────────────────────────────────────
section "7. labwc window rules"
LABWC_RC="$HOME_DIR/.config/labwc/rc.xml"
if [ -f "$LABWC_RC" ]; then
    info "rc.xml present:"
    sed 's/^/      /' "$LABWC_RC"
    if grep -q '<labwc_config>' "$LABWC_RC"; then
        bad "rc.xml uses <labwc_config> root — should be <openbox_config> (Audit #3)"
    elif grep -q '<openbox_config' "$LABWC_RC"; then
        ok "rc.xml uses correct <openbox_config> root"
    fi
else
    warn "no rc.xml — windows will not be assigned to specific outputs"
fi

# ── 8. Recent kiosk log ─────────────────────────────────────────────────────
section "8. Recent kiosk log (last 50 lines of $LOG_FILE)"
if [ -f "$LOG_FILE" ]; then
    tail -50 "$LOG_FILE" 2>/dev/null | sed 's/^/    /'
else
    warn "no log at $LOG_FILE"
fi

# ── 9. Watchdog status ──────────────────────────────────────────────────────
section "9. Systemd watchdog"
if systemctl is-enabled hanryx-watchdog >/dev/null 2>&1; then
    ok "hanryx-watchdog enabled"
    info "$(systemctl is-active hanryx-watchdog)"
else
    info "hanryx-watchdog not installed"
fi
if systemctl is-enabled hanryx-heal.timer >/dev/null 2>&1; then
    ok "hanryx-heal.timer enabled (the new self-heal stack)"
    info "$(systemctl list-timers --no-pager | grep hanryx || echo 'no scheduled run')"
else
    warn "hanryx-heal.timer NOT enabled — run install-reliability.sh"
fi

# ── 10. Connectivity to main Pi ─────────────────────────────────────────────
section "10. Reachability to main Pi"
if [ -f "$CONFIG_FILE" ]; then
    MAIN=$(grep '^MAIN_PI_TS_HOST=' "$CONFIG_FILE" | cut -d= -f2)
    if [ -n "$MAIN" ]; then
        if ping -c1 -W2 "$MAIN" >/dev/null 2>&1; then
            ok "$MAIN reachable"
        else
            bad "$MAIN unreachable"
        fi
        for ep in /health /admin /kiosk; do
            code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://$MAIN:8080$ep" 2>/dev/null || echo 000)
            if [[ "$code" =~ ^2|^3 ]]; then
                ok "http://$MAIN:8080$ep → $code"
            else
                bad "http://$MAIN:8080$ep → $code"
            fi
        done
    fi
fi

# ── 11. Recommendations ─────────────────────────────────────────────────────
section "RECOMMENDATIONS"
RECS=()
[ "$(hostname)" = "hanryxvault" ] && RECS+=("Run setup-satellite-hostname-fix.sh — fixes Audit #1")
grep -q "^ADMIN_OUTPUT=" "$CONFIG_FILE" 2>/dev/null || RECS+=("Run satellite-screens.sh and pick which screen is admin/kiosk — fixes Audit #2/#5")
[ -f "$LABWC_RC" ] && grep -q '<labwc_config>' "$LABWC_RC" 2>/dev/null && RECS+=("Window rule schema is wrong — Audit #3 needs launcher patch")
systemctl is-enabled hanryx-heal.timer >/dev/null 2>&1 || RECS+=("Run install-reliability.sh on this Pi too — adds the self-heal sweep")

if [ ${#RECS[@]} -eq 0 ]; then
    ok "No outstanding recommendations from this run."
else
    for r in "${RECS[@]}"; do echo "    • $r"; done
fi

echo ""
echo -e "${B}End of diagnostic.${N} Paste this entire output back to me for analysis."
echo ""
