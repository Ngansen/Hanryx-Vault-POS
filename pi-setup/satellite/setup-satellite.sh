#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  HanryxVault — Trade Show (Satellite) Pi Installer
#
#  Run this on the SECOND (trade show) Pi:
#    sudo bash setup-satellite.sh
#
#  What it does:
#  1. Installs the full POS server stack (same as home Pi)
#  2. Installs the satellite sync agent (runs on every boot)
#  3. Installs WireGuard VPN client so the trade show Pi tunnels to home Pi
#     over the internet when plugged in anywhere — hotel, show floor, anywhere
#  4. Sets up barcode scanner daemon
#
#  The trade show Pi is fully standalone — works completely offline.
#  VPN + sync only fires when the Pi is powered on AND has internet.
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/hanryxvault"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[satellite]${NC} $*"; }
warn()  { echo -e "${YELLOW}[satellite]${NC} $*"; }
step()  { echo -e "${CYAN}══ $* ══${NC}"; }
error() { echo -e "${RED}[satellite] ERROR:${NC} $*"; exit 1; }

[[ $EUID -ne 0 ]] && error "Run with sudo"

VPN_ENABLED=false
WG_INTERFACE="wg0"

echo ""
echo -e "${CYAN}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║  HanryxVault Satellite Pi Installer     ║"
echo "  ║  Trade Show / Away-Game Setup            ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ── Step 1: Run main POS server installer ─────────────────────────────────────
step "Installing POS server stack"
PARENT_INSTALL="$SCRIPT_DIR/../install.sh"
[[ -f "$PARENT_INSTALL" ]] || error "Cannot find install.sh at $PARENT_INSTALL"
bash "$PARENT_INSTALL"

# ── Step 2: Install satellite sync agent ──────────────────────────────────────
step "Installing satellite sync agent + connection monitor"
cp "$SCRIPT_DIR/satellite_sync.py"    "$INSTALL_DIR/satellite_sync.py"
cp "$SCRIPT_DIR/satellite_monitor.py" "$INSTALL_DIR/satellite_monitor.py"
chown hanryxvault:hanryxvault "$INSTALL_DIR/satellite_sync.py"
chown hanryxvault:hanryxvault "$INSTALL_DIR/satellite_monitor.py"
chmod 644 "$INSTALL_DIR/satellite_sync.py"
chmod 644 "$INSTALL_DIR/satellite_monitor.py"
info "Installed satellite_sync.py + satellite_monitor.py"

# ── Step 3: WireGuard VPN client ──────────────────────────────────────────────
step "WireGuard VPN (internet tunnel to home Pi)"
echo ""
echo "WireGuard lets this Pi reach your home Pi from anywhere — trade shows,"
echo "hotels, phone hotspot — as long as it has any internet connection."
echo ""
echo "Before continuing, run this on your HOME Pi to generate the client config:"
echo ""
echo -e "  ${CYAN}sudo bash scripts/add-vpn-client.sh satellite-pi${NC}"
echo ""
echo "That creates a file like /tmp/satellite-pi.conf — copy it here via:"
echo "  USB drive, SCP, or paste the contents into a file."
echo ""
read -rp "Path to WireGuard client config file (Enter to skip VPN for now): " WG_CONF_SRC

if [[ -n "$WG_CONF_SRC" ]]; then
    if [[ ! -f "$WG_CONF_SRC" ]]; then
        warn "File not found: $WG_CONF_SRC — skipping VPN setup"
    else
        info "Installing WireGuard..."
        apt-get install -y --no-install-recommends wireguard-tools >/dev/null

        WG_CONF_DST="/etc/wireguard/${WG_INTERFACE}.conf"
        cp "$WG_CONF_SRC" "$WG_CONF_DST"
        chmod 600 "$WG_CONF_DST"

        # Extract home Pi VPN IP from AllowedIPs / Endpoint in the conf
        HOME_VPN_IP=$(grep -i 'Endpoint' "$WG_CONF_DST" \
                      | head -1 | sed 's/.*Endpoint\s*=\s*//' | cut -d: -f1 || true)

        systemctl enable wg-quick@${WG_INTERFACE}
        systemctl start  wg-quick@${WG_INTERFACE} || \
            warn "VPN did not start immediately — will connect on next boot or when internet is available"

        VPN_ENABLED=true
        info "WireGuard VPN enabled (interface: ${WG_INTERFACE})"
        info "Trade show Pi will auto-connect to home Pi VPN on every boot"
    fi
else
    warn "Skipping VPN — sync will use whatever URL you set in satellite.conf"
fi

# ── Step 4: Configure home Pi URL ─────────────────────────────────────────────
step "Configuring home Pi connection"

echo ""
if [[ "$VPN_ENABLED" == "true" ]]; then
    echo "VPN is enabled. Your home Pi's WireGuard IP is typically 10.10.0.1."
    echo "Check your WireGuard server config on the home Pi to confirm."
    DEFAULT_URL="http://10.10.0.1:8080"
else
    echo "Enter the URL of your HOME Pi:"
    echo "  Via WireGuard VPN (recommended): http://10.10.0.1:8080"
    echo "  Via Tailscale                  : http://hanryxvault.tailcfc0a3.ts.net"
    echo "  Via LAN (same network only)    : http://192.168.1.50:8080"
    DEFAULT_URL="http://10.10.0.1:8080"
fi
echo ""
read -rp "Home Pi URL [${DEFAULT_URL}]: " HOME_URL
HOME_URL="${HOME_URL:-$DEFAULT_URL}"

# ── Generate a shared secret token for satellite authentication ───────────────
step "Generating satellite authentication token"
SATELLITE_TOKEN=""
if command -v openssl >/dev/null 2>&1; then
    SATELLITE_TOKEN=$(openssl rand -hex 32)
elif [[ -r /dev/urandom ]]; then
    SATELLITE_TOKEN=$(tr -dc 'a-f0-9' < /dev/urandom | head -c 64)
fi

if [[ -z "$SATELLITE_TOKEN" ]]; then
    warn "Could not generate a random token — sync will run in open (unauthenticated) mode"
    warn "You can add 'satellite_token=<secret>' to satellite.conf manually later"
fi

CONF_FILE="$INSTALL_DIR/satellite.conf"
cat > "$CONF_FILE" << EOF
# HanryxVault Satellite Configuration — generated by setup-satellite.sh
# Edit this file any time; changes apply immediately (monitor re-reads on restart).

# ── Home Pi connection ──────────────────────────────────────────────────────
home_pi_url=${HOME_URL}

# ── Shared authentication token ────────────────────────────────────────────
# This token must match the value registered on the home Pi.
# See the registration command printed at the end of this installer.
satellite_token=${SATELLITE_TOKEN}

# ── WireGuard VPN ──────────────────────────────────────────────────────────
vpn_interface=${WG_INTERFACE}
vpn_wait_s=8

# ── Connectivity + sync behaviour ─────────────────────────────────────────
# Seconds between connectivity checks (how fast a reconnect is detected)
poll_interval_s=30

# Seconds to wait for the home Pi to respond before marking as unreachable
timeout_s=15

# While online: re-sync if there's pending data and this many seconds have passed
online_sync_interval_s=300

# While offline: log pending count this often (so you can see what's waiting)
offline_log_interval_s=300

# Retries (and delay) before giving up on a push/pull step
retry_count=3
retry_delay_s=4
EOF
chmod 600 "$CONF_FILE"   # contains the token — restrict read access
chown hanryxvault:hanryxvault "$CONF_FILE"
info "Saved config  →  home_pi_url=${HOME_URL}"
[[ -n "$SATELLITE_TOKEN" ]] && info "Token generated ✓  (64-char hex)"

# ── Step 5: Install systemd sync service ──────────────────────────────────────
step "Installing sync systemd service"

# Build After= line — include wg-quick if VPN is enabled
if [[ "$VPN_ENABLED" == "true" ]]; then
    AFTER_LINE="After=network-online.target wg-quick@${WG_INTERFACE}.service hanryxvault.service"
    WANTS_LINE="Wants=network-online.target wg-quick@${WG_INTERFACE}.service"
else
    AFTER_LINE="After=network-online.target hanryxvault.service"
    WANTS_LINE="Wants=network-online.target"
fi

cat > /etc/systemd/system/hanryxvault-satellite-sync.service << EOF
[Unit]
Description=HanryxVault Satellite Sync (push sales to home, pull inventory)
${AFTER_LINE}
${WANTS_LINE}
Requires=hanryxvault.service

[Service]
Type=simple
User=hanryxvault
WorkingDirectory=/opt/hanryxvault
ExecStartPre=/bin/sleep 8
ExecStart=/opt/hanryxvault/venv/bin/python3 /opt/hanryxvault/satellite_monitor.py
Restart=always
RestartSec=15
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hanryxvault-sync
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable hanryxvault-satellite-sync.service
info "Sync monitor enabled — runs continuously, syncs whenever connection returns"

# ── Step 6: Install QR scan hub ───────────────────────────────────────────────
step "Installing QR scan hub (USB + Bluetooth HID → all apps)"

SCAN_HUB_SRC="$SCRIPT_DIR/../scripts/barcode_daemon.py"
if [[ -f "$SCAN_HUB_SRC" ]]; then
    cp "$SCAN_HUB_SRC" "$INSTALL_DIR/barcode_daemon.py"
    chown root:root "$INSTALL_DIR/barcode_daemon.py"
    chmod 644 "$INSTALL_DIR/barcode_daemon.py"

    "$INSTALL_DIR/venv/bin/pip" install --quiet evdev

    # Create scan_endpoints.conf if it doesn't already exist
    ENDPOINTS_CONF="$INSTALL_DIR/scan_endpoints.conf"
    if [[ ! -f "$ENDPOINTS_CONF" ]]; then
        cat > "$ENDPOINTS_CONF" << 'EOF'
# HanryxVault Scan Endpoints
# One URL per line — every QR scan is forwarded to ALL listed apps simultaneously.
# Blank lines and lines starting with # are ignored.
# Restart hanryxvault-scan-hub after editing:
#   sudo systemctl restart hanryxvault-scan-hub

# POS server (always include this)
http://localhost:8080/scan

# Add your other apps here:
#   http://localhost:8081/scan   ← Pokémon lookup app
#   http://localhost:8082/scan   ← another project
EOF
        chown root:root "$ENDPOINTS_CONF"
        chmod 644 "$ENDPOINTS_CONF"
    fi

    cat > /etc/systemd/system/hanryxvault-scan-hub.service << 'EOSVC'
[Unit]
Description=HanryxVault QR Scan Hub (USB + Bluetooth HID, broadcasts to all apps)
After=hanryxvault.service bluetooth.target
Wants=bluetooth.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/hanryxvault
Environment=HANRYX_DIR=/opt/hanryxvault
Environment=SCAN_HUB_PORT=8765
ExecStart=/opt/hanryxvault/venv/bin/python3 /opt/hanryxvault/barcode_daemon.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hanryxvault-scan-hub

[Install]
WantedBy=multi-user.target
EOSVC

    systemctl daemon-reload
    systemctl enable --now hanryxvault-scan-hub.service
    info "QR scan hub installed and running on port 8765"
    info "Edit $ENDPOINTS_CONF to add more apps"
else
    warn "barcode_daemon.py not found — skipping scan hub setup"
fi

# ── Step 7: WiFi manager desktop app ──────────────────────────────────────────
step "Installing WiFi Manager desktop app"

WIFI_MGR_SRC="$SCRIPT_DIR/../scripts/wifi_manager.py"
WIFI_MGR_INSTALL="$SCRIPT_DIR/../scripts/install-wifi-manager.sh"

if [[ -f "$WIFI_MGR_INSTALL" ]]; then
    bash "$WIFI_MGR_INSTALL"
else
    warn "install-wifi-manager.sh not found — skipping desktop app"
fi

# ── Step 8: Network auto-failover (WiFi primary, USB phone tethering backup) ──
step "Setting up network auto-failover"

NETFAIL_SCRIPT="$SCRIPT_DIR/../scripts/setup-network-failover.sh"
if [[ -f "$NETFAIL_SCRIPT" ]]; then
    bash "$NETFAIL_SCRIPT"
else
    warn "setup-network-failover.sh not found at $NETFAIL_SCRIPT — skipping"
    warn "Run it manually later:  sudo bash scripts/setup-network-failover.sh"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║  Trade show Pi is ready!                 ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"
info "Home Pi URL : ${HOME_URL}"
info "VPN enabled : ${VPN_ENABLED}"
info "Local POS   : http://localhost:8080/admin"
echo ""

# ── Token registration prompt ─────────────────────────────────────────────────
if [[ -n "$SATELLITE_TOKEN" ]]; then
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}  ACTION REQUIRED — Register token on your HOME Pi${NC}"
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo "  SSH into your home Pi and run ONE of the following:"
    echo ""
    echo -e "  ${CYAN}# Option A: sqlite3 directly${NC}"
    echo "  sqlite3 /opt/hanryxvault/vault_pos.db \\"
    echo "    \"INSERT OR REPLACE INTO server_state(key,value)"
    echo "     VALUES('satellite_token','${SATELLITE_TOKEN}')\""
    echo ""
    echo -e "  ${CYAN}# Option B: via curl (home Pi must be running)${NC}"
    echo "  curl -s -X POST http://localhost:8080/admin/set-satellite-token \\"
    echo "    -H 'Content-Type: application/json' \\"
    echo "    -d '{\"token\":\"${SATELLITE_TOKEN}\"}'"
    echo ""
    echo "  Until you do this, the home Pi accepts syncs from any Pi (open mode)."
    echo ""
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
fi

info "Network (auto-failover):"
info "  • WiFi is the primary connection (metric 100 — always preferred)"
info "  • Plug in your phone via USB → enable tethering → Pi switches over automatically"
info "  • Returns to WiFi automatically when WiFi comes back"
info "  • WireGuard VPN tunnels over whichever connection is active — no extra setup"
echo ""
info "Offline sales sync:"
info "  • All sales stored locally in SQLite — nothing lost while offline"
info "  • Monitor checks every 30s and syncs the instant connection is restored"
info "  • Logs pending count while offline so you know what's waiting"
echo ""
info "Useful commands:"
info "  Watch sync logs        : sudo journalctl -u hanryxvault-satellite-sync -f"
info "  Force manual sync      : /opt/hanryxvault/venv/bin/python3 /opt/hanryxvault/satellite_sync.py"
info "  Network interfaces     : nmcli device status"
info "  Current routes         : ip route show"
if [[ "$VPN_ENABLED" == "true" ]]; then
info "  VPN status             : sudo wg show"
info "  VPN reconnect          : sudo systemctl restart wg-quick@${WG_INTERFACE}"
fi
info "  Bluetooth printer      : sudo bash scripts/setup-bluetooth-printer.sh"
info "  Pair BT QR scanner     : sudo bluetoothctl → pair <MAC> → trust → connect"
info "  Scan hub logs          : sudo journalctl -u hanryxvault-scan-hub -f"
info "  Scan hub health        : curl http://localhost:8765/health"
info "  Add app to scan hub    : edit /opt/hanryxvault/scan_endpoints.conf"

# ── Optional: kiosk display setup ─────────────────────────────────────────────
echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}  Optional: Kiosk display (monitor plugged into this Pi)${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  If you have a monitor connected to this satellite Pi and want"
echo "  the admin dashboard to auto-launch fullscreen on boot:"
echo ""
read -rp "  Install kiosk display? [y/N]: " INSTALL_KIOSK
if [[ "${INSTALL_KIOSK,,}" == "y" ]]; then
    KIOSK_SCRIPT="$SCRIPT_DIR/../kiosk/install-kiosk.sh"
    if [[ -f "$KIOSK_SCRIPT" ]]; then
        bash "$KIOSK_SCRIPT"
    else
        warn "Kiosk installer not found at $KIOSK_SCRIPT"
        warn "Run manually: sudo bash pi-setup/kiosk/install-kiosk.sh"
    fi
else
    info "Skipping kiosk. Run later if needed:"
    info "  sudo bash pi-setup/kiosk/install-kiosk.sh"
fi
