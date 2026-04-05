#!/usr/bin/env bash
# =============================================================================
#  HanryxVault — Main Pi Network Setup
#  Ethernet-as-gateway: wlan0 (WiFi/internet) → eth0 → unmanaged switch
#
#  Topology
#  ─────────────────────────────────────────────────────────────────────────
#  Internet (ISP)
#      │
#   wlan0  ←─ Pi connects to your WiFi network here
#      │
#   [Main Pi — 192.168.10.1]
#      │
#   eth0  ──── Netgear Unmanaged Switch
#                   ├── Netgear R6020 (AP mode, LAN port only — NO WAN cable)
#                   ├── Satellite Pi eth0
#                   └── Any other wired device
#
#  The Pi does NAT/masquerade: all downstream devices share the Pi's WiFi.
#  DHCP range: 192.168.10.50–192.168.10.200
#
#  Usage:  sudo bash setup-main-pi-network.sh
# =============================================================================

set -euo pipefail

# ── Configurable variables ───────────────────────────────────────────────────
UPSTREAM_IFACE="wlan0"          # interface with internet (WiFi)
LAN_IFACE="eth0"                # interface going to the switch
LAN_IP="192.168.10.1"           # Pi's static IP on the LAN
LAN_SUBNET="192.168.10.0/24"
DHCP_START="192.168.10.50"
DHCP_END="192.168.10.200"
DHCP_LEASE="12h"
DNS_SERVERS="8.8.8.8,1.1.1.1"  # pushed to downstream clients
HOSTNAME_LABEL="hanryx-main"
# ─────────────────────────────────────────────────────────────────────────────

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
die()   { echo -e "${RED}[✗] $*${NC}"; exit 1; }

[[ $EUID -ne 0 ]] && die "Run as root: sudo bash $0"

info "Installing packages…"
apt-get update -qq
apt-get install -y -qq dnsmasq iptables iptables-persistent netfilter-persistent

# ── 1. Static IP on eth0 via dhcpcd ─────────────────────────────────────────
DHCPCD_CONF="/etc/dhcpcd.conf"
info "Configuring static IP $LAN_IP on $LAN_IFACE…"

# Remove any previous HanryxVault block
sed -i '/# BEGIN hanryx-network/,/# END hanryx-network/d' "$DHCPCD_CONF"

cat >> "$DHCPCD_CONF" << EOF

# BEGIN hanryx-network — managed by setup-main-pi-network.sh
interface $LAN_IFACE
    static ip_address=$LAN_IP/24
    static domain_name_servers=$DNS_SERVERS
    nohook wpa_supplicant
# END hanryx-network
EOF

# ── 2. dnsmasq (DHCP + DNS for the LAN) ─────────────────────────────────────
info "Writing dnsmasq config…"
DNSMASQ_CONF="/etc/dnsmasq.d/hanryx-lan.conf"
cat > "$DNSMASQ_CONF" << EOF
# HanryxVault main-Pi LAN — managed by setup-main-pi-network.sh
interface=$LAN_IFACE
bind-interfaces

# DHCP pool
dhcp-range=$DHCP_START,$DHCP_END,$DHCP_LEASE

# Push DNS servers to clients
dhcp-option=option:dns-server,$DNS_SERVERS

# Advertise Pi as the default gateway
dhcp-option=option:router,$LAN_IP

# Friendly local domain
domain=hanryx.local
local=/hanryx.local/

# Fixed addresses for known hardware (add MAC → IP mappings below)
# dhcp-host=aa:bb:cc:dd:ee:ff,satellite-pi,192.168.10.10
# dhcp-host=aa:bb:cc:dd:ee:00,netgear-r6020,192.168.10.11

# Speed: use /etc/hosts for local names
expand-hosts
EOF

# ── 3. IP forwarding ─────────────────────────────────────────────────────────
info "Enabling IP forwarding…"
SYSCTL_CONF="/etc/sysctl.d/99-hanryx-forward.conf"
cat > "$SYSCTL_CONF" << EOF
# HanryxVault — allow Pi to route packets between interfaces
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
EOF
sysctl --system -q

# ── 4. iptables NAT (wlan0 ← masquerade ← eth0) ─────────────────────────────
info "Setting up NAT masquerade ($LAN_IFACE → $UPSTREAM_IFACE)…"
iptables -t nat -F POSTROUTING
iptables -F FORWARD

# Masquerade all outbound traffic from the LAN
iptables -t nat -A POSTROUTING -o "$UPSTREAM_IFACE" -j MASQUERADE

# Forward established/related sessions back to LAN
iptables -A FORWARD -i "$UPSTREAM_IFACE" -o "$LAN_IFACE" \
    -m state --state RELATED,ESTABLISHED -j ACCEPT

# Forward new connections from LAN to upstream
iptables -A FORWARD -i "$LAN_IFACE" -o "$UPSTREAM_IFACE" -j ACCEPT

# Save rules so they survive reboot
netfilter-persistent save
info "iptables rules saved."

# ── 5. Persist hostname ───────────────────────────────────────────────────────
info "Setting hostname to $HOSTNAME_LABEL…"
hostnamectl set-hostname "$HOSTNAME_LABEL"
grep -q "$HOSTNAME_LABEL" /etc/hosts || \
    echo "127.0.1.1  $HOSTNAME_LABEL" >> /etc/hosts

# Add friendly names to /etc/hosts for downstream devices
grep -q "satellite-pi" /etc/hosts || \
    echo "192.168.10.10  satellite-pi satellite-pi.hanryx.local" >> /etc/hosts
grep -q "netgear-r6020" /etc/hosts || \
    echo "192.168.10.11  netgear-r6020 netgear-r6020.hanryx.local" >> /etc/hosts

# ── 6. Resolve port-53 conflict (systemd-resolved vs dnsmasq) ────────────────
# systemd-resolved listens on 127.0.0.53:53 which blocks dnsmasq from starting.
# We disable it and write a static resolv.conf instead.
if systemctl is-active --quiet systemd-resolved; then
    info "Stopping systemd-resolved (conflicts with dnsmasq on port 53)…"
    systemctl stop    systemd-resolved
    systemctl disable systemd-resolved
fi
# Write a clean resolv.conf that points directly to dnsmasq / upstream DNS
rm -f /etc/resolv.conf
printf 'nameserver 8.8.8.8\nnameserver 1.1.1.1\n' > /etc/resolv.conf

# ── 7. Start & enable services ───────────────────────────────────────────────
info "Restarting services…"
systemctl unmask dnsmasq
systemctl enable dnsmasq
systemctl restart dnsmasq
systemctl restart dhcpcd

# ── 7. Firewall: allow POS dashboard from LAN only ───────────────────────────
info "Allowing POS port 8080 from LAN…"
iptables -C INPUT -i "$LAN_IFACE" -p tcp --dport 8080 -j ACCEPT 2>/dev/null || \
    iptables -A INPUT -i "$LAN_IFACE" -p tcp --dport 8080 -j ACCEPT
iptables -C INPUT -i lo -j ACCEPT 2>/dev/null || \
    iptables -A INPUT -i lo -j ACCEPT
netfilter-persistent save

echo ""
echo -e "${GREEN}══════════════════════════════════════════${NC}"
echo -e "${GREEN}  Main Pi network setup complete!${NC}"
echo -e "${GREEN}══════════════════════════════════════════${NC}"
echo ""
echo "  Pi LAN IP    : $LAN_IP"
echo "  DHCP range   : $DHCP_START – $DHCP_END"
echo "  Local domain : hanryx.local"
echo "  Upstream     : $UPSTREAM_IFACE (WiFi)"
echo ""
echo "  Next steps:"
echo "  1. Plug eth0 into the Netgear unmanaged switch."
echo "  2. Plug R6020 LAN port (not WAN) into the switch."
echo "  3. Set R6020 to AP mode (disable its DHCP server in its web UI)."
echo "  4. Run setup-satellite-network.sh on the satellite Pi."
echo "  5. Run: sudo systemctl status dnsmasq   to confirm DHCP is up."
echo ""
warn "Reboot recommended to apply all settings cleanly:"
echo "  sudo reboot"
