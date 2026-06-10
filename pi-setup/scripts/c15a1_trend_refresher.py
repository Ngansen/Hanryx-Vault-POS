#!/usr/bin/env python3
"""C15a.1: Background daemon that refreshes price_trends_daily on a fixed
interval so /card/enrich's `trend` chip stays live without manual cron.

Pairs with refresh_price_trends.py (one-shot) — same logic, just looped.

Run
---
    docker compose exec -d pos sh -c \\
      'nohup python /app/scripts/c15a1_trend_refresher.py > /tmp/trend_refresher.log 2>&1 < /dev/null &'

Flags
-----
    --interval-min 30   minutes between refreshes (default 30)
    --once              refresh once then exit (for ad-hoc use)
"""
from __future__ import annotations
import argparse, os, signal, sys, time
import psycopg2

_STOP = False
def _sigterm(*_):
    global _STOP
    _STOP = True
    print("[trend-refresher] SIGTERM — exiting after current refresh", flush=True)
signal.signal(signal.SIGTERM, _sigterm)
signal.signal(signal.SIGINT,  _sigterm)


def _refresh_once(db_url: str) -> tuple[int, int, float, str]:
    t0 = time.time()
    with psycopg2.connect(db_url) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            try:
                cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY price_trends_daily")
                mode = "CONCURRENTLY"
            except psycopg2.Error:
                cur.execute("REFRESH MATERIALIZED VIEW price_trends_daily")
                mode = "blocking"
            cur.execute("""
                SELECT count(*),
                       count(*) FILTER (WHERE pct_7d IS NOT NULL)
                FROM price_trends_daily
            """)
            n_total, n_with_7d = cur.fetchone()
    return int(n_total), int(n_with_7d), time.time() - t0, mode


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--interval-min", type=int, default=30)
    p.add_argument("--once",         action="store_true")
    args = p.parse_args()

    db_url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not db_url:
        sys.exit("FATAL: DATABASE_URL not set")

    print(f"[trend-refresher] started  interval={args.interval_min}m  once={args.once}", flush=True)

    while True:
        try:
            n, n7, dur, mode = _refresh_once(db_url)
            print(f"[trend-refresher] refreshed ({mode}): {n} rows  "
                  f"({n7} with 7d delta) in {dur:.1f}s", flush=True)
        except Exception as e:
            print(f"[trend-refresher] ERROR: {e}", flush=True)

        if args.once or _STOP:
            break

        # Sleep in 30s chunks for SIGTERM responsiveness
        end = time.time() + args.interval_min * 60
        while time.time() < end and not _STOP:
            time.sleep(30)

    print("[trend-refresher] exited cleanly", flush=True)


if __name__ == "__main__":
    main()
