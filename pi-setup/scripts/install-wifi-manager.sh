#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────────────────────
#  Install HanryxVault WiFi Manager as a desktop application on the trade show Pi
#  Run: sudo bash scripts/install-wifi-manager.sh
# ───────────────────────────────────────────────────────────────────────────────

set -euo pipefail

GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info() { echo -e "${GREEN}[wifi-manager]${NC} $*"; }
step() { echo -e "${CYAN}══ $* ══${NC}"; }

[[ $EUID -ne 0 ]] && { echo "Run with sudo"; exit 1; }

INSTALL_DIR="/opt/hanryxvault"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── 1. Copy script ─────────────────────────────────────────────────────────────
step "Installing wifi_manager.py"
cp "$SCRIPT_DIR/wifi_manager.py" "$INSTALL_DIR/wifi_manager.py"
chown hanryxvault:hanryxvault "$INSTALL_DIR/wifi_manager.py"
chmod 644 "$INSTALL_DIR/wifi_manager.py"

# ── 2. Wrapper that runs with sudo (needs nmcli connect/disconnect) ────────────
cat > /usr/local/bin/hanryxvault-wifi << 'EOF'
#!/usr/bin/env bash
# Wrapper: launch WiFi manager using system Python3 (tkinter is system-level)
exec python3 /opt/hanryxvault/wifi_manager.py "$@"
EOF
chmod +x /usr/local/bin/hanryxvault-wifi

# Allow the hanryxvault user to run specific nmcli commands without a password
SUDOERS_FILE="/etc/sudoers.d/hanryxvault-nmcli"
cat > "$SUDOERS_FILE" << 'EOF'
# Allow HanryxVault WiFi manager to manage networks without password prompt
hanryxvault ALL=(ALL) NOPASSWD: /usr/bin/nmcli device wifi connect *
hanryxvault ALL=(ALL) NOPASSWD: /usr/bin/nmcli device disconnect *
hanryxvault ALL=(ALL) NOPASSWD: /usr/bin/nmcli connection delete *
hanryxvault ALL=(ALL) NOPASSWD: /usr/bin/nmcli connection modify *
hanryxvault ALL=(ALL) NOPASSWD: /usr/bin/nmcli connection up *
EOF
chmod 440 "$SUDOERS_FILE"
info "Passwordless nmcli access configured"

# ── 3. Desktop shortcut ────────────────────────────────────────────────────────
step "Creating desktop shortcut"

# Find the desktop directory for the primary user
PRIMARY_USER=$(getent passwd 1000 | cut -d: -f1 || echo "pi")
DESKTOP_DIR="/home/${PRIMARY_USER}/Desktop"
mkdir -p "$DESKTOP_DIR"

cat > "$DESKTOP_DIR/hanryxvault-wifi.desktop" << EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=HanryxVault WiFi Manager
Comment=Manage WiFi networks and monitor home Pi sync
Exec=/usr/local/bin/hanryxvault-wifi
Icon=network-wireless
Terminal=false
Categories=Network;Utility;
StartupNotify=true
EOF
chmod +x "$DESKTOP_DIR/hanryxvault-wifi.desktop"
chown "${PRIMARY_USER}:${PRIMARY_USER}" "$DESKTOP_DIR/hanryxvault-wifi.desktop"

# Also install to applications menu
mkdir -p /usr/share/applications
cp "$DESKTOP_DIR/hanryxvault-wifi.desktop" /usr/share/applications/
info "Desktop shortcut created for user: ${PRIMARY_USER}"

# ── 4. Ensure tkinter is available ────────────────────────────────────────────
step "Checking tkinter"
if ! "$INSTALL_DIR/venv/bin/python3" -c "import tkinter" 2>/dev/null; then
    apt-get install -y --no-install-recommends python3-tk >/dev/null
    info "Installed python3-tk"
else
    info "tkinter available ✓"
fi

echo ""
echo -e "${GREEN}WiFi Manager installed!${NC}"
info "Open via: Desktop → HanryxVault WiFi Manager"
info "Or run  : hanryxvault-wifi"
