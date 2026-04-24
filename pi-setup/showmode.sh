#!/usr/bin/env bash
#
# showmode.sh — Card-show mode for HanryxVault POS
#
# Brings up the Pi's Wi-Fi access point (HanryxVault-Show), verifies the
# printer is reachable, checks for internet via iPhone USB tether, and
# restarts the POS container so it picks up any IP changes.
#
# Usage:
#   sudo ./showmode.sh             # full check + bring everything up
#   sudo ./showmode.sh --status    # just print current state, no changes
#   sudo ./showmode.sh --down      # tear down AP, return to home Wi-Fi
#

set -u

AP_NAME="HanryxVault-AP"
AP_SSID="HanryxVault-Show"
AP_IP="10.42.0.1"
PRINTER_IP="${PRINTER_NETWORK_HOST:-10.42.0.50}"
PRINTER_PORT="${PRINTER_NETWORK_PORT:-9100}"
COMPOSE_DIR="$(cd "$(dirname "$0")" && pwd)"

# ----- colors --------------------------------------------------------------
if [ -t 1 ]; then
  G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[1;34m'; N='\033[0m'
else
  G=''; Y=''; R=''; B=''; N=''
fi

ok()    { printf "${G}[ OK ]${N} %s\n" "$*"; }
warn()  { printf "${Y}[WARN]${N} %s\n" "$*"; }
fail()  { printf "${R}[FAIL]${N} %s\n" "$*"; }
info()  { printf "${B}[INFO]${N} %s\n" "$*"; }
hdr()   { printf "\n${B}==== %s ====${N}\n" "$*"; }

# ----- root check ----------------------------------------------------------
if [ "$EUID" -ne 0 ]; then
  fail "Run with sudo: sudo $0 $*"
  exit 1
fi

# ----- helpers -------------------------------------------------------------
ap_is_active() {
  nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null \
    | grep -q "^${AP_NAME}:"
}

ap_exists() {
  nmcli -t -f NAME connection show 2>/dev/null | grep -q "^${AP_NAME}$"
}

internet_iface() {
  # First non-wlan0, non-loopback interface that has a default route.
  ip -4 route show default 2>/dev/null \
    | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}' \
    | grep -v '^wlan0$' | head -n1
}

# ----- modes ---------------------------------------------------------------
mode_status() {
  hdr "Current state"
  if ap_is_active; then
    ok "AP '${AP_SSID}' is UP on $(nmcli -t -f NAME,DEVICE connection show --active | awk -F: -v n="${AP_NAME}" '$1==n{print $2}')"
  else
    warn "AP '${AP_SSID}' is DOWN"
  fi

  if ip -4 addr show wlan0 2>/dev/null | grep -q "${AP_IP}"; then
    ok "wlan0 has AP IP ${AP_IP}"
  else
    warn "wlan0 does NOT have ${AP_IP}"
  fi

  iface=$(internet_iface)
  if [ -n "${iface}" ]; then
    ok "Internet route via ${iface}"
  else
    warn "No upstream internet route (Tailscale + cloud sync OFFLINE)"
  fi

  if ping -c 1 -W 1 "${PRINTER_IP}" >/dev/null 2>&1; then
    ok "Printer ${PRINTER_IP} responds to ping"
  else
    warn "Printer ${PRINTER_IP} not reachable"
  fi

  if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^pos$'; then
    ok "POS container is running"
  else
    fail "POS container is NOT running"
  fi
}

mode_down() {
  hdr "Tearing down show-mode AP"
  if ap_is_active; then
    nmcli connection down "${AP_NAME}" && ok "AP stopped" || fail "Could not stop AP"
  else
    info "AP wasn't running"
  fi
  info "Pi will rejoin home Wi-Fi if in range"
}

mode_up() {
  hdr "Bringing up show-mode AP"

  # Sanity check: AP profile must exist (created during one-time setup).
  if ! ap_exists; then
    fail "Connection profile '${AP_NAME}' not found."
    fail "Run the one-time setup first:"
    cat <<EOF

  sudo nmcli connection add type wifi ifname wlan0 con-name ${AP_NAME} \\
       autoconnect no ssid ${AP_SSID}
  sudo nmcli connection modify ${AP_NAME} 802-11-wireless.mode ap \\
       802-11-wireless.band bg ipv4.method shared
  sudo nmcli connection modify ${AP_NAME} wifi-sec.key-mgmt wpa-psk \\
       wifi-sec.psk "ChangeMeAt2026"
  sudo nmcli connection modify ${AP_NAME} ipv4.addresses ${AP_IP}/24
  sudo nmcli connection modify ${AP_NAME} autoconnect yes \\
       connection.autoconnect-priority 100

EOF
    exit 1
  fi

  # Disconnect from any client Wi-Fi on wlan0 so AP mode can take it.
  active_wlan_client=$(nmcli -t -f NAME,DEVICE,TYPE connection show --active \
    | awk -F: -v n="${AP_NAME}" '$3=="802-11-wireless" && $2=="wlan0" && $1!=n {print $1}')
  if [ -n "${active_wlan_client}" ]; then
    info "Disconnecting client Wi-Fi: ${active_wlan_client}"
    nmcli connection down "${active_wlan_client}" >/dev/null 2>&1
  fi

  # Bring the AP up.
  if ap_is_active; then
    info "AP already active"
  else
    if nmcli connection up "${AP_NAME}" >/dev/null 2>&1; then
      ok "AP ${AP_SSID} is broadcasting on ${AP_IP}"
    else
      fail "Failed to bring up AP — check 'sudo journalctl -u NetworkManager -n 30'"
      exit 1
    fi
  fi

  # Show what clients can connect to.
  hdr "Tablet / printer connection info"
  echo "  SSID:        ${AP_SSID}"
  echo "  Pi address:  http://${AP_IP}:8080  (use this in tablet POS settings)"
  echo "  DHCP range:  ${AP_IP%.*}.10 – ${AP_IP%.*}.254"

  # Internet check (iPhone USB tether or anything else).
  hdr "Upstream internet (for Tailscale + cloud sync)"
  iface=$(internet_iface)
  if [ -n "${iface}" ]; then
    ok "Internet via ${iface}"
    if ping -c 1 -W 2 1.1.1.1 >/dev/null 2>&1; then
      ok "Reached 1.1.1.1"
    else
      warn "${iface} has a route but ping to 1.1.1.1 failed"
    fi
  else
    warn "No internet — POS will run OFFLINE"
    warn "Plug iPhone into Pi via USB and turn on Personal Hotspot for upstream internet"
  fi

  # Printer check.
  hdr "Printer ${PRINTER_IP}:${PRINTER_PORT}"
  # Give DHCP a moment if printer just powered on with the AP.
  for i in 1 2 3 4 5; do
    if ping -c 1 -W 1 "${PRINTER_IP}" >/dev/null 2>&1; then
      ok "Printer responds to ping (attempt ${i})"
      break
    fi
    [ "${i}" -eq 5 ] && warn "Printer ${PRINTER_IP} not reachable — power-cycle it and re-run" || sleep 2
  done

  if command -v nc >/dev/null 2>&1; then
    if nc -z -w 2 "${PRINTER_IP}" "${PRINTER_PORT}" 2>/dev/null; then
      ok "Printer port ${PRINTER_PORT} is open"
    else
      warn "Printer port ${PRINTER_PORT} not open — check printer Wi-Fi config"
    fi
  fi

  # Restart POS container so it re-reads the network and printer env.
  hdr "POS container"
  if [ -f "${COMPOSE_DIR}/docker-compose.yml" ]; then
    cd "${COMPOSE_DIR}" || exit 1
    if docker ps --format '{{.Names}}' | grep -q '^pos$'; then
      info "Restarting pos container so it picks up new network state…"
      docker compose restart pos >/dev/null 2>&1 \
        && ok "pos restarted" \
        || fail "Could not restart pos container"
    else
      info "pos container not running — starting it"
      docker compose up -d pos >/dev/null 2>&1 \
        && ok "pos started" \
        || fail "Could not start pos — run 'docker compose up -d' manually"
    fi
  else
    warn "docker-compose.yml not found at ${COMPOSE_DIR} — skipping container restart"
  fi

  # Final API health check.
  hdr "POS API health"
  for i in 1 2 3 4 5 6 7 8; do
    if curl -fsS --max-time 2 "http://${AP_IP}:8080/health" >/dev/null 2>&1; then
      ok "POS API responding at http://${AP_IP}:8080"
      break
    fi
    [ "${i}" -eq 8 ] && fail "POS API never came up — check 'docker compose logs pos --tail=40'" || sleep 2
  done

  hdr "READY FOR THE SHOW"
  echo "  1. Tablet → connect to Wi-Fi '${AP_SSID}'"
  echo "  2. Tablet POS app → set Pi URL to http://${AP_IP}:8080"
  echo "  3. Take the first sale."
  echo
}

# ----- dispatch ------------------------------------------------------------
case "${1:-up}" in
  up)        mode_up; mode_status ;;
  down)      mode_down ;;
  --down)    mode_down ;;
  status)    mode_status ;;
  --status)  mode_status ;;
  *)
    cat <<EOF
Usage: sudo $0 [up|status|down]

  up      (default) Bring AP up, verify printer, restart POS container
  status  Print current state, change nothing
  down    Stop AP, return wlan0 to home Wi-Fi
EOF
    exit 1
    ;;
esac
