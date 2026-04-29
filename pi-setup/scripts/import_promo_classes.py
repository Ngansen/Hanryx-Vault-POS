#!/usr/bin/env python3
"""
import_promo_classes.py — load pi-setup/data/promo_classes.csv into Postgres.

Where ref_set_alias maps NUMBERED expansion sets across regions, this
table maps PROMO BUCKETS — the global promo classes (Movie Promo,
Anniversary Promo, McDonald's Promo, World Championship, Pre-Release,
Staff, etc.) keyed by the per-region bucket codes that promo cards
carry as their set_id (`SM-P`, `SV-P`, `S-P`, `プロモ`, `WCS`, `PR`,
`STAFF`, `BX`, `ST`, `SIR`, `MC-P`, …).

This data deliberately does NOT feed the spine canonicaliser: many JP
promo codes ('プロモ', 'コロコロ') are catch-all buckets that map to
multiple distinct EN promo classes, so canonicalising them would
wrongly merge unrelated cards. The consolidator instead uses this
table for read-only enrichment of `promo_source` when a spine row's
set_id matches a known promo bucket.

Schema: see DDL_REF_PROMO_CLASS in unified/schema.py.

CSV columns (header row required):
    Global Promo ID, Promo Name, Promo Category,
    English Variant, Japanese Variant, Korean Variant, Chinese Variant,
    EN Code, JP Code, KR Code, CN Code,
    Language Coverage, Notes

Usage
-----
    DATABASE_URL=postgresql://hanryx:PASSWORD@localhost:5432/hanryx \\
        python3 pi-setup/scripts/import_promo_classes.py

    # Custom CSV path:
    python3 pi-setup/scripts/import_promo_classes.py \\
        --file /path/to/promo_classes.csv

Idempotent: TRUNCATE + INSERT in one transaction so the table always
reflects the current CSV (no stale rows from previous imports).
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from pathlib import Path

import psycopg2
import psycopg2.extras

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

from unified.schema import init_unified_schema  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("import_promo_classes")

DEFAULT_FILE = REPO / "data" / "promo_classes.csv"


def _safe(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _norm_code(v: str) -> str:
    """N/A and empty placeholders → empty string. Keep canonical codes
    verbatim (no case folding — the consolidator handles that)."""
    s = _safe(v)
    if not s or s.upper() in ("N/A", "NA", "NONE", "-", "(EN ONLY)"):
        return ""
    return s


def import_promo_classes(conn, src_path: Path) -> dict:
    """TRUNCATE + reload ref_promo_class from `src_path`. Returns stats."""
    init_unified_schema(conn)

    log.info("Reading %s …", src_path)
    rows: list[tuple] = []
    skipped_blank = 0
    seen_ids: set[str] = set()
    dup_ids = 0

    with open(src_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            if not any((raw.get(k) or "").strip() for k in raw):
                skipped_blank += 1
                continue
            class_id = _safe(raw.get("Global Promo ID"))
            if not class_id:
                # Without a primary key the row can't be loaded.
                skipped_blank += 1
                continue
            if class_id in seen_ids:
                dup_ids += 1
                continue
            seen_ids.add(class_id)

            rows.append((
                class_id,
                _safe(raw.get("Promo Name")),
                _safe(raw.get("Promo Category")),
                _safe(raw.get("English Variant")),
                _safe(raw.get("Japanese Variant")),
                _safe(raw.get("Korean Variant")),
                _safe(raw.get("Chinese Variant")),
                _norm_code(raw.get("EN Code")),
                _norm_code(raw.get("JP Code")),
                _norm_code(raw.get("KR Code")),
                _norm_code(raw.get("CN Code")),
                _safe(raw.get("Language Coverage")),
                _safe(raw.get("Notes")),
                int(time.time()),
            ))

    log.info("Parsed %d data rows (skipped %d blank, %d duplicate IDs)",
             len(rows), skipped_blank, dup_ids)
    if not rows:
        log.warning("No rows to import — leaving table untouched")
        return {"parsed": 0, "loaded": 0}

    cur = conn.cursor()
    cur.execute("BEGIN")
    cur.execute("TRUNCATE TABLE ref_promo_class")
    psycopg2.extras.execute_values(cur, """
        INSERT INTO ref_promo_class
          (class_id, promo_name, promo_category,
           variant_en, variant_jp, variant_kr, variant_chs,
           code_en, code_jp, code_kr, code_chs,
           lang_coverage, notes, imported_at)
        VALUES %s
    """, rows, page_size=500)
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM ref_promo_class")
    total = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(DISTINCT promo_category) "
                "FROM ref_promo_class WHERE promo_category <> ''")
    n_cat = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(DISTINCT code_en)  FROM ref_promo_class WHERE code_en  <> ''")
    n_en = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(DISTINCT code_jp)  FROM ref_promo_class WHERE code_jp  <> ''")
    n_jp = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(DISTINCT code_kr)  FROM ref_promo_class WHERE code_kr  <> ''")
    n_kr = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(DISTINCT code_chs) FROM ref_promo_class WHERE code_chs <> ''")
    n_chs = int(cur.fetchone()[0])

    return {
        "parsed":             len(rows),
        "loaded":             total,
        "distinct_categories": n_cat,
        "distinct_en_codes":  n_en,
        "distinct_jp_codes":  n_jp,
        "distinct_kr_codes":  n_kr,
        "distinct_chs_codes": n_chs,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", help=f"Path to CSV (default: {DEFAULT_FILE})")
    args = ap.parse_args()

    src = Path(args.file) if args.file else DEFAULT_FILE
    if not src.is_file():
        log.error("CSV not found at %s", src)
        return 2

    url = os.environ.get("DATABASE_URL")
    if not url:
        log.error("DATABASE_URL is not set")
        return 1

    with psycopg2.connect(url) as conn:
        stats = import_promo_classes(conn, src)
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
