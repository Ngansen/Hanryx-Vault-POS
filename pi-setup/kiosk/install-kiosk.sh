#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# HanryxVault Kiosk — One-shot installer for Raspberry Pi 5 (Bookworm)
#
# What it does
# ────────────
#   1. Installs OS packages needed for a headless X kiosk
#   2. Copies kiosk scripts to /opt/hanryxvault/kiosk/
#   3. Installs + enables the hanryxvault-kiosk systemd service
#   4. Disables the desktop environment (LightDM / SDDM) if present
#   5. Enables console auto-login for the 'pi' user so xinit can start
#      without a password prompt
#   6. Prints final instructions
#
# Run as root:
#   sudo bash pi-setup/kiosk/install-kiosk.sh
#
# To uninstall:
#   sudo systemctl disable --now hanryxvault-kiosk
#   sudo rm /etc/systemd/system/hanryxvault-kiosk.service
#   sudo systemctl daemon-reload
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

INSTALL_DIR=/opt/hanryxvault
KIOSK_SRC="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$KIOSK_SRC/../.." && pwd)"
PI_SETUP="$REPO_ROOT/pi-setup"
KIOSK_DEST="$INSTALL_DIR/kiosk"
SERVICE_NAME=hanryxvault-kiosk
SERVICE_FILE=/etc/systemd/system/${SERVICE_NAME}.service
KIOSK_USER=${KIOSK_USER:-pi}

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[kiosk]${NC} $*"; }
warn()    { echo -e "${YELLOW}[kiosk]${NC} $*"; }
die()     { echo -e "${RED}[kiosk] ERROR:${NC} $*" >&2; exit 1; }

# ── Root check ───────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Run this script with sudo: sudo bash $0"

# ── OS package dependencies ───────────────────────────────────────────────────
info "Installing OS packages …"
apt-get update -qq
apt-get install -y --no-install-recommends \
    xserver-xorg \
    xinit \
    x11-xserver-utils \
    unclutter \
    curl \
    python3-tk \
    python3-pil \
    python3-pil.imagetk

# ── Create kiosk directory ────────────────────────────────────────────────────
info "Copying kiosk files to $KIOSK_DEST …"
mkdir -p "$KIOSK_DEST"

cp "$KIOSK_SRC/start-monitor.sh"         "$KIOSK_DEST/start-monitor.sh"
cp "$KIOSK_SRC/hanryxvault-kiosk.service" "$SERVICE_FILE"
chmod +x "$KIOSK_DEST/start-monitor.sh"

# Copy the monitor app itself (or symlink if already installed)
if [[ -f "$PI_SETUP/desktop_monitor.py" ]]; then
    cp "$PI_SETUP/desktop_monitor.py" "$INSTALL_DIR/desktop_monitor.py"
    info "Copied desktop_monitor.py to $INSTALL_DIR/"
fi

# ── Correct PYTHON path in service if venv is missing ────────────────────────
if [[ ! -x "$INSTALL_DIR/venv/bin/python3" ]]; then
    SYSPY=$(command -v python3)
    warn "No venv found at $INSTALL_DIR/venv — using system python3: $SYSPY"
    sed -i "s|/opt/hanryxvault/venv/bin/python3|$SYSPY|g" "$SERVICE_FILE"
fi

# ── Disable desktop environment (LightDM / SDDM / GDM) ──────────────────────
for DM in lightdm sddm gdm3 gdm display-manager; do
    if systemctl is-enabled "$DM" &>/dev/null 2>&1; then
        info "Disabling display manager: $DM"
        systemctl disable "$DM" || true
    fi
done

# ── Enable console auto-login for $KIOSK_USER ────────────────────────────────
# Uses raspi-config non-interactive if available, falls back to manual override.
if command -v raspi-config &>/dev/null; then
    info "Setting console auto-login via raspi-config (B2) …"
    raspi-config nonint do_boot_behaviour B2 || true
else
    warn "raspi-config not found — configuring getty override manually"
    GETTY_OVERRIDE=/etc/systemd/system/getty@tty1.service.d
    mkdir -p "$GETTY_OVERRIDE"
    cat > "$GETTY_OVERRIDE/autologin.conf" <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin ${KIOSK_USER} --noclear %I \$TERM
EOF
fi

# ── xinitrc: start our kiosk session on 'startx' or 'xinit' ──────────────────
# The service calls xinit directly, but set up ~/.xinitrc as a fallback so
# 'startx' from the console also opens the kiosk.
XINITRC="/home/${KIOSK_USER}/.xinitrc"
if [[ ! -f "$XINITRC" ]]; then
    info "Writing $XINITRC …"
    cat > "$XINITRC" <<EOF
#!/bin/bash
exec /opt/hanryxvault/kiosk/start-monitor.sh
EOF
    chown "${KIOSK_USER}:${KIOSK_USER}" "$XINITRC"
    chmod +x "$XINITRC"
fi

# ── Install + enable the systemd service ─────────────────────────────────────
info "Installing systemd service: $SERVICE_NAME"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

# ── Ownership ─────────────────────────────────────────────────────────────────
chown -R "${KIOSK_USER}:${KIOSK_USER}" "$KIOSK_DEST" || true

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  HanryxVault Kiosk installed successfully${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  What happens at boot:"
echo "    1. Pi logs in to the console as '${KIOSK_USER}' automatically"
echo "    2. X11 starts on virtual terminal 7 (no desktop environment)"
echo "    3. The script waits up to 2 min for the POS server to be healthy"
echo "    4. The admin monitor opens full-screen on the connected display"
echo ""
echo "  Keyboard shortcuts (when the monitor is running):"
echo "    F11           — exit kiosk / restart service"
echo "    Ctrl+Alt+Q    — exit kiosk / restart service"
echo ""
echo "  To start immediately (without rebooting):"
echo "    sudo systemctl start $SERVICE_NAME"
echo ""
echo "  To check status / logs:"
echo "    sudo systemctl status $SERVICE_NAME"
echo "    sudo journalctl -u $SERVICE_NAME -f"
echo ""
echo "  To uninstall:"
echo "    sudo systemctl disable --now $SERVICE_NAME"
echo ""
echo -e "${YELLOW}  Reboot the Pi to activate the kiosk:${NC}"
echo "    sudo reboot"
echo ""
