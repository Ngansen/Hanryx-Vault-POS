#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# diagnostics-grafana-kiosk.sh — launcher for the 7" admin diagnostics screen
#
# Replaces the old tkinter desktop_monitor.py kiosk with a Chromium kiosk
# pointed at the auto-provisioned Grafana "HanryxVault Pi Operator" dashboard.
# Same screen, same purpose (live system health for the operator at the
# booth) — just driven by Grafana + Prometheus instead of a hand-rolled
# Python TK app, so we get historical graphs, alerts, and zero-maintenance
# panels.
#
# Behaviour:
#   1. Waits for the X server to be ready (LXDE autostart can fire before
#      DISPLAY :0 is fully up on a Pi 5).
#   2. Waits for Grafana to answer 200 on /grafana/api/health via the local
#      NPM proxy — avoids Chromium loading an error page if monitoring
#      stack is still starting after a cold boot.
#   3. Disables screen blanking / DPMS so the dashboard stays visible
#      24/7 at the trade-show booth.
#   4. Launches Chromium in kiosk mode at the dashboard URL with the
#      &kiosk Grafana flag (hides chrome) and &refresh=10s (live tiles).
#   5. Watchdog loop: if Chromium ever exits, wait 5 s and relaunch.
#      Prevents a black screen if Chromium crashes mid-show.
#
# Logs:  /tmp/grafana-kiosk.log  (truncated on every boot)
# ─────────────────────────────────────────────────────────────────────────────
set -u

LOG=/tmp/grafana-kiosk.log
URL="http://localhost:3000/d/hanryx-pi-ops/hanryxvault-pi-operator?orgId=1&refresh=10s&kiosk&theme=dark"
USER_DATA_DIR=/tmp/chromium-grafana
HEALTH_URL="http://localhost:3000/api/health"

: > "$LOG"
exec >>"$LOG" 2>&1
echo "[$(date -Is)] diagnostics-grafana-kiosk starting"

export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-/home/$(id -un)/.Xauthority}"

# 1. Wait for X (max 60 s)
for i in $(seq 1 60); do
    if xset q >/dev/null 2>&1; then
        echo "[$(date -Is)] X ready on $DISPLAY after ${i}s"
        break
    fi
    sleep 1
done

# 2. Wait for Grafana to answer 200 (max 120 s — monitoring stack starts
#    after the POS containers on a cold boot)
for i in $(seq 1 120); do
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 "$HEALTH_URL" || echo 000)
    if [[ "$code" == "200" ]]; then
        echo "[$(date -Is)] Grafana healthy after ${i}s"
        break
    fi
    sleep 1
done

# 3. Disable blanking / DPMS — booth screen must never sleep.
xset s off          2>/dev/null || true
xset -dpms          2>/dev/null || true
xset s noblank      2>/dev/null || true
unclutter -idle 0.5 -root >/dev/null 2>&1 &

# Resolve chromium binary (Bookworm = chromium, older RPi OS = chromium-browser)
CHROMIUM=""
for c in chromium chromium-browser; do
    if command -v "$c" >/dev/null 2>&1; then
        CHROMIUM="$c"
        break
    fi
done
if [[ -z "$CHROMIUM" ]]; then
    echo "[$(date -Is)] FATAL: no chromium binary on PATH" >&2
    exit 1
fi
echo "[$(date -Is)] using $CHROMIUM"

# 4 + 5. Watchdog loop — relaunch Chromium if it ever dies.
while true; do
    echo "[$(date -Is)] launching $CHROMIUM kiosk"
    "$CHROMIUM" \
        --kiosk \
        --noerrdialogs \
        --disable-infobars \
        --disable-translate \
        --disable-features=TranslateUI \
        --check-for-update-interval=31536000 \
        --overscroll-history-navigation=0 \
        --disable-pinch \
        --no-first-run \
        --user-data-dir="$USER_DATA_DIR" \
        --autoplay-policy=no-user-gesture-required \
        "$URL"
    rc=$?
    echo "[$(date -Is)] chromium exited rc=$rc — relaunching in 5 s"
    sleep 5
done
