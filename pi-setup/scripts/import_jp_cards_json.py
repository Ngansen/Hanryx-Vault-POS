#!/usr/bin/env python3
"""
import_jp_cards_json.py — load pokemon-card-jp-database/cards.json into
Postgres so the consolidator can backfill JP names on cards that other
sources missed.

Schema lives in unified/schema.py (table src_jp_cards_json). This script
just upserts rows from the JSON dump produced by the Ngansen fork at
https://github.com/Ngansen/pokemon-card-jp-database — pulled by
sync_card_mirror.py Phase A.

Usage
-----
    DATABASE_URL=postgresql://hanryx:PASSWORD@localhost:5432/hanryx \\
        MIRROR_ROOT=/mnt/cards \\
        python3 pi-setup/scripts/import_jp_cards_json.py

    # Override the file path explicitly:
    python3 pi-setup/scripts/import_jp_cards_json.py \\
        --file /path/to/cards.json

Idempotent: re-running just refreshes the table from the latest JSON.
Run after every Phase A sync (cron-friendly).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import psycopg2
import psycopg2.extras

# Make `unified.schema` importable when run from repo root or scripts/.
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

from unified.schema import init_unified_schema  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("import_jp_cards_json")

MIRROR_ROOT = Path(os.environ.get("MIRROR_ROOT",
                                  os.environ.get("USB_CARDS_ROOT", "/mnt/cards")))
DEFAULT_FILE = MIRROR_ROOT / "pokemon-card-jp-database" / "cards.json"


def _safe_int(v):
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _safe_str(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


def import_jp_cards_json(conn, src_path: Path) -> dict:
    """Upsert every card in `src_path` into src_jp_cards_json. Returns stats."""
    init_unified_schema(conn)

    log.info("Loading %s …", src_path)
    started = time.time()
    with open(src_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    cards = data.get("cards") or []
    generated = data.get("generatedDate", "?")
    log.info("Parsed %d cards (generatedDate=%s) in %.1fs",
             len(cards), generated, time.time() - started)

    if not cards:
        log.warning("No cards in JSON — nothing to upsert")
        return {"parsed": 0, "upserted": 0}

    now = int(time.time())
    rows = []
    skipped_no_id = 0
    for c in cards:
        cid = _safe_str(c.get("cardId"))
        if not cid:
            skipped_no_id += 1
            continue
        rows.append((
            cid,
            _safe_str(c.get("name")),
            _safe_str(c.get("edition")),
            _safe_str(c.get("dimension")),
            _safe_str(c.get("description")),
            _safe_str(c.get("element")),
            _safe_int(c.get("health")),
            _safe_int(c.get("numero")),
            json.dumps(c.get("attacks") or [], ensure_ascii=False),
            json.dumps(c, ensure_ascii=False),
            now,
        ))

    if skipped_no_id:
        log.warning("Skipped %d cards with no cardId", skipped_no_id)

    cur = conn.cursor()
    log.info("Upserting %d rows into src_jp_cards_json …", len(rows))
    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO src_jp_cards_json
          (card_id, name, edition, dimension, description, element,
           health, numero, attacks, raw, imported_at)
        VALUES %s
        ON CONFLICT (card_id) DO UPDATE SET
          name        = EXCLUDED.name,
          edition     = EXCLUDED.edition,
          dimension   = EXCLUDED.dimension,
          description = EXCLUDED.description,
          element     = EXCLUDED.element,
          health      = EXCLUDED.health,
          numero      = EXCLUDED.numero,
          attacks     = EXCLUDED.attacks,
          raw         = EXCLUDED.raw,
          imported_at = EXCLUDED.imported_at
        """,
        rows,
        page_size=500,
    )
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM src_jp_cards_json")
    total = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(DISTINCT edition) FROM src_jp_cards_json WHERE edition <> ''")
    distinct_eds = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM src_jp_cards_json WHERE numero IS NOT NULL")
    with_dex = int(cur.fetchone()[0])

    return {
        "parsed":       len(cards),
        "upserted":     len(rows),
        "table_total":  total,
        "distinct_eds": distinct_eds,
        "with_pokedex": with_dex,
        "generated":    generated,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", help=f"Path to cards.json (default: {DEFAULT_FILE})")
    args = ap.parse_args()

    src = Path(args.file) if args.file else DEFAULT_FILE
    if not src.is_file():
        log.error("cards.json not found at %s", src)
        log.error("Run sync_card_mirror.py --phase A first to clone the fork.")
        return 2

    url = os.environ.get("DATABASE_URL")
    if not url:
        log.error("DATABASE_URL is not set")
        return 1

    with psycopg2.connect(url) as conn:
        stats = import_jp_cards_json(conn, src)
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
