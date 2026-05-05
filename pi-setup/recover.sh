#!/usr/bin/env bash
# =============================================================================
# recover.sh — HanryxVault one-shot heal/verify
#
# Runs both manually ("I just moved venues, fix everything") and automatically
# via the hanryx-heal.timer every 2 minutes.
#
# Idempotent: safe to run as many times as you want, doesn't break anything
# that's already working. Designed for trade-show conditions where WiFi flaps,
# Tailscale lease changes, and Chromium occasionally gets confused.
#
# What it does (in order, with retries):
#   1.  Wait up to 30s for the local LAN gateway to be reachable (WiFi up?)
#   2.  Wait up to 30s for Tailscale to be authenticated and have an IP
#   3.  Bring up the main POS docker stack (idempotent — no restart if healthy)
#   4.  Bring up the monitoring stack (Prometheus + Grafana + node-exporter)
#   5.  Verify each container is healthy; restart only the unhealthy ones
#   6.  Verify each port is actually listening on the host
#   7.  Verify each HTTP endpoint returns 2xx
#   8.  Reset Chromium kiosks IF they died (stale process, dead profile)
#   9.  Print a colored status report (or a single OK / BROKEN line in --quiet)
#
# Usage:
#   bash recover.sh              # full coloured status report
#   bash recover.sh --quiet      # one-line OK/BROKEN — used by the heal timer
#   bash recover.sh --kiosk-only # skip docker, just fix the Chromium kiosks
#
# Exit code: 0 if everything healthy at end, 1 if anything still broken.
# =============================================================================
set -u

QUIET=0
KIOSK_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --quiet)       QUIET=1 ;;
        --kiosk-only)  KIOSK_ONLY=1 ;;
    esac
done

REPO_DIR="${REPO_DIR:-$HOME/Hanryx-Vault-POS}"
[ -d "$REPO_DIR" ] || REPO_DIR="/home/ngansen/Hanryx-Vault-POS"
LOG=/var/log/hanryx-recover.log
STATUS_FILE=/run/hanryx-status

# ── Colors ──────────────────────────────────────────────────────────────────
G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; C='\033[0;36m'; B='\033[1m'; N='\033[0m'
ISSUES=()
say()  { [ $QUIET -eq 1 ] || echo -e "$@"; echo -e "$@" >>"$LOG" 2>/dev/null || true; }
ok()   { say "${G}[✓]${N} $1"; }
info() { say "${Y}[→]${N} $1"; }
warn() { say "${R}[!]${N} $1"; ISSUES+=("$1"); }
note() { say "${C}[i]${N} $1"; }

mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
{
    echo ""
    echo "═══════════════════════════════════════════════════════════════════"
    echo "$(date -Is) — recover.sh $* — user=$(whoami) host=$(hostname)"
    echo "═══════════════════════════════════════════════════════════════════"
} >>"$LOG" 2>/dev/null || true

# ── 1. Wait for LAN gateway (= WiFi associated) ─────────────────────────────
wait_for_gateway() {
    local gw timeout=30 i
    gw=$(ip route | awk '/^default/ {print $3; exit}')
    if [ -z "$gw" ]; then
        warn "no default route — WiFi/Ethernet down"
        return 1
    fi
    for i in $(seq 1 $timeout); do
        if ping -c1 -W1 "$gw" >/dev/null 2>&1; then
            ok "gateway $gw reachable (${i}s)"
            return 0
        fi
        sleep 1
    done
    warn "gateway $gw unreachable after ${timeout}s"
    return 1
}

# ── 2. Wait for Tailscale ───────────────────────────────────────────────────
wait_for_tailscale() {
    local ts_ip i
    if ! command -v tailscale >/dev/null 2>&1; then
        note "tailscale not installed — skipping"
        return 0
    fi
    for i in $(seq 1 30); do
        ts_ip=$(tailscale ip -4 2>/dev/null | head -1)
        if [ -n "$ts_ip" ] && [[ "$ts_ip" == 100.* ]]; then
            ok "tailscale $ts_ip (${i}s)"
            return 0
        fi
        if [ "$i" = "1" ]; then
            tailscale up --hostname="$(hostname)" >/dev/null 2>&1 &
        fi
        sleep 1
    done
    warn "tailscale not authenticated"
    return 1
}

# ── 3+4. Bring up docker stacks ─────────────────────────────────────────────
ensure_compose_up() {
    local dir="$1" label="$2"
    if [ ! -f "$dir/docker-compose.yml" ]; then
        note "$label: no docker-compose.yml at $dir — skipping"
        return 0
    fi
    if ( cd "$dir" && docker compose ps --status=running --quiet 2>/dev/null \
            | grep -q . ); then
        ok "$label stack up"
    else
        info "$label stack starting…"
        ( cd "$dir" && docker compose up -d >>"$LOG" 2>&1 ) \
            && ok "$label brought up" \
            || warn "$label failed to start (see $LOG)"
    fi
}

# ── 5. Per-container health check — restart only what's unhealthy ──────────
restart_unhealthy() {
    local names
    # Containers in unhealthy / restarting / exited that should be running
    names=$(docker ps -a --format '{{.Names}}\t{{.Status}}' \
        | awk -F'\t' '/unhealthy|Restarting|Exited/ {print $1}')
    if [ -z "$names" ]; then
        ok "all containers healthy"
        return 0
    fi
    while IFS= read -r n; do
        info "restarting unhealthy container: $n"
        docker restart "$n" >>"$LOG" 2>&1 \
            && ok "  $n restarted" \
            || warn "  $n failed to restart"
    done <<<"$names"
}

# ── 6. Port listening check ─────────────────────────────────────────────────
check_port() {
    local port="$1" label="$2"
    if ss -tln 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}\$"; then
        ok "port $port ($label) listening"
    else
        warn "port $port ($label) NOT listening"
    fi
}

# ── 7. HTTP endpoint check with retry ───────────────────────────────────────
check_http() {
    local url="$1" label="$2" code i
    for i in 1 2 3; do
        code=$(curl -s -o /dev/null --max-time 5 -w "%{http_code}" "$url" 2>/dev/null || echo 000)
        if [[ "$code" =~ ^2|^3 ]]; then
            ok "$label HTTP $code"
            return 0
        fi
        sleep 2
    done
    warn "$label unreachable (HTTP $code at $url)"
    return 1
}

# ── 8. Chromium kiosk reset (only if dead) ──────────────────────────────────
reset_chromium_kiosk_if_dead() {
    if pgrep -af 'chromium.*--kiosk' >/dev/null 2>&1; then
        ok "chromium kiosk running"
        return 0
    fi
    info "chromium kiosk not running — attempting relaunch"
    # Clean up any stale process / profile
    pkill -9 chromium 2>/dev/null || true
    pkill -9 chromium-browser 2>/dev/null || true
    rm -rf /tmp/chromium-grafana 2>/dev/null || true
    # Delegate to the existing diagnostics-grafana-kiosk launcher (if present)
    local launcher="$REPO_DIR/pi-setup/diagnostics-grafana-kiosk.sh"
    if [ -x "$launcher" ]; then
        # Need user's display + wayland env — find the active session
        local target_uid target_user runtime_dir
        target_user=$(loginctl list-sessions --no-legend 2>/dev/null \
            | awk '$3 != "" && $3 != "root" {print $3; exit}')
        target_user="${target_user:-ngansen}"
        target_uid=$(id -u "$target_user" 2>/dev/null || echo "")
        if [ -n "$target_uid" ]; then
            runtime_dir="/run/user/$target_uid"
            sudo -u "$target_user" \
                env DISPLAY=:0 \
                    WAYLAND_DISPLAY=wayland-1 \
                    XDG_RUNTIME_DIR="$runtime_dir" \
                nohup bash "$launcher" >/dev/null 2>&1 & disown
            ok "chromium kiosk relaunched (uid=$target_uid)"
        else
            warn "could not detect graphical session user — kiosk not relaunched"
        fi
    else
        note "no kiosk launcher at $launcher — skipping chromium relaunch"
    fi
}

# =============================================================================
# Main flow
# =============================================================================
say ""
say "${B}  HanryxVault — recovery sweep${N}"
say "  $(date -Is) — host $(hostname)"
say "  ──────────────────────────────────────────────────────────────"

if [ $KIOSK_ONLY -eq 0 ]; then
    wait_for_gateway || true
    wait_for_tailscale || true
    ensure_compose_up "$REPO_DIR/pi-setup"            "POS"
    ensure_compose_up "$REPO_DIR/pi-setup/monitoring" "monitoring"
    sleep 2
    restart_unhealthy

    # Port checks
    check_port 8080 "POS"        || true
    check_port 9090 "Prometheus" || true
    check_port 9100 "node-exp"   || true
    check_port 3001 "Grafana"    || true

    # Endpoint checks (wrap in subshell so a failure doesn't abort)
    check_http "http://localhost:8080/health"      "POS health"     || true
    check_http "http://localhost:9090/-/healthy"   "Prometheus"     || true
    check_http "http://localhost:9100/metrics"     "node-exporter"  || true
    check_http "http://localhost:3001/api/health"  "Grafana"        || true
fi

reset_chromium_kiosk_if_dead || true

say "  ──────────────────────────────────────────────────────────────"

if [ ${#ISSUES[@]} -eq 0 ]; then
    echo "OK $(date +%s)" > "$STATUS_FILE" 2>/dev/null || true
    if [ $QUIET -eq 1 ]; then
        echo "OK $(hostname) $(date -Is)"
    else
        say "${G}${B}  ALL SYSTEMS GO${N} — $(date +%H:%M:%S)"
    fi
    exit 0
else
    echo "BROKEN $(date +%s) ${ISSUES[*]}" > "$STATUS_FILE" 2>/dev/null || true
    if [ $QUIET -eq 1 ]; then
        echo "BROKEN $(hostname) ${#ISSUES[@]} issue(s): ${ISSUES[*]}"
    else
        say "${R}${B}  ${#ISSUES[@]} ISSUE(S) REMAIN${N}:"
        for i in "${ISSUES[@]}"; do say "    ${R}•${N} $i"; done
        say ""
        say "  Full log: $LOG"
    fi
    exit 1
fi
