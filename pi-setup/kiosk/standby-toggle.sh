#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# HanryxVault Kiosk — Standby toggle (F9)
#
# Toggles all connected HDMI monitors between ON and OFF.
#
# Strategy:
#   1. Try DPMS (xset).  Pi 5 + Bookworm X11 usually lacks the DPMS extension,
#      in which case xset prints "server does not have extension..." and we
#      fall through silently.
#   2. Fall back to `xrandr --output <name> --off` / `--auto`, which works on
#      every modern KMS-driven X server.
#
# State is persisted in /tmp/hanryxvault-standby.state so successive F9
# presses reliably alternate ON/OFF without polling display state.
# ─────────────────────────────────────────────────────────────────────────────

export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-/home/ngansen/.Xauthority}"

STATE_FILE="/tmp/hanryxvault-standby.state"
[[ -f "$STATE_FILE" ]] && CURRENT="$(cat "$STATE_FILE")" || CURRENT="on"

if [[ "$CURRENT" == "on" ]]; then
    NEXT="off"
else
    NEXT="on"
fi

# ── 1. Try DPMS (silent if unsupported) ──────────────────────────────────────
if [[ "$NEXT" == "off" ]]; then
    xset +dpms       2>/dev/null && xset dpms force off 2>/dev/null
else
    xset dpms force on 2>/dev/null
fi

# ── 2. Fall back to xrandr per-output toggle (always works on KMS) ───────────
mapfile -t OUTPUTS < <(xrandr 2>/dev/null | awk '/ connected/ {print $1}')
for o in "${OUTPUTS[@]}"; do
    if [[ "$NEXT" == "off" ]]; then
        xrandr --output "$o" --off 2>/dev/null
    else
        xrandr --output "$o" --auto 2>/dev/null
    fi
done

echo "$NEXT" > "$STATE_FILE"
echo "[standby-toggle] monitors -> $NEXT  (outputs: ${OUTPUTS[*]:-none})"
