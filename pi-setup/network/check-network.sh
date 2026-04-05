#!/usr/bin/env bash
# =============================================================================
#  HanryxVault — Network Status Checker
#  Run on either Pi to diagnose the routing / DHCP / NAT setup.
#  Usage:  bash check-network.sh
# =============================================================================

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}✓${NC}  $*"; }
warn() { echo -e "  ${YELLOW}!${NC}  $*"; }
fail() { echo -e "  ${RED}✗${NC}  $*"; }

section() { echo ""; echo -e "${GREEN}── $* ──────────────────────────────────${NC}"; }

section "Interfaces"
ip -br addr show | while read -r iface state addr; do
    [[ "$state" == "UP" ]] && ok "$iface  $addr" || warn "$iface  $addr ($state)"
done

section "Routing table"
ip route show

section "IP forwarding"
FWD=$(sysctl -n net.ipv4.ip_forward)
[[ "$FWD" == "1" ]] && ok "IPv4 forwarding enabled" || fail "IPv4 forwarding DISABLED — run: echo 1 | sudo tee /proc/sys/net/ipv4/ip_forward"

section "iptables NAT rules"
if [[ $EUID -ne 0 ]]; then
    warn "Run with sudo for iptables check:  sudo bash check-network.sh"
    warn "Skipping iptables check (needs root)."
elif iptables -t nat -L POSTROUTING -n 2>/dev/null | grep -q MASQUERADE; then
    ok "MASQUERADE rule present"
    iptables -t nat -L POSTROUTING -n --line-numbers
else
    fail "No MASQUERADE rule — run: sudo bash setup-nat-service.sh"
fi

section "dnsmasq status"
if systemctl is-active --quiet dnsmasq; then
    ok "dnsmasq is running"
    echo "  Config files:"
    ls /etc/dnsmasq.d/ 2>/dev/null | sed 's/^/    /'
    echo "  Active leases:"
    cat /var/lib/misc/dnsmasq.leases 2>/dev/null | awk '{print "    "$3"\t"$4"\t("$2")"}' || echo "    (none yet)"
else
    fail "dnsmasq is NOT running"
    echo "  → sudo systemctl start dnsmasq"
    echo "  → sudo journalctl -u dnsmasq -n 30 --no-pager"
fi

section "Internet connectivity"
if ping -c 1 -W 2 8.8.8.8 &>/dev/null; then
    ok "Internet reachable (8.8.8.8)"
else
    fail "Cannot reach 8.8.8.8 — check WiFi / upstream connection"
fi

if ping -c 1 -W 2 google.com &>/dev/null; then
    ok "DNS resolving (google.com)"
else
    fail "DNS not resolving — check /etc/resolv.conf and dnsmasq"
fi

section "LAN connectivity"
for target in 192.168.10.1 192.168.11.1; do
    if ping -c 1 -W 1 "$target" &>/dev/null; then
        ok "Reachable: $target"
    else
        warn "Unreachable: $target (expected if this is that Pi)"
    fi
done

section "Listening services"
ss -tlnp | grep -E ":(8080|53|67|68|80|443)" | while read -r line; do
    ok "$line"
done

section "Done"
echo ""
