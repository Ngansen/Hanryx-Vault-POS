#!/usr/bin/env bash
# =============================================================================
#  HanryxVault — Netgear R6020 AP-Mode Configuration Helper
#
#  The R6020's AP mode is set through its web admin UI (192.168.0.1 by default).
#  This script:
#    1. Tries to reach the R6020 on the LAN after it's plugged into the switch.
#    2. Prints step-by-step instructions with the right URLs for your setup.
#    3. Optionally sends a reboot command via the Netgear API.
#
#  Usage:  bash configure-r6020-ap-mode.sh
#
#  Physical setup FIRST:
#    - Connect R6020 LAN port (any numbered port) → Netgear unmanaged switch
#    - Do NOT plug anything into the R6020's WAN/Internet port
#    - Connect Pi eth0 → same unmanaged switch
# =============================================================================

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[✓]${NC} $*"; }
step()  { echo -e "${CYAN}[→]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }

# Default Netgear R6020 gateway — it ships with 192.168.0.1
# If you changed this previously, update here:
R6020_DEFAULT_IP="192.168.0.1"
R6020_ADMIN_USER="admin"
# Default password is "password" on factory reset devices

echo ""
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Netgear R6020 → AP Mode Setup Guide${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"

# ── Step 1: Check if R6020 is reachable ──────────────────────────────────────
echo ""
echo -e "Checking if R6020 is reachable at $R6020_DEFAULT_IP…"
if ping -c 2 -W 2 "$R6020_DEFAULT_IP" &>/dev/null; then
    info "R6020 found at $R6020_DEFAULT_IP"
    R6020_IP="$R6020_DEFAULT_IP"
else
    warn "R6020 not found at $R6020_DEFAULT_IP"
    warn "It may be on a different IP if previously configured."
    echo ""
    echo "  Try scanning your switch for connected devices:"
    echo "    sudo arp-scan --interface=eth0 --localnet"
    echo "  Or check the R6020's label for its default IP."
    echo ""
    read -rp "  Enter R6020 IP address (or press Enter to skip): " R6020_IP
    R6020_IP="${R6020_IP:-$R6020_DEFAULT_IP}"
fi

# ── Step 2: Print manual steps ───────────────────────────────────────────────
echo ""
echo -e "${CYAN}Manual steps to set AP mode on the R6020:${NC}"
echo ""
step "1. Open a browser and go to: http://$R6020_IP"
step "   Login: Username=admin  Password=password (or your custom password)"
echo ""
step "2. Navigate to:  ADVANCED → Advanced Setup → Wireless AP"
echo "   (On older firmware: Setup → Wireless Settings → Access Point Mode)"
echo ""
step "3. Tick the box: 'Enable Access Point Mode'"
echo ""
step "4. Set the AP IP address to something in the Pi's LAN range:"
echo "   IP Address   : 192.168.10.11"
echo "   Subnet Mask  : 255.255.255.0"
echo "   Gateway      : 192.168.10.1   ← Pi's eth0 address"
echo ""
step "5. DISABLE the R6020's DHCP server:"
echo "   ADVANCED → Setup → LAN Setup → uncheck 'Use Router as DHCP Server'"
echo ""
step "6. Set your WiFi SSID and password on the R6020 (it will broadcast as a"
echo "   WiFi access point — clients connect to it and get DHCP from the Pi)."
echo ""
step "7. Click Apply. The R6020 will reboot and come up at 192.168.10.11."
echo ""

# ── Step 3: After AP mode — verify from Pi ───────────────────────────────────
echo -e "${CYAN}After the R6020 reboots, verify from this Pi:${NC}"
echo ""
echo "   ping 192.168.10.11          # R6020 responds"
echo "   arp -n | grep 192.168.10    # see all devices on the switch"
echo "   cat /var/lib/misc/dnsmasq.leases   # devices getting DHCP from Pi"
echo ""

# ── Step 4: Factory-reset reminder ───────────────────────────────────────────
warn "If the R6020 was previously configured and you can't access its UI:"
echo "   Hold the Reset button for 10 seconds until the power LED blinks."
echo "   Factory default: 192.168.0.1 / admin / password"
echo ""

# ── Step 5: Add R6020 to /etc/hosts if reachable ────────────────────────────
TARGET_IP="192.168.10.11"
if ! grep -q "netgear-r6020" /etc/hosts 2>/dev/null; then
    read -rp "Add R6020 to /etc/hosts as netgear-r6020? [Y/n] " ans
    if [[ "${ans:-y}" =~ ^[Yy] ]]; then
        echo "$TARGET_IP  netgear-r6020 netgear-r6020.hanryx.local" | sudo tee -a /etc/hosts > /dev/null
        info "Added netgear-r6020 → $TARGET_IP to /etc/hosts"
    fi
fi

echo ""
info "Guide complete. Once AP mode is active, WiFi devices connecting to the"
info "R6020 will receive DHCP leases from the Pi (192.168.10.50–200)."
echo ""
