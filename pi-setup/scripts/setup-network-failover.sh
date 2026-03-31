#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  HanryxVault — Network Auto-Failover Setup (Trade Show Pi)
#
#  Configures the Pi to:
#    1. Use WiFi as the primary internet connection (preferred)
#    2. Automatically use a USB-tethered phone as a backup modem when WiFi
#       is unavailable or loses internet — no manual intervention needed
#    3. Switch back to WiFi automatically when it returns
#
#  Supports:
#    • Android USB tethering (RNDIS / ECM — plug in, enable tethering in Settings)
#    • iPhone USB tethering (Personal Hotspot — requires ipheth driver)
#    • Any device that presents as a USB network adapter
#
#  Routing metric strategy:
#    WiFi          → metric 100  (always preferred when up)
#    USB tethering → metric 200  (used only when WiFi has no route)
#
#  Run this on the TRADE SHOW Pi:
#    sudo bash scripts/setup-network-failover.sh
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[net-failover]${NC} $*"; }
warn()  { echo -e "${YELLOW}[net-failover]${NC} $*"; }
step()  { echo -e "${CYAN}══ $* ══${NC}"; }
error() { echo -e "${RED}[net-failover] ERROR:${NC} $*"; exit 1; }

[[ $EUID -ne 0 ]] && error "Run with sudo"

echo ""
echo -e "${CYAN}"
echo "  ╔═══════════════════════════════════════════════╗"
echo "  ║  HanryxVault Network Auto-Failover Setup     ║"
echo "  ║  WiFi primary · USB phone tethering backup   ║"
echo "  ╚═══════════════════════════════════════════════╝"
echo -e "${NC}"

# ── Step 1: Install NetworkManager and USB tethering drivers ──────────────────
step "Installing NetworkManager and USB tethering support"

apt-get update -qq
apt-get install -y --no-install-recommends \
    network-manager \
    usb-modeswitch \
    usb-modeswitch-data \
    libimobiledevice6 \
    ipheth-utils \
    usbutils \
    >/dev/null

info "Packages installed"

# ── Step 2: Make sure NetworkManager manages all interfaces ───────────────────
step "Configuring NetworkManager"

# Disable dhcpcd managing interfaces that NM should own
# (Pi OS often has both; we want NM in charge)
if systemctl is-enabled dhcpcd &>/dev/null; then
    # Tell dhcpcd to leave USB and WiFi interfaces alone — NM will handle them
    DHCPCD_CONF="/etc/dhcpcd.conf"
    if ! grep -q "denyinterfaces usb\*" "$DHCPCD_CONF" 2>/dev/null; then
        cat >> "$DHCPCD_CONF" << 'EOF'

# Managed by NetworkManager — do not touch these interfaces
denyinterfaces usb* enp* eth1 eth2 eth3
EOF
        info "Told dhcpcd to ignore USB interfaces (NetworkManager will manage them)"
    fi
fi

# Ensure NM is running and enabled
systemctl enable --now NetworkManager
info "NetworkManager enabled and running"

# ── Step 3: Detect the WiFi interface ────────────────────────────────────────
step "Detecting WiFi interface"

WIFI_IFACE=$(nmcli -t -f DEVICE,TYPE device \
    | awk -F: '$2=="wifi"{print $1; exit}')

if [[ -z "$WIFI_IFACE" ]]; then
    warn "No WiFi interface found — is the Pi's WiFi enabled?"
    WIFI_IFACE="wlan0"   # default, user can correct later
fi
info "WiFi interface: ${WIFI_IFACE}"

# ── Step 4: Set WiFi connection metric ────────────────────────────────────────
step "Setting WiFi routing metric (lower = preferred)"

# Find the active WiFi connection profile name
WIFI_CON=$(nmcli -t -f NAME,DEVICE con show --active \
    | awk -F: -v iface="$WIFI_IFACE" '$2==iface{print $1; exit}')

if [[ -n "$WIFI_CON" ]]; then
    nmcli connection modify "$WIFI_CON" \
        ipv4.route-metric 100 \
        ipv6.route-metric 100
    nmcli connection up "$WIFI_CON" 2>/dev/null || true
    info "Set metric 100 on WiFi connection: '${WIFI_CON}'"
else
    warn "No active WiFi connection found — connect to WiFi first, then re-run"
    warn "Or set the metric manually: nmcli connection modify <name> ipv4.route-metric 100"
fi

# ── Step 5: NetworkManager dispatcher — auto-configure USB tethering ──────────
step "Installing USB tethering auto-configuration dispatcher"

# NetworkManager calls dispatcher scripts when interfaces come up/go down.
# This script runs automatically whenever a USB network interface appears
# and sets it up as a metered backup with metric 200.

cat > /etc/NetworkManager/dispatcher.d/70-usb-tethering-failover << 'DISPATCHER'
#!/usr/bin/env bash
# NetworkManager dispatcher: auto-configure USB tethering as backup internet.
# Called by NetworkManager with: <interface> <action>

IFACE="$1"
ACTION="$2"

# Only act on interfaces that look like USB network adapters
# usb0, usb1 = Android tethering (RNDIS/ECM)
# enp*u* = USB ethernet adapters / Android on some kernels
# eth1+ = iPhone (ipheth driver sometimes names it eth1, eth2, etc.)
is_usb_tether() {
    local iface="$1"
    # Check by interface name pattern
    [[ "$iface" =~ ^usb[0-9]+$     ]] && return 0
    [[ "$iface" =~ ^enp.*u[0-9]    ]] && return 0
    # Check sysfs — USB interfaces have a parent USB device
    local sysfs="/sys/class/net/${iface}/device"
    if [[ -L "$sysfs" ]]; then
        readlink -f "$sysfs" | grep -q '/usb[0-9]' && return 0
    fi
    return 1
}

# Skip non-USB interfaces and non-relevant actions
is_usb_tether "$IFACE" || exit 0
[[ "$ACTION" == "up" || "$ACTION" == "connectivity-change" ]] || exit 0

logger -t usb-tethering "Interface ${IFACE} came up — configuring as backup internet (metric 200)"

# Check if NetworkManager already has a connection for this interface
EXISTING=$(nmcli -t -f NAME,DEVICE con show | awk -F: -v i="$IFACE" '$2==i{print $1; exit}')

if [[ -n "$EXISTING" ]]; then
    # Update existing connection metric
    nmcli connection modify "$EXISTING" \
        ipv4.route-metric 200 \
        ipv6.route-metric 200 \
        connection.autoconnect yes \
        ipv4.dhcp-timeout 30 \
        2>/dev/null || true
    nmcli connection up "$EXISTING" 2>/dev/null || true
    logger -t usb-tethering "Updated existing profile '${EXISTING}' — metric 200"
else
    # Create a new connection profile for this USB interface
    CON_NAME="usb-tether-${IFACE}"
    nmcli connection add \
        type ethernet \
        ifname "$IFACE" \
        con-name "$CON_NAME" \
        connection.autoconnect yes \
        ipv4.method auto \
        ipv4.route-metric 200 \
        ipv4.dhcp-timeout 30 \
        ipv6.route-metric 200 \
        ipv6.method auto \
        2>/dev/null || true
    nmcli connection up "$CON_NAME" 2>/dev/null || true
    logger -t usb-tethering "Created new profile '${CON_NAME}' for ${IFACE} — metric 200"
fi

logger -t usb-tethering "USB tethering ready on ${IFACE} — WiFi remains preferred (metric 100)"
DISPATCHER

chmod +x /etc/NetworkManager/dispatcher.d/70-usb-tethering-failover
info "Dispatcher installed → /etc/NetworkManager/dispatcher.d/70-usb-tethering-failover"

# ── Step 6: iPhone ipheth udev rule ───────────────────────────────────────────
step "iPhone tethering (ipheth) udev rule"

# When an iPhone is plugged in, pair it and load the ipheth driver.
# The user must also trust the computer on the iPhone screen (tap Trust).
cat > /etc/udev/rules.d/40-iphone-tether.rules << 'UDEV'
# Load ipheth kernel module when an iPhone is plugged in
SUBSYSTEM=="usb", ATTRS{idVendor}=="05ac", ACTION=="add", \
    RUN+="/sbin/modprobe ipheth", \
    RUN+="/usr/bin/idevicepair pair"
UDEV

udevadm control --reload-rules
info "iPhone udev rule installed (plug in, tap Trust, tethering starts automatically)"

# ── Step 7: Connectivity check ping target ────────────────────────────────────
step "Configuring NetworkManager connectivity check"

# NM can detect whether an interface actually has internet (not just link).
# This lets it prefer a working interface over one that's "up" but has no internet.
NM_CONF="/etc/NetworkManager/conf.d/20-connectivity.conf"
cat > "$NM_CONF" << 'EOF'
[connectivity]
uri=http://connectivity-check.ubuntu.com
interval=30
EOF
info "Connectivity check enabled (NM will detect captive portals / no-internet WiFi)"

# Restart NM to apply all changes
systemctl restart NetworkManager
sleep 2
info "NetworkManager restarted"

# ── Step 8: Modprobe ipheth on boot ───────────────────────────────────────────
echo "ipheth" >> /etc/modules-load.d/ipheth.conf 2>/dev/null || true

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}"
echo "  ╔═══════════════════════════════════════════════╗"
echo "  ║  Network failover configured!                 ║"
echo "  ╚═══════════════════════════════════════════════╝"
echo -e "${NC}"
info "How it works:"
info "  WiFi (metric 100)          — always used when available"
info "  USB phone tethering (200)  — takes over automatically when WiFi drops"
info "  Returns to WiFi            — automatically when WiFi comes back"
echo ""
info "To use USB tethering:"
info "  Android : plug in phone → Settings → Hotspot → USB tethering"
info "  iPhone  : plug in → tap Trust on phone → Settings → Personal Hotspot → on"
echo ""
info "Useful commands:"
info "  See current routes  : ip route show"
info "  See interfaces      : nmcli device status"
info "  See connections     : nmcli connection show --active"
info "  Test connectivity   : nmcli networking connectivity check"
info "  Watch NM events     : journalctl -u NetworkManager -f"
