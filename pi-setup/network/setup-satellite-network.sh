#!/usr/bin/env bash
# =============================================================================
#  HanryxVault — Satellite Pi Network Setup
#  Ethernet-as-gateway: wlan0 (WiFi/internet) → eth0 → unmanaged switch
#
#  Topology
#  ─────────────────────────────────────────────────────────────────────────
#  Internet (ISP)
#      │
#   wlan0  ←─ Satellite Pi connects to your WiFi network here
#      │
#  [Satellite Pi — 192.168.11.1]
#      │
#   eth0  ──── wired devices on satellite's local subnet (192.168.11.x)
#
#  The satellite Pi runs its own DHCP range so it can serve devices even
#  when the main Pi is offline.  Both subnets are reachable from each Pi
#  if you add a static route (see step 8 below).
#
#  Usage:  sudo bash setup-satellite-network.sh
# =============================================================================

set -euo pipefail

# ── Configurable variables ───────────────────────────────────────────────────
UPSTREAM_IFACE="wlan0"
LAN_IFACE="eth0"
LAN_IP="192.168.11.1"           # different subnet from main Pi
LAN_SUBNET="192.168.11.0/24"
DHCP_START="192.168.11.50"
DHCP_END="192.168.11.200"
DHCP_LEASE="12h"
DNS_SERVERS="8.8.8.8,1.1.1.1"
HOSTNAME_LABEL="hanryx-satellite"
MAIN_PI_IP="192.168.10.1"       # route to main Pi's LAN
MAIN_PI_SUBNET="192.168.10.0/24"
# ─────────────────────────────────────────────────────────────────────────────

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
die()   { echo -e "${RED}[✗] $*${NC}"; exit 1; }

[[ $EUID -ne 0 ]] && die "Run as root: sudo bash $0"

info "Installing packages…"
apt-get update -qq
apt-get install -y -qq dnsmasq iptables iptables-persistent netfilter-persistent

# ── 1. Static IP on eth0 ─────────────────────────────────────────────────────
DHCPCD_CONF="/etc/dhcpcd.conf"
info "Configuring static IP $LAN_IP on $LAN_IFACE…"
sed -i '/# BEGIN hanryx-network/,/# END hanryx-network/d' "$DHCPCD_CONF"
cat >> "$DHCPCD_CONF" << EOF

# BEGIN hanryx-network — managed by setup-satellite-network.sh
interface $LAN_IFACE
    static ip_address=$LAN_IP/24
    static domain_name_servers=$DNS_SERVERS
    nohook wpa_supplicant
# END hanryx-network
EOF

# ── 2. dnsmasq ───────────────────────────────────────────────────────────────
info "Writing dnsmasq config…"
DNSMASQ_CONF="/etc/dnsmasq.d/hanryx-lan.conf"
cat > "$DNSMASQ_CONF" << EOF
# HanryxVault satellite-Pi LAN — managed by setup-satellite-network.sh
interface=$LAN_IFACE
bind-interfaces

dhcp-range=$DHCP_START,$DHCP_END,$DHCP_LEASE
dhcp-option=option:dns-server,$DNS_SERVERS
dhcp-option=option:router,$LAN_IP

domain=hanryx-sat.local
local=/hanryx-sat.local/

# dhcp-host=aa:bb:cc:dd:ee:ff,my-device,192.168.11.10

expand-hosts
EOF

# ── 3. IP forwarding ─────────────────────────────────────────────────────────
info "Enabling IP forwarding…"
cat > /etc/sysctl.d/99-hanryx-forward.conf << EOF
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
EOF
sysctl --system -q

# ── 4. NAT masquerade ────────────────────────────────────────────────────────
info "Setting up NAT masquerade ($LAN_IFACE → $UPSTREAM_IFACE)…"
iptables -t nat -F POSTROUTING
iptables -F FORWARD
iptables -t nat -A POSTROUTING -o "$UPSTREAM_IFACE" -j MASQUERADE
iptables -A FORWARD -i "$UPSTREAM_IFACE" -o "$LAN_IFACE" \
    -m state --state RELATED,ESTABLISHED -j ACCEPT
iptables -A FORWARD -i "$LAN_IFACE" -o "$UPSTREAM_IFACE" -j ACCEPT
netfilter-persistent save

# ── 5. Static route back to main Pi subnet ───────────────────────────────────
# Allows satellite devices to reach 192.168.10.x (main Pi's LAN).
# The main Pi must also have a route back (see step 8 in README section below).
ROUTE_SERVICE="/etc/systemd/system/hanryx-static-route.service"
info "Writing persistent static route service…"
cat > "$ROUTE_SERVICE" << EOF
[Unit]
Description=HanryxVault static route to main Pi subnet
After=network.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/sbin/ip route add $MAIN_PI_SUBNET via $MAIN_PI_IP dev $LAN_IFACE || true
ExecStop=/sbin/ip route del $MAIN_PI_SUBNET || true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable hanryx-static-route
systemctl start  hanryx-static-route

# ── 6. Hostname ───────────────────────────────────────────────────────────────
info "Setting hostname to $HOSTNAME_LABEL…"
hostnamectl set-hostname "$HOSTNAME_LABEL"
grep -q "$HOSTNAME_LABEL" /etc/hosts || \
    echo "127.0.1.1  $HOSTNAME_LABEL" >> /etc/hosts

# ── 7. Firewall: allow POS port from LAN ─────────────────────────────────────
iptables -C INPUT -i "$LAN_IFACE" -p tcp --dport 8080 -j ACCEPT 2>/dev/null || \
    iptables -A INPUT -i "$LAN_IFACE" -p tcp --dport 8080 -j ACCEPT
iptables -C INPUT -i lo -j ACCEPT 2>/dev/null || \
    iptables -A INPUT -i lo -j ACCEPT
netfilter-persistent save

# ── 8. Enable services ───────────────────────────────────────────────────────
systemctl unmask dnsmasq
systemctl enable dnsmasq
systemctl restart dnsmasq
systemctl restart dhcpcd

echo ""
echo -e "${GREEN}══════════════════════════════════════════${NC}"
echo -e "${GREEN}  Satellite Pi network setup complete!${NC}"
echo -e "${GREEN}══════════════════════════════════════════${NC}"
echo ""
echo "  Pi LAN IP    : $LAN_IP"
echo "  DHCP range   : $DHCP_START – $DHCP_END"
echo "  Local domain : hanryx-sat.local"
echo "  Main Pi LAN  : $MAIN_PI_SUBNET (route installed)"
echo ""
echo "  To let main Pi reach this subnet, run on the main Pi:"
echo "    sudo ip route add $LAN_SUBNET via <satellite-wlan0-ip>"
echo "  (make it permanent by adding it to setup-main-pi-network.sh)"
echo ""
warn "Reboot recommended:"
echo "  sudo reboot"
