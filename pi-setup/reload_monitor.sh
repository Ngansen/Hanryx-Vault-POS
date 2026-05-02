#!/usr/bin/env bash
# reload_monitor.sh — pull + restart the admin desktop_monitor.py kiosk.
#
# Replaces the 200-character one-liner the operator has been pasting
# every time they push a Diagnostics tab tweak from the workstation.
#
# What it does, in order:
#   1. cd into the repo and `git pull --ff-only` (refuses to fast-forward
#      past local commits — safer than a plain `git pull`, which would
#      silently auto-merge on a divergence).
#   2. SIGTERM any running `desktop_monitor.py` processes, wait up to 3 s
#      for clean exit, then SIGKILL whatever's still alive.
#   3. Re-launch via `nohup python3 …  --kiosk` with the X11 env vars
#      the kiosk needs (DISPLAY=:0, XAUTHORITY=/home/<user>/.Xauthority).
#      Output is redirected to /tmp/monitor.log so the next reload can
#      show its tail on failure.
#   4. Wait 2 s and confirm the new PID is still alive. If it died at
#      startup (import error, syntax error, X11 grab failure, etc.)
#      the script dumps the last 25 lines of /tmp/monitor.log and
#      exits non-zero so an SSH session can see the failure instead
#      of returning a misleading green prompt.
#
# Usage:
#   bash ~/Hanryx-Vault-POS/pi-setup/reload_monitor.sh
#   bash ~/Hanryx-Vault-POS/pi-setup/reload_monitor.sh --no-pull
#   bash ~/Hanryx-Vault-POS/pi-setup/reload_monitor.sh --no-kiosk
#
# Flags (all optional, can combine):
#   --no-pull      skip the `git pull` step (use when iterating on a
#                  local edit that hasn't been pushed yet)
#   --no-kiosk     launch without --kiosk (useful for debugging when
#                  you want a normal-window monitor instead of the
#                  fullscreen tkinter takeover)
#
# Exit codes:
#   0 — new monitor is alive after the 2 s settle window
#   1 — bad CLI argument
#   2 — git pull failed (non-fast-forward, no network, etc.)
#   3 — pkill+SIGKILL still left a process running (very rare; would
#       indicate the process is in uninterruptible sleep waiting on
#       stuck I/O — usually means the USB dock has detached mid-read)
#   4 — new monitor crashed within 2 s of launch (tail of log shown)

set -u
set -o pipefail

REPO_DIR="${HOME}/Hanryx-Vault-POS"
SCRIPT="${REPO_DIR}/pi-setup/desktop_monitor.py"
LOGFILE="/tmp/monitor.log"
DISPLAY_VAR="${DISPLAY:-:0}"
XAUTH_VAR="${XAUTHORITY:-${HOME}/.Xauthority}"

DO_PULL=1
KIOSK_FLAG="--kiosk"

# ── parse flags ───────────────────────────────────────────────────────
for arg in "$@"; do
    case "$arg" in
        --no-pull)  DO_PULL=0 ;;
        --no-kiosk) KIOSK_FLAG="" ;;
        -h|--help)
            sed -n '2,40p' "$0"
            exit 0
            ;;
        *)
            echo "reload_monitor: unknown flag: $arg" >&2
            echo "valid flags: --no-pull --no-kiosk -h --help" >&2
            exit 1
            ;;
    esac
done

# ── 1. git pull ───────────────────────────────────────────────────────
if [[ $DO_PULL -eq 1 ]]; then
    echo "[reload_monitor] pulling latest from origin/main…"
    if ! git -C "$REPO_DIR" pull --ff-only; then
        echo "[reload_monitor] FATAL: git pull --ff-only failed." >&2
        echo "  Check for local commits that diverge from origin/main:" >&2
        echo "    git -C $REPO_DIR status" >&2
        echo "    git -C $REPO_DIR log --oneline -5" >&2
        echo "  If you want to skip the pull and use the on-disk file as-is:" >&2
        echo "    bash $0 --no-pull" >&2
        exit 2
    fi
else
    echo "[reload_monitor] --no-pull → using on-disk file as-is"
fi

# ── 2. SIGTERM existing, wait, then SIGKILL stragglers ────────────────
old_pids=$(pgrep -f 'python3?.*desktop_monitor\.py' || true)
if [[ -n "$old_pids" ]]; then
    echo "[reload_monitor] stopping existing processes: $old_pids"
    # SIGTERM gives the tkinter loop a chance to tear down windows
    # cleanly so we don't leave orphan X11 grabs.
    # shellcheck disable=SC2086  # word-splitting intentional here
    kill $old_pids 2>/dev/null || true
    # Poll up to 3 s for graceful exit.
    for _ in 1 2 3 4 5 6; do
        sleep 0.5
        still=$(pgrep -f 'python3?.*desktop_monitor\.py' || true)
        [[ -z "$still" ]] && break
    done
    # If anything's still alive, escalate to SIGKILL.
    still=$(pgrep -f 'python3?.*desktop_monitor\.py' || true)
    if [[ -n "$still" ]]; then
        echo "[reload_monitor] SIGTERM ignored after 3 s, escalating to SIGKILL: $still"
        # shellcheck disable=SC2086
        kill -9 $still 2>/dev/null || true
        sleep 0.5
        still=$(pgrep -f 'python3?.*desktop_monitor\.py' || true)
        if [[ -n "$still" ]]; then
            echo "[reload_monitor] FATAL: process still alive after SIGKILL: $still" >&2
            echo "  Likely cause: uninterruptible-sleep on stuck I/O" >&2
            echo "  (USB dock unplugged mid-read, NFS hang, etc.)" >&2
            exit 3
        fi
    fi
else
    echo "[reload_monitor] no existing monitor process to stop"
fi

# ── 3. relaunch ───────────────────────────────────────────────────────
if [[ ! -f "$SCRIPT" ]]; then
    echo "[reload_monitor] FATAL: script not found: $SCRIPT" >&2
    exit 4
fi

echo "[reload_monitor] launching: DISPLAY=$DISPLAY_VAR XAUTHORITY=$XAUTH_VAR"
echo "                 python3 $SCRIPT $KIOSK_FLAG  →  $LOGFILE"

# Redirect-to-logfile + nohup + & + disown so the new process
# survives this script's exit AND the SSH session's exit.
DISPLAY="$DISPLAY_VAR" XAUTHORITY="$XAUTH_VAR" \
    nohup python3 "$SCRIPT" $KIOSK_FLAG \
        > "$LOGFILE" 2>&1 &
new_pid=$!
disown 2>/dev/null || true

# ── 4. settle window + liveness check ─────────────────────────────────
sleep 2
if kill -0 "$new_pid" 2>/dev/null; then
    echo "[reload_monitor] OK — new pid $new_pid is alive"
    echo "                 tail log with: tail -f $LOGFILE"
    exit 0
else
    echo "[reload_monitor] FATAL: new process (pid $new_pid) died within 2 s of launch." >&2
    echo "  Last 25 lines of $LOGFILE:" >&2
    echo "  ----------------------------------------" >&2
    tail -25 "$LOGFILE" >&2 || echo "    (log file empty or unreadable)" >&2
    echo "  ----------------------------------------" >&2
    exit 4
fi
