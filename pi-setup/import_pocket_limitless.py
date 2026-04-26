"""
import_pocket_limitless.py — Limitless TCG Pocket cards loader (U8b)

Pulls Ngansen/pokemon-tcg-pocket-cards (chase-manning's fork) which
publishes per-version JSON dumps scraped from Limitless TCG's
Pocket section. We always read v4.json (the latest version, ~1 MB).

Why an *alternate* TCG Pocket source: the existing
import_jpn_pocket_cards.py reads from flibustier's
pokemon-tcg-pocket-database npm package. Limitless data sometimes
has cards earlier (within hours of release) and uses different rarity
notation (◊◊◊◊◊ vs C/U/R). Keeping both lets the consolidator pick
whichever has fresher data and lets the operator dashboard surface
disagreements.

Schema per record:
  id: string             e.g. 'a1-001'
  name: string           English
  rarity: string         '◊', '◊◊', '◊◊◊', '◊◊◊◊', '★', '★★', '★★★', '♛'
  pack: string           'Mewtwo' / 'Pikachu' / 'Charizard' / 'Mythical Island' / etc
  health: string         "70" (string, not int)
  image: string          full https URL
  fullart: 'Yes' / 'No'
  ex: 'Yes' / 'No'
  artist: string
  type: string           'Grass', 'Lightning', 'Fire', etc
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

import psycopg2
import psycopg2.extras
import requests

from unified.schema import init_unified_schema

log = logging.getLogger("import_pocket_limitless")

CARDS_URL = ("https://raw.githubusercontent.com/Ngansen/"
             "pokemon-tcg-pocket-cards/main/v4.json")


def _safe_int(v):
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _fetch(timeout: int = 60) -> list[dict]:
    headers = {"User-Agent": "HanryxVault-POS/1.0"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"token {token}"
    log.info("[pocket-lt] downloading %s", CARDS_URL)
    r = requests.get(CARDS_URL, headers=headers, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise ValueError(f"expected list, got {type(data).__name__}")
    log.info("[pocket-lt] %d cards", len(data))
    return data


def _ingest(db_conn, cards: list[dict]) -> int:
    cur = db_conn.cursor()
    cur.execute("DELETE FROM src_pocket_limitless")
    now = int(time.time())
    insert_sql = """
        INSERT INTO src_pocket_limitless
          (expansion_id, card_number, name, rarity, card_type,
           pack, image_url, raw_row, imported_at)
        VALUES %s
    """
    batch: list[tuple] = []
    BATCH = 500
    inserted = 0

    def flush():
        nonlocal inserted
        if not batch: return
        psycopg2.extras.execute_values(cur, insert_sql, batch, page_size=500)
        inserted += len(batch)
        batch.clear()

    for c in cards:
        if not isinstance(c, dict): continue
        cid = str(c.get("id") or "").strip()
        if not cid: continue
        if "-" in cid:
            expansion, number = cid.rsplit("-", 1)
        else:
            expansion, number = "", cid
        batch.append((
            expansion,
            number,
            str(c.get("name") or "").strip(),
            str(c.get("rarity") or "").strip(),
            str(c.get("type") or "").strip(),
            str(c.get("pack") or "").strip(),
            str(c.get("image") or "").strip(),
            json.dumps(c, ensure_ascii=False, default=str),
            now,
        ))
        if len(batch) >= BATCH: flush()
    flush()
    db_conn.commit()
    return inserted


def import_pocket_limitless(db_conn, *, force: bool = False) -> dict:
    init_unified_schema(db_conn)
    cur = db_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM src_pocket_limitless")
    pre = int(cur.fetchone()[0])
    if pre > 0 and not force:
        return {"skipped": True, "pre_count": pre}
    cards = _fetch()
    inserted = _ingest(db_conn, cards)
    return {"inserted": inserted, "fetched": len(cards)}


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr); return 1
    with psycopg2.connect(url) as conn:
        print(json.dumps(import_pocket_limitless(conn, force=args.force),
                         indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
