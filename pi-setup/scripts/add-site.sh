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

[ "$EUID" -ne 0 ] && err "Please run as root: sudo bash scripts/add-site.sh"

echo ""
echo "============================================"
echo "  Add a Website to Your Pi Server"
echo "============================================"
echo ""

read -p "Domain name (e.g. mysite.com): " DOMAIN
[ -z "$DOMAIN" ] && err "Domain cannot be empty."

read -p "Path to your site's files (e.g. /opt/sites/mysite): " SITE_PATH
[ -z "$SITE_PATH" ] && err "Site path cannot be empty."

read -p "Is this a static site (HTML/CSS/JS files)? [y/n]: " IS_STATIC

NEXT_PORT=3001
# Find a free port starting at 3001
while ss -tlnp | grep -q ":${NEXT_PORT} "; do
  NEXT_PORT=$((NEXT_PORT + 1))
done

mkdir -p "${SITE_PATH}"

CONF_FILE="/etc/nginx/sites-available/${DOMAIN}"

if [ "$IS_STATIC" = "y" ] || [ "$IS_STATIC" = "Y" ]; then
  # ── Static site config ───────────────────────────────────────────────────────
  log "Creating static site config for ${DOMAIN}..."
  cat > "${CONF_FILE}" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN} www.${DOMAIN};

    root ${SITE_PATH};
    index index.html index.htm;

    gzip on;
    gzip_vary on;
    gzip_types text/plain text/css application/json application/javascript text/xml application/xml text/javascript;

    # Cache static assets
    location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|woff|woff2|ttf|eot)$ {
        expires 1y;
        add_header Cache-Control "public, immutable";
    }

    location / {
        try_files \$uri \$uri/ /index.html;
    }

    # Security headers
    add_header X-Frame-Options "SAMEORIGIN";
    add_header X-Content-Type-Options "nosniff";
    add_header Referrer-Policy "strict-origin-when-cross-origin";
}
EOF

else
  # ── Node.js app config ───────────────────────────────────────────────────────
  log "Creating Node.js proxy config for ${DOMAIN} on port ${NEXT_PORT}..."

  read -p "Start command for your app (e.g. 'node dist/index.js'): " START_CMD
  [ -z "$START_CMD" ] && START_CMD="node index.js"

  SERVICE_NAME="${DOMAIN//./-}"

  # Create systemd service for this site
  cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=${DOMAIN} Node.js App
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=${SITE_PATH}
ExecStart=/usr/bin/${START_CMD}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}
Environment=NODE_ENV=production
Environment=PORT=${NEXT_PORT}
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}"
  systemctl start "${SERVICE_NAME}"

  cat > "${CONF_FILE}" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN} www.${DOMAIN};

    gzip on;
    gzip_vary on;
    gzip_types text/plain text/css application/json application/javascript text/javascript;

    location / {
        proxy_pass http://127.0.0.1:${NEXT_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_cache_bypass \$http_upgrade;
        proxy_read_timeout 60s;
    }

    add_header X-Frame-Options "SAMEORIGIN";
    add_header X-Content-Type-Options "nosniff";
}
EOF

fi

ln -sf "${CONF_FILE}" "/etc/nginx/sites-enabled/${DOMAIN}"
nginx -t && systemctl reload nginx

echo ""
log "Site '${DOMAIN}' added successfully!"
echo ""
warn "Next: enable HTTPS for this domain:"
echo "  sudo bash scripts/enable-https.sh ${DOMAIN}"
echo ""
