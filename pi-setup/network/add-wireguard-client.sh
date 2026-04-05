#!/usr/bin/env bash
# =============================================================================
#  HanryxVault — Add a new WireGuard client
#  Run AFTER setup-wireguard-server.sh has been run once.
#
#  Usage:  sudo bash add-wireguard-client.sh <device-name>
#  e.g.:   sudo bash add-wireguard-client.sh work-laptop
#          sudo bash add-wireguard-client.sh expo-phone
# =============================================================================

set -euo pipefail

GREEN='\033[0;32m'; CYAN='\033[0;36m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[✓]${NC} $*"; }
die()  { echo -e "${RED}[✗] $*${NC}"; exit 1; }

[[ $EUID -ne 0 ]]           && die "Run as root: sudo bash $0 <device-name>"
[[ $# -lt 1 ]]              && die "Usage: sudo bash $0 <device-name>"
command -v wg    &>/dev/null || die "WireGuard not installed — run setup-wireguard-server.sh first"
command -v qrencode &>/dev/null || apt-get install -y -qq qrencode

CLIENT_NAME="$1"
WG_IFACE="wg0"
CONFIG_DIR="/etc/wireguard"
CLIENTS_DIR="$CONFIG_DIR/clients"
SERVER_CONF="$CONFIG_DIR/${WG_IFACE}.conf"

[[ ! -f "$SERVER_CONF" ]] && die "Server config not found at $SERVER_CONF"

# ── Find next available IP ───────────────────────────────────────────────────
LAST_OCTET=$(grep -oP '10\.8\.0\.\K\d+' "$SERVER_CONF" | sort -n | tail -1)
NEXT_OCTET=$(( ${LAST_OCTET:-1} + 1 ))
CLIENT_IP="10.8.0.${NEXT_OCTET}"

[[ $NEXT_OCTET -gt 253 ]] && die "IP pool exhausted (10.8.0.2–253)"

# ── Load server public key and endpoint ─────────────────────────────────────
SERVER_PUBKEY=$(cat "$CONFIG_DIR/server.pub")
ENDPOINT=$(grep "Endpoint" "$CLIENTS_DIR"/*/phone.conf 2>/dev/null | head -1 | awk '{print $3}')
if [[ -z "$ENDPOINT" ]]; then
    PUBLIC_IP=$(curl -s --max-time 5 https://api.ipify.org)
    ENDPOINT="${PUBLIC_IP}:51820"
fi

# ── Generate keys ────────────────────────────────────────────────────────────
OUT_DIR="$CLIENTS_DIR/$CLIENT_NAME"
mkdir -p "$OUT_DIR"
chmod 700 "$OUT_DIR"

PRIV="$OUT_DIR/private.key"
PUB="$OUT_DIR/public.key"
PSK="$OUT_DIR/preshared.key"
CONF="$OUT_DIR/${CLIENT_NAME}.conf"

wg genkey | tee "$PRIV" | wg pubkey > "$PUB"
wg genpsk > "$PSK"
chmod 600 "$PRIV" "$PSK"

CLIENT_PRIVKEY=$(cat "$PRIV")
CLIENT_PUBKEY=$(cat  "$PUB")
PRESHARED=$(cat "$PSK")

# ── Write client config ──────────────────────────────────────────────────────
cat > "$CONF" << EOF
# HanryxVault VPN — $CLIENT_NAME
[Interface]
PrivateKey = $CLIENT_PRIVKEY
Address    = ${CLIENT_IP}/32
DNS        = 8.8.8.8, 1.1.1.1

[Peer]
PublicKey    = $SERVER_PUBKEY
PresharedKey = $PRESHARED
Endpoint     = $ENDPOINT
# Full tunnel (all traffic through VPN):
AllowedIPs   = 0.0.0.0/0
# Split tunnel (only HanryxVault LAN — uncomment to use):
# AllowedIPs = 10.8.0.0/24, 192.168.10.0/24, 192.168.11.0/24
PersistentKeepalive = 25
EOF
chmod 600 "$CONF"

# ── Add peer to server config ────────────────────────────────────────────────
cat >> "$SERVER_CONF" << EOF

[Peer]
# $CLIENT_NAME
PublicKey    = $CLIENT_PUBKEY
PresharedKey = $PRESHARED
AllowedIPs   = ${CLIENT_IP}/32
EOF

# ── Hot-reload server (no restart needed) ───────────────────────────────────
wg syncconf "$WG_IFACE" <(wg-quick strip "$WG_IFACE")
info "Peer added live — no restart needed."

# ── Print QR code ────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}══════════════════════════════════════════${NC}"
echo -e "${GREEN}  Client added: $CLIENT_NAME${NC}"
echo -e "${GREEN}══════════════════════════════════════════${NC}"
echo ""
echo "  VPN IP   : $CLIENT_IP"
echo "  Endpoint : $ENDPOINT"
echo "  Config   : $CONF"
echo ""
echo -e "${CYAN}  Scan this QR code in the WireGuard app:${NC}"
echo ""
qrencode -t ansiutf8 < "$CONF"
echo ""
echo -e "${CYAN}  Or copy the config file to your device:${NC}"
echo "  scp pi@192.168.10.1:$CONF ~/Downloads/${CLIENT_NAME}.conf"
echo ""
info "Check connected peers with:  sudo wg show"
