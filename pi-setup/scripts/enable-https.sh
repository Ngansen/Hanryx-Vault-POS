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

[ "$EUID" -ne 0 ] && err "Please run as root: sudo bash scripts/enable-https.sh [domain]"

DOMAIN="${1:-}"

echo ""
echo "============================================"
echo "  Enable HTTPS (Let's Encrypt)"
echo "  Required for Zettle payments"
echo "============================================"
echo ""

if [ -z "$DOMAIN" ]; then
  read -p "Domain name to enable HTTPS for (e.g. hanryxvault.com): " DOMAIN
fi
[ -z "$DOMAIN" ] && err "Domain cannot be empty."

# Verify nginx config exists for this domain
if [ ! -f "/etc/nginx/sites-available/${DOMAIN}" ] && [ ! -f "/etc/nginx/sites-available/hanryxvault" ]; then
  warn "No nginx config found for '${DOMAIN}'."
  warn "If this is the main HanryxVault server, run install.sh first."
  warn "For a website, run: sudo bash scripts/add-site.sh"
  echo ""
  read -p "Continue anyway? [y/n]: " CONT
  [ "$CONT" != "y" ] && exit 0
fi

# Check DNS is pointing here
info "Checking DNS for ${DOMAIN}..."
MY_IP=$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || echo "unknown")
DNS_IP=$(dig +short "${DOMAIN}" 2>/dev/null | tail -1 || echo "unknown")

if [ "$MY_IP" != "unknown" ] && [ "$DNS_IP" != "unknown" ]; then
  if [ "$MY_IP" != "$DNS_IP" ]; then
    warn "DNS mismatch!"
    warn "  Your Pi's public IP: ${MY_IP}"
    warn "  ${DOMAIN} resolves to: ${DNS_IP}"
    warn ""
    warn "HTTPS will fail if DNS isn't pointing to this Pi."
    warn "Update your domain's A record to: ${MY_IP}"
    echo ""
    read -p "Try anyway? [y/n]: " TRY
    [ "$TRY" != "y" ] && exit 0
  else
    log "DNS OK — ${DOMAIN} → ${MY_IP}"
  fi
fi

# Install certbot if not present
if ! command -v certbot &>/dev/null; then
  log "Installing certbot..."
  apt-get install -y -qq certbot python3-certbot-nginx
fi

# Run certbot
log "Requesting SSL certificate for ${DOMAIN}..."
echo ""
certbot --nginx \
  -d "${DOMAIN}" \
  -d "www.${DOMAIN}" \
  --non-interactive \
  --agree-tos \
  --redirect \
  --email "admin@${DOMAIN}" 2>/dev/null || \
certbot --nginx \
  -d "${DOMAIN}" \
  --non-interactive \
  --agree-tos \
  --redirect \
  --register-unsafely-without-email

# Set up auto-renewal
if ! crontab -l 2>/dev/null | grep -q "certbot renew"; then
  log "Setting up automatic certificate renewal..."
  (crontab -l 2>/dev/null; echo "0 3 * * * certbot renew --quiet && systemctl reload nginx") | crontab -
fi

nginx -t && systemctl reload nginx

echo ""
echo "============================================"
echo -e "${GREEN}  HTTPS enabled for ${DOMAIN}!${NC}"
echo "============================================"
echo ""
info "Certificate auto-renews every 90 days at 3:00 AM"
info "Zettle callback: https://${DOMAIN}/zettle/callback"
echo ""
warn "NEXT: Update your Zettle developer app's redirect URI to:"
echo "  https://${DOMAIN}/zettle/callback"
echo ""
