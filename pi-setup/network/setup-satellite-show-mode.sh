#!/usr/bin/env bash
# =============================================================================
#  HanryxVault — Satellite Pi "Show Mode" toggle
#
#  Turns the satellite Pi's built-in WiFi (wlan0) into an Access Point so
#  your phone, tablet, customers, etc. can join it directly at a card show
#  WITHOUT needing any other router or internet uplink.
#
#  When ON:
#    SSID:      HanryxVault-Show     (override with SHOW_SSID env)
#    Password:  hanryx2026           (override with SHOW_PSK  env, min 8 chars)
#    Channel:   6                    (override with SHOW_CHAN env)
#    Subnet:    192.168.12.0/24
#    Pi IP:     192.168.12.1
#    DHCP:      192.168.12.50–192.168.12.200, 12-h leases
#    URLs your phone/tablet will hit:
#      • POS dashboard  :  http://192.168.12.1:8080/
#      • Kiosk view     :  http://192.168.12.1:8080/kiosk
#      • Scan hub       :  http://192.168.12.1:8765/scan/stream
#
#  When OFF:
#    wlan0 reverts to "client" mode and reconnects to your home WiFi via
#    NetworkManager / wpa_supplicant.
#
#  Usage:
#    sudo bash setup-satellite-show-mode.sh on        # turn AP on
#    sudo bash setup-satellite-show-mode.sh off       # turn AP off, rejoin home WiFi
#    sudo bash setup-satellite-show-mode.sh status    # show current state
#    sudo bash setup-satellite-show-mode.sh qr        # print join-WiFi QR code
#
#  Network plan for a card show:
#    [Phone] ──┐
#              ├── WiFi to satellite Pi (192.168.12.x)
#    [Tablet] ─┘                │
#                          [Satellite Pi]
#                               │  optional: eth0 to LTE hotspot/router
#                               ▼
#                            Internet  →  WireGuard back to Main Pi (10.8.0.1)
# =============================================================================

set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[✓]${NC} $*"; }
step() { echo -e "${CYAN}[→]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
die()  { echo -e "${RED}[✗] $*${NC}"; exit 1; }

[[ $EUID -ne 0 ]] && die "Run as root: sudo bash $0 <on|off|status|qr>"

# ── Configurable via env vars ────────────────────────────────────────────────
AP_IFACE="${AP_IFACE:-wlan0}"
SHOW_SSID="${SHOW_SSID:-HanryxVault-Show}"
SHOW_PSK="${SHOW_PSK:-hanryx2026}"
SHOW_CHAN="${SHOW_CHAN:-6}"
COUNTRY_CODE="${COUNTRY_CODE:-US}"

AP_IP="192.168.12.1"
AP_NET="192.168.12.0/24"
AP_DHCP_START="192.168.12.50"
AP_DHCP_END="192.168.12.200"
AP_DHCP_LEASE="12h"

POS_PORT=8080
SCAN_PORT=8765

HOSTAPD_CONF="/etc/hostapd/hanryx-show.conf"
DNSMASQ_CONF="/etc/dnsmasq.d/hanryx-show.conf"
DHCPCD_DROPIN="/etc/dhcpcd.conf.d/hanryx-show.conf"
NM_KEEPOUT="/etc/NetworkManager/conf.d/99-hanryx-show.conf"
SHOW_FLAG="/var/lib/hanryx-show-mode.on"

mkdir -p /etc/dhcpcd.conf.d /etc/NetworkManager/conf.d

# ── Validate basic config ────────────────────────────────────────────────────
if [[ ${#SHOW_PSK} -lt 8 ]]; then
    die "SHOW_PSK must be at least 8 characters (current length ${#SHOW_PSK})."
fi

CMD="${1:-}"
[[ -z "$CMD" ]] && die "Usage: sudo bash $0 <on|off|status|qr>"

# ─────────────────────────────────────────────────────────────────────────────
install_packages_if_needed() {
    local need=()
    command -v hostapd  >/dev/null 2>&1 || need+=(hostapd)
    command -v dnsmasq  >/dev/null 2>&1 || need+=(dnsmasq)
    command -v qrencode >/dev/null 2>&1 || need+=(qrencode)
    command -v iptables >/dev/null 2>&1 || need+=(iptables)
    if (( ${#need[@]} )); then
        step "Installing: ${need[*]}"
        apt-get update -qq
        apt-get install -y -qq "${need[@]}"
    fi
}

write_hostapd_conf() {
    cat >"$HOSTAPD_CONF" <<EOF
interface=${AP_IFACE}
driver=nl80211
ssid=${SHOW_SSID}
hw_mode=g
channel=${SHOW_CHAN}
country_code=${COUNTRY_CODE}
ieee80211d=1
ieee80211n=1
wmm_enabled=1
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase=${SHOW_PSK}
EOF
    chmod 600 "$HOSTAPD_CONF"
}

write_dnsmasq_conf() {
    cat >"$DNSMASQ_CONF" <<EOF
interface=${AP_IFACE}
bind-interfaces
domain-needed
bogus-priv
dhcp-range=${AP_DHCP_START},${AP_DHCP_END},255.255.255.0,${AP_DHCP_LEASE}
dhcp-option=option:router,${AP_IP}
dhcp-option=option:dns-server,${AP_IP},1.1.1.1,8.8.8.8
# Convenience hostname so phones can hit "http://hanryx" instead of an IP
address=/hanryx/${AP_IP}
address=/hanryx.local/${AP_IP}
EOF
}

write_dhcpcd_dropin() {
    cat >"$DHCPCD_DROPIN" <<EOF
interface ${AP_IFACE}
static ip_address=${AP_IP}/24
nohook wpa_supplicant
EOF
}

block_networkmanager_on_iface() {
    cat >"$NM_KEEPOUT" <<EOF
[keyfile]
unmanaged-devices=interface-name:${AP_IFACE}
EOF
}

unblock_networkmanager_on_iface() {
    rm -f "$NM_KEEPOUT"
}

# Allow inbound POS + scan-hub from the AP subnet (idempotent)
firewall_open_ports() {
    iptables -C INPUT -i "$AP_IFACE" -p tcp --dport "$POS_PORT" -j ACCEPT 2>/dev/null \
        || iptables -I INPUT -i "$AP_IFACE" -p tcp --dport "$POS_PORT" -j ACCEPT
    iptables -C INPUT -i "$AP_IFACE" -p tcp --dport "$SCAN_PORT" -j ACCEPT 2>/dev/null \
        || iptables -I INPUT -i "$AP_IFACE" -p tcp --dport "$SCAN_PORT" -j ACCEPT
    iptables -C INPUT -i "$AP_IFACE" -p udp --dport 67 -j ACCEPT 2>/dev/null \
        || iptables -I INPUT -i "$AP_IFACE" -p udp --dport 67 -j ACCEPT
    iptables -C INPUT -i "$AP_IFACE" -p udp --dport 53 -j ACCEPT 2>/dev/null \
        || iptables -I INPUT -i "$AP_IFACE" -p udp --dport 53 -j ACCEPT
}

# Optional uplink: if eth0 has internet (LTE hotspot, etc.), share it to AP clients
maybe_enable_internet_share() {
    if ip route | grep -q "default.*eth0"; then
        echo 1 > /proc/sys/net/ipv4/ip_forward
        iptables -t nat -C POSTROUTING -o eth0 -j MASQUERADE 2>/dev/null \
            || iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
        iptables -C FORWARD -i eth0 -o "$AP_IFACE" -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null \
            || iptables -A FORWARD -i eth0 -o "$AP_IFACE" -m state --state RELATED,ESTABLISHED -j ACCEPT
        iptables -C FORWARD -i "$AP_IFACE" -o eth0 -j ACCEPT 2>/dev/null \
            || iptables -A FORWARD -i "$AP_IFACE" -o eth0 -j ACCEPT
        info "Internet share enabled: ${AP_IFACE} clients will route via eth0"
    else
        warn "No default route on eth0 — AP will be local-only (no internet for clients)."
        warn "Plug a USB-Ethernet to a phone hotspot or LTE router to share internet."
    fi
}

print_join_qr() {
    command -v qrencode >/dev/null 2>&1 || apt-get install -y -qq qrencode
    echo
    echo -e "${CYAN}Scan this QR code with your phone camera to join WiFi:${NC}"
    qrencode -t ANSIUTF8 "WIFI:T:WPA;S:${SHOW_SSID};P:${SHOW_PSK};H:false;;"
    echo
    echo -e "${CYAN}Then open in your browser:${NC}  http://${AP_IP}:${POS_PORT}/"
    echo -e "${CYAN}Or scan this URL QR:${NC}"
    qrencode -t ANSIUTF8 "http://${AP_IP}:${POS_PORT}/"
    echo
}

# ─────────────────────────────────────────────────────────────────────────────
turn_on() {
    install_packages_if_needed

    step "Stopping wpa_supplicant on ${AP_IFACE} (so it can host an AP)…"
    systemctl stop "wpa_supplicant@${AP_IFACE}" 2>/dev/null || true
    systemctl stop wpa_supplicant 2>/dev/null || true
    block_networkmanager_on_iface
    systemctl reload NetworkManager 2>/dev/null || true

    step "Writing hostapd / dnsmasq / dhcpcd configs…"
    write_hostapd_conf
    write_dnsmasq_conf
    write_dhcpcd_dropin

    # Tell hostapd which conf to use
    sed -i 's|^#*DAEMON_CONF=.*|DAEMON_CONF="'"$HOSTAPD_CONF"'"|' /etc/default/hostapd 2>/dev/null \
        || echo "DAEMON_CONF=\"$HOSTAPD_CONF\"" > /etc/default/hostapd

    # Country regulatory bits — required on Pi 5 or wlan0 won't broadcast
    iw reg set "$COUNTRY_CODE" 2>/dev/null || true
    rfkill unblock wifi 2>/dev/null || true

    step "Configuring static IP ${AP_IP} on ${AP_IFACE}…"
    ip addr flush dev "$AP_IFACE" 2>/dev/null || true
    ip addr add "${AP_IP}/24" dev "$AP_IFACE"
    ip link set "$AP_IFACE" up

    step "Restarting dhcpcd / unmasking + starting hostapd + dnsmasq…"
    systemctl restart dhcpcd 2>/dev/null || true
    systemctl unmask hostapd 2>/dev/null || true
    systemctl enable --now hostapd
    systemctl restart hostapd
    systemctl enable --now dnsmasq
    systemctl restart dnsmasq

    firewall_open_ports
    maybe_enable_internet_share

    touch "$SHOW_FLAG"

    echo
    info "✓ Show-mode access point is UP"
    echo "    SSID     : ${SHOW_SSID}"
    echo "    Password : ${SHOW_PSK}"
    echo "    Channel  : ${SHOW_CHAN}"
    echo "    Pi IP    : ${AP_IP}"
    echo
    echo "  Phone/tablet steps:"
    echo "    1. Join WiFi: ${SHOW_SSID}  (password: ${SHOW_PSK})"
    echo "    2. Open browser: http://${AP_IP}:${POS_PORT}/"
    echo
    echo "  Tip: 'sudo bash $0 qr' prints a QR code for one-tap join."
}

turn_off() {
    step "Stopping AP services…"
    systemctl disable --now hostapd 2>/dev/null || true
    systemctl disable --now dnsmasq 2>/dev/null || true

    step "Removing AP configs…"
    rm -f "$HOSTAPD_CONF" "$DNSMASQ_CONF" "$DHCPCD_DROPIN" "$SHOW_FLAG"

    step "Releasing ${AP_IFACE} static IP…"
    ip addr flush dev "$AP_IFACE" 2>/dev/null || true

    step "Re-enabling NetworkManager / wpa_supplicant on ${AP_IFACE}…"
    unblock_networkmanager_on_iface
    systemctl reload NetworkManager 2>/dev/null || true
    systemctl restart dhcpcd 2>/dev/null || true
    systemctl restart wpa_supplicant 2>/dev/null || true
    # Nudge it to reassociate with home WiFi
    wpa_cli -i "$AP_IFACE" reconfigure 2>/dev/null || true

    info "Show mode OFF — ${AP_IFACE} returned to client mode."
    echo "  Wait ~10 s, then check: iwconfig ${AP_IFACE}"
}

show_status() {
    if [[ -f "$SHOW_FLAG" ]]; then
        info "Mode: SHOW (AP)"
    else
        info "Mode: HOME (client)"
    fi
    echo
    echo "── ${AP_IFACE} ──"
    iwconfig "$AP_IFACE" 2>/dev/null | head -3 || true
    ip -4 addr show "$AP_IFACE" 2>/dev/null | grep -E 'inet ' || true
    echo
    echo "── Services ──"
    for svc in hostapd dnsmasq dhcpcd; do
        printf '  %-10s : %s\n' "$svc" "$(systemctl is-active "$svc" 2>/dev/null || echo not-installed)"
    done
    echo
    if [[ -f "$SHOW_FLAG" ]]; then
        echo "── Connected clients ──"
        if command -v iw >/dev/null; then
            iw dev "$AP_IFACE" station dump 2>/dev/null \
                | awk '/Station/ {mac=$2} /signal:/ {sig=$2 " " $3} /tx bitrate:/ {print "  "mac"   signal "sig"   tx "$3" "$4}'
        fi
    fi
}

case "$CMD" in
    on)     turn_on ;;
    off)    turn_off ;;
    status) show_status ;;
    qr)     [[ -f "$SHOW_FLAG" ]] || warn "Show mode appears OFF — QR is for the configured SSID anyway."
            print_join_qr ;;
    *)      die "Usage: sudo bash $0 <on|off|status|qr>" ;;
esac
