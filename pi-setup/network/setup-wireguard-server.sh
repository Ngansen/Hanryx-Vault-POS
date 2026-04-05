#!/usr/bin/env bash
# =============================================================================
#  HanryxVault — WireGuard VPN Server Setup
#  Runs on the Main Pi (the Ethernet gateway)
#
#  After this runs you can VPN in from anywhere and appear on your
#  192.168.10.x LAN — access POS dashboard, satellite Pi, R6020, everything.
#
#  Google Nest port-forward step (done ONCE in the Google Home app):
#    Google Home → Wi-Fi → Settings → Advanced Networking → Port Management
#    → Add Rule:
#        Protocol    : UDP
#        External    : 51820
#        Internal IP : <Pi's wlan0 IP shown by this script>
#        Internal    : 51820
#
#  Usage:  sudo bash setup-wireguard-server.sh
# =============================================================================

set -euo pipefail

# ── Configurable variables ───────────────────────────────────────────────────
WG_IFACE="wg0"
WG_PORT=51820
WG_SERVER_TUNNEL_IP="10.8.0.1/24"   # VPN tunnel address for the Pi
WG_CLIENT_BASE="10.8.0"             # clients get 10.8.0.2, .3, .4 …

LAN_IFACE="eth0"                    # your LAN (downstream switch)
UPSTREAM_IFACE="wlan0"              # your internet (Google Nest WiFi)

LAN_SUBNET="192.168.10.0/24"        # reachable once VPN'd in
SAT_SUBNET="192.168.11.0/24"        # satellite Pi's LAN (if set up)

CONFIG_DIR="/etc/wireguard"
CLIENTS_DIR="/etc/wireguard/clients"

# DuckDNS — fill these in if you want auto-updating DNS
# (leave blank to skip — you'll use your raw public IP instead)
DUCKDNS_TOKEN="e28fdab6-047d-4e09-b6ed-03f777af2e6c"
DUCKDNS_DOMAIN="hanryxvault"        # connects as hanryxvault.duckdns.org
# ─────────────────────────────────────────────────────────────────────────────

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[✓]${NC} $*"; }
step()  { echo -e "${CYAN}[→]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
die()   { echo -e "${RED}[✗] $*${NC}"; exit 1; }

[[ $EUID -ne 0 ]] && die "Run as root: sudo bash $0"

# ── 1. Install WireGuard ─────────────────────────────────────────────────────
info "Installing WireGuard…"
apt-get update -qq
apt-get install -y -qq wireguard wireguard-tools qrencode curl

# ── 2. Detect Pi's local IP (from Nest) and public IP ───────────────────────
LOCAL_IP=$(ip -4 addr show "$UPSTREAM_IFACE" | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1)
PUBLIC_IP=$(curl -s --max-time 5 https://api.ipify.org || echo "UNKNOWN")

echo ""
echo -e "${CYAN}  Pi local IP (wlan0) : $LOCAL_IP${NC}"
echo -e "${CYAN}  Public IP           : $PUBLIC_IP${NC}"
echo ""

# Determine the endpoint clients will connect to
if [[ -n "$DUCKDNS_DOMAIN" ]]; then
    ENDPOINT="${DUCKDNS_DOMAIN}.duckdns.org:${WG_PORT}"
else
    ENDPOINT="${PUBLIC_IP}:${WG_PORT}"
fi
warn "Endpoint clients will use: $ENDPOINT"
warn "(If your public IP changes, set up DuckDNS — see instructions at the end)"
echo ""

# ── 3. Generate server keys ──────────────────────────────────────────────────
mkdir -p "$CONFIG_DIR" "$CLIENTS_DIR"
chmod 700 "$CONFIG_DIR"

SERVER_PRIVKEY_FILE="$CONFIG_DIR/server.key"
SERVER_PUBKEY_FILE="$CONFIG_DIR/server.pub"

if [[ ! -f "$SERVER_PRIVKEY_FILE" ]]; then
    info "Generating server keypair…"
    wg genkey | tee "$SERVER_PRIVKEY_FILE" | wg pubkey > "$SERVER_PUBKEY_FILE"
    chmod 600 "$SERVER_PRIVKEY_FILE"
else
    info "Server keypair already exists — reusing."
fi

SERVER_PRIVKEY=$(cat "$SERVER_PRIVKEY_FILE")
SERVER_PUBKEY=$(cat  "$SERVER_PUBKEY_FILE")

# ── 4. Write server config ───────────────────────────────────────────────────
info "Writing /etc/wireguard/${WG_IFACE}.conf…"
cat > "$CONFIG_DIR/${WG_IFACE}.conf" << EOF
# HanryxVault WireGuard Server — managed by setup-wireguard-server.sh
[Interface]
Address    = $WG_SERVER_TUNNEL_IP
ListenPort = $WG_PORT
PrivateKey = $SERVER_PRIVKEY

# NAT: VPN clients → internet via wlan0
PostUp   = iptables -t nat -A POSTROUTING -o $UPSTREAM_IFACE -j MASQUERADE; \
           iptables -A FORWARD -i $WG_IFACE -j ACCEPT; \
           iptables -A FORWARD -o $WG_IFACE -j ACCEPT
PostDown = iptables -t nat -D POSTROUTING -o $UPSTREAM_IFACE -j MASQUERADE; \
           iptables -D FORWARD -i $WG_IFACE -j ACCEPT; \
           iptables -D FORWARD -o $WG_IFACE -j ACCEPT

# ── Peers are appended below by add-wireguard-client.sh ──────────────────────
EOF
chmod 600 "$CONFIG_DIR/${WG_IFACE}.conf"

# ── 5. IP forwarding (ensure it's on) ───────────────────────────────────────
sysctl -w net.ipv4.ip_forward=1 > /dev/null
grep -q "net.ipv4.ip_forward=1" /etc/sysctl.d/99-hanryx-forward.conf 2>/dev/null || \
    echo "net.ipv4.ip_forward=1" >> /etc/sysctl.d/99-hanryx-forward.conf

# ── 6. Enable & start WireGuard ─────────────────────────────────────────────
info "Enabling wg-quick@${WG_IFACE}…"
systemctl enable  "wg-quick@${WG_IFACE}"
systemctl restart "wg-quick@${WG_IFACE}"

# ── 7. Generate first client (phone / laptop) ────────────────────────────────
generate_client() {
    local CLIENT_NAME="$1"
    local CLIENT_NUM="$2"
    local CLIENT_IP="${WG_CLIENT_BASE}.${CLIENT_NUM}"
    local OUT_DIR="$CLIENTS_DIR/$CLIENT_NAME"
    mkdir -p "$OUT_DIR"
    chmod 700 "$OUT_DIR"

    local PRIV="$OUT_DIR/private.key"
    local PUB="$OUT_DIR/public.key"
    local PSK="$OUT_DIR/preshared.key"
    local CONF="$OUT_DIR/${CLIENT_NAME}.conf"

    wg genkey | tee "$PRIV" | wg pubkey > "$PUB"
    wg genpsk > "$PSK"
    chmod 600 "$PRIV" "$PSK"

    local CLIENT_PRIVKEY=$(cat "$PRIV")
    local CLIENT_PUBKEY=$(cat  "$PUB")
    local PRESHARED=$(cat "$PSK")

    # Client config file
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
# Route ALL traffic through VPN (full tunnel):
AllowedIPs   = 0.0.0.0/0
# OR — split tunnel (only LAN traffic, keep local internet):
# AllowedIPs = 10.8.0.0/24, $LAN_SUBNET, $SAT_SUBNET
PersistentKeepalive = 25
EOF
    chmod 600 "$CONF"

    # Add peer block to server config
    cat >> "$CONFIG_DIR/${WG_IFACE}.conf" << EOF

[Peer]
# $CLIENT_NAME
PublicKey    = $CLIENT_PUBKEY
PresharedKey = $PRESHARED
AllowedIPs   = ${CLIENT_IP}/32
EOF

    echo "$CONF"
}

# Generate default clients
info "Generating client configs…"
PHONE_CONF=$(generate_client "phone"   2)
LAPTOP_CONF=$(generate_client "laptop" 3)

# Reload server to pick up new peers
wg syncconf "$WG_IFACE" <(wg-quick strip "$WG_IFACE") 2>/dev/null || \
    systemctl restart "wg-quick@${WG_IFACE}"

# ── 8. DuckDNS auto-updater (optional) ─────────────────────────────────────
if [[ -n "$DUCKDNS_TOKEN" && -n "$DUCKDNS_DOMAIN" ]]; then
    info "Setting up DuckDNS auto-updater…"
    DUCK_SCRIPT="/usr/local/bin/duckdns-update.sh"
    cat > "$DUCK_SCRIPT" << 'DUCKEOF'
#!/usr/bin/env bash
TOKEN="__TOKEN__"
DOMAIN="__DOMAIN__"
curl -s "https://www.duckdns.org/update?domains=${DOMAIN}&token=${TOKEN}&ip=" -o /tmp/duckdns.log
DUCKEOF
    sed -i "s/__TOKEN__/$DUCKDNS_TOKEN/; s/__DOMAIN__/$DUCKDNS_DOMAIN/" "$DUCK_SCRIPT"
    chmod +x "$DUCK_SCRIPT"

    # Run every 5 minutes via cron
    CRON_LINE="*/5 * * * * /usr/local/bin/duckdns-update.sh"
    (crontab -l 2>/dev/null | grep -v duckdns-update; echo "$CRON_LINE") | crontab -
    "$DUCK_SCRIPT"
    info "DuckDNS updater installed — runs every 5 minutes."
fi

# ── 9. Print QR codes + summary ─────────────────────────────────────────────
echo ""
echo -e "${GREEN}══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  WireGuard server is LIVE on port $WG_PORT (UDP)${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════════════${NC}"
echo ""
echo "  Server tunnel IP : 10.8.0.1"
echo "  Public endpoint  : $ENDPOINT"
echo "  Local IP (Pi)    : $LOCAL_IP"
echo ""
echo -e "${CYAN}  ── Google Nest port-forward (do this once) ──────────────${NC}"
echo "  Google Home app → Wi-Fi → Settings → Advanced Networking"
echo "  → Port Management → Add Rule:"
echo "      Protocol      : UDP"
echo "      External port : $WG_PORT"
echo "      Internal IP   : $LOCAL_IP"
echo "      Internal port : $WG_PORT"
echo ""
echo -e "${CYAN}  ── Client configs saved to ──────────────────────────────${NC}"
echo "  Phone  : $PHONE_CONF"
echo "  Laptop : $LAPTOP_CONF"
echo ""
echo -e "${CYAN}  ── Phone QR code (scan in WireGuard app) ───────────────${NC}"
echo ""
qrencode -t ansiutf8 < "$PHONE_CONF"
echo ""
echo -e "${CYAN}  ── Laptop QR code ───────────────────────────────────────${NC}"
echo ""
qrencode -t ansiutf8 < "$LAPTOP_CONF"
echo ""
echo -e "${CYAN}  ── Add more devices later ───────────────────────────────${NC}"
echo "  sudo bash add-wireguard-client.sh <device-name>"
echo ""
echo -e "${CYAN}  ── Useful commands ──────────────────────────────────────${NC}"
echo "  sudo wg show              # live connection status"
echo "  sudo wg show wg0 peers    # connected peers"
echo "  sudo systemctl status wg-quick@wg0"
echo ""
warn "If your public IP changes and you didn't set up DuckDNS:"
echo "  Re-run this script OR update the Endpoint line in each client config."
echo "  Free DuckDNS: https://www.duckdns.org (takes 2 minutes to set up)"
echo ""
