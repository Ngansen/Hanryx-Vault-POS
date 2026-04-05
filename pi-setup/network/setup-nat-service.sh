#!/usr/bin/env bash
# =============================================================================
#  HanryxVault — Persistent NAT service (runs after Docker on every boot)
#
#  Docker flushes the FORWARD chain on startup, wiping iptables NAT rules.
#  This creates a systemd service that runs AFTER Docker and re-applies them.
#
#  Usage:  sudo bash setup-nat-service.sh
# =============================================================================

set -euo pipefail

UPSTREAM_IFACE="wlan0"
LAN_IFACE="eth0"

GREEN='\033[0;32m'; NC='\033[0m'
info() { echo -e "${GREEN}[✓]${NC} $*"; }

[[ $EUID -ne 0 ]] && { echo "Run as root: sudo bash $0"; exit 1; }

# ── Write the NAT rules script ────────────────────────────────────────────────
cat > /usr/local/bin/hanryx-nat.sh << EOF
#!/usr/bin/env bash
# Re-apply NAT masquerade after Docker clears iptables on boot
UPSTREAM="$UPSTREAM_IFACE"
LAN="$LAN_IFACE"

# Masquerade LAN traffic going out via WiFi
iptables -t nat -C POSTROUTING -o "\$UPSTREAM" -j MASQUERADE 2>/dev/null || \
    iptables -t nat -A POSTROUTING -o "\$UPSTREAM" -j MASQUERADE

# Forward established connections back to LAN
iptables -C FORWARD -i "\$UPSTREAM" -o "\$LAN" \
    -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || \
    iptables -A FORWARD -i "\$UPSTREAM" -o "\$LAN" \
        -m state --state RELATED,ESTABLISHED -j ACCEPT

# Forward new connections from LAN to internet
iptables -C FORWARD -i "\$LAN" -o "\$UPSTREAM" -j ACCEPT 2>/dev/null || \
    iptables -A FORWARD -i "\$LAN" -o "\$UPSTREAM" -j ACCEPT

# Allow POS dashboard from LAN
iptables -C INPUT -i "\$LAN" -p tcp --dport 8080 -j ACCEPT 2>/dev/null || \
    iptables -A INPUT -i "\$LAN" -p tcp --dport 8080 -j ACCEPT

echo "[hanryx-nat] NAT rules applied: \$LAN → \$UPSTREAM"
EOF
chmod +x /usr/local/bin/hanryx-nat.sh

# ── Write the systemd service ─────────────────────────────────────────────────
cat > /etc/systemd/system/hanryx-nat.service << 'EOF'
[Unit]
Description=HanryxVault NAT masquerade (LAN → WiFi)
# Run AFTER Docker so Docker can't wipe our rules
After=network-online.target docker.service
Wants=network-online.target
# Restart if Docker restarts (which re-flushes iptables)
BindsTo=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/hanryx-nat.sh
ExecReload=/usr/local/bin/hanryx-nat.sh

[Install]
WantedBy=multi-user.target
EOF

# ── Enable & start it now ────────────────────────────────────────────────────
systemctl daemon-reload
systemctl enable hanryx-nat
systemctl start  hanryx-nat

info "NAT service installed and running."
echo ""
# Verify immediately
if iptables -t nat -L POSTROUTING -n | grep -q MASQUERADE; then
    echo -e "${GREEN}[✓]${NC} MASQUERADE rule is active."
else
    echo "  Something went wrong — check: sudo journalctl -u hanryx-nat -n 20"
fi
echo ""
echo "  The service will automatically re-apply rules after every reboot"
echo "  and every time Docker restarts."
echo ""
echo "  Check status any time with:"
echo "    sudo systemctl status hanryx-nat"
echo "    sudo iptables -t nat -L POSTROUTING -n"
