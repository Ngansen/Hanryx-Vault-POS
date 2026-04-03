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
hr()   { echo -e "${BLUE}────────────────────────────────────────────${NC}"; }

[ "$EUID" -ne 0 ] && err "Run as root:  sudo bash install.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/hanryxvault"
LOG_DIR="/var/log/hanryxvault"
APP_USER="hanryxvault"

echo ""
echo -e "${YELLOW}╔══════════════════════════════════════════════╗${NC}"
echo -e "${YELLOW}║       HanryxVault — Raspberry Pi 5 Setup     ║${NC}"
echo -e "${YELLOW}║  POS Server · Both Websites · HTTPS · Zettle  ║${NC}"
echo -e "${YELLOW}╚══════════════════════════════════════════════╝${NC}"
echo ""

# ── 1. System update ──────────────────────────────────────────────────────────
hr
log "Step 1/9 — Updating system packages..."
apt-get update -qq
apt-get upgrade -y -qq

# ── 2. System dependencies ────────────────────────────────────────────────────
log "Step 2/9 — Installing system dependencies..."
apt-get install -y -qq \
  python3 python3-pip python3-venv \
  nginx certbot python3-certbot-nginx \
  ufw fail2ban curl wget git \
  sqlite3 dnsutils net-tools

# ── 3. Create dedicated user ──────────────────────────────────────────────────
log "Step 3/9 — Creating system user '${APP_USER}'..."
if ! id "${APP_USER}" &>/dev/null; then
  useradd --system --no-create-home --shell /bin/false "${APP_USER}"
fi

# ── 4. Deploy POS server ──────────────────────────────────────────────────────
log "Step 4/9 — Deploying HanryxVault POS server..."
mkdir -p "${INSTALL_DIR}" "${LOG_DIR}"

# Copy server files
cp "${SCRIPT_DIR}/server.py"        "${INSTALL_DIR}/server.py"
cp "${SCRIPT_DIR}/requirements.txt" "${INSTALL_DIR}/requirements.txt"

# Copy APK if it exists
if [ -f "${SCRIPT_DIR}/hanryxvault.apk" ]; then
  cp "${SCRIPT_DIR}/hanryxvault.apk" "${INSTALL_DIR}/hanryxvault.apk"
  log "  APK copied to ${INSTALL_DIR}/hanryxvault.apk"
fi

# Create Python virtual environment
log "  Creating Python virtual environment..."
python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"

# Set permissions
chown -R "${APP_USER}:${APP_USER}" "${INSTALL_DIR}" "${LOG_DIR}"
chmod 750 "${INSTALL_DIR}"

# ── 5. Collect Zettle credentials ─────────────────────────────────────────────
log "Step 5/9 — Zettle credentials..."
hr
echo ""
warn "You need your Zettle Developer App credentials."
warn "Get them at: https://developer.zettle.com/  →  Your Apps"
echo ""
read -p "  Zettle Client ID     : " ZETTLE_ID
read -s -p "  Zettle Client Secret : " ZETTLE_SECRET
echo ""
hr

# Save credentials securely
mkdir -p /etc/hanryxvault
cat > /etc/hanryxvault/zettle.env <<EOF
ZETTLE_CLIENT_ID=${ZETTLE_ID}
ZETTLE_CLIENT_SECRET=${ZETTLE_SECRET}
ZETTLE_REDIRECT_URI=https://hanryxvault.tailcfc0a3.ts.net/zettle/callback
EOF
chmod 600 /etc/hanryxvault/zettle.env

# ── 6. Systemd service ────────────────────────────────────────────────────────
log "Step 6/9 — Installing systemd service..."
cp "${SCRIPT_DIR}/systemd/hanryxvault.service" /etc/systemd/system/hanryxvault.service

# Inject Zettle credentials into service
sed -i "s|__ZETTLE_CLIENT_ID__|${ZETTLE_ID}|g"       /etc/systemd/system/hanryxvault.service
sed -i "s|__ZETTLE_CLIENT_SECRET__|${ZETTLE_SECRET}|g" /etc/systemd/system/hanryxvault.service

systemctl daemon-reload
systemctl enable hanryxvault

# Initialize DB and start server
sudo -u "${APP_USER}" "${INSTALL_DIR}/venv/bin/python3" \
  -c "import sys; sys.path.insert(0, '${INSTALL_DIR}'); import server; server.init_db()" || true

systemctl start hanryxvault
sleep 2

if systemctl is-active --quiet hanryxvault; then
  log "  POS server running on 127.0.0.1:8080"
else
  warn "  Server may not have started — check: sudo journalctl -u hanryxvault -n 50"
fi

# ── 7. nginx setup ────────────────────────────────────────────────────────────
log "Step 7/9 — Configuring nginx..."

# Remove default site
rm -f /etc/nginx/sites-enabled/default

# POS server (Tailscale domain)
cp "${SCRIPT_DIR}/nginx/hanryxvault.conf" \
   /etc/nginx/sites-available/hanryxvault
ln -sf /etc/nginx/sites-available/hanryxvault \
       /etc/nginx/sites-enabled/hanryxvault

# hanryxvault.cards
cat > /etc/nginx/sites-available/hanryxvault.cards <<'NGINX'
server {
    listen 80;
    listen [::]:80;
    server_name hanryxvault.cards www.hanryxvault.cards;

    client_max_body_size 50M;
    gzip on;
    gzip_vary on;
    gzip_types text/plain text/css application/json application/javascript text/javascript;

    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;

    # Static site root — copy your site files here after install
    root /var/www/hanryxvault.cards;
    index index.html index.htm;

    location / {
        try_files $uri $uri/ /index.html;
    }

    # Cache static assets aggressively
    location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|woff|woff2|ttf|eot|webp)$ {
        expires 1y;
        add_header Cache-Control "public, immutable";
    }

    # Proxy /api calls to the POS server
    location /api/ {
        proxy_pass         http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
NGINX

# hanryxvault.app
cat > /etc/nginx/sites-available/hanryxvault.app <<'NGINX'
server {
    listen 80;
    listen [::]:80;
    server_name hanryxvault.app www.hanryxvault.app;

    client_max_body_size 50M;
    gzip on;
    gzip_vary on;
    gzip_types text/plain text/css application/json application/javascript text/javascript;

    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;

    # Static site root — copy your site files here after install
    root /var/www/hanryxvault.app;
    index index.html index.htm;

    location / {
        try_files $uri $uri/ /index.html;
    }

    # Cache static assets
    location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|woff|woff2|ttf|eot|webp)$ {
        expires 1y;
        add_header Cache-Control "public, immutable";
    }

    # Proxy /api calls to the POS server
    location /api/ {
        proxy_pass         http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
NGINX

# Create web roots
mkdir -p /var/www/hanryxvault.cards /var/www/hanryxvault.app
chown -R www-data:www-data /var/www/hanryxvault.cards /var/www/hanryxvault.app

# Enable sites
ln -sf /etc/nginx/sites-available/hanryxvault.cards \
       /etc/nginx/sites-enabled/hanryxvault.cards
ln -sf /etc/nginx/sites-available/hanryxvault.app \
       /etc/nginx/sites-enabled/hanryxvault.app

nginx -t && systemctl restart nginx
log "  nginx configured for all 4 domains"

# ── 8. Firewall ───────────────────────────────────────────────────────────────
log "Step 8/9 — Configuring firewall..."
ufw --force reset > /dev/null
ufw default deny incoming  > /dev/null
ufw default allow outgoing > /dev/null
ufw allow ssh              > /dev/null
ufw allow 80/tcp           > /dev/null
ufw allow 443/tcp          > /dev/null
ufw --force enable         > /dev/null
log "  Firewall: SSH + HTTP + HTTPS only"

# ── 9. fail2ban ───────────────────────────────────────────────────────────────
log "Step 9/9 — Enabling fail2ban (brute-force protection)..."
systemctl enable fail2ban --quiet
systemctl start fail2ban

# ── Performance tuning ────────────────────────────────────────────────────────
log "Step 10/11 — Applying performance & reliability tuning..."
PERF_SCRIPT="${SCRIPT_DIR}/scripts/setup-performance.sh"
if [[ -f "$PERF_SCRIPT" ]]; then
    bash "$PERF_SCRIPT"
else
    warn "  setup-performance.sh not found — run it manually: sudo bash scripts/setup-performance.sh"
fi

# ── QR Scan Hub ───────────────────────────────────────────────────────────────
log "Step 11/11 — Installing QR scan hub (USB + Bluetooth HID → all apps)..."
SCAN_HUB_SRC="${SCRIPT_DIR}/scripts/barcode_daemon.py"
if [[ -f "$SCAN_HUB_SRC" ]]; then
    cp "$SCAN_HUB_SRC" "${INSTALL_DIR}/barcode_daemon.py"
    chown root:root "${INSTALL_DIR}/barcode_daemon.py"
    chmod 644 "${INSTALL_DIR}/barcode_daemon.py"

    # evdev reads raw /dev/input devices — requires root
    "${INSTALL_DIR}/venv/bin/pip" install --quiet evdev

    # Default scan_endpoints.conf — lists every app that should receive scans
    ENDPOINTS_CONF="${INSTALL_DIR}/scan_endpoints.conf"
    if [[ ! -f "$ENDPOINTS_CONF" ]]; then
        cat > "$ENDPOINTS_CONF" << 'EOF'
# HanryxVault Scan Endpoints
# One URL per line — every QR scan is forwarded to ALL listed apps simultaneously.
# Blank lines and lines starting with # are ignored.
# Restart hanryxvault-scan-hub after editing:
#   sudo systemctl restart hanryxvault-scan-hub

# POS server (always include this)
http://localhost:8080/scan

# Add your other apps here:
#   http://localhost:8081/scan   ← Pokémon lookup app
#   http://localhost:8082/scan   ← another project
EOF
        chown root:root "$ENDPOINTS_CONF"
        chmod 644 "$ENDPOINTS_CONF"
        log "  Created scan_endpoints.conf"
    else
        log "  scan_endpoints.conf already exists — not overwritten"
    fi

    cp "${SCRIPT_DIR}/systemd/hanryxvault-barcode.service" \
       /etc/systemd/system/hanryxvault-scan-hub.service
    systemctl daemon-reload
    systemctl enable --now hanryxvault-scan-hub.service
    log "  QR scan hub installed and running on port 8765"
    log "  Add apps to ${ENDPOINTS_CONF} to receive scans"
else
    warn "  barcode_daemon.py not found — skipping scan hub setup"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}╔══════════════════════════════════════════════╗${NC}"
echo -e "${YELLOW}║              SETUP COMPLETE                   ║${NC}"
echo -e "${YELLOW}╚══════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${GREEN}  POS Server:${NC}  http://$(hostname -I | awk '{print $1}'):8080/admin"
echo -e "${GREEN}  Admin dash:${NC}  http://$(hostname -I | awk '{print $1}')/admin"
echo ""
echo -e "${YELLOW}  NEXT STEPS (in order):${NC}"
echo ""
echo "  1. Copy your website files to the Pi:"
echo "     hanryxvault.cards → /var/www/hanryxvault.cards/"
echo "     hanryxvault.app   → /var/www/hanryxvault.app/"
echo "     (See README.md for how to export from Replit)"
echo ""
echo "  2. Point your domains' DNS A records to this Pi's IP:"
echo "     Your public IP: $(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || echo 'run: curl ifconfig.me')"
echo ""
echo "  3. Enable HTTPS for each domain (required for Zettle):"
echo "     sudo bash scripts/enable-https.sh hanryxvault.cards"
echo "     sudo bash scripts/enable-https.sh hanryxvault.app"
echo "     sudo bash scripts/enable-https.sh hanryxvault.tailcfc0a3.ts.net"
echo ""
echo "  4. Check POS server logs anytime:"
echo "     sudo journalctl -u hanryxvault -f"
echo ""
echo "  5. QR scanner (USB or Bluetooth HID) is handled by the scan hub:"
echo "     sudo journalctl -u hanryxvault-scan-hub -f"
echo "     curl http://localhost:8765/health"
echo "     To add another app: edit /opt/hanryxvault/scan_endpoints.conf"
echo "     Pair BT scanner:    sudo bluetoothctl → pair <MAC> → trust → connect"
echo ""
