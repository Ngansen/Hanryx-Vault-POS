#!/usr/bin/env python3
"""
import_set_aliases.py — load pi-setup/data/set_aliases.csv into Postgres.

The CSV maps every Pokémon TCG set across English / Japanese / Korean /
Chinese, including the abbreviations each region uses. The consolidator
uses this table to translate set codes between regions when backfilling
from JP / KR / CN sources, dramatically improving cross-language join
match rate.

Schema: see DDL_REF_SET_ALIAS in unified/schema.py.

CSV columns (header row required):
    English Set, Japanese Set, Korean (Translated), Chinese (Translated),
    EN Abbrev,   JP Code,      KR Abbrev,           CN Abbrev,
    Relationship, Era

Usage
-----
    DATABASE_URL=postgresql://hanryx:PASSWORD@localhost:5432/hanryx \\
        python3 pi-setup/scripts/import_set_aliases.py

    # Custom CSV path:
    python3 pi-setup/scripts/import_set_aliases.py \\
        --file /path/to/set_aliases.csv

Idempotent: runs TRUNCATE + INSERT in one transaction so the table
always reflects the current CSV (no stale rows from previous imports).
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
log = logging.getLogger("import_set_aliases")

DEFAULT_FILE = REPO / "data" / "set_aliases.csv"


def _safe(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _norm_code(v: str) -> str:
    """N/A and (EN only)-style placeholders → empty string. Keep canonical
    codes verbatim (no case folding — the consolidator handles that)."""
    s = _safe(v)
    if not s or s.upper() in ("N/A", "NA", "NONE", "(EN ONLY)", "(REPRINTS)"):
        return ""
    return s


def import_set_aliases(conn, src_path: Path) -> dict:
    """TRUNCATE + reload ref_set_alias from `src_path`. Returns stats."""
    init_unified_schema(conn)

    log.info("Reading %s …", src_path)
    rows: list[tuple] = []
    skipped_blank = 0
    seen_pairs: set[tuple] = set()
    dup_pairs = 0

    with open(src_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            # Skip the eras' blank separator rows
            if not any((raw.get(k) or "").strip() for k in raw):
                skipped_blank += 1
                continue
            code_en  = _norm_code(raw.get("EN Abbrev"))
            code_jp  = _norm_code(raw.get("JP Code"))
            code_kr  = _norm_code(raw.get("KR Abbrev"))
            code_chs = _norm_code(raw.get("CN Abbrev"))
            # Skip rows with no usable code in any region (header noise).
            if not (code_en or code_jp or code_kr or code_chs):
                skipped_blank += 1
                continue
            pair = (code_en.upper(), code_jp.upper(),
                    _safe(raw.get("Relationship")))
            if pair in seen_pairs:
                dup_pairs += 1
                continue
            seen_pairs.add(pair)
            rows.append((
                _safe(raw.get("English Set")),
                _safe(raw.get("Japanese Set")),
                _safe(raw.get("Korean (Translated)")),
                _safe(raw.get("Chinese (Translated)")),
                code_en, code_jp, code_kr, code_chs,
                _safe(raw.get("Relationship")),
                _safe(raw.get("Era")),
                int(time.time()),
            ))

    log.info("Parsed %d data rows (skipped %d blank, %d duplicate)",
             len(rows), skipped_blank, dup_pairs)
    if not rows:
        log.warning("No rows to import — leaving table untouched")
        return {"parsed": 0, "loaded": 0}

    cur = conn.cursor()
    cur.execute("BEGIN")
    cur.execute("TRUNCATE TABLE ref_set_alias")
    psycopg2.extras.execute_values(cur, """
        INSERT INTO ref_set_alias
          (name_en, name_jp, name_kr, name_chs,
           code_en, code_jp, code_kr, code_chs,
           relationship, era, imported_at)
        VALUES %s
    """, rows, page_size=500)
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM ref_set_alias")
    total = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(DISTINCT code_en) FROM ref_set_alias WHERE code_en <> ''")
    n_en = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(DISTINCT code_jp) FROM ref_set_alias WHERE code_jp <> ''")
    n_jp = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(DISTINCT era) FROM ref_set_alias WHERE era <> ''")
    n_era = int(cur.fetchone()[0])

    return {
        "parsed":            len(rows),
        "loaded":            total,
        "distinct_en_codes": n_en,
        "distinct_jp_codes": n_jp,
        "distinct_eras":     n_era,
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
        stats = import_set_aliases(conn, src)
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
