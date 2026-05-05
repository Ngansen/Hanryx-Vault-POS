#!/usr/bin/env bash
# =============================================================================
# HanryxVault — Satellite Pi One-Shot Bootstrap
#
# Run on a FRESH Raspberry Pi OS (Bookworm/Trixie 64-bit) install — typically
# right after Raspberry Pi Imager has flashed the SD card with WiFi + SSH
# pre-configured. Handles everything from blank Pi to a working dual-monitor
# kiosk that joins Tailscale and reports metrics to the Main Pi.
#
# Usage (on the satellite Pi, after first SSH):
#   curl -fsSL https://raw.githubusercontent.com/Ngansen/Hanryx-Vault-POS/main/pi-setup/satellite-bootstrap.sh | bash
#
# Or with a Tailscale auth key (skips browser prompt):
#   curl -fsSL https://raw.githubusercontent.com/Ngansen/Hanryx-Vault-POS/main/pi-setup/satellite-bootstrap.sh \
#     | TAILSCALE_AUTH_KEY=tskey-auth-xxxxx bash
#
# Optional env vars:
#   TAILSCALE_AUTH_KEY  Pre-authorise this Pi without the browser link.
#   MAIN_PI_HOST        Main Pi Tailscale IP (default 100.125.5.34).
#   SAT_HOSTNAME        Tailscale hostname for this device (default hanryxvault-sat).
#   SCREENS             1 or 2 — how many monitors are plugged in (default 2).
#   SKIP_KIOSK          Set to 1 to install monitoring only, no Chromium kiosk.
#
# What this script does:
#   1. apt update + upgrade (with auto-yes)
#   2. Installs base tools: git, curl, jq, ca-certificates
#   3. Installs prometheus-node-exporter on port 9100 (so Main Pi's Grafana
#      can chart this satellite)
#   4. Installs Tailscale and joins your tailnet
#   5. Clones the Hanryx-Vault-POS repo to ~/Hanryx-Vault-POS
#   6. Runs the existing setup-satellite-kiosk-boot.sh non-interactively
#      using the Main Pi as $MAIN_PI_HOST (skipped if SKIP_KIOSK=1)
#   7. Prints a summary block with the new IPs — copy-paste ready for
#      updating prometheus.yml on the Main Pi.
# =============================================================================
set -euo pipefail

# ── Defaults ────────────────────────────────────────────────────────────────
MAIN_PI_HOST="${MAIN_PI_HOST:-100.125.5.34}"
SAT_HOSTNAME="${SAT_HOSTNAME:-hanryxvault-sat}"
SCREENS="${SCREENS:-2}"
SKIP_KIOSK="${SKIP_KIOSK:-0}"
REPO_URL="${REPO_URL:-https://github.com/Ngansen/Hanryx-Vault-POS.git}"

CURRENT_USER="${SUDO_USER:-$(whoami)}"
HOME_DIR=$(getent passwd "$CURRENT_USER" | cut -d: -f6)
REPO_DIR="$HOME_DIR/Hanryx-Vault-POS"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'
RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
ok()    { echo -e "${GREEN}[✓]${NC} $1"; }
info()  { echo -e "${YELLOW}[→]${NC} $1"; }
note()  { echo -e "${CYAN}[i]${NC} $1"; }
fatal() { echo -e "${RED}[✗]${NC} $1" >&2; exit 1; }

require_root() {
    if [ "$EUID" -ne 0 ]; then
        info "Re-running with sudo…"
        exec sudo -E bash "$0" "$@"
    fi
}
require_root "$@"

echo ""
echo -e "${BOLD}  HanryxVault — Satellite Pi One-Shot Bootstrap${NC}"
echo "  ============================================================"
echo "  User           : $CURRENT_USER"
echo "  Home           : $HOME_DIR"
echo "  Main Pi (TS)   : $MAIN_PI_HOST"
echo "  Sat hostname   : $SAT_HOSTNAME"
echo "  Screens        : $SCREENS"
echo "  Skip kiosk     : $SKIP_KIOSK"
echo "  Repo           : $REPO_URL"
echo "  ============================================================"
echo ""

# ── 1. apt update ───────────────────────────────────────────────────────────
info "Updating apt cache…"
DEBIAN_FRONTEND=noninteractive apt-get update -qq
ok "apt cache updated"

info "Installing base tools (git, curl, jq, ca-certificates)…"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    git curl jq ca-certificates gnupg lsb-release \
    >/dev/null
ok "Base tools installed"

# ── 2. node-exporter — so Grafana on the Main Pi can chart this Pi ──────────
info "Installing prometheus-node-exporter…"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq prometheus-node-exporter >/dev/null
systemctl enable --now prometheus-node-exporter >/dev/null 2>&1 || true
sleep 2
if curl -fsS --max-time 3 http://localhost:9100/metrics >/dev/null 2>&1; then
    ok "node-exporter healthy on :9100"
else
    note "node-exporter installed but /metrics not yet responding — will retry on first reboot"
fi

# ── 3. Tailscale ────────────────────────────────────────────────────────────
info "Installing Tailscale…"
if ! command -v tailscale >/dev/null 2>&1; then
    curl -fsSL https://tailscale.com/install.sh | sh
    ok "Tailscale installed"
else
    ok "Tailscale already present ($(tailscale version | head -1))"
fi

systemctl enable --now tailscaled >/dev/null 2>&1 || true

if [ -n "${TAILSCALE_AUTH_KEY:-}" ]; then
    info "Authenticating Tailscale with provided key…"
    tailscale up --authkey="$TAILSCALE_AUTH_KEY" --hostname="$SAT_HOSTNAME" --accept-routes 2>/dev/null \
        || tailscale up --authkey="$TAILSCALE_AUTH_KEY" --hostname="$SAT_HOSTNAME" 2>/dev/null \
        || true
else
    note "No TAILSCALE_AUTH_KEY env var — running interactive auth."
    note "Open the URL printed below in any browser to approve this Pi."
    echo ""
    tailscale up --hostname="$SAT_HOSTNAME" --accept-routes &
    TS_PID=$!
    sleep 5
    read -rp "  Press Enter once you have approved the device in the Tailscale admin… "
    wait "$TS_PID" 2>/dev/null || true
fi

TS_IP=$(tailscale ip -4 2>/dev/null | head -1 || echo "not-connected")
ok "Tailscale IP: $TS_IP"

# Reachability check Main Pi
if ping -c1 -W2 "$MAIN_PI_HOST" >/dev/null 2>&1; then
    ok "Main Pi $MAIN_PI_HOST reachable over Tailscale"
else
    note "Main Pi $MAIN_PI_HOST not pingable yet — Tailscale may still be settling"
fi

# ── 4. Clone repo ───────────────────────────────────────────────────────────
if [ ! -d "$REPO_DIR/.git" ]; then
    info "Cloning $REPO_URL → $REPO_DIR…"
    sudo -u "$CURRENT_USER" git clone --depth=20 "$REPO_URL" "$REPO_DIR"
    ok "Repo cloned"
else
    info "Repo already present — pulling latest…"
    sudo -u "$CURRENT_USER" git -C "$REPO_DIR" pull --ff-only || \
        note "git pull failed — leaving working tree as-is"
fi

# ── 5. Run dual-monitor kiosk setup (unless skipped) ────────────────────────
if [ "$SKIP_KIOSK" = "1" ]; then
    note "SKIP_KIOSK=1 — skipping Chromium kiosk install"
else
    KIOSK_SCRIPT="$REPO_DIR/pi-setup/setup-satellite-kiosk-boot.sh"
    if [ -x "$KIOSK_SCRIPT" ] || [ -f "$KIOSK_SCRIPT" ]; then
        info "Running dual-monitor kiosk setup…"
        # Pre-seed config so the existing script runs non-interactively
        SAT_CONF_DIR="$HOME_DIR/.hanryx"
        mkdir -p "$SAT_CONF_DIR"
        cat > "$SAT_CONF_DIR/satellite.conf" <<EOF
MAIN_PI_TS_HOST=$MAIN_PI_HOST
ADMIN_URL=http://$MAIN_PI_HOST:8080/admin
KIOSK_URL=http://$MAIN_PI_HOST:8080/kiosk
HEALTH_URL=http://$MAIN_PI_HOST:8080/health
USE_TAILSCALE=y
SWAP_SCREENS=n
EOF
        chown -R "$CURRENT_USER:$CURRENT_USER" "$SAT_CONF_DIR"

        # Auto-answer the two prompts: "1" (Tailscale) + "1" (no swap)
        # MAIN_PI_HOST + TAILSCALE_AUTH_KEY are already in the env via sudo -E
        if printf '1\n\n1\n' | MAIN_PI_HOST="$MAIN_PI_HOST" \
            bash "$KIOSK_SCRIPT"; then
            ok "Kiosk setup completed"
        else
            note "Kiosk setup exited non-zero — review /var/log/hanryx-kiosk.log on next boot"
        fi
    else
        note "Kiosk script not found at $KIOSK_SCRIPT — skipping"
    fi
fi

# ── 6. Final summary ────────────────────────────────────────────────────────
LAN_IP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${BOLD}  ============================================================${NC}"
echo -e "${BOLD}  Satellite bootstrap complete${NC}"
echo -e "${BOLD}  ============================================================${NC}"
echo "  Hostname        : $(hostname)"
echo "  LAN IP          : $LAN_IP"
echo "  Tailscale IP    : $TS_IP"
echo "  Tailscale name  : $SAT_HOSTNAME"
echo "  node-exporter   : http://$LAN_IP:9100/metrics"
echo "                  : http://$TS_IP:9100/metrics  (preferred — never changes)"
echo ""
echo -e "${CYAN}  ── Next step on the MAIN Pi ────────────────────────────────${NC}"
echo "  Update Prometheus to scrape the new satellite IP, then reload:"
echo ""
echo "    cd ~/Hanryx-Vault-POS/pi-setup/monitoring"
echo "    bash update-satellite-target.sh $TS_IP"
echo "    docker kill -s HUP prometheus"
echo ""
echo -e "${CYAN}  ── Reboot recommended ──────────────────────────────────────${NC}"
echo "    sudo reboot"
echo ""
