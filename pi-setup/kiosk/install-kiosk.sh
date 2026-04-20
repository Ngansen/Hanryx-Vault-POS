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
# Auto-detect kiosk user: explicit env > sudo invoker > 'pi' > first /home/* dir
if [[ -z "${KIOSK_USER:-}" ]]; then
    if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
        KIOSK_USER="$SUDO_USER"
    elif id pi &>/dev/null && [[ -d /home/pi ]]; then
        KIOSK_USER="pi"
    else
        KIOSK_USER=$(ls /home 2>/dev/null | head -1)
    fi
fi
[[ -z "$KIOSK_USER" ]] && { echo "Could not determine kiosk user — set KIOSK_USER=<name>"; exit 1; }
id "$KIOSK_USER" &>/dev/null || { echo "User '$KIOSK_USER' does not exist"; exit 1; }
[[ -d "/home/$KIOSK_USER" ]] || { echo "Home dir /home/$KIOSK_USER missing"; exit 1; }

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[kiosk]${NC} $*"; }
warn()    { echo -e "${YELLOW}[kiosk]${NC} $*"; }
die()     { echo -e "${RED}[kiosk] ERROR:${NC} $*" >&2; exit 1; }

# ── Root check ───────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Run this script with sudo: sudo bash $0"

# ── OS package dependencies ───────────────────────────────────────────────────
info "Kiosk user: $KIOSK_USER  (home: /home/$KIOSK_USER)"
info "Installing OS packages …"
apt-get update -qq
apt-get install -y --no-install-recommends \
    xserver-xorg \
    xinit \
    x11-xserver-utils \
    x11-utils \
    unclutter \
    xbindkeys \
    curl \
    yt-dlp \
    ffmpeg \
    fonts-noto-color-emoji \
    fonts-dejavu-core

# Chromium: package name differs across Debian/Raspbian versions.
# Try chromium-browser first (older RPi OS), then chromium (Debian 13 / current).
if ! command -v chromium-browser &>/dev/null && ! command -v chromium &>/dev/null; then
    info "Installing Chromium …"
    apt-get install -y --no-install-recommends chromium-browser 2>/dev/null \
        || apt-get install -y --no-install-recommends chromium \
        || warn "Could not install Chromium — kiosk will fail to launch the browser"
fi

# ── Create kiosk directory ────────────────────────────────────────────────────
info "Copying kiosk files to $KIOSK_DEST …"
mkdir -p "$KIOSK_DEST"

cp "$KIOSK_SRC/start-monitor.sh"         "$KIOSK_DEST/start-monitor.sh"
cp "$KIOSK_SRC/hanryxvault-kiosk.service" "$SERVICE_FILE"
chmod +x "$KIOSK_DEST/start-monitor.sh"

# F9 standby toggle helper
if [[ -f "$KIOSK_SRC/standby-toggle.sh" ]]; then
    cp "$KIOSK_SRC/standby-toggle.sh" "$KIOSK_DEST/standby-toggle.sh"
    chmod +x "$KIOSK_DEST/standby-toggle.sh"
fi

# Idle-screen video playlist downloader
if [[ -f "$KIOSK_SRC/download-playlist.sh" ]]; then
    cp "$KIOSK_SRC/download-playlist.sh" "$KIOSK_DEST/download-playlist.sh"
    chmod +x "$KIOSK_DEST/download-playlist.sh"
    mkdir -p "$KIOSK_DEST/videos"
fi

# Substitute the kiosk user/home into the service template (template ships with
# User=pi / /home/pi as defaults; rewrite to whatever KIOSK_USER we picked).
if [[ "$KIOSK_USER" != "pi" ]]; then
    info "Rewriting service file: User=pi → User=${KIOSK_USER}"
    sed -i "s|^User=.*|User=${KIOSK_USER}|"   "$SERVICE_FILE"
    sed -i "s|^Group=.*|Group=${KIOSK_USER}|" "$SERVICE_FILE"
    sed -i "s|/home/pi|/home/${KIOSK_USER}|g" "$SERVICE_FILE"
fi

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

# ── Ensure xserver-xorg-legacy is installed (provides setuid Xorg wrapper) ──
# Debian 13 ships /usr/bin/Xorg as a non-setuid 274-byte wrapper by default,
# which means even with needs_root_rights=yes, X cannot grab a VT and dies
# with "xf86OpenConsole: Cannot open virtual console N (Permission denied)".
if ! dpkg -s xserver-xorg-legacy &>/dev/null; then
    info "Installing xserver-xorg-legacy (provides setuid Xorg wrapper) …"
    DEBIAN_FRONTEND=noninteractive apt-get install -y xserver-xorg-legacy || \
        warn "Failed to install xserver-xorg-legacy — kiosk may not launch X"
fi
# Force setuid bit on both the wrapper and the real binary, since apt
# sometimes doesn't set it (depends on Xwrapper.config priority).
for xbin in /usr/bin/Xorg /usr/lib/xorg/Xorg; do
    if [[ -f "$xbin" ]] && [[ ! -u "$xbin" ]]; then
        chmod u+s "$xbin" && info "Set setuid bit on $xbin"
    fi
done

# ── Allow non-console users to start X (needed for systemd-launched xinit) ──
# Debian default: /etc/X11/Xwrapper.config restricts X to console users, which
# kills xinit when launched by a systemd Service=. Allow anybody, request root.
XWRAP=/etc/X11/Xwrapper.config
mkdir -p /etc/X11
if [[ -f "$XWRAP" ]] \
   && grep -qE '^allowed_users\s*=\s*anybody'      "$XWRAP" \
   && grep -qE '^needs_root_rights\s*=\s*yes'      "$XWRAP"; then
    info "Xwrapper.config already permissive."
else
    info "Patching $XWRAP (allowed_users=anybody, needs_root_rights=yes)"
    cat > "$XWRAP" <<'EOF'
allowed_users=anybody
needs_root_rights=yes
EOF
fi
# Make sure the kiosk user can open input + tty devices
for grp in tty video input audio; do
    if getent group "$grp" >/dev/null && ! id -nG "$KIOSK_USER" | grep -qw "$grp"; then
        usermod -a -G "$grp" "$KIOSK_USER" && info "Added $KIOSK_USER to group $grp"
    fi
done

# ── Install + enable the systemd service ─────────────────────────────────────
info "Installing systemd service: $SERVICE_NAME"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

# ── Install nightly restart timer (clears chromium memory leaks) ─────────────
RESTART_SVC=/etc/systemd/system/${SERVICE_NAME}-restart.service
RESTART_TMR=/etc/systemd/system/${SERVICE_NAME}-restart.timer
if [[ -f "$KIOSK_SRC/${SERVICE_NAME}-restart.service" && -f "$KIOSK_SRC/${SERVICE_NAME}-restart.timer" ]]; then
    info "Installing nightly restart timer (04:30 local) …"
    cp "$KIOSK_SRC/${SERVICE_NAME}-restart.service" "$RESTART_SVC"
    cp "$KIOSK_SRC/${SERVICE_NAME}-restart.timer"   "$RESTART_TMR"
    systemctl daemon-reload
    systemctl enable --now "${SERVICE_NAME}-restart.timer"
    info "Next nightly restart: $(systemctl list-timers ${SERVICE_NAME}-restart.timer --no-pager 2>/dev/null | awk 'NR==2{print $1,$2,$3}')"
else
    warn "Nightly restart timer files not found in $KIOSK_SRC — skipping."
fi

# ── Install weekly playlist refresh timer ────────────────────────────────────
PLAYLIST_SVC=/etc/systemd/system/${SERVICE_NAME}-videos.service
PLAYLIST_TMR=/etc/systemd/system/${SERVICE_NAME}-videos.timer
if [[ -f "$KIOSK_SRC/${SERVICE_NAME}-videos.service" && -f "$KIOSK_SRC/${SERVICE_NAME}-videos.timer" ]]; then
    info "Installing weekly playlist refresh timer (Sun 03:15) …"
    cp "$KIOSK_SRC/${SERVICE_NAME}-videos.service" "$PLAYLIST_SVC"
    cp "$KIOSK_SRC/${SERVICE_NAME}-videos.timer"   "$PLAYLIST_TMR"
    if [[ "$KIOSK_USER" != "ngansen" ]]; then
        sed -i "s|^User=.*|User=${KIOSK_USER}|"   "$PLAYLIST_SVC"
        sed -i "s|^Group=.*|Group=${KIOSK_USER}|" "$PLAYLIST_SVC"
    fi
    systemctl daemon-reload
    systemctl enable --now "${SERVICE_NAME}-videos.timer"
fi

# ── /etc/default/hanryxvault-kiosk — playlist URL + monitor layout ───────────
DEFAULTS=/etc/default/hanryxvault-kiosk
if [[ ! -f "$DEFAULTS" ]]; then
    info "Writing default config to $DEFAULTS"
    cat > "$DEFAULTS" <<EOF
# HanryxVault Kiosk — environment defaults (sourced by start-monitor.sh and
# download-playlist.sh).  Edit then 'sudo systemctl restart hanryxvault-kiosk'.

# Idle-screen YouTube playlist (downloaded weekly to ${KIOSK_DEST}/videos/)
KIOSK_PLAYLIST_URL="https://www.youtube.com/playlist?list=PLo60BvbiWBuqUwSRFou3pbPV2IAWaP0rg"
KIOSK_VIDEOS_DIR="${KIOSK_DEST}/videos"
KIOSK_VIDEO_HEIGHT=480
KIOSK_VIDEO_MAX_COUNT=40
EOF
elif ! grep -q '^KIOSK_PLAYLIST_URL=' "$DEFAULTS"; then
    info "Adding KIOSK_PLAYLIST_URL to $DEFAULTS"
    cat >> "$DEFAULTS" <<EOF

# Idle-screen YouTube playlist (downloaded weekly to ${KIOSK_DEST}/videos/)
KIOSK_PLAYLIST_URL="https://www.youtube.com/playlist?list=PLo60BvbiWBuqUwSRFou3pbPV2IAWaP0rg"
KIOSK_VIDEOS_DIR="${KIOSK_DEST}/videos"
KIOSK_VIDEO_HEIGHT=480
KIOSK_VIDEO_MAX_COUNT=40
EOF
fi

# Allow the kiosk user to write into the videos dir
chown -R "${KIOSK_USER}:${KIOSK_USER}" "$KIOSK_DEST/videos" 2>/dev/null || true

# Kick off the first download in the background so the kiosk has content soon.
if command -v yt-dlp &>/dev/null && [[ -x "$KIOSK_DEST/download-playlist.sh" ]]; then
    info "Starting initial playlist download in background (this may take a while) …"
    systemctl start "${SERVICE_NAME}-videos.service" --no-block || true
fi

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
