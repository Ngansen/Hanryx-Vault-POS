#!/usr/bin/env bash
# =============================================================================
#  HanryxVault — Pi Setup Script
#  Run this on a fresh Raspberry Pi OS (Bookworm) to install everything.
#
#  USAGE — one-liner from any Pi terminal:
#    curl -fsSL https://raw.githubusercontent.com/Ngansen/Hanryx-Vault-POS/main/pi-setup/setup.sh | bash
#
#  Or download first, then run:
#    curl -fsSL https://raw.githubusercontent.com/Ngansen/Hanryx-Vault-POS/main/pi-setup/setup.sh -o setup.sh
#    bash setup.sh
#
#  What it does:
#    1. Installs Docker + Docker Compose (if missing)
#    2. Clones / updates the GitHub repo
#    3. Creates your .env config file (prompts for secrets)
#    4. Builds and starts all Docker containers
#    5. Shows you where to reach the admin dashboard
# =============================================================================
set -euo pipefail

# ── Colours ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $*"; }
info() { echo -e "${BLUE}[i]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*"; exit 1; }
hr()   { echo -e "${BLUE}──────────────────────────────────────────────────────${NC}"; }
ask()  {
    # ask <var_name> <prompt> [default]
    local __var=$1 __prompt=$2 __default="${3:-}"
    local __reply
    if [[ -n "$__default" ]]; then
        read -r -p "    ${__prompt} [${__default}]: " __reply
        printf -v "$__var" '%s' "${__reply:-$__default}"
    else
        read -r -p "    ${__prompt}: " __reply
        printf -v "$__var" '%s' "$__reply"
    fi
}
ask_secret() {
    local __var=$1 __prompt=$2
    local __reply
    read -r -s -p "    ${__prompt} (hidden): " __reply; echo
    printf -v "$__var" '%s' "$__reply"
}

REPO_URL="https://github.com/Ngansen/Hanryx-Vault-POS.git"
INSTALL_DIR="$HOME/hanryx-vault-pos"
PI_SETUP="$INSTALL_DIR/pi-setup"

# ── Banner ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}${BOLD}"
echo "  ╔══════════════════════════════════════════════════╗"
echo "  ║        HanryxVault — Raspberry Pi Setup          ║"
echo "  ║   POS · Admin Dashboard · Storefront · Docker    ║"
echo "  ╚══════════════════════════════════════════════════╝"
echo -e "${NC}"
echo ""

# ── 1. Check OS ────────────────────────────────────────────────────────────────
hr
info "Step 1 — Checking system..."
if [[ "$(uname -s)" != "Linux" ]]; then
    err "This script is for Raspberry Pi / Linux only."
fi
ARCH=$(uname -m)
info "Architecture: $ARCH"
info "OS: $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || uname -r)"
echo ""

# ── 2. Install Docker ─────────────────────────────────────────────────────────
hr
info "Step 2 — Docker..."

if command -v docker &>/dev/null; then
    log "Docker already installed: $(docker --version)"
else
    warn "Docker not found — installing..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq ca-certificates curl gnupg lsb-release

    # Official Docker install script (handles Pi / Debian / Ubuntu)
    curl -fsSL https://get.docker.com | sudo sh

    # Add current user to docker group so we don't need sudo for every command
    sudo usermod -aG docker "$USER"
    log "Docker installed — you may need to log out and back in for group changes"
fi

# Ensure Compose v2 plugin is available (comes with modern Docker)
if docker compose version &>/dev/null 2>&1; then
    log "Docker Compose: $(docker compose version)"
else
    warn "Installing Docker Compose plugin..."
    sudo apt-get install -y -qq docker-compose-plugin || \
        sudo curl -fsSL \
            "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" \
            -o /usr/local/bin/docker-compose && \
        sudo chmod +x /usr/local/bin/docker-compose
    log "Docker Compose installed"
fi
echo ""

# ── 3. Clone / update repo ────────────────────────────────────────────────────
hr
info "Step 3 — Getting the latest code from GitHub..."

if [[ -d "$INSTALL_DIR/.git" ]]; then
    log "Repo already exists at $INSTALL_DIR — pulling latest..."
    git -C "$INSTALL_DIR" fetch --quiet origin
    git -C "$INSTALL_DIR" reset --quiet --hard origin/main
    log "Updated to: $(git -C "$INSTALL_DIR" log -1 --format='%h %s')"
else
    info "Cloning into $INSTALL_DIR ..."
    git clone --depth=1 "$REPO_URL" "$INSTALL_DIR"
    log "Cloned successfully"
fi
echo ""

# ── 4. Create .env ────────────────────────────────────────────────────────────
hr
info "Step 4 — Configuration (.env)..."

ENV_FILE="$PI_SETUP/.env"

if [[ -f "$ENV_FILE" ]]; then
    warn ".env already exists at $ENV_FILE"
    read -r -p "    Keep existing .env? [Y/n]: " KEEP_ENV
    KEEP_ENV="${KEEP_ENV:-Y}"
    if [[ "${KEEP_ENV,,}" != "n" ]]; then
        log "Keeping existing .env — skipping config step"
        SKIP_CONFIG=1
    fi
fi

if [[ "${SKIP_CONFIG:-0}" != "1" ]]; then
    echo ""
    echo -e "  ${BOLD}Fill in your secrets below. Press Enter to keep the default shown in [brackets].${NC}"
    echo -e "  ${YELLOW}Zettle credentials can be left blank now and added later in .env${NC}"
    echo ""

    # Generate sensible random defaults for secrets
    _rand() { LC_ALL=C tr -dc 'a-zA-Z0-9' < /dev/urandom | head -c 32 2>/dev/null || echo "changeme-$(date +%s)"; }

    ask         DB_PASS      "Database password"                  "$(_rand)"
    ask_secret  SESSION_SEC  "Session secret"
    SESSION_SEC="${SESSION_SEC:-$(_rand)}"
    ask         ADMIN_PASS   "Admin dashboard password"           "hanryxvault"
    ask         ZETTLE_ID    "Zettle Client ID     (or leave blank)" ""
    ask_secret  ZETTLE_SEC   "Zettle Client Secret (or leave blank)"
    ask         PTCG_KEY     "Pokémon TCG API key  (or leave blank)" ""
    ask         SMTP_USER    "Gmail address for sale alerts       (or leave blank)" ""
    ask_secret  SMTP_PASS    "Gmail App Password                  (or leave blank)"
    ask         OPENAI_KEY   "OpenAI API key for photo-ID         (or leave blank)" ""

    echo ""

    cp "$PI_SETUP/.env.example" "$ENV_FILE"

    # Substitute values — use | as delimiter to avoid clashing with passwords
    sed -i "s|DB_PASSWORD=.*|DB_PASSWORD=${DB_PASS}|"                 "$ENV_FILE"
    sed -i "s|SESSION_SECRET=.*|SESSION_SECRET=${SESSION_SEC}|"        "$ENV_FILE"
    sed -i "s|ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${ADMIN_PASS}|"         "$ENV_FILE"
    sed -i "s|ZETTLE_CLIENT_ID=.*|ZETTLE_CLIENT_ID=${ZETTLE_ID}|"     "$ENV_FILE"
    sed -i "s|ZETTLE_CLIENT_SECRET=.*|ZETTLE_CLIENT_SECRET=${ZETTLE_SEC}|" "$ENV_FILE"
    sed -i "s|PTCG_API_KEY=.*|PTCG_API_KEY=${PTCG_KEY}|"              "$ENV_FILE"
    sed -i "s|SMTP_USER=.*|SMTP_USER=${SMTP_USER}|"                    "$ENV_FILE"
    sed -i "s|SMTP_APP_PASSWORD=.*|SMTP_APP_PASSWORD=${SMTP_PASS}|"    "$ENV_FILE"
    sed -i "s|OPENAI_API_KEY=.*|OPENAI_API_KEY=${OPENAI_KEY}|"         "$ENV_FILE"

    chmod 600 "$ENV_FILE"
    log ".env written to $ENV_FILE"
fi
echo ""

# ── 5. Build & start containers ───────────────────────────────────────────────
hr
info "Step 5 — Building and starting Docker containers..."
info "This takes 2–4 minutes on first run (downloading base images + building)."
echo ""

cd "$PI_SETUP"

# Use sudo for docker if the user isn't in the docker group yet in this session
DOCKER_CMD="docker"
if ! docker ps &>/dev/null 2>&1; then
    DOCKER_CMD="sudo docker"
    warn "Using sudo for Docker (group change needs re-login — next time you won't need sudo)"
fi

$DOCKER_CMD compose up -d --build

echo ""

# ── 6. Wait for health check ──────────────────────────────────────────────────
hr
info "Step 6 — Waiting for POS server to be ready..."
MAX_WAIT=90
WAITED=0
until curl -sf "http://127.0.0.1:8080/health" &>/dev/null; do
    if [[ $WAITED -ge $MAX_WAIT ]]; then
        warn "Server didn't respond within ${MAX_WAIT}s — check logs:"
        warn "  $DOCKER_CMD compose -f $PI_SETUP/docker-compose.yml logs pos"
        break
    fi
    printf "."
    sleep 3
    WAITED=$((WAITED + 3))
done
echo ""

if curl -sf "http://127.0.0.1:8080/health" &>/dev/null; then
    log "POS server is up and healthy!"
fi
echo ""

# ── 7. Summary ────────────────────────────────────────────────────────────────
hr
PI_IP=$(hostname -I | awk '{print $1}' 2>/dev/null || echo "YOUR-PI-IP")

echo ""
echo -e "${YELLOW}${BOLD}"
echo "  ╔══════════════════════════════════════════════════╗"
echo "  ║               SETUP COMPLETE  ✓                  ║"
echo "  ╚══════════════════════════════════════════════════╝"
echo -e "${NC}"
echo -e "  ${GREEN}${BOLD}Admin Dashboard:${NC}   http://${PI_IP}:8080/admin"
echo -e "  ${GREEN}${BOLD}POS Health:${NC}        http://${PI_IP}:8080/health"
echo -e "  ${GREEN}${BOLD}Storefront:${NC}        http://${PI_IP}:3000"
echo ""
echo -e "  ${BOLD}Useful commands:${NC}"
echo ""
echo -e "  ${BLUE}# View live server logs${NC}"
echo -e "  $DOCKER_CMD compose -f $PI_SETUP/docker-compose.yml logs -f pos"
echo ""
echo -e "  ${BLUE}# Pull latest code and rebuild${NC}"
echo -e "  git -C $INSTALL_DIR pull && $DOCKER_CMD compose -f $PI_SETUP/docker-compose.yml up -d --build pos"
echo ""
echo -e "  ${BLUE}# Stop everything${NC}"
echo -e "  $DOCKER_CMD compose -f $PI_SETUP/docker-compose.yml down"
echo ""
echo -e "  ${BLUE}# Edit your config (.env)${NC}"
echo -e "  nano $ENV_FILE"
echo -e "  $DOCKER_CMD compose -f $PI_SETUP/docker-compose.yml up -d   # (no rebuild needed for env changes)"
echo ""
echo -e "  ${YELLOW}Repo: $INSTALL_DIR${NC}"
echo ""
hr
