"""Apply the C14 schema migration. Idempotent — safe to re-run.

    docker compose exec -T pos python /app/scripts/c14_apply.py
"""
from __future__ import annotations
import os, sys, pathlib, psycopg2

SQL_PATH = pathlib.Path(__file__).resolve().parent.parent / "migrations" / "c14_data_expansion.sql"
DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
if not DB_URL:
    sys.exit("ERROR: DATABASE_URL / POSTGRES_URL not set")

if not SQL_PATH.exists():
    # Fallback to absolute path inside container (where migrations live in /app/migrations)
    alt = pathlib.Path("/app/migrations/c14_data_expansion.sql")
    if alt.exists(): SQL_PATH = alt
    else: sys.exit(f"ERROR: SQL file not found at {SQL_PATH}")

sql = SQL_PATH.read_text()
print(f"[c14] applying {SQL_PATH} ({len(sql):,} bytes) ...")

# Ensure pg_trgm exists (needed for the gin_trgm_ops index on card_text)
with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    conn.commit()

with psycopg2.connect(DB_URL) as conn:
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
print("[c14] schema applied — verifying ...")

with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
    checks = [
        ("buyback_rules count", "SELECT count(*) FROM buyback_rules"),
        ("sealed_products count", "SELECT count(*) FROM sealed_products"),
        ("cards_master new cols",
         "SELECT count(*) FROM information_schema.columns "
         "WHERE table_name='cards_master' AND column_name IN "
         "('variant','abilities_jsonb','attacks_jsonb','card_text','rarity_subtype')"),
        ("price_trends_daily exists",
         "SELECT count(*) FROM pg_matviews WHERE matviewname='price_trends_daily'"),
    ]
    for label, q in checks:
        cur.execute(q); print(f"  {label:30s} -> {cur.fetchone()[0]}")

print("[c14] DONE. Next:")
print("  - python /app/scripts/refresh_price_trends.py   # populate the trend view")
print("  - python /app/scripts/import_card_details.py --limit 50  # smoke-test enrichment")
