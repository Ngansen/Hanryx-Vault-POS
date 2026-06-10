#!/bin/sh
# Generic pidfile-guarded launcher.
#   Usage: guarded_launch.sh <slug> <python-script> [args...]
#   Pidfile = /tmp/<slug>.pid, log = /tmp/<slug>.log
# Why: docker compose exec -d + nohup + & double-spawned everything we
# launched this way (see c16 stale-pid mess). Same fix, parameterised.
set -e
SLUG="$1"; SCRIPT="$2"; shift 2 || true
if [ -z "$SLUG" ] || [ -z "$SCRIPT" ]; then
  echo "usage: $0 <slug> <python-script> [args...]" >&2
  exit 2
fi
PIDFILE="/tmp/${SLUG}.pid"
LOGFILE="/tmp/${SLUG}.log"
if [ -f "$PIDFILE" ]; then
  OLD=$(cat "$PIDFILE" 2>/dev/null)
  if [ -n "$OLD" ] && [ -e "/proc/$OLD/cmdline" ]; then
    echo "[$SLUG] already running as pid $OLD, refusing to start" >&2
    exit 1
  fi
  echo "[$SLUG] stale pidfile (pid $OLD gone), reclaiming" >&2
fi
echo $$ > "$PIDFILE"
trap "rm -f $PIDFILE" EXIT INT TERM
exec python3 "$SCRIPT" "$@" >> "$LOGFILE" 2>&1 < /dev/null
