"""Upsert sealed-product inventory from a CSV file.

CSV columns (header row required, blanks ok):
  set_id, set_name, name, product_type, language, image_url, upc,
  msrp_usd, market_price_usd, market_price_native, native_currency,
  qty_on_hand, cost_basis_usd, bin_location, source, source_url, notes

Usage:
    docker compose exec -T pos python /app/scripts/import_sealed_csv.py /app/data/sealed.csv
"""
from __future__ import annotations
import csv, os, sys, psycopg2

DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
if not DB_URL: sys.exit("ERROR: DATABASE_URL not set")
if len(sys.argv) < 2: sys.exit("usage: import_sealed_csv.py <path.csv>")


def num(v):
    try: return float(v) if v not in (None, "", "NULL") else None
    except (TypeError, ValueError): return None
def i(v):
    try: return int(v) if v not in (None, "", "NULL") else 0
    except (TypeError, ValueError): return 0
def s(v): return v if v not in (None, "") else None


with open(sys.argv[1], "r", encoding="utf-8", newline="") as f:
    rows = list(csv.DictReader(f))
print(f"[sealed-csv] {len(rows)} rows from {sys.argv[1]}")

UPSERT = """
INSERT INTO sealed_products
  (set_id, set_name, name, product_type, language, image_url, upc,
   msrp_usd, market_price_usd, market_price_native, native_currency,
   qty_on_hand, cost_basis_usd, bin_location, source, source_url, notes)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (set_id, name, product_type, language) DO UPDATE SET
  image_url           = COALESCE(EXCLUDED.image_url,           sealed_products.image_url),
  upc                 = COALESCE(EXCLUDED.upc,                 sealed_products.upc),
  msrp_usd            = COALESCE(EXCLUDED.msrp_usd,            sealed_products.msrp_usd),
  market_price_usd    = COALESCE(EXCLUDED.market_price_usd,    sealed_products.market_price_usd),
  market_price_native = COALESCE(EXCLUDED.market_price_native, sealed_products.market_price_native),
  native_currency     = COALESCE(EXCLUDED.native_currency,     sealed_products.native_currency),
  qty_on_hand         = EXCLUDED.qty_on_hand,
  cost_basis_usd      = COALESCE(EXCLUDED.cost_basis_usd,      sealed_products.cost_basis_usd),
  bin_location        = COALESCE(EXCLUDED.bin_location,        sealed_products.bin_location),
  source              = COALESCE(EXCLUDED.source,              sealed_products.source),
  source_url          = COALESCE(EXCLUDED.source_url,          sealed_products.source_url),
  notes               = COALESCE(EXCLUDED.notes,               sealed_products.notes),
  updated_at          = NOW();
"""

n_ok = 0
with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
    for r in rows:
        try:
            cur.execute(UPSERT, (
                s(r.get("set_id")), s(r.get("set_name")), r.get("name"),
                s(r.get("product_type")), r.get("language") or "en",
                s(r.get("image_url")), s(r.get("upc")),
                num(r.get("msrp_usd")), num(r.get("market_price_usd")),
                num(r.get("market_price_native")), s(r.get("native_currency")),
                i(r.get("qty_on_hand")), num(r.get("cost_basis_usd")),
                s(r.get("bin_location")), s(r.get("source")) or "csv",
                s(r.get("source_url")), s(r.get("notes")),
            ))
            n_ok += 1
        except psycopg2.Error as e:
            print(f"  SKIP row: {r.get('name')!r}: {e}")
            conn.rollback()
print(f"[sealed-csv] upserted {n_ok}/{len(rows)} rows")
