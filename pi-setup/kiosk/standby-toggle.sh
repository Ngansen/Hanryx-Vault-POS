#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# HanryxVault Kiosk — Standby toggle
# Bound to F9 by xbindkeys.  Forces both monitors into DPMS standby.
# Any keypress / mouse movement wakes them back up automatically (handled by
# the X server, since DPMS is re-enabled briefly to allow the wake).
# ─────────────────────────────────────────────────────────────────────────────
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-/home/ngansen/.Xauthority}"

# Briefly enable DPMS so "force off" actually takes effect, then sleep so a
# wake event re-enables the screen normally.
xset +dpms
xset dpms force off
sleep 1
# Leave DPMS enabled with a long timeout so the wake can be triggered by any
# input device.  start-monitor.sh re-disables DPMS on next launch/restart.
xset dpms 0 0 0
