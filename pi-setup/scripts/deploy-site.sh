#!/bin/bash
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${GREEN}[+]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[x]${NC} $1"; exit 1; }
info() { echo -e "${BLUE}[i]${NC} $1"; }

echo ""
echo "============================================"
echo "  Deploy / Update a Website on Your Pi"
echo "============================================"
echo ""
echo "Which site are you deploying?"
echo "  1) hanryxvault.cards"
echo "  2) hanryxvault.app"
echo "  3) Custom domain"
echo ""
read -p "Choice [1-3]: " CHOICE

case "$CHOICE" in
  1) DOMAIN="hanryxvault.cards"; WEB_ROOT="/var/www/hanryxvault.cards" ;;
  2) DOMAIN="hanryxvault.app";   WEB_ROOT="/var/www/hanryxvault.app"   ;;
  3)
    read -p "Domain name: " DOMAIN
    WEB_ROOT="/var/www/${DOMAIN}"
    ;;
  *) err "Invalid choice" ;;
esac

echo ""
echo "Where are your built site files? (the folder containing index.html)"
echo "Examples:"
echo "  ./dist         (Vite / React build output)"
echo "  ./build        (Create React App output)"
echo "  ./out          (Next.js static export)"
echo "  .              (already in the right folder)"
echo ""
read -p "Path to site files: " SRC_PATH

[ -z "$SRC_PATH" ] && err "Path cannot be empty."
[ ! -d "$SRC_PATH" ] && err "Directory '${SRC_PATH}' not found."
[ ! -f "${SRC_PATH}/index.html" ] && warn "No index.html found in '${SRC_PATH}' — make sure you built the project first."

log "Deploying ${SRC_PATH} → ${WEB_ROOT}..."
mkdir -p "${WEB_ROOT}"

# Copy files, preserving permissions
rsync -a --delete \
  --exclude='.git' \
  --exclude='node_modules' \
  --exclude='*.map' \
  "${SRC_PATH}/" "${WEB_ROOT}/"

chown -R www-data:www-data "${WEB_ROOT}"
find "${WEB_ROOT}" -type f -exec chmod 644 {} \;
find "${WEB_ROOT}" -type d -exec chmod 755 {} \;

nginx -t && systemctl reload nginx

echo ""
log "Done! ${DOMAIN} is now live."
echo ""
info "Test it locally: curl -s http://localhost -H 'Host: ${DOMAIN}' | head -5"
info "Or visit:        http://${DOMAIN}"
echo ""

if ! certbot certificates 2>/dev/null | grep -q "${DOMAIN}"; then
  warn "HTTPS is not yet enabled for ${DOMAIN}."
  echo "  Run: sudo bash scripts/enable-https.sh ${DOMAIN}"
fi
