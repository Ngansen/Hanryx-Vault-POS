"""Refresh price_trends_daily. Run on a 1h cron from the sync container.

    docker compose exec -T pos python /app/scripts/refresh_price_trends.py
"""
from __future__ import annotations
import os, sys, time, psycopg2

DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
if not DB_URL: sys.exit("ERROR: DATABASE_URL not set")
t0 = time.time()
with psycopg2.connect(DB_URL) as conn:
    conn.autocommit = True
    with conn.cursor() as cur:
        try:
            cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY price_trends_daily")
            mode = "CONCURRENTLY"
        except psycopg2.Error:
            cur.execute("REFRESH MATERIALIZED VIEW price_trends_daily")
            mode = "blocking"
        cur.execute("SELECT count(*), count(*) FILTER (WHERE pct_7d IS NOT NULL) FROM price_trends_daily")
        n_total, n_with_7d = cur.fetchone()
print(f"[trends] refreshed ({mode}): {n_total} rows ({n_with_7d} with 7d delta) in {time.time()-t0:.1f}s")
