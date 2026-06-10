#!/usr/bin/env python3
"""C16 progress sidecar — polls Postgres + the c16 pidfile and writes a
JSON status snapshot every 30s. Non-invasive: no edits to
c16_ebay_backfill.py. Read by the desktop monitor.
"""
from __future__ import annotations
import json, os, re, sys, time
from datetime import datetime, timezone
from pathlib import Path
import psycopg2

PIDFILE = Path("/tmp/c16.pid")
LOGFILE = Path("/tmp/c16.log")
STATUS  = Path("/tmp/c16_status.json")
TICK    = 30
LINE_RX = re.compile(r"\[c16\] \[(\d+)/(\d+)\]\s+(\S+)")

def pid_alive():
    if not PIDFILE.exists(): return (False, None)
    try: pid = int(PIDFILE.read_text().strip())
    except Exception: return (False, None)
    return (Path(f"/proc/{pid}").exists(), pid)

def tail_progress():
    if not LOGFILE.exists(): return ("", None, None, None)
    sz = LOGFILE.stat().st_size
    with LOGFILE.open("rb") as f:
        f.seek(max(0, sz - 64_000))
        chunk = f.read().decode("utf-8", errors="replace")
    last = ""; done = total = None; card = None
    for line in chunk.splitlines()[-400:]:
        m = LINE_RX.search(line)
        if m:
            last = line.strip()
            done, total = int(m.group(1)), int(m.group(2))
            card = m.group(3)
    return (last, done, total, card)

def rows_written(conn):
    with conn.cursor() as c:
        c.execute("SELECT count(*) FROM price_history WHERE source='ebay_sold'")
        return c.fetchone()[0]

def fmt_eta(h):
    if h is None or h <= 0: return None
    d, hr = divmod(int(h), 24)
    return f"{d}d{hr}h" if d else f"{hr}h"

def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"]); conn.autocommit = True
    prev_done = prev_ts = None
    while True:
        try:
            alive, pid = pid_alive()
            last, done, total, card = tail_progress()
            rows = rows_written(conn)
            now = time.time(); rate = eta_h = None
            if prev_done is not None and done is not None and prev_ts is not None:
                dt_h = (now - prev_ts) / 3600
                if dt_h > 0 and done > prev_done:
                    rate = (done - prev_done) / dt_h
                    if rate > 0 and total: eta_h = (total - done) / rate
            STATUS.write_text(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "alive": alive, "pid": pid,
                "cards_total": total, "cards_done": done,
                "cards_remaining": (total - done) if (total and done is not None) else None,
                "rows_written": rows,
                "rate_cards_per_hr": round(rate, 1) if rate else None,
                "eta_hours": round(eta_h, 1) if eta_h else None,
                "eta_human": fmt_eta(eta_h),
                "last_card_id": card, "last_log_line": last,
            }, indent=2))
            if done is not None: prev_done, prev_ts = done, now
        except Exception as e:
            print(f"[sidecar] {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        time.sleep(TICK)

if __name__ == "__main__":
    main()
