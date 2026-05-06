#!/usr/bin/env bash
# =============================================================================
# HanryxVault — Satellite Kiosk via pure systemd (no lightdm, no greeter)
# =============================================================================
# Replaces the lightdm + autologin + wayland-session approach with a single
# systemd service that runs labwc directly on tty1 as the kiosk user.
#
#   systemd boot
#     └── hanryx-kiosk.service     (Type=simple, User=ngansen, tty1)
#           └── labwc -C /etc/hanryx-kiosk/labwc
#                 └── /etc/hanryx-kiosk/labwc/autostart
#                       └── /home/ngansen/.hanryx-dual-monitor.sh
#                             ├── chromium --kiosk /admin   (10.1")
#                             └── chromium --kiosk /kiosk   (5")
#
# Why: lightdm + greeter has too many failure modes for an unattended kiosk
# (greeter timeout, X/Wayland session race, autologin not firing). A single
# systemd unit removes all of that. One service, one log, no greeter.
#
# Idempotent. Run as root on the satellite Pi only. Reboot when done.
# =============================================================================
set -euo pipefail

BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; NC=$'\033[0m'
info() { echo -e "${BOLD}[*]${NC} $*"; }
ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*" >&2; }

[ "$(id -u)" -eq 0 ] || { err "Run as root (sudo bash $0)"; exit 1; }

# ── Hostname guard — SATELLITE ONLY ──────────────────────────────────────────
# Main Pi (hanryxvault) runs only the diagnostic Grafana screen. Running this
# there would disable lightdm and replace its desktop with a kiosk service,
# killing the dashboard.
EXPECTED_HOST="hanryxvault-sat"
ACTUAL_HOST="$(hostname)"
if [ "$ACTUAL_HOST" != "$EXPECTED_HOST" ]; then
    err "ABORT: this script is for the SATELLITE Pi only (expected hostname '$EXPECTED_HOST')."
    err "       Current hostname is '$ACTUAL_HOST'."
    err ""
    err "       If you really mean to run it here: SKIP_HOSTNAME_GUARD=1 sudo bash $0"
    [ "${SKIP_HOSTNAME_GUARD:-0}" = "1" ] || exit 1
    warn "SKIP_HOSTNAME_GUARD=1 set — proceeding anyway."
fi

KIOSK_USER="${KIOSK_USER:-ngansen}"
KIOSK_HOME="$(getent passwd "$KIOSK_USER" | cut -d: -f6)"
KIOSK_UID="$(id -u "$KIOSK_USER")"
[ -d "$KIOSK_HOME" ] || { err "User $KIOSK_USER has no home dir"; exit 1; }

LAUNCH_SCRIPT="$KIOSK_HOME/.hanryx-dual-monitor.sh"
[ -x "$LAUNCH_SCRIPT" ] || { err "Launcher missing at $LAUNCH_SCRIPT — run setup-satellite-kiosk-boot.sh first"; exit 1; }

# ── 1. Ensure required packages are installed ────────────────────────────────
info "Ensuring labwc + Xwayland + wlr-randr are installed…"
apt-get update -qq
apt-get install -y --no-install-recommends labwc xwayland wlr-randr foot seatd >/dev/null
ok "Wayland stack ready: $(command -v labwc) $(command -v Xwayland)"

# ── 2. Add kiosk user to required groups (seat/input/video/render) ──────────
info "Adding $KIOSK_USER to seat/input/video/render groups…"
for g in video render input seat tty; do
    if getent group "$g" >/dev/null 2>&1; then
        gpasswd -a "$KIOSK_USER" "$g" >/dev/null 2>&1 || true
    fi
done
ok "Group membership updated"

# Enable seatd so labwc can claim the seat without a logind session from a greeter
systemctl enable --now seatd.service >/dev/null 2>&1 || true

# ── 3. Write clean labwc kiosk config (no Pi desktop) ────────────────────────
info "Writing /etc/hanryx-kiosk/labwc (clean config)…"
install -d -m 755 /etc/hanryx-kiosk/labwc

cat > /etc/hanryx-kiosk/labwc/autostart << EOF
#!/bin/sh
# HanryxVault kiosk autostart — runs once when labwc starts, as $KIOSK_USER.
# No panel, no file manager. Just the dual-monitor launcher.
sleep 3 && $LAUNCH_SCRIPT >> /var/log/hanryx-kiosk.log 2>&1 &
EOF
chmod +x /etc/hanryx-kiosk/labwc/autostart

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
    <windowRule identifier="chromium" serverDecoration="no">
      <action name="ToggleFullscreen"/>
    </windowRule>
    <windowRule identifier="Chromium" serverDecoration="no">
      <action name="ToggleFullscreen"/>
    </windowRule>
  </windowRules>
</labwc_config>
EOF

echo '<?xml version="1.0"?><openbox_menu/>' > /etc/hanryx-kiosk/labwc/menu.xml
ok "Clean labwc config installed at /etc/hanryx-kiosk/labwc/"

# ── 4. Ensure /var/log/hanryx-kiosk.log exists and is writable ──────────────
touch /var/log/hanryx-kiosk.log
chown "$KIOSK_USER:$KIOSK_USER" /var/log/hanryx-kiosk.log
chmod 664 /var/log/hanryx-kiosk.log

# ── 5. Wrapper that exports the runtime env labwc needs on tty1 ─────────────
info "Writing /usr/local/bin/hanryx-kiosk-launch wrapper…"
cat > /usr/local/bin/hanryx-kiosk-launch << EOF
#!/bin/sh
# Launched by hanryx-kiosk.service as $KIOSK_USER. Sets the runtime env labwc
# needs when started outside a logind session (no greeter).
set -e

export HOME="$KIOSK_HOME"
export USER="$KIOSK_USER"
export LOGNAME="$KIOSK_USER"
export SHELL="/bin/bash"

# logind/seatd-friendly runtime dir (XDG_RUNTIME_DIR must exist + be 0700)
export XDG_RUNTIME_DIR="/run/user/$KIOSK_UID"
if [ ! -d "\$XDG_RUNTIME_DIR" ]; then
    mkdir -p "\$XDG_RUNTIME_DIR"
    chown "$KIOSK_USER:$KIOSK_USER" "\$XDG_RUNTIME_DIR"
    chmod 0700 "\$XDG_RUNTIME_DIR"
fi

# Kiosk-session env (also re-applied by /etc/hanryx-kiosk/labwc/environment)
export XDG_CURRENT_DESKTOP=labwc:wlroots
export XDG_SESSION_DESKTOP=hanryx-kiosk
export XDG_SESSION_TYPE=wayland
export MOZ_ENABLE_WAYLAND=1

cd "\$HOME"
exec /usr/bin/labwc -C /etc/hanryx-kiosk/labwc
EOF
chmod +x /usr/local/bin/hanryx-kiosk-launch
ok "Launcher wrapper installed"

# ── 6. Write the systemd service ────────────────────────────────────────────
info "Writing /etc/systemd/system/hanryx-kiosk.service…"
cat > /etc/systemd/system/hanryx-kiosk.service << EOF
[Unit]
Description=HanryxVault Satellite Kiosk (labwc on tty1)
Documentation=https://github.com/Ngansen/Hanryx-Vault-POS
# Wait for the seat (graphics + input) and the network to be ready.
After=systemd-user-sessions.service seatd.service network-online.target
Wants=seatd.service network-online.target
# Own the graphical seat.
Conflicts=getty@tty1.service lightdm.service
After=getty@tty1.service

[Service]
Type=simple
User=$KIOSK_USER
Group=$KIOSK_USER
PAMName=login
WorkingDirectory=$KIOSK_HOME

# Bind to tty1 so labwc/wlroots can grab the KMS device.
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
TTYVTDisallocate=yes
StandardInput=tty
StandardOutput=journal
StandardError=journal
UtmpIdentifier=tty1
UtmpMode=user

ExecStart=/usr/local/bin/hanryx-kiosk-launch

# If labwc dies, restart — but back off so we don't hammer.
Restart=always
RestartSec=5s
# Don't give up after N failures (kiosk must always come back).
StartLimitIntervalSec=0

[Install]
WantedBy=graphical.target
EOF
ok "Systemd unit written"

# ── 7. Disable lightdm — it would race for tty1 / the seat ──────────────────
info "Disabling lightdm.service (kiosk owns the seat now)…"
if systemctl is-enabled lightdm.service >/dev/null 2>&1; then
    systemctl disable lightdm.service >/dev/null 2>&1 || true
    ok "lightdm.service disabled"
else
    ok "lightdm.service was already disabled"
fi
systemctl stop lightdm.service >/dev/null 2>&1 || true

# Remove the previous lightdm-autologin drop-in if present (from old approach).
rm -f /etc/lightdm/lightdm.conf.d/60-hanryx-kiosk.conf 2>/dev/null || true

# Also disable the getty on tty1 — our service owns it.
systemctl disable getty@tty1.service >/dev/null 2>&1 || true
systemctl stop    getty@tty1.service >/dev/null 2>&1 || true

# ── 8. Enable + set graphical target ────────────────────────────────────────
info "Enabling hanryx-kiosk.service and setting graphical.target as default…"
systemctl daemon-reload
systemctl enable hanryx-kiosk.service >/dev/null
systemctl set-default graphical.target >/dev/null
ok "hanryx-kiosk.service enabled (graphical.target default)"

echo ""
echo -e "${BOLD}=============================================================${NC}"
echo -e "${GREEN}  Satellite kiosk (systemd) installed.${NC}"
echo ""
echo "  What it does at boot:"
echo "    1. systemd reaches graphical.target"
echo "    2. hanryx-kiosk.service starts as $KIOSK_USER on /dev/tty1"
echo "    3. labwc launches with /etc/hanryx-kiosk/labwc"
echo "    4. autostart fires $LAUNCH_SCRIPT"
echo "    5. dual chromium kiosks land on the two HDMI outputs"
echo ""
echo "  ${BOLD}Reboot to activate:${NC}"
echo "      sudo reboot"
echo ""
echo "  Logs:"
echo "      journalctl -u hanryx-kiosk.service -f         # service"
echo "      tail -f /var/log/hanryx-kiosk.log             # launcher + chromium"
echo ""
echo "  Manual control (no reboot needed):"
echo "      sudo systemctl restart hanryx-kiosk.service"
echo "      sudo systemctl stop    hanryx-kiosk.service   # drops to console"
echo ""
echo "  Roll back to lightdm:"
echo "      sudo systemctl disable --now hanryx-kiosk.service"
echo "      sudo systemctl enable  --now lightdm.service"
echo "      sudo systemctl enable  getty@tty1.service"
echo -e "${BOLD}=============================================================${NC}"
