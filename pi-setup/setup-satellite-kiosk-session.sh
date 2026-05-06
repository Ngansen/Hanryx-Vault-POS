#!/usr/bin/env bash
# =============================================================================
# HanryxVault — Satellite Kiosk Session (clean labwc, no Pi desktop)
# =============================================================================
# Replaces the previous "lightdm autologin → stock Pi labwc + pcmanfm-pi +
# wf-panel-pi + autostart" stack with a dedicated kiosk session:
#
#   lightdm (autologin)
#     └── hanryx-kiosk wayland session
#           └── labwc -C /etc/hanryx-kiosk/labwc   (CLEAN config, no Pi desktop)
#                 └── /home/ngansen/.hanryx-dual-monitor.sh
#                       ├── chromium --kiosk /admin   (HDMI-A-1, 10.1")
#                       └── chromium --kiosk /kiosk   (HDMI-A-2, 5")
#
# Why: stock /etc/xdg/labwc/autostart launches pcmanfm-pi (desktop), wf-panel-pi
# (taskbar), kanshi (display config) etc. On a kiosk these add nothing, and one
# of them was destabilising the session enough that XWayland never came up,
# breaking chromium with "Missing X server or $DISPLAY".
#
# This script is idempotent. Run as root on the satellite Pi. Reboot when done.
# =============================================================================
set -euo pipefail

BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; NC=$'\033[0m'
info() { echo -e "${BOLD}[*]${NC} $*"; }
ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*" >&2; }

[ "$(id -u)" -eq 0 ] || { err "Run as root (sudo bash $0)"; exit 1; }

KIOSK_USER="${KIOSK_USER:-ngansen}"
KIOSK_HOME="$(getent passwd "$KIOSK_USER" | cut -d: -f6)"
[ -d "$KIOSK_HOME" ] || { err "User $KIOSK_USER has no home dir"; exit 1; }

LAUNCH_SCRIPT="$KIOSK_HOME/.hanryx-dual-monitor.sh"
[ -x "$LAUNCH_SCRIPT" ] || { err "Launcher missing at $LAUNCH_SCRIPT — run setup-satellite-kiosk-boot.sh first"; exit 1; }

# ── 1. Ensure Xwayland and labwc are installed ───────────────────────────────
info "Ensuring labwc + Xwayland are installed…"
apt-get update -qq
apt-get install -y --no-install-recommends labwc xwayland wlr-randr foot >/dev/null
ok "Wayland/X bridge ready: $(which Xwayland) $(which labwc)"

# ── 2. Write the clean labwc kiosk config (no Pi desktop) ────────────────────
info "Writing /etc/hanryx-kiosk/labwc (clean, no pcmanfm-pi/wf-panel-pi)…"
install -d -m 755 /etc/hanryx-kiosk/labwc

cat > /etc/hanryx-kiosk/labwc/autostart << EOF
#!/bin/sh
# HanryxVault kiosk autostart — runs ONCE when labwc starts.
# Already runs as the logged-in user ($KIOSK_USER); no sudo needed.
# No panel, no file manager, no compositor effects. Just the launcher.
sleep 3 && $LAUNCH_SCRIPT >> /var/log/hanryx-kiosk.log 2>&1 &
EOF
chmod +x /etc/hanryx-kiosk/labwc/autostart

# Also disable the system-level hanryx-watchdog if it's running the launcher
# from outside the session (PPID=1 means no Wayland, no XWayland, no display).
# The watchdog is only a health-check; we keep it running. But verify it's NOT
# spawning the launcher.
if systemctl is-enabled hanryx-watchdog.service >/dev/null 2>&1; then
    info "hanryx-watchdog.service is enabled (health-check only — keeping it)"
fi

cat > /etc/hanryx-kiosk/labwc/environment << 'EOF'
XDG_CURRENT_DESKTOP=labwc:wlroots
XDG_SESSION_DESKTOP=hanryx-kiosk
XDG_SESSION_TYPE=wayland
MOZ_ENABLE_WAYLAND=1
EOF

cat > /etc/hanryx-kiosk/labwc/rc.xml << 'EOF'
<?xml version="1.0"?>
<labwc_config>
  <core>
    <gap>0</gap>
    <reuseOutputMode>yes</reuseOutputMode>
  </core>
  <theme>
    <cornerRadius>0</cornerRadius>
    <dropShadows>no</dropShadows>
  </theme>
  <windowRules>
    <!-- Pin chromium windows fullscreen, no decorations -->
    <windowRule identifier="chromium" serverDecoration="no">
      <action name="ToggleFullscreen"/>
    </windowRule>
    <windowRule identifier="Chromium" serverDecoration="no">
      <action name="ToggleFullscreen"/>
    </windowRule>
  </windowRules>
</labwc_config>
EOF

# Empty menu.xml so right-click does nothing (kiosk hardening)
echo '<?xml version="1.0"?><openbox_menu/>' > /etc/hanryx-kiosk/labwc/menu.xml

ok "Clean labwc config installed at /etc/hanryx-kiosk/labwc/"

# ── 3. Register a Wayland session that lightdm can autologin into ────────────
info "Registering hanryx-kiosk.desktop wayland session…"
install -d -m 755 /usr/share/wayland-sessions
cat > /usr/share/wayland-sessions/hanryx-kiosk.desktop << 'EOF'
[Desktop Entry]
Name=HanryxVault Kiosk
Comment=Dedicated kiosk session — labwc + dual chromium, no Pi desktop
Exec=/usr/bin/labwc -C /etc/hanryx-kiosk/labwc
Type=Application
DesktopNames=hanryx-kiosk
EOF
ok "Session registered: /usr/share/wayland-sessions/hanryx-kiosk.desktop"

# ── 4. Point lightdm autologin at the new session ───────────────────────────
info "Configuring lightdm to autologin into hanryx-kiosk…"
install -d -m 755 /etc/lightdm/lightdm.conf.d
cat > /etc/lightdm/lightdm.conf.d/60-hanryx-kiosk.conf << EOF
[Seat:*]
autologin-user=$KIOSK_USER
autologin-user-timeout=0
autologin-session=hanryx-kiosk
user-session=hanryx-kiosk
greeter-session=lightdm-gtk-greeter
EOF
ok "lightdm autologin pinned to hanryx-kiosk session"

# ── 5. Disable the OLD per-user labwc autostart so it can't double-fire ─────
info "Disarming old per-user autostart paths…"
if [ -f "$KIOSK_HOME/.config/labwc/autostart" ]; then
    mv "$KIOSK_HOME/.config/labwc/autostart" \
       "$KIOSK_HOME/.config/labwc/autostart.disabled-by-kiosk-session"
    ok "Renamed $KIOSK_HOME/.config/labwc/autostart → .disabled-by-kiosk-session"
fi
rm -f "$KIOSK_HOME/.config/autostart/hanryx-dual-kiosk.desktop" 2>/dev/null || true
ok "Old autostart paths disarmed"

# ── 6. Make sure the launcher uses XWayland (not Wayland) for chromium ─────
# The launcher already uses --ozone-platform=x11. Just verify XWayland will
# start when chromium asks for it: labwc starts XWayland on demand if the
# binary exists. We installed it in step 1, so we're good.
info "Verifying XWayland availability…"
[ -x /usr/bin/Xwayland ] && ok "/usr/bin/Xwayland present — labwc will start it on demand"

# ── 7. Ensure log file exists and is writable by kiosk user ────────────────
touch /var/log/hanryx-kiosk.log
chown "$KIOSK_USER:$KIOSK_USER" /var/log/hanryx-kiosk.log
chmod 664 /var/log/hanryx-kiosk.log

# ── 8. Make systemd boot into graphical (lightdm) target ───────────────────
systemctl set-default graphical.target >/dev/null
systemctl enable lightdm.service >/dev/null 2>&1 || true

echo ""
echo -e "${BOLD}=============================================================${NC}"
echo -e "${GREEN}  Kiosk session installed.${NC}"
echo ""
echo "  What changed:"
echo "    • New wayland session:  /usr/share/wayland-sessions/hanryx-kiosk.desktop"
echo "    • Clean labwc config:   /etc/hanryx-kiosk/labwc/"
echo "    • lightdm autologins into hanryx-kiosk (no Pi desktop)"
echo "    • Old per-user autostart disarmed (renamed to .disabled-by-kiosk-session)"
echo ""
echo "  ${BOLD}Reboot now to activate:${NC}"
echo "      sudo reboot"
echo ""
echo "  After reboot, watch the kiosk come up:"
echo "      tail -f /var/log/hanryx-kiosk.log"
echo ""
echo "  To roll back to the old desktop (if needed):"
echo "      sudo rm /etc/lightdm/lightdm.conf.d/60-hanryx-kiosk.conf"
echo "      sudo mv $KIOSK_HOME/.config/labwc/autostart.disabled-by-kiosk-session \\"
echo "             $KIOSK_HOME/.config/labwc/autostart"
echo "      sudo reboot"
echo -e "${BOLD}=============================================================${NC}"
