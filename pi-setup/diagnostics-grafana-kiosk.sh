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

# ── Hostname guard ──────────────────────────────────────────────────────────
# This kiosk is for the MAIN pi's 7" admin diagnostic screen ONLY. Grafana
# binds to localhost:3001 only on the main pi; on the satellite localhost:3001
# does not exist and chromium shows ERR_CONNECTION_REFUSED on whichever HDMI
# output labwc happens to place it on. Bail loudly so this never silently
# hijacks a satellite kiosk screen again.
HOSTNAME_NOW="$(hostname -s 2>/dev/null || hostname)"
case "$HOSTNAME_NOW" in
  hanryxvault) ;;  # main pi — proceed
  *)
    echo "[diagnostics-grafana-kiosk] refusing to run on host '$HOSTNAME_NOW'" >&2
    echo "[diagnostics-grafana-kiosk] this script is for the MAIN pi (hanryxvault) ONLY" >&2
    echo "[diagnostics-grafana-kiosk] grafana is at localhost:3001 only on the main pi" >&2
    exit 2
    ;;
esac

LOG=/tmp/grafana-kiosk.log
URL="http://localhost:3001/d/hanryx-pi-ops/hanryxvault-pi-operator?orgId=1&refresh=10s&kiosk&theme=dark"
USER_DATA_DIR=/tmp/chromium-grafana
HEALTH_URL="http://localhost:3001/api/health"
LOCK_FILE=/tmp/grafana-kiosk.lock

# Single-instance guard — multiple launchers would fight over USER_DATA_DIR
# and cause "Opening in existing browser session" / rc=0 crash loops.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[$(date -Is)] another grafana-kiosk launcher holds the lock — exiting" >> "$LOG"
    exit 0
fi

# Override Pi OS's /etc/chromium.d/* default flags. They include flags that
# newer chromium versions (147+) reject (e.g. --no-decommit-pooled-pages),
# causing chromium to exit immediately every launch. We control all flags
# explicitly below.
export CHROMIUM_FLAGS=""

: > "$LOG"
exec >>"$LOG" 2>&1
echo "[$(date -Is)] diagnostics-grafana-kiosk starting (PID $$)"

export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-/home/$(id -un)/.Xauthority}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

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
#    X11 path (XWayland):
xset s off          2>/dev/null || true
xset -dpms          2>/dev/null || true
xset s noblank      2>/dev/null || true
unclutter -idle 0.5 -root >/dev/null 2>&1 &
#    Wayland path (wlroots/labwc): the compositor can blank outputs at the
#    wlroots level independently of X11 DPMS. Re-enable all connected
#    outputs every 55 s so the dashboard stays visible even when no
#    pointer/keyboard events reach the compositor's idle timer.
#    chromium --ozone-platform=wayland (below) also sends the
#    zwp_idle_inhibit_manager_v1 protocol in fullscreen/kiosk mode, which
#    is the proper first-line defence; this loop is belt-and-suspenders.
if command -v wlr-randr >/dev/null 2>&1 && [[ -n "${WAYLAND_DISPLAY:-}" ]]; then
    (while true; do
        wlr-randr 2>/dev/null \
            | awk '/^[A-Z]/{out=$1} / enabled/{print out}' \
            | xargs -r -I{} wlr-randr --output {} --on 2>/dev/null
        sleep 55
    done) &
fi

# Resolve chromium binary. Prefer the REAL binary at /usr/lib/chromium/chromium
# over the /usr/bin/chromium wrapper script — the Pi OS wrapper adds default
# flags (--no-decommit-pooled-pages, --force-renderer-accessibility, etc.)
# that newer chromium versions reject, causing immediate exit.
CHROMIUM=""
for c in /usr/lib/chromium/chromium /usr/lib/chromium-browser/chromium-browser chromium chromium-browser; do
    if [[ -x "$c" ]] || command -v "$c" >/dev/null 2>&1; then
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
    # Clear stale singleton files left by unclean shutdowns (power cuts etc.)
    # Without this, chromium prints "Opening in existing browser session" and
    # exits rc=0 on every launch loop iteration after an abrupt power loss.
    rm -f "${USER_DATA_DIR}/SingletonLock" \
          "${USER_DATA_DIR}/SingletonCookie" \
          "${USER_DATA_DIR}/SingletonSocket"
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
        --password-store=basic \
        --use-mock-keychain \
        --autoplay-policy=no-user-gesture-required \
        --ozone-platform=wayland \
        --enable-features=UseOzonePlatform,VaapiVideoDecoder \
        --disable-dev-shm-usage \
        "$URL"
    rc=$?
    echo "[$(date -Is)] chromium exited rc=$rc — relaunching in 5 s"
    sleep 5
done
