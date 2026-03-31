#!/bin/bash
# add-vpn-client.sh — Add a new WireGuard VPN client device
# Usage: sudo bash scripts/add-vpn-client.sh <device-name> [server-public-ip] [server-public-key]
set -e

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "${GREEN}[+]${NC} $1"; }
info() { echo -e "${BLUE}[i]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }

[ "$EUID" -ne 0 ] && { echo "Run as root: sudo bash scripts/add-vpn-client.sh <name>"; exit 1; }

WG_DIR="/etc/wireguard"
WG_IFACE="wg0"
WG_PORT=51820
VPN_SUBNET="10.8.0"
CLIENT_DIR="${WG_DIR}/clients"

CLIENT_NAME="${1:-}"
SERVER_IP="${2:-$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')}"
SERVER_PUBLIC="${3:-$(cat ${WG_DIR}/server_public.key 2>/dev/null)}"

if [ -z "$CLIENT_NAME" ]; then
  read -p "Device name (e.g. phone, laptop): " CLIENT_NAME
fi
[ -z "$CLIENT_NAME" ] && { echo "Name required."; exit 1; }

# Sanitize name
CLIENT_SLUG=$(echo "$CLIENT_NAME" | tr '[:upper:]' '[:lower:]' | tr ' ' '-' | tr -cd 'a-z0-9-')
mkdir -p "${CLIENT_DIR}/${CLIENT_SLUG}"

# Find next available IP
NEXT_IP=2
while grep -q "${VPN_SUBNET}.${NEXT_IP}" "${WG_DIR}/${WG_IFACE}.conf" 2>/dev/null; do
  NEXT_IP=$((NEXT_IP + 1))
  [ $NEXT_IP -gt 254 ] && { echo "No free IPs"; exit 1; }
done
CLIENT_IP="${VPN_SUBNET}.${NEXT_IP}"

# Generate client keys
CLIENT_PRIVATE="${CLIENT_DIR}/${CLIENT_SLUG}/private.key"
CLIENT_PUBLIC="${CLIENT_DIR}/${CLIENT_SLUG}/public.key"

if [ ! -f "$CLIENT_PRIVATE" ]; then
  wg genkey | tee "$CLIENT_PRIVATE" | wg pubkey > "$CLIENT_PUBLIC"
  chmod 600 "$CLIENT_PRIVATE"
fi

CLIENT_PRIV=$(cat "$CLIENT_PRIVATE")
CLIENT_PUB=$(cat "$CLIENT_PUBLIC")

# Add peer to server config
log "Adding ${CLIENT_NAME} (${CLIENT_IP}) to server..."
cat >> "${WG_DIR}/${WG_IFACE}.conf" <<EOF

# Client: ${CLIENT_NAME} — added $(date '+%Y-%m-%d %H:%M')
[Peer]
PublicKey  = ${CLIENT_PUB}
AllowedIPs = ${CLIENT_IP}/32
EOF

# Write client config
CLIENT_CONF="${CLIENT_DIR}/${CLIENT_SLUG}/${CLIENT_SLUG}.conf"
cat > "$CLIENT_CONF" <<EOF
[Interface]
PrivateKey = ${CLIENT_PRIV}
Address    = ${CLIENT_IP}/24
DNS        = 1.1.1.1, 8.8.8.8

[Peer]
PublicKey  = ${SERVER_PUBLIC}
Endpoint   = ${SERVER_IP}:${WG_PORT}
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
EOF
chmod 600 "$CLIENT_CONF"

# Reload WireGuard live (no downtime)
wg addconf "${WG_IFACE}" <(wg-quick strip "${WG_IFACE}") 2>/dev/null || \
  systemctl reload wg-quick@"${WG_IFACE}" 2>/dev/null || \
  wg syncconf "${WG_IFACE}" <(wg showconf "${WG_IFACE}") 2>/dev/null || true

echo ""
log "Client '${CLIENT_NAME}' created — IP: ${CLIENT_IP}"
echo ""
info "Config file: ${CLIENT_CONF}"
echo ""
warn "Scan this QR code with the WireGuard mobile app:"
echo ""
qrencode -t ansiutf8 < "$CLIENT_CONF" 2>/dev/null || \
  warn "Install qrencode for QR: sudo apt-get install -y qrencode"
echo ""
info "Or copy the config file to your device:"
echo "  scp pi@$(hostname -I | awk '{print $1}'):${CLIENT_CONF} ./"
echo ""
info "Then open WireGuard app → + → Import from file"
echo ""
