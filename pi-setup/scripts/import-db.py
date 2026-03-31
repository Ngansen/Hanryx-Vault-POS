#!/usr/bin/env python3
"""
import-db.py — Run this on your Pi to import data exported from Replit.

Usage:
  sudo python3 scripts/import-db.py vault_pos_export_YYYYMMDD.json

It merges the exported data into /opt/hanryxvault/vault_pos.db
without overwriting any records already on the Pi (safe to run multiple times).
"""

import sqlite3
import json
import sys
import os
import datetime

DB_PATH = "/opt/hanryxvault/vault_pos.db"

SEARCH_PATHS = [
    DB_PATH,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vault_pos.db"),
    "vault_pos.db",
]

# ── Find export file ──────────────────────────────────────────────────────────
if len(sys.argv) < 2:
    print("Usage: sudo python3 scripts/import-db.py <export_file.json>")
    print("Example: sudo python3 scripts/import-db.py vault_pos_export_20250101_120000.json")
    sys.exit(1)

export_file = sys.argv[1]
if not os.path.exists(export_file):
    print(f"ERROR: Export file not found: {export_file}")
    sys.exit(1)

# ── Find database ─────────────────────────────────────────────────────────────
db_path = None
for p in SEARCH_PATHS:
    if os.path.exists(p):
        db_path = p
        break

if not db_path:
    # Create it at the default location
    db_path = DB_PATH
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    print(f"Database not found — will create at {db_path}")
    print("Run install.sh first to initialize the database structure.")
    sys.exit(1)

print(f"\nImporting into: {db_path}")
print(f"From file:      {export_file}")
print()

# ── Load export ───────────────────────────────────────────────────────────────
with open(export_file, "r") as f:
    export = json.load(f)

exported_at = export.get("exported_at", "unknown")
print(f"Export date: {exported_at}")
print(f"Source:      {export.get('source', 'unknown')}")
print()

conn = sqlite3.connect(db_path)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")

total_inserted = 0
total_skipped  = 0

# ── Import each table ─────────────────────────────────────────────────────────

def import_table(table, rows, upsert_sql, value_fn):
    inserted = 0
    skipped  = 0
    for row in rows:
        try:
            values = value_fn(row)
            conn.execute(upsert_sql, values)
            if conn.execute("SELECT changes()").fetchone()[0] > 0:
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"  [!] Row error in {table}: {e}")
            skipped += 1
    conn.commit()
    print(f"  {table}: inserted {inserted}, skipped {skipped} (already existed)")
    return inserted, skipped


# Sales
rows = export.get("tables", {}).get("sales", [])
print(f"Importing sales ({len(rows)} rows)...")
i, s = import_table(
    "sales", rows,
    """INSERT OR IGNORE INTO sales
        (transaction_id, timestamp_ms, subtotal, tax_amount, tip_amount,
         total_amount, payment_method, employee_id, items_json,
         cash_received, change_given, is_refunded, received_at)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
    lambda r: (
        r.get("transaction_id"), r.get("timestamp_ms", 0),
        r.get("subtotal", 0), r.get("tax_amount", 0), r.get("tip_amount", 0),
        r.get("total_amount", 0), r.get("payment_method", "UNKNOWN"),
        r.get("employee_id", "UNKNOWN"), r.get("items_json", "[]"),
        r.get("cash_received", 0), r.get("change_given", 0),
        r.get("is_refunded", 0), r.get("received_at", 0),
    )
)
total_inserted += i; total_skipped += s

# Inventory (upsert — always update to latest)
rows = export.get("tables", {}).get("inventory", [])
print(f"\nImporting inventory ({len(rows)} rows)...")
i, s = import_table(
    "inventory", rows,
    """INSERT INTO inventory
        (qr_code, name, price, category, rarity, set_code, description, stock, last_updated)
       VALUES (?,?,?,?,?,?,?,?,?)
       ON CONFLICT(qr_code) DO UPDATE SET
         name=excluded.name, price=excluded.price, category=excluded.category,
         rarity=excluded.rarity, set_code=excluded.set_code,
         description=excluded.description, stock=excluded.stock,
         last_updated=excluded.last_updated""",
    lambda r: (
        r.get("qr_code"), r.get("name"), r.get("price", 0),
        r.get("category", "General"), r.get("rarity", ""),
        r.get("set_code", ""), r.get("description", ""),
        r.get("stock", 0), r.get("last_updated", 0),
    )
)
total_inserted += i; total_skipped += s

# Stock deductions
rows = export.get("tables", {}).get("stock_deductions", [])
print(f"\nImporting stock deductions ({len(rows)} rows)...")
i, s = import_table(
    "stock_deductions", rows,
    """INSERT OR IGNORE INTO stock_deductions
        (id, transaction_id, qr_code, name, quantity, unit_price, line_total, deducted_at)
       VALUES (?,?,?,?,?,?,?,?)""",
    lambda r: (
        r.get("id"), r.get("transaction_id"), r.get("qr_code", ""),
        r.get("name", ""), r.get("quantity", 1),
        r.get("unit_price", 0), r.get("line_total", 0), r.get("deducted_at", 0),
    )
)
total_inserted += i; total_skipped += s

# Sale history
rows = export.get("tables", {}).get("sale_history", [])
print(f"\nImporting sale history ({len(rows)} rows)...")
i, s = import_table(
    "sale_history", rows,
    """INSERT OR IGNORE INTO sale_history (id, name, price, quantity, sold_at)
       VALUES (?,?,?,?,?)""",
    lambda r: (
        r.get("id"), r.get("name", ""), r.get("price", 0),
        r.get("quantity", 1), r.get("sold_at", 0),
    )
)
total_inserted += i; total_skipped += s

conn.close()

print()
print("=" * 50)
print(f"  Import complete!")
print(f"  Total inserted: {total_inserted}")
print(f"  Total skipped:  {total_skipped} (safe — already existed)")
print()
print("  Restart the server to pick up the new data:")
print("    sudo systemctl restart hanryxvault")
print()
