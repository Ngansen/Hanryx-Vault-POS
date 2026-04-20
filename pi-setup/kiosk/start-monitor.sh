#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# HanryxVault Kiosk — X session startup script (dual-monitor capable)
#
# Called by xinit. Runs inside a bare X server with no desktop environment.
# Configures the displays, waits for the POS server to become healthy, then
# launches one Chromium kiosk window per connected monitor.
#
# Default layout:
#   Left  monitor → KIOSK_LEFT_URL   (default: /kiosk — customer-facing)
#   Right monitor → KIOSK_RIGHT_URL  (default: /admin — staff dashboard)
#
# Configurable via /etc/default/hanryxvault-kiosk:
#   POS_HOST=http://192.168.86.36:8080      # base URL for both windows
#   KIOSK_LEFT_URL=$POS_HOST/kiosk          # full URL for left monitor
#   KIOSK_RIGHT_URL=$POS_HOST/admin         # full URL for right monitor
#   KIOSK_LEFT_OUTPUT=HDMI-1                # xrandr output name (auto if blank)
#   KIOSK_RIGHT_OUTPUT=HDMI-2               # xrandr output name (auto if blank)
#   KIOSK_URL=...                           # legacy single-monitor override
# ─────────────────────────────────────────────────────────────────────────────

# Optional config override
[[ -f /etc/default/hanryxvault-kiosk ]] && . /etc/default/hanryxvault-kiosk

POS_HOST="${POS_HOST:-http://127.0.0.1:8080}"
KIOSK_LEFT_URL="${KIOSK_LEFT_URL:-${KIOSK_URL:-$POS_HOST/kiosk}}"
KIOSK_RIGHT_URL="${KIOSK_RIGHT_URL:-$POS_HOST/admin}"
HEALTH_URL="${HEALTH_URL:-$POS_HOST/health}"
PROFILE_DIR="${PROFILE_DIR:-$HOME/.config/hanryxvault-chromium}"
MAX_WAIT=120

# ── Display hardening ────────────────────────────────────────────────────────
xset s off
xset s noblank
xset -dpms

if command -v unclutter &>/dev/null; then
    unclutter -idle 3 -root &
fi
if command -v xsetroot &>/dev/null; then
    xsetroot -solid black
fi

# ── Hotkey daemon (F9 = standby toggle) ──────────────────────────────────────
if command -v xbindkeys &>/dev/null; then
    XBK_RC="$HOME/.xbindkeysrc"
    cat > "$XBK_RC" <<EOF
"/opt/hanryxvault/kiosk/standby-toggle.sh"
    F9
EOF
    pkill -x xbindkeys 2>/dev/null || true
    xbindkeys -f "$XBK_RC"
    echo "[kiosk] xbindkeys started — F9 toggles monitor standby"
else
    echo "[kiosk] xbindkeys not installed — F9 standby hotkey disabled"
    echo "        install with: sudo apt-get install -y xbindkeys"
fi

# ── Detect monitors and lay them out side-by-side ────────────────────────────
mapfile -t CONNECTED < <(xrandr | awk '/ connected/ {print $1}')
echo "[kiosk] xrandr connected outputs: ${CONNECTED[*]:-<none>}"

LEFT_OUTPUT="${KIOSK_LEFT_OUTPUT:-${CONNECTED[0]:-}}"
RIGHT_OUTPUT="${KIOSK_RIGHT_OUTPUT:-${CONNECTED[1]:-}}"

if [[ -n "$LEFT_OUTPUT" ]]; then
    xrandr --output "$LEFT_OUTPUT" --auto --pos 0x0 --primary 2>/dev/null || true
fi
if [[ -n "$RIGHT_OUTPUT" && "$RIGHT_OUTPUT" != "$LEFT_OUTPUT" ]]; then
    xrandr --output "$RIGHT_OUTPUT" --auto --right-of "$LEFT_OUTPUT" 2>/dev/null || true
fi

# Re-read geometry after xrandr changes
get_geom() {
    # echoes "WIDTH HEIGHT XOFFSET YOFFSET" for the given output
    xrandr | awk -v o="$1" '$1==o && /connected/ {
        for (i=1;i<=NF;i++) if ($i ~ /^[0-9]+x[0-9]+\+[0-9]+\+[0-9]+/) { print $i; exit }
    }' | sed -E 's/x|\+/ /g'
}

LEFT_GEOM=$(get_geom "$LEFT_OUTPUT")
RIGHT_GEOM=$(get_geom "$RIGHT_OUTPUT")

# Fallback: ask X for the screen dimensions if xrandr parsing failed.
# This catches small touch panels (10.1" = 1280x800, 7" = 1024x600) where
# the 1920x1080 default would render off-screen and look "cropped".
if [[ -z "$LEFT_GEOM" ]] && command -v xdpyinfo &>/dev/null; then
    SCREEN_DIM=$(xdpyinfo 2>/dev/null | awk '/dimensions:/ {print $2}' | head -1)
    if [[ "$SCREEN_DIM" =~ ^([0-9]+)x([0-9]+)$ ]]; then
        LEFT_GEOM="${BASH_REMATCH[1]} ${BASH_REMATCH[2]} 0 0"
        echo "[kiosk] xrandr geom missing — using xdpyinfo screen dims: $SCREEN_DIM"
    fi
fi

read -r LW LH LX LY <<<"${LEFT_GEOM:-0 0 0 0}"
read -r RW RH RX RY <<<"${RIGHT_GEOM:-0 0 0 0}"
echo "[kiosk] Left  output=$LEFT_OUTPUT  geom=${LW}x${LH}+${LX}+${LY}  url=$KIOSK_LEFT_URL"
echo "[kiosk] Right output=$RIGHT_OUTPUT geom=${RW}x${RH}+${RX}+${RY}  url=$KIOSK_RIGHT_URL"

# How many actual displays are we driving?
DISPLAY_COUNT=0
(( LW > 0 )) && (( DISPLAY_COUNT++ ))
(( RW > 0 )) && (( DISPLAY_COUNT++ ))
echo "[kiosk] Driving $DISPLAY_COUNT display(s)"

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
echo "[kiosk] Server ready after ${waited}s — launching Chromium"

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
echo "[kiosk] Using browser: $CHROMIUM"

# Common Chromium flags
# Memory / GPU-conservative for Pi 5 — reduces YouTube iframe renderer crashes.
COMMON_FLAGS=(
    --noerrdialogs
    --disable-infobars
    --disable-translate
    --disable-features=TranslateUI,Notifications,CalculateNativeWinOcclusion
    --no-first-run
    --autoplay-policy=no-user-gesture-required
    --disable-pinch
    --overscroll-history-navigation=0
    --check-for-update-interval=31536000
    # Pi 5 hardening — prevents long-running renderer OOM with YouTube iframe
    --disable-software-rasterizer
    --enable-low-end-device-mode
    --disk-cache-size=33554432
    --media-cache-size=33554432
    --js-flags=--max-old-space-size=384
    --process-per-site
)

launch_window() {
    local url="$1" w="$2" h="$3" x="$4" y="$5" name="$6"
    local profile="$PROFILE_DIR/$name"
    mkdir -p "$profile"
    sed -i 's/"exited_cleanly":false/"exited_cleanly":true/' "$profile/Default/Preferences" 2>/dev/null || true
    sed -i 's/"exit_type":"Crashed"/"exit_type":"Normal"/'  "$profile/Default/Preferences" 2>/dev/null || true

    # Single-display setups (the satellite Pi with the 10.1" panel) use
    # --kiosk: chromium auto-fits the actual screen at native resolution.
    # This avoids the --window-size + --start-fullscreen interaction that
    # rendered the page at the wrong viewport on small touch panels and
    # showed only part of the kiosk UI.
    #
    # Multi-display setups MUST use --window-position + --window-size
    # because --kiosk grabs the whole virtual screen and you can't pin a
    # window to a specific monitor that way.
    if (( DISPLAY_COUNT <= 1 )); then
        "$CHROMIUM" "${COMMON_FLAGS[@]}" \
            --user-data-dir="$profile" \
            --kiosk \
            --app="$url" &
    else
        # Multi-display: --app gives borderless chrome, explicit
        # --window-position + --window-size pin the window to the right
        # monitor. Do NOT add --start-fullscreen here — it makes chromium
        # snap the window to whatever it considers "primary" (ignoring
        # --window-position) and then squeezes the rendered viewport into
        # that monitor's resolution, which on the 10.1" + 5" satellite
        # setup made the 10.1" show only a corner of the page.
        "$CHROMIUM" "${COMMON_FLAGS[@]}" \
            --user-data-dir="$profile" \
            --window-position="${x},${y}" \
            --window-size="${w},${h}" \
            --app="$url" &
    fi
}

# ── Launch one window per detected monitor ──────────────────────────────────
if (( LW > 0 )); then
    launch_window "$KIOSK_LEFT_URL"  "$LW" "$LH" "$LX" "$LY" "left"
fi
if (( RW > 0 )); then
    sleep 2  # stagger start so the second window gets focus on its own monitor
    launch_window "$KIOSK_RIGHT_URL" "$RW" "$RH" "$RX" "$RY" "right"
fi

# ── Watchdog ─────────────────────────────────────────────────────────────────
# Two failure modes we recover from:
#   1. A chromium child process exits  → wait -n returns → we exit → systemd
#      restarts the whole kiosk service (clean X + chromium).
#   2. The chromium browser process keeps running but a tab/renderer crashed
#      (the "Aw Snap" sad face).  JavaScript is dead so the page-level
#      YouTube watchdog can't recover.  We poll renderer count + uptime and
#      force-exit so systemd recycles.
MAX_UPTIME=14400      # 4 hours — proactive recycle even if nothing crashed
MIN_RENDERERS=1       # at least one renderer per window expected
EXPECTED_WINDOWS=$(( (LW > 0) + (RW > 0) ))
START_TS=$(date +%s)

(
    sleep 60   # let chromium settle before first check
    while sleep 30; do
        now=$(date +%s)
        uptime=$(( now - START_TS ))
        if (( uptime >= MAX_UPTIME )); then
            echo "[kiosk-watchdog] Uptime ${uptime}s >= ${MAX_UPTIME}s — recycling"
            pkill -TERM -f "$CHROMIUM" 2>/dev/null || true
            exit 0
        fi
        # Count renderer processes (one per visible tab/window)
        renderers=$(pgrep -fc 'chromium.*--type=renderer' || echo 0)
        if (( renderers < EXPECTED_WINDOWS * MIN_RENDERERS )); then
            echo "[kiosk-watchdog] Renderers=$renderers (expected >= $EXPECTED_WINDOWS) — recycling"
            pkill -TERM -f "$CHROMIUM" 2>/dev/null || true
            exit 0
        fi
    done
) &
WATCHDOG_PID=$!

# Keep the X session alive until any chromium exits, then exit (xinit will
# tear down X and systemd will restart us).
wait -n
echo "[kiosk] A Chromium window exited or watchdog tripped — shutting down session"
kill "$WATCHDOG_PID" 2>/dev/null || true
pkill -TERM -P $$ 2>/dev/null || true
exit 0
