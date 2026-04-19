#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# HanryxVault Kiosk — X session startup script
#
# Called by xinit. Runs inside the bare X server with no desktop environment.
# Configures the display, waits for the POS server to become healthy, then
# launches the full /kiosk page (YouTube idle, sales feed, broken-iframe
# hardening, nightly auto-recycle) in Chromium kiosk mode.
#
# Set KIOSK_URL=/admin in /etc/default/hanryxvault-kiosk to show the admin
# dashboard instead, or KIOSK_URL=http://other-host:8080/kiosk to point at
# a different host (e.g. on a satellite without a local nginx).
# ─────────────────────────────────────────────────────────────────────────────

# Optional config override
[[ -f /etc/default/hanryxvault-kiosk ]] && . /etc/default/hanryxvault-kiosk

INSTALL_DIR="${INSTALL_DIR:-/opt/hanryxvault}"
KIOSK_URL="${KIOSK_URL:-http://127.0.0.1:8080/kiosk}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8080/health}"
PROFILE_DIR="${PROFILE_DIR:-$HOME/.config/hanryxvault-chromium}"
MAX_WAIT=120

# ── Display hardening ────────────────────────────────────────────────────────
xset s off          # disable screensaver
xset s noblank      # don't blank the screen
xset -dpms          # disable DPMS power-saving (monitor stays on)

# Hide idle mouse cursor (requires unclutter)
if command -v unclutter &>/dev/null; then
    unclutter -idle 3 -root &
fi

# Black background
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
echo "[kiosk] Server ready after ${waited}s — launching Chromium → $KIOSK_URL"

# ── Pick a chromium binary ───────────────────────────────────────────────────
CHROMIUM=""
for c in chromium-browser chromium google-chrome chrome; do
    if command -v "$c" &>/dev/null; then
        CHROMIUM="$c"
        break
    fi
done

if [[ -z "$CHROMIUM" ]]; then
    echo "[kiosk] ERROR: no chromium/chrome binary found. Install with:"
    echo "        sudo apt-get install -y chromium-browser"
    sleep 30
    exit 1
fi

mkdir -p "$PROFILE_DIR"

# Clear any "browser was closed unexpectedly" prompt from a hard reboot
sed -i 's/"exited_cleanly":false/"exited_cleanly":true/'   "$PROFILE_DIR/Default/Preferences" 2>/dev/null || true
sed -i 's/"exit_type":"Crashed"/"exit_type":"Normal"/'      "$PROFILE_DIR/Default/Preferences" 2>/dev/null || true

# ── Launch Chromium in kiosk mode ────────────────────────────────────────────
exec "$CHROMIUM" \
    --kiosk \
    --noerrdialogs \
    --disable-infobars \
    --disable-translate \
    --disable-features=TranslateUI,Notifications \
    --no-first-run \
    --start-maximized \
    --autoplay-policy=no-user-gesture-required \
    --disable-pinch \
    --overscroll-history-navigation=0 \
    --check-for-update-interval=31536000 \
    --user-data-dir="$PROFILE_DIR" \
    --app="$KIOSK_URL"
