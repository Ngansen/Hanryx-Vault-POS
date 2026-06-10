#!/bin/sh
# Pidfile-guarded launcher for c16_ebay_backfill.py.
#
# Why: `docker compose exec -d pos sh -c "nohup python ... &"` was creating
# TWO procs per invocation — `-d` detaches the exec, and `&` inside sh -c
# also backgrounds, so each call spawned both a nohup pgroup AND a bg
# child. Stale procs (1478, 10943, 11676, 11863, 12086, 12099, 12327,
# 12333) accumulated, all writing dual rows. This wrapper de-duplicates:
# if a previous c16 is still alive (per /proc/<pid>/cmdline), refuse to
# start. Otherwise claim the pidfile and exec the script in the foreground
# — no `nohup`, no `&` (the outer `docker compose exec -d` handles
# detachment on its own).
PIDFILE=/tmp/c16.pid
LOGFILE=/tmp/c16.log
if [ -f "$PIDFILE" ]; then
  OLD=$(cat "$PIDFILE" 2>/dev/null)
  if [ -n "$OLD" ] && [ -e "/proc/$OLD/cmdline" ]; then
    echo "[c16] already running as pid $OLD, refusing to start" >&2
    exit 1
  fi
  echo "[c16] stale pidfile (pid $OLD gone), reclaiming" >&2
fi
echo $$ > "$PIDFILE"
trap "rm -f $PIDFILE" EXIT INT TERM
exec python3 /app/scripts/c16_ebay_backfill.py "$@" >> "$LOGFILE" 2>&1 < /dev/null
