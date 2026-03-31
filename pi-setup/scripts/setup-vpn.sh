#!/bin/bash
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${GREEN}[+]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[x]${NC} $1"; exit 1; }
info() { echo -e "${BLUE}[i]${NC} $1"; }
hr()   { echo -e "${BLUE}────────────────────────────────────────────${NC}"; }

[ "$EUID" -ne 0 ] && err "Run as root: sudo bash scripts/setup-vpn.sh"

WG_DIR="/etc/wireguard"
WG_IFACE="wg0"
WG_PORT=51820
VPN_SUBNET="10.8.0"           # Pi = 10.8.0.1, clients = 10.8.0.2, 10.8.0.3 ...
MAX_CLIENTS=10

echo ""
echo -e "${YELLOW}╔══════════════════════════════════════════════╗${NC}"
echo -e "${YELLOW}║        HanryxVault — WireGuard VPN           ║${NC}"
echo -e "${YELLOW}║  Access your Pi server securely from anywhere ║${NC}"
echo -e "${YELLOW}╚══════════════════════════════════════════════╝${NC}"
echo ""

# ── 1. Install WireGuard ──────────────────────────────────────────────────────
log "Installing WireGuard..."
apt-get update -qq
apt-get install -y -qq wireguard wireguard-tools qrencode

# ── 2. Enable IP forwarding (required for VPN routing) ───────────────────────
log "Enabling IP forwarding..."
if ! grep -q "^net.ipv4.ip_forward=1" /etc/sysctl.conf; then
  echo "net.ipv4.ip_forward=1"   >> /etc/sysctl.conf
  echo "net.ipv6.conf.all.forwarding=1" >> /etc/sysctl.conf
fi
sysctl -p > /dev/null 2>&1

# ── 3. Detect public network interface ───────────────────────────────────────
PUB_IFACE=$(ip route | grep '^default' | awk '{print $5}' | head -1)
[ -z "$PUB_IFACE" ] && PUB_IFACE="eth0"
log "Detected public interface: ${PUB_IFACE}"

# ── 4. Get public IP ──────────────────────────────────────────────────────────
PUBLIC_IP=$(curl -s --max-time 8 https://api.ipify.org 2>/dev/null || \
            curl -s --max-time 8 https://ifconfig.me 2>/dev/null || \
            hostname -I | awk '{print $1}')
log "Public IP: ${PUBLIC_IP}"

# ── 5. Generate server keys ───────────────────────────────────────────────────
mkdir -p "${WG_DIR}"
chmod 700 "${WG_DIR}"

if [ ! -f "${WG_DIR}/server_private.key" ]; then
  log "Generating server keys..."
  wg genkey | tee "${WG_DIR}/server_private.key" | wg pubkey > "${WG_DIR}/server_public.key"
  chmod 600 "${WG_DIR}/server_private.key"
fi

SERVER_PRIVATE=$(cat "${WG_DIR}/server_private.key")
SERVER_PUBLIC=$(cat "${WG_DIR}/server_public.key")

# ── 6. Write server config ────────────────────────────────────────────────────
if [ ! -f "${WG_DIR}/${WG_IFACE}.conf" ]; then
  log "Writing server WireGuard config..."
  cat > "${WG_DIR}/${WG_IFACE}.conf" <<EOF
[Interface]
Address    = ${VPN_SUBNET}.1/24
ListenPort = ${WG_PORT}
PrivateKey = ${SERVER_PRIVATE}

# NAT — route VPN traffic through your Pi's internet connection
PostUp   = iptables -A FORWARD -i ${WG_IFACE} -j ACCEPT; iptables -A FORWARD -o ${WG_IFACE} -j ACCEPT; iptables -t nat -A POSTROUTING -o ${PUB_IFACE} -j MASQUERADE
PostDown = iptables -D FORWARD -i ${WG_IFACE} -j ACCEPT; iptables -D FORWARD -o ${WG_IFACE} -j ACCEPT; iptables -t nat -D POSTROUTING -o ${PUB_IFACE} -j MASQUERADE

# Clients are added below automatically by add-vpn-client.sh
EOF
  chmod 600 "${WG_DIR}/${WG_IFACE}.conf"
fi

# ── 7. Open VPN port in firewall ──────────────────────────────────────────────
log "Opening VPN port ${WG_PORT}/udp in firewall..."
ufw allow ${WG_PORT}/udp > /dev/null

# ── 8. Enable and start WireGuard ─────────────────────────────────────────────
log "Starting WireGuard VPN..."
systemctl enable wg-quick@${WG_IFACE}
systemctl restart wg-quick@${WG_IFACE}
sleep 1

if systemctl is-active --quiet wg-quick@${WG_IFACE}; then
  log "WireGuard VPN is running on port ${WG_PORT}/udp"
else
  warn "WireGuard may not have started — check: sudo systemctl status wg-quick@wg0"
fi

# ── 9. Create the first client (for you) ─────────────────────────────────────
hr
echo ""
info "Creating your first VPN client..."
echo ""
read -p "  Name for this device (e.g. phone, laptop, tablet): " CLIENT_NAME
[ -z "$CLIENT_NAME" ] && CLIENT_NAME="my-device"

bash "$(dirname "${BASH_SOURCE[0]}")/add-vpn-client.sh" "${CLIENT_NAME}" "${PUBLIC_IP}" "${SERVER_PUBLIC}"

echo ""
hr
echo ""
echo -e "${GREEN}  WireGuard VPN is ready!${NC}"
echo ""
info "VPN server: ${PUBLIC_IP}:${WG_PORT}"
info "VPN subnet: ${VPN_SUBNET}.0/24"
info "Your Pi on VPN: ${VPN_SUBNET}.1"
echo ""
warn "IMPORTANT — Port forward UDP ${WG_PORT} on your router to your Pi:"
echo "  Pi's local IP: $(hostname -I | awk '{print $1}')"
echo "  Router admin is usually at: http://192.168.1.1"
echo "  Forward: UDP port ${WG_PORT} → $(hostname -I | awk '{print $1}'):${WG_PORT}"
echo ""
warn "To add more devices later:"
echo "  sudo bash scripts/add-vpn-client.sh <device-name>"
echo ""
