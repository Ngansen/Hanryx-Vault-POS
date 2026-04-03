#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# HanryxVault Kiosk — X session startup script
#
# Called by xinit.  Runs inside the bare X server with no desktop environment.
# Configures the display, waits for the POS server to become healthy, then
# launches the admin monitor in fullscreen kiosk mode.
# ─────────────────────────────────────────────────────────────────────────────

INSTALL_DIR="${INSTALL_DIR:-/opt/hanryxvault}"
MONITOR_PY="$INSTALL_DIR/desktop_monitor.py"
PYTHON="${PYTHON:-$(command -v python3)}"
HEALTH_URL="http://127.0.0.1:8080/health"
MAX_WAIT=120   # seconds to wait for the server before giving up

# ── Display hardening ────────────────────────────────────────────────────────
xset s off          # disable screensaver
xset s noblank      # don't blank the screen
xset -dpms          # disable DPMS power-saving (monitor stays on)

# Hide idle mouse cursor (requires unclutter)
if command -v unclutter &>/dev/null; then
    unclutter -idle 3 -root &
fi

# Optional: set a black wallpaper/background
if command -v xsetroot &>/dev/null; then
    xsetroot -solid black
fi

# ── Wait for POS server ──────────────────────────────────────────────────────
echo "[kiosk] Waiting for POS server at $HEALTH_URL …"
waited=0
until curl -sf "$HEALTH_URL" > /dev/null 2>&1; do
    if (( waited >= MAX_WAIT )); then
        echo "[kiosk] Server did not become ready within ${MAX_WAIT}s — launching anyway"
        break
    fi
    sleep 2
    (( waited += 2 ))
done
echo "[kiosk] Server ready after ${waited}s — launching monitor"

# ── Launch the admin monitor ─────────────────────────────────────────────────
exec "$PYTHON" "$MONITOR_PY" --kiosk
