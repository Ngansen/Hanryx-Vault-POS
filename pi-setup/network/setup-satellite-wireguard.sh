#!/usr/bin/env bash
# =============================================================================
#  HanryxVault — Satellite Pi WireGuard Client Setup
#  Run this ON THE SATELLITE PI to join the Main Pi's WireGuard tunnel so the
#  satellite can reach the main Pi from anywhere on the internet.
#
#  PREREQ (one-time, on the Main Pi):
#    cd ~/hanryx-vault-pos/pi-setup/network
#    sudo bash setup-wireguard-server.sh                 # installs server
#    sudo bash add-wireguard-client.sh satellite-pi      # generates client conf
#    sudo scp /etc/wireguard/clients/satellite-pi/satellite-pi.conf \
#        ngansen@<satellite-ip>:~/satellite-pi.conf
#
#  THEN ON THE SATELLITE:
#    sudo bash setup-satellite-wireguard.sh ~/satellite-pi.conf
#
#  After this, the satellite can reach the main Pi at 10.8.0.1 from any
#  network (home WiFi, hotspot, hotel, anywhere with internet).
# =============================================================================

set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[✓]${NC} $*"; }
step() { echo -e "${CYAN}[→]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
die()  { echo -e "${RED}[✗] $*${NC}"; exit 1; }

# ── Args ─────────────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && die "Run as root: sudo bash $0 <client-conf>"
[[ $# -lt 1 ]]    && die "Usage: sudo bash $0 <path-to-satellite-pi.conf>"

SRC_CONF="$1"
[[ ! -f "$SRC_CONF" ]] && die "Config file not found: $SRC_CONF"

WG_IFACE="wg0"
WG_CONF="/etc/wireguard/${WG_IFACE}.conf"

# Default: tunnel ONLY traffic for the WG subnet + the home LAN, not all
# internet. This keeps the satellite's own LAN + internet at full speed and
# avoids breaking its kiosk's local-LAN access when at home.
HOME_LAN_SUBNET="${HOME_LAN_SUBNET:-192.168.86.0/24}"
WG_TUNNEL_SUBNET="${WG_TUNNEL_SUBNET:-10.8.0.0/24}"

step "Satellite WireGuard installer"
echo  "  Source config : $SRC_CONF"
echo  "  Target config : $WG_CONF"
echo  "  AllowedIPs    : ${WG_TUNNEL_SUBNET}, ${HOME_LAN_SUBNET}"
echo

# ── 1. Install wireguard if missing ──────────────────────────────────────────
if ! command -v wg-quick &>/dev/null; then
    step "Installing wireguard…"
    apt-get update -qq
    apt-get install -y -qq wireguard wireguard-tools resolvconf
    info  "wireguard installed"
else
    info "wireguard already installed ($(wg --version | head -1))"
fi

# ── 2. Stop existing tunnel if it's running ──────────────────────────────────
if systemctl is-active --quiet "wg-quick@${WG_IFACE}"; then
    step "Stopping existing wg-quick@${WG_IFACE}…"
    systemctl stop "wg-quick@${WG_IFACE}" || true
fi

# ── 3. Back up any pre-existing config ───────────────────────────────────────
if [[ -f "$WG_CONF" ]]; then
    BACKUP="${WG_CONF}.bak.$(date +%Y%m%d-%H%M%S)"
    cp "$WG_CONF" "$BACKUP"
    warn "Existing config saved to $BACKUP"
fi

# ── 4. Install the new config ────────────────────────────────────────────────
install -o root -g root -m 600 "$SRC_CONF" "$WG_CONF"
info "Installed $WG_CONF (mode 600)"

# ── 5. Rewrite AllowedIPs to the safe split-tunnel value ─────────────────────
# Replaces the first "AllowedIPs = …" line under the [Peer] section. If the
# generated client conf already has the value we want, this is a no-op.
DESIRED_ALLOWED="${WG_TUNNEL_SUBNET}, ${HOME_LAN_SUBNET}"
if grep -qE '^\s*AllowedIPs\s*=' "$WG_CONF"; then
    sed -i -E "s|^\s*AllowedIPs\s*=.*|AllowedIPs = ${DESIRED_ALLOWED}|" "$WG_CONF"
    info "AllowedIPs set to: ${DESIRED_ALLOWED}"
else
    warn "No AllowedIPs line found — leaving config untouched"
fi

# ── 6. Add a PersistentKeepalive so NAT/firewalls don't drop the tunnel ──────
if ! grep -qE '^\s*PersistentKeepalive\s*=' "$WG_CONF"; then
    echo "PersistentKeepalive = 25" >> "$WG_CONF"
    info "Added PersistentKeepalive = 25"
fi

# ── 7. Enable IP forwarding (lets satellite talk to home LAN through tunnel) ─
if ! grep -q '^net.ipv4.ip_forward=1' /etc/sysctl.conf; then
    echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf
    sysctl -p >/dev/null
    info "Enabled net.ipv4.ip_forward"
fi

# ── 8. Enable + start the tunnel at boot ─────────────────────────────────────
step "Bringing tunnel up and enabling at boot…"
systemctl enable "wg-quick@${WG_IFACE}"  >/dev/null
systemctl start  "wg-quick@${WG_IFACE}"
sleep 2

# ── 9. Verify ────────────────────────────────────────────────────────────────
if ! systemctl is-active --quiet "wg-quick@${WG_IFACE}"; then
    die "wg-quick@${WG_IFACE} failed to start — check: journalctl -u wg-quick@${WG_IFACE}"
fi
info "wg-quick@${WG_IFACE} is active"
echo
step "Current WireGuard status:"
wg show
echo

# ── 10. Smoke test against main Pi over the tunnel ───────────────────────────
MAIN_PI_TUNNEL_IP="10.8.0.1"
step "Pinging main Pi at ${MAIN_PI_TUNNEL_IP} over the tunnel…"
if ping -c 3 -W 2 "$MAIN_PI_TUNNEL_IP" >/dev/null 2>&1; then
    info "✓ Tunnel works — main Pi reachable at ${MAIN_PI_TUNNEL_IP}"
    echo
    step "Trying POS health endpoint…"
    if curl -s --max-time 5 "http://${MAIN_PI_TUNNEL_IP}:8080/healthz" >/dev/null; then
        info "✓ POS server reachable over tunnel"
    else
        warn "POS health endpoint did not respond (port 8080 maybe not open via wg)"
    fi
else
    warn "Main Pi did not respond to ping. Check that:"
    warn "  • Server is running on main Pi:  sudo systemctl status wg-quick@wg0"
    warn "  • Google Nest forwards UDP 51820 → main Pi"
    warn "  • DuckDNS resolves correctly:  dig hanryxvault.duckdns.org"
fi

echo
info "Done. Tunnel will auto-reconnect on every boot."
echo
echo "  Stop tunnel    : sudo systemctl stop wg-quick@${WG_IFACE}"
echo "  Start tunnel   : sudo systemctl start wg-quick@${WG_IFACE}"
echo "  Tail handshake : sudo wg show"
echo "  Edit config    : sudo nano ${WG_CONF}  (then: systemctl restart wg-quick@${WG_IFACE})"
