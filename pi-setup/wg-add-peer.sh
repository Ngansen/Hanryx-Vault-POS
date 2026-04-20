#!/usr/bin/env bash
# wg-add-peer.sh — add a new WireGuard peer to the Main Pi in one command.
#
# Usage:
#     sudo ./wg-add-peer.sh <peer-name> [client-ip]
#
# Examples:
#     sudo ./wg-add-peer.sh tablet              # auto-picks next free IP
#     sudo ./wg-add-peer.sh phone 10.8.0.5      # force a specific IP
#
# What it does:
#   1. Generates a fresh keypair for the peer.
#   2. Appends a [Peer] block to /etc/wireguard/wg0.conf.
#   3. Applies the peer live with `wg set` (no restart needed).
#   4. Writes /etc/wireguard/<peer-name>.conf   (the client config).
#   5. Prints a QR code in the terminal — scan it with the WireGuard
#      mobile app ("Create from QR code").
#
# Re-running with an existing peer name is safe: the script refuses to
# overwrite, so you can't accidentally nuke an already-deployed config.
#
# Env overrides (optional):
#   WG_IFACE      interface name (default: wg0)
#   WG_SUBNET     /24 base     (default: 10.8.0)
#   WG_ENDPOINT   public host:port the client dials
#                 (default: read from /etc/wireguard/endpoint.txt, else
#                  prompt interactively)
#   WG_DNS        DNS the client uses (default: 192.168.86.1)
#   WG_ALLOWED    AllowedIPs on the client side
#                 (default: 192.168.86.0/24,10.8.0.0/24  — split tunnel)

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "ERR: must be run as root (use sudo)." >&2
    exit 1
fi

PEER_NAME="${1:-}"
FORCED_IP="${2:-}"
if [[ -z "$PEER_NAME" ]]; then
    echo "Usage: sudo $0 <peer-name> [client-ip]" >&2
    exit 2
fi
if ! [[ "$PEER_NAME" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo "ERR: peer name must be alphanumeric / dash / underscore only." >&2
    exit 2
fi

IFACE="${WG_IFACE:-wg0}"
SUBNET="${WG_SUBNET:-10.8.0}"
DNS="${WG_DNS:-192.168.86.1}"
ALLOWED="${WG_ALLOWED:-192.168.86.0/24,10.8.0.0/24}"
WG_DIR="/etc/wireguard"
SERVER_CONF="$WG_DIR/$IFACE.conf"
CLIENT_CONF="$WG_DIR/$PEER_NAME.conf"

if [[ ! -f "$SERVER_CONF" ]]; then
    echo "ERR: $SERVER_CONF not found — is WireGuard set up on this Pi?" >&2
    exit 3
fi
if [[ -e "$CLIENT_CONF" ]]; then
    echo "ERR: $CLIENT_CONF already exists. Delete it first if you want to regenerate." >&2
    exit 4
fi

# Ensure tools are present
need() { command -v "$1" >/dev/null || { echo "Installing $2..."; apt-get install -y "$2" >/dev/null; }; }
need wg            wireguard-tools
need qrencode      qrencode

umask 077
cd "$WG_DIR"

# ── Resolve server public key ────────────────────────────────────────────
SERVER_PUBKEY=""
if [[ -f "$WG_DIR/server_public.key" ]]; then
    SERVER_PUBKEY=$(cat "$WG_DIR/server_public.key")
fi
if [[ -z "$SERVER_PUBKEY" ]]; then
    SERVER_PUBKEY=$(wg show "$IFACE" public-key 2>/dev/null || true)
fi
if [[ -z "$SERVER_PUBKEY" ]]; then
    # Last-ditch: derive from the PrivateKey inside wg0.conf
    PRIV_LINE=$(awk -F'= *' '/^PrivateKey/ {print $2; exit}' "$SERVER_CONF")
    if [[ -n "$PRIV_LINE" ]]; then
        SERVER_PUBKEY=$(echo "$PRIV_LINE" | wg pubkey)
    fi
fi
if [[ -z "$SERVER_PUBKEY" ]]; then
    echo "ERR: could not determine server public key." >&2
    exit 5
fi

# ── Resolve endpoint (what the client dials) ─────────────────────────────
ENDPOINT="${WG_ENDPOINT:-}"
if [[ -z "$ENDPOINT" && -f "$WG_DIR/endpoint.txt" ]]; then
    ENDPOINT=$(tr -d '[:space:]' < "$WG_DIR/endpoint.txt")
fi
if [[ -z "$ENDPOINT" ]]; then
    read -r -p "Public endpoint for clients to dial (e.g. myhome.duckdns.org:51820): " ENDPOINT
    if [[ -n "$ENDPOINT" ]]; then
        echo "$ENDPOINT" > "$WG_DIR/endpoint.txt"
        chmod 600 "$WG_DIR/endpoint.txt"
        echo "Saved endpoint to $WG_DIR/endpoint.txt (reused next time)."
    fi
fi
if [[ -z "$ENDPOINT" ]]; then
    echo "ERR: no endpoint provided." >&2
    exit 6
fi

# ── Pick client IP ───────────────────────────────────────────────────────
pick_ip() {
    # Collect IPs already claimed in AllowedIPs entries (ignoring /24 server line).
    local used
    used=$(grep -E "^AllowedIPs" "$SERVER_CONF" | grep -oE "${SUBNET//./\\.}\.[0-9]+" | sort -u)
    for n in $(seq 2 254); do
        if ! grep -qx "$SUBNET.$n" <<<"$used"; then
            echo "$SUBNET.$n"
            return
        fi
    done
    echo ""
}

if [[ -n "$FORCED_IP" ]]; then
    CLIENT_IP="$FORCED_IP"
    if grep -qE "${CLIENT_IP//./\\.}/32" "$SERVER_CONF"; then
        echo "ERR: $CLIENT_IP is already assigned in $SERVER_CONF." >&2
        exit 7
    fi
else
    CLIENT_IP=$(pick_ip)
    if [[ -z "$CLIENT_IP" ]]; then
        echo "ERR: no free IP in $SUBNET.0/24." >&2
        exit 8
    fi
fi

# ── Generate keypair ─────────────────────────────────────────────────────
CLIENT_PRIV=$(wg genkey)
CLIENT_PUB=$(echo "$CLIENT_PRIV" | wg pubkey)

# Persist the keys so you can reprint the QR later if needed
echo "$CLIENT_PRIV" > "${PEER_NAME}_private.key"; chmod 600 "${PEER_NAME}_private.key"
echo "$CLIENT_PUB"  > "${PEER_NAME}_public.key";  chmod 644 "${PEER_NAME}_public.key"

# ── Append to server config ──────────────────────────────────────────────
{
    echo ""
    echo "# ${PEER_NAME} — added $(date -Iseconds)"
    echo "[Peer]"
    echo "PublicKey = ${CLIENT_PUB}"
    echo "AllowedIPs = ${CLIENT_IP}/32"
} >> "$SERVER_CONF"

# Apply live — no wg-quick down/up churn, no existing peers dropped.
wg set "$IFACE" peer "$CLIENT_PUB" allowed-ips "$CLIENT_IP/32"

# ── Write client config ──────────────────────────────────────────────────
cat > "$CLIENT_CONF" <<EOF
# WireGuard client: ${PEER_NAME}
# Generated $(date -Iseconds) on $(hostname)
[Interface]
PrivateKey = ${CLIENT_PRIV}
Address = ${CLIENT_IP}/24
DNS = ${DNS}

[Peer]
PublicKey = ${SERVER_PUBKEY}
Endpoint = ${ENDPOINT}
AllowedIPs = ${ALLOWED}
PersistentKeepalive = 25
EOF
chmod 600 "$CLIENT_CONF"

# ── Print summary + QR code ──────────────────────────────────────────────
echo ""
echo "================================================================"
echo " Peer added: ${PEER_NAME}"
echo "   IP on VPN      : ${CLIENT_IP}"
echo "   Endpoint       : ${ENDPOINT}"
echo "   AllowedIPs     : ${ALLOWED}"
echo "   Client config  : ${CLIENT_CONF}"
echo "================================================================"
echo ""
echo " Scan this QR with the WireGuard mobile app"
echo "   (app → + → Create from QR code):"
echo ""
qrencode -t ansiutf8 < "$CLIENT_CONF"
echo ""
echo " To reprint later:   sudo qrencode -t ansiutf8 < ${CLIENT_CONF}"
echo " To remove the peer: sudo wg set ${IFACE} peer ${CLIENT_PUB} remove"
echo "                     then delete the [Peer] block from ${SERVER_CONF}"
echo ""
