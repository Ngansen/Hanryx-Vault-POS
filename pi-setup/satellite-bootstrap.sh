#!/usr/bin/env bash
# =============================================================================
# satellite-bootstrap.sh — ONE-SHOT post-reflash setup for the satellite Pi
#
# Run this once on a freshly flashed Pi OS Bookworm satellite Pi after
# completing first-boot setup (user 'ngansen' created, WiFi joined).
#
# What it does, in order:
#   1.  apt update + install required packages
#   2.  Set hostname to hanryxvault-sat (avoids collision with main Pi)
#   3.  Install Tailscale and prompt for auth
#   4.  Run the dual-monitor kiosk installer (setup-satellite-kiosk-boot.sh)
#   5.  Run the reliability stack (install-reliability.sh)
#   6.  Configure ~/.hanryx/satellite.conf with sensible defaults
#   7.  Offer to reboot
#
# Idempotent — safe to re-run if a step fails.
# All progress logged to /var/log/hanryx-bootstrap.log
# =============================================================================
set -u

LOG=/var/log/hanryx-bootstrap.log
exec > >(tee -a "$LOG") 2>&1

C='\033[0;36m'; G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[1m'; N='\033[0m'
step()  { echo ""; echo -e "${B}${C}══ $1 ══${N}"; }
ok()    { echo -e "${G}[✓]${N} $1"; }
info()  { echo -e "${C}[i]${N} $1"; }
warn()  { echo -e "${Y}[!]${N} $1"; }
bad()   { echo -e "${R}[✗]${N} $1"; }
die()   { bad "$1"; exit 1; }

[ "$EUID" -ne 0 ] && die "Run with sudo: sudo bash $0"

CURRENT_USER="${SUDO_USER:-ngansen}"
HOME_DIR=$(getent passwd "$CURRENT_USER" | cut -d: -f6)
REPO_DIR="${REPO_DIR:-$HOME_DIR/Hanryx-Vault-POS}"
MAIN_PI_TS_HOST="${MAIN_PI_TS_HOST:-hanryxvault}"
NEW_HOSTNAME="${NEW_HOSTNAME:-hanryxvault-sat}"

echo ""
echo -e "${B}  HanryxVault — Satellite Pi One-Shot Bootstrap${N}"
echo "  $(date -Is)"
echo "  user=$CURRENT_USER  home=$HOME_DIR  repo=$REPO_DIR"
echo "  target hostname=$NEW_HOSTNAME  main pi=$MAIN_PI_TS_HOST"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
step "1. System packages"
apt-get update -qq
apt-get install -y --no-install-recommends \
    git curl jq zstd wlr-randr unclutter \
    chromium-browser openssh-server \
    avahi-daemon avahi-utils \
    nginx \
    python3-pip python3-venv \
    || die "apt-get install failed"
ok "Required packages present"

# ─────────────────────────────────────────────────────────────────────────────
step "2. Hostname → $NEW_HOSTNAME"
CURRENT_HN=$(hostname)
if [ "$CURRENT_HN" != "$NEW_HOSTNAME" ]; then
    echo "$NEW_HOSTNAME" > /etc/hostname
    hostnamectl set-hostname "$NEW_HOSTNAME"
    sed -i '/127\.0\.1\.1/d' /etc/hosts
    echo "127.0.1.1  $NEW_HOSTNAME ${NEW_HOSTNAME}.local" >> /etc/hosts
    systemctl restart avahi-daemon 2>/dev/null || true
    ok "Hostname changed: $CURRENT_HN → $NEW_HOSTNAME"
else
    ok "Hostname already $NEW_HOSTNAME"
fi

# ─────────────────────────────────────────────────────────────────────────────
step "3. Tailscale install + auth"
if ! command -v tailscale >/dev/null 2>&1; then
    info "Installing Tailscale…"
    curl -fsSL https://tailscale.com/install.sh | sh
fi
if ! tailscale status >/dev/null 2>&1; then
    info "Starting Tailscale (you will be prompted to visit a URL to authenticate)…"
    tailscale up --hostname="$NEW_HOSTNAME" --ssh || warn "tailscale up failed — re-run manually after bootstrap"
else
    info "Updating Tailscale hostname to $NEW_HOSTNAME…"
    tailscale set --hostname="$NEW_HOSTNAME" 2>/dev/null \
        || tailscale up --reset --hostname="$NEW_HOSTNAME" --ssh 2>/dev/null \
        || warn "Tailscale rename failed — run manually: sudo tailscale up --reset --hostname=$NEW_HOSTNAME --ssh"
fi
TS_IP=$(tailscale ip -4 2>/dev/null | head -1 || echo "(not connected)")
ok "Tailscale: $TS_IP  ($NEW_HOSTNAME)"

# ─────────────────────────────────────────────────────────────────────────────
step "4. Repository clone / update"
if [ ! -d "$REPO_DIR/.git" ]; then
    if [ -n "${GITHUB_TOKEN:-}" ]; then
        info "Cloning private repo with GITHUB_TOKEN…"
        sudo -u "$CURRENT_USER" git clone \
            "https://${GITHUB_TOKEN}@github.com/Ngansen/Hanryx-Vault-POS.git" \
            "$REPO_DIR" || die "git clone failed"
    else
        die "GITHUB_TOKEN env var not set and $REPO_DIR doesn't exist. Re-run with: sudo GITHUB_TOKEN=... bash $0"
    fi
else
    info "Repo present — pulling latest…"
    sudo -u "$CURRENT_USER" git -C "$REPO_DIR" pull --ff-only \
        || warn "git pull failed — continuing with whatever is checked out"
fi
ok "Repo at $REPO_DIR"

# ─────────────────────────────────────────────────────────────────────────────
step "5. Dual-monitor kiosk installer"
KIOSK_INSTALL="$REPO_DIR/pi-setup/setup-satellite-kiosk-boot.sh"
if [ -f "$KIOSK_INSTALL" ]; then
    # The installer is interactive (asks Tailscale y/n etc) — feed defaults
    # via env vars and pipe a series of "y\n" answers to the prompts.
    USE_TAILSCALE=y MAIN_PI_TS_HOST="$MAIN_PI_TS_HOST" \
        bash "$KIOSK_INSTALL" </dev/null \
        || warn "kiosk installer reported errors — review $LOG"
    ok "Kiosk installer complete"
else
    warn "Kiosk installer not found at $KIOSK_INSTALL — skipping"
fi

# ─────────────────────────────────────────────────────────────────────────────
step "6. Reliability stack (heal timer + recover.sh)"
RELIABILITY="$REPO_DIR/pi-setup/install-reliability.sh"
if [ -f "$RELIABILITY" ]; then
    REPO_DIR="$REPO_DIR" bash "$RELIABILITY" \
        || warn "reliability installer had warnings — review $LOG"
    ok "Reliability stack installed"
else
    warn "install-reliability.sh not found — skipping"
fi

# ─────────────────────────────────────────────────────────────────────────────
step "7. Satellite config defaults"
CONFIG_DIR="$HOME_DIR/.hanryx"
CONFIG_FILE="$CONFIG_DIR/satellite.conf"
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_FILE" ]; then
    cat > "$CONFIG_FILE" << EOF
# HanryxVault satellite configuration — created by satellite-bootstrap.sh
# Edit and reboot to take effect, or restart the launcher.

MAIN_PI_TS_HOST=$MAIN_PI_TS_HOST
ADMIN_URL=http://\${MAIN_PI_TS_HOST}:8080/admin
KIOSK_URL=http://\${MAIN_PI_TS_HOST}:8080/kiosk
HEALTH_URL=http://\${MAIN_PI_TS_HOST}:8080/health
USE_TAILSCALE=y

# Pin which HDMI output shows admin (10.1") vs kiosk (5").
# Run after first boot to set these:  bash pi-setup/satellite-screens.sh
# ADMIN_OUTPUT=HDMI-A-1
# KIOSK_OUTPUT=HDMI-A-2

# Set to 'y' to swap if you can't easily switch HDMI cables
SWAP_SCREENS=n
EOF
    chown -R "$CURRENT_USER:$CURRENT_USER" "$CONFIG_DIR"
    ok "Created $CONFIG_FILE"
else
    info "Config exists — leaving alone: $CONFIG_FILE"
fi

# ─────────────────────────────────────────────────────────────────────────────
step "8. Final verification"
echo ""
echo "  Hostname:        $(hostname)"
echo "  mDNS:            $(hostname).local"
echo "  Tailscale IP:    $(tailscale ip -4 2>/dev/null | head -1 || echo 'not connected')"
echo "  Tailscale name:  $(tailscale status --self --peers=false 2>/dev/null | awk 'NR==1{print $2}' || echo unknown)"
echo "  Main Pi reach:   $(ping -c1 -W2 "$MAIN_PI_TS_HOST" >/dev/null 2>&1 && echo OK || echo FAIL)"
echo "  Heal timer:      $(systemctl is-enabled hanryx-heal.timer 2>/dev/null || echo 'not installed')"
echo "  Watchdog:        $(systemctl is-enabled hanryx-watchdog 2>/dev/null || echo 'not installed')"
echo "  Launcher script: $([ -f "$HOME_DIR/.hanryx-dual-monitor.sh" ] && echo present || echo MISSING)"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
step "9. SSH from main Pi check"
echo "  After reboot, from the main Pi you should be able to:"
echo "    ssh ngansen@${NEW_HOSTNAME}"
echo "    ssh ngansen@${NEW_HOSTNAME}.local"
echo "    ssh ngansen@$(tailscale ip -4 2>/dev/null | head -1)"
echo ""
echo "  Tailscale SSH is enabled — you can also use:"
echo "    tailscale ssh ngansen@${NEW_HOSTNAME}"
echo "    (no key setup needed, uses Tailscale identity)"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
step "DONE"
ok "Bootstrap complete. Full log: $LOG"
echo ""
read -t 30 -rp "  Reboot now to activate everything? [Y/n] (auto-yes in 30s): " REBOOT_ANS || REBOOT_ANS=y
REBOOT_ANS="${REBOOT_ANS:-y}"
if [[ "${REBOOT_ANS,,}" == "y" ]]; then
    info "Rebooting in 5 seconds…"
    sleep 5
    reboot
else
    info "Skipping reboot — run 'sudo reboot' when ready."
fi
