#!/usr/bin/env python3
"""
export-db.py — Run this on Replit to export all your data.

Usage (in Replit Shell tab):
  python3 scripts/export-db.py

It will create a file called: vault_pos_export_YYYYMMDD.json
Download that file and copy it to your Pi alongside install.sh.
Then run: sudo python3 scripts/import-db.py vault_pos_export_YYYYMMDD.json
"""

import sqlite3
import json
import datetime
import os
import sys

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vault_pos.db")

# Also check common alternative locations
SEARCH_PATHS = [
    DB_PATH,
    "vault_pos.db",
    os.path.expanduser("~/vault_pos.db"),
    "/home/runner/workspace/vault_pos.db",
]

db_path = None
for p in SEARCH_PATHS:
    if os.path.exists(p):
        db_path = p
        break

if not db_path:
    print("ERROR: Could not find vault_pos.db")
    print("Searched:", SEARCH_PATHS)
    print("Make sure server.py has been run at least once to create the database.")
    sys.exit(1)

print(f"Found database: {db_path}")

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

TABLES = [
    "sales",
    "inventory",
    "stock_deductions",
    "scan_queue",
    "sale_history",
]

export = {
    "exported_at": datetime.datetime.now().isoformat(),
    "source": "Replit HanryxVault POS",
    "tables": {}
}

total_rows = 0

for table in TABLES:
    try:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        export["tables"][table] = [dict(r) for r in rows]
        count = len(rows)
        total_rows += count
        print(f"  {table}: {count} rows")
    except sqlite3.OperationalError as e:
        print(f"  {table}: SKIPPED ({e})")
        export["tables"][table] = []

conn.close()

today = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
out_file = f"vault_pos_export_{today}.json"

with open(out_file, "w") as f:
    json.dump(export, f, indent=2, default=str)

size_kb = os.path.getsize(out_file) / 1024

print()
print(f"Export complete!")
print(f"  File:       {out_file}")
print(f"  Total rows: {total_rows}")
print(f"  File size:  {size_kb:.1f} KB")
print()
print("Next steps:")
print(f"  1. Download '{out_file}' from Replit (click the file in the file tree, then Download)")
print(f"  2. Copy it to your Pi next to the pi-setup folder:")
print(f"       scp {out_file} pi@<YOUR_PI_IP>:/home/pi/pi-setup/")
print(f"  3. On the Pi, run:")
print(f"       sudo python3 /home/pi/pi-setup/scripts/import-db.py /home/pi/pi-setup/{out_file}")
