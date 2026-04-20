#!/usr/bin/env bash
# =============================================================================
#  HanryxVault — One-stop Deploy / Upgrade Script
#
#  Run this on either Pi to pull the latest code from GitHub and apply EVERY
#  feature & fix added in this development cycle. Idempotent — safe to re-run
#  any time.
#
#  USAGE — from any Pi terminal:
#    curl -fsSL https://raw.githubusercontent.com/Ngansen/Hanryx-Vault-POS/main/pi-setup/deploy-all.sh | bash
#
#  Or from a checked-out repo:
#    bash pi-setup/deploy-all.sh           # auto-detect role
#    bash pi-setup/deploy-all.sh main      # force "main Pi" role
#    bash pi-setup/deploy-all.sh satellite # force "satellite Pi" role
#
#  Roles:
#    MAIN      — 192.168.86.36 — Flask + Postgres + Redis + recognizer (Docker)
#    SATELLITE — 192.168.86.22 — kiosk display + nginx + Bluetooth printer
#
#  What it does on MAIN:
#    • Pulls latest code
#    • Re-bakes Docker images (recognizer fix, kiosk fit-script, payment-flow fix)
#    • Restarts containers with the new images
#    • Runs DB-auth preflight + waits for /health to go green
#    • (Re-)installs nightly backup + TCG-refresh systemd timers
#    • Triggers any missing card-DB imports (KR / CHS / JPN-Pocket / Multi-TCG)
#
#  What it does on SATELLITE:
#    • Pulls latest code
#    • Installs missing OS packages (x11-utils for screen-fit fix)
#    • Copies updated kiosk start-monitor.sh into /opt/hanryxvault/kiosk
#    • Writes /etc/default/hanryxvault-kiosk (configurable per host)
#    • Restarts kiosk service + verifies it's running
#    • Downloads idle-video playlist if missing
# =============================================================================
set -euo pipefail

# ── Colours / log helpers ────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GREEN}[✓]${NC} $*"; }
info() { echo -e "${BLUE}[i]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*"; exit 1; }
hr()   { echo -e "${BLUE}────────────────────────────────────────────────────${NC}"; }
step() { echo; hr; echo -e "${BOLD}${CYAN}▸ $*${NC}"; hr; }

REPO_URL="https://github.com/Ngansen/Hanryx-Vault-POS.git"
REPO_DIR="${REPO_DIR:-$HOME/Hanryx-Vault-POS}"
MAIN_HOST_DEFAULT="192.168.86.36"
SAT_HOST_DEFAULT="192.168.86.22"

# ── Role detection ───────────────────────────────────────────────────────────
ROLE="${1:-}"
if [[ -z "$ROLE" ]]; then
    if command -v docker &>/dev/null && \
       sudo docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^pi-setup-pos-1$'; then
        ROLE="main"
    elif systemctl list-unit-files 2>/dev/null | grep -q '^hanryxvault-kiosk\.service'; then
        ROLE="satellite"
    else
        # Heuristic: if a 10.1" / 5" HDMI screen is attached, this is the satellite
        if command -v xrandr &>/dev/null && \
           DISPLAY=:0 xrandr 2>/dev/null | grep -q 'connected'; then
            ROLE="satellite"
        else
            ROLE="main"
        fi
    fi
    info "Auto-detected role: ${BOLD}${ROLE^^}${NC}"
fi

case "$ROLE" in
    main|satellite) ;;
    *) err "Unknown role '${ROLE}'. Use: main | satellite" ;;
esac

# ── 1. Pull latest code (clone if needed) ────────────────────────────────────
step "1/6 — Sync repo from GitHub"

if [[ ! -d "$REPO_DIR/.git" ]]; then
    info "Cloning $REPO_URL → $REPO_DIR"
    git clone "$REPO_URL" "$REPO_DIR"
else
    info "Updating existing checkout at $REPO_DIR"
    # If a previous run died mid-rebase / mid-merge, clean it up first.
    if [[ -d "$REPO_DIR/.git/rebase-merge" || -d "$REPO_DIR/.git/rebase-apply" ]]; then
        warn "Previous rebase was interrupted — aborting it"
        git -C "$REPO_DIR" rebase --abort 2>/dev/null || true
    fi
    if [[ -f "$REPO_DIR/.git/MERGE_HEAD" ]]; then
        warn "Previous merge was interrupted — aborting it"
        git -C "$REPO_DIR" merge --abort 2>/dev/null || true
    fi

    git -C "$REPO_DIR" fetch --quiet origin main

    # Stash uncommitted edits with a timestamped tag so they're never lost.
    if ! git -C "$REPO_DIR" diff --quiet || ! git -C "$REPO_DIR" diff --cached --quiet; then
        STAMP="deploy-all-$(date +%Y%m%d-%H%M%S)"
        warn "Uncommitted local edits detected — stashing as '$STAMP' (recover with: git stash list)"
        git -C "$REPO_DIR" stash push -u -m "$STAMP" || true
    fi

    # If the Pi has local commits that aren't on origin, save them to a backup
    # branch (so they're recoverable) then hard-reset to origin/main. This
    # avoids ever getting stuck in a merge/rebase conflict mid-deploy.
    AHEAD=$(git -C "$REPO_DIR" rev-list --count origin/main..HEAD 2>/dev/null || echo 0)
    if (( AHEAD > 0 )); then
        BACKUP="backup/pre-deploy-$(date +%Y%m%d-%H%M%S)"
        warn "Pi has $AHEAD local commit(s) not on origin/main — saving to branch '$BACKUP'"
        git -C "$REPO_DIR" branch "$BACKUP"
        git -C "$REPO_DIR" log --oneline origin/main..HEAD | sed 's/^/    /'
        warn "If you want these on GitHub, push them later with:"
        warn "    git -C $REPO_DIR push origin $BACKUP"
    fi

    info "Hard-resetting working tree to origin/main"
    git -C "$REPO_DIR" reset --hard origin/main
fi

cd "$REPO_DIR/pi-setup"
log "Repo synced @ $(git -C "$REPO_DIR" rev-parse --short HEAD)"

# =============================================================================
#                              MAIN PI BRANCH
# =============================================================================
if [[ "$ROLE" == "main" ]]; then

step "2/6 — Verify Docker is installed"
if ! command -v docker &>/dev/null; then
    err "Docker not installed. Run pi-setup/setup.sh first for fresh installs."
fi
sudo docker info >/dev/null 2>&1 || err "Docker daemon not running. Try: sudo systemctl start docker"
log "Docker OK ($(sudo docker --version | awk '{print $3}' | tr -d ,))"

step "3/6 — Re-bake & restart containers"
info "Building updated images (recognizer pin fix, kiosk fit-script, payment fix)…"
# Build first so we don't drop the running containers if a build fails
sudo docker compose build pos
# Recognizer is heavy (~3 GB wheels) — try to build, but don't abort the
# whole script if it fails (e.g. wheel mirror hiccup). User can retry.
if ! sudo docker compose build recognizer; then
    warn "Recognizer build failed — continuing without it. Retry later with:"
    warn "    sudo docker compose build --no-cache recognizer"
fi
info "Bringing the stack up …"
sudo docker compose up -d
log "Containers running:"
sudo docker compose ps --format 'table {{.Name}}\t{{.Status}}'

step "4/6 — Wait for /health to go green"
HEALTH_URL="http://127.0.0.1:8080/health"
waited=0
until curl -sf "$HEALTH_URL" >/dev/null 2>&1; do
    if (( waited >= 90 )); then
        err "Server did not become healthy within 90 s. Check: sudo docker logs pi-setup-pos-1"
    fi
    sleep 3; (( waited += 3 ))
    echo -n "."
done
echo
log "/health is green after ${waited}s"

step "5/6 — Install systemd timers (backup + TCG refresh)"
SYSTEMD_DIR="$REPO_DIR/pi-setup/systemd"
if [[ -d "$SYSTEMD_DIR" ]]; then
    for unit in "$SYSTEMD_DIR"/*.{service,timer}; do
        [[ -f "$unit" ]] || continue
        sudo cp "$unit" /etc/systemd/system/
        info "Installed $(basename "$unit")"
    done
    sudo systemctl daemon-reload
    # Enable the timers (services run on schedule, no need to enable them)
    for t in "$SYSTEMD_DIR"/*.timer; do
        [[ -f "$t" ]] || continue
        sudo systemctl enable --now "$(basename "$t")" 2>/dev/null || true
    done
    log "Systemd timers active:"
    systemctl list-timers --no-pager | grep hanryxvault || true
else
    warn "No systemd directory at $SYSTEMD_DIR — skipping"
fi

step "6/6 — Trigger card-DB imports if tables are empty"
DB_EXEC="sudo docker exec -u postgres pi-setup-db-1 psql -U vaultpos -d vaultpos -tAc"
import_if_empty() {
    local table="$1" script="$2"
    local count
    count=$($DB_EXEC "SELECT COUNT(*) FROM $table" 2>/dev/null || echo 0)
    if [[ "$count" -eq 0 ]]; then
        info "Table '$table' is empty — running $script (background)"
        sudo docker exec -d pi-setup-pos-1 python "$script" || \
            warn "Failed to launch $script — check container logs"
    else
        log "Table '$table' has $count rows — skip import"
    fi
}
import_if_empty cards_kr          import_kr_cards.py        || true
import_if_empty cards_chs         import_chs_cards.py       || true
import_if_empty cards_jpn         import_jpn_cards.py       || true
import_if_empty cards_jpn_pocket  import_jpn_pocket_cards.py || true
import_if_empty cards_multi       import_multi_tcg.py       || true

# Build the recognizer's perceptual-hash index in the background. Skips
# already-hashed cards, so safe to launch on every deploy.
HASH_COUNT=$($DB_EXEC "SELECT COUNT(*) FROM card_hashes" 2>/dev/null || echo 0)
info "card_hashes index currently has $HASH_COUNT row(s)"
if sudo docker exec pi-setup-pos-1 test -f /app/import_artwork_hashes.py 2>/dev/null; then
    info "Launching artwork-hash importer in background (resumable, ~minutes-hours)"
    sudo docker exec -d pi-setup-pos-1 \
        python /app/import_artwork_hashes.py 2>/dev/null || \
        warn "Artwork hash importer failed to launch — run manually with:"
        warn "  sudo docker exec -it pi-setup-pos-1 python import_artwork_hashes.py"
else
    warn "import_artwork_hashes.py not found in pos image — rebuild pos to include it"
fi

echo
hr
log "${BOLD}MAIN Pi deploy complete${NC}"
hr
echo "  Admin:    http://${MAIN_HOST_DEFAULT}:8080/admin"
echo "  Kiosk:    http://${MAIN_HOST_DEFAULT}:8080/kiosk"
echo "  Health:   http://${MAIN_HOST_DEFAULT}:8080/health"
echo "  Logs:     sudo docker compose -f $REPO_DIR/pi-setup/docker-compose.yml logs -f pos"
echo
echo "  Next steps:"
echo "    • If satellite needs updating, run on 192.168.86.22:"
echo "        curl -fsSL https://raw.githubusercontent.com/Ngansen/Hanryx-Vault-POS/main/pi-setup/deploy-all.sh | bash"
echo "    • Verify the recognizer:    curl http://localhost:8080/card/scan/recognizer/status"
echo "    • See RUNBOOK.md for ops & troubleshooting."

fi  # end MAIN

# =============================================================================
#                          SATELLITE PI BRANCH
# =============================================================================
if [[ "$ROLE" == "satellite" ]]; then

step "2/6 — Install / update OS packages (kiosk dependencies)"
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
    xserver-xorg xinit x11-xserver-utils x11-utils \
    unclutter xbindkeys curl yt-dlp ffmpeg \
    fonts-noto-color-emoji \
    chromium-browser 2>/dev/null \
    || sudo apt-get install -y --no-install-recommends chromium
log "Kiosk OS packages installed"

step "3/6 — Run/refresh kiosk installer"
KIOSK_INSTALL="$REPO_DIR/pi-setup/kiosk/install-kiosk.sh"
if [[ -x "$KIOSK_INSTALL" ]]; then
    sudo bash "$KIOSK_INSTALL"
else
    warn "$KIOSK_INSTALL not found or not executable — skipping full installer"
fi

step "4/6 — Update kiosk launch script + config"
sudo install -d /opt/hanryxvault/kiosk
sudo install -m 0755 "$REPO_DIR/pi-setup/kiosk/start-monitor.sh"   /opt/hanryxvault/kiosk/
sudo install -m 0755 "$REPO_DIR/pi-setup/kiosk/standby-toggle.sh"  /opt/hanryxvault/kiosk/ 2>/dev/null || true
sudo install -m 0755 "$REPO_DIR/pi-setup/kiosk/download-playlist.sh" /opt/hanryxvault/kiosk/ 2>/dev/null || true
log "Kiosk scripts copied to /opt/hanryxvault/kiosk/"

# Write env config only if it doesn't exist; otherwise preserve operator edits
ENV_FILE="/etc/default/hanryxvault-kiosk"
if [[ ! -f "$ENV_FILE" ]]; then
    info "Writing default $ENV_FILE (10.1\"=admin, 5\"=kiosk)"
    sudo tee "$ENV_FILE" > /dev/null <<EOF
# HanryxVault kiosk environment — edit to swap displays / URLs.
# The 10.1" 1024x600 screen (HDMI-1) is wired here as the ADMIN dashboard,
# and the 5" 800x480 screen (HDMI-2) is the customer-facing kiosk.
# Swap the OUTPUT names to flip them.
POS_HOST=http://${MAIN_HOST_DEFAULT}:8080
KIOSK_LEFT_OUTPUT=HDMI-2
KIOSK_LEFT_URL=http://${MAIN_HOST_DEFAULT}:8080/kiosk
KIOSK_RIGHT_OUTPUT=HDMI-1
KIOSK_RIGHT_URL=http://${MAIN_HOST_DEFAULT}:8080/admin
EOF
    log "Wrote $ENV_FILE"
else
    info "$ENV_FILE already exists — leaving operator edits intact"
fi

step "5/6 — Restart kiosk service"
sudo systemctl daemon-reload
sudo systemctl enable hanryxvault-kiosk.service 2>/dev/null || true
sudo systemctl restart hanryxvault-kiosk.service
sleep 4
if systemctl is-active --quiet hanryxvault-kiosk.service; then
    log "Kiosk service is running"
else
    warn "Kiosk service is not active — last 20 log lines:"
    sudo journalctl -u hanryxvault-kiosk --no-pager -n 20
fi

step "6/6 — Idle-video playlist sanity check"
VIDEOS_DIR="/opt/hanryxvault/kiosk/videos"
sudo install -d "$VIDEOS_DIR"
VIDEO_COUNT=$(sudo find "$VIDEOS_DIR" -maxdepth 1 -type f \( -iname '*.mp4' -o -iname '*.webm' \) 2>/dev/null | wc -l)
if (( VIDEO_COUNT == 0 )); then
    warn "No idle videos in $VIDEOS_DIR"
    if [[ -x /opt/hanryxvault/kiosk/download-playlist.sh ]]; then
        info "Launching playlist download in background — this takes 30-60 min"
        sudo nohup /opt/hanryxvault/kiosk/download-playlist.sh \
            > /var/log/hanryxvault-playlist.log 2>&1 &
        log "Download started — tail with: tail -f /var/log/hanryxvault-playlist.log"
    else
        warn "Playlist downloader missing — videos will need to be added manually"
    fi
else
    log "$VIDEO_COUNT idle video(s) on disk — playlist ready"
fi

echo
hr
log "${BOLD}SATELLITE Pi deploy complete${NC}"
hr
echo "  Status:   sudo systemctl status hanryxvault-kiosk"
echo "  Logs:     sudo journalctl -u hanryxvault-kiosk -f"
echo "  Edit env: sudo nano /etc/default/hanryxvault-kiosk && sudo systemctl restart hanryxvault-kiosk"
echo "  Standby toggle: F9 on the satellite keyboard"
echo
echo "  If the kiosk page looks cropped or scrolls:"
echo "    DISPLAY=:0 xrandr        # check actual screen sizes"
echo "    sudo journalctl -u hanryxvault-kiosk --since today | grep -E 'output=|geom='"

fi  # end SATELLITE

echo
log "All done. ${BOLD}Re-run this script any time${NC} — it's idempotent."
