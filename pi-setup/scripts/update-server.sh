#!/bin/bash
# update-server.sh — pull the latest server.py to the Pi and restart
set -e

GREEN='\033[0;32m'
NC='\033[0m'
log() { echo -e "${GREEN}[+]${NC} $1"; }

INSTALL_DIR="/opt/hanryxvault"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[ "$EUID" -ne 0 ] && { echo "Run as root: sudo bash scripts/update-server.sh"; exit 1; }

log "Updating server.py..."
cp "${SCRIPT_DIR}/server.py" "${INSTALL_DIR}/server.py"
chown hanryxvault:hanryxvault "${INSTALL_DIR}/server.py"

log "Updating Python dependencies..."
"${INSTALL_DIR}/venv/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"

log "Restarting HanryxVault POS server..."
systemctl restart hanryxvault
sleep 2

if systemctl is-active --quiet hanryxvault; then
  log "Server restarted successfully."
  echo ""
  curl -s http://localhost:8080/health | python3 -m json.tool 2>/dev/null || true
else
  echo "Server failed to start — check logs:"
  journalctl -u hanryxvault -n 30 --no-pager
fi
