"""
import_jp_pokemoncardcom.py — pokemon-card-jp-database loader (U8a)

Pulls Ngansen/pokemon-card-jp-database — a single 3.2 MB cards.json
scraped from the official pokemon-card.com (Japan) website.

Schema (per record):
  cardId: string                  e.g. '44472'
  name: string                    Japanese name in kana/kanji
  element: Element                grass/fire/water/electric/psychic/...
  edition: Edition                e.g. 'SVG', 'SV1V', 'SV-P'
  description: string?            flavor text in JP
  dimension: string?              "高さ：0.7 m　重さ：6.9 kg"
  health: number
  numero: number?                 card number within set
  attacks: [{name, damage, effect, cost: Element[]}]

This is an alternate JP source vs the existing cards_jpn (which is
Pokellector-scraped). Useful for cross-verification and for getting
official JP descriptions / attack texts that Pokellector doesn't
carry. The consolidator uses cards_jpn as the primary JP source and
falls back to this one when cards_jpn lacks a record for the given
edition+numero.
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

log = logging.getLogger("import_jp_pokemoncardcom")

CARDS_URL = ("https://raw.githubusercontent.com/Ngansen/"
             "pokemon-card-jp-database/main/cards.json")


def _fetch(timeout: int = 90) -> list[dict]:
    headers = {"User-Agent": "HanryxVault-POS/1.0"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"token {token}"
    log.info("[jp-pcc] downloading %s", CARDS_URL)
    r = requests.get(CARDS_URL, headers=headers, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise ValueError(f"expected list, got {type(data).__name__}")
    log.info("[jp-pcc] %d cards", len(data))
    return data


def _ingest(db_conn, cards: list[dict]) -> int:
    cur = db_conn.cursor()
    cur.execute("DELETE FROM src_jp_pokemoncardcom")
    now = int(time.time())
    insert_sql = """
        INSERT INTO src_jp_pokemoncardcom
          (card_id, set_code, set_name, card_number,
           name_jp, rarity, card_type, image_url, raw_row, imported_at)
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
        if not isinstance(c, dict):
            continue
        cid = str(c.get("cardId") or "").strip()
        if not cid:
            continue
        edition = str(c.get("edition") or "").strip()
        numero = c.get("numero")
        try:
            numero_s = str(int(numero)) if numero is not None else ""
        except (TypeError, ValueError):
            numero_s = str(numero) if numero is not None else ""

        batch.append((
            cid,
            edition,
            edition,                       # set_name == edition for now (no fuller name in JSON)
            numero_s,
            str(c.get("name") or "").strip(),
            "",                             # rarity not in this dataset
            str(c.get("element") or "").strip(),
            "",                             # image not in this dataset (would need separate scrape)
            json.dumps(c, ensure_ascii=False, default=str),
            now,
        ))
        if len(batch) >= BATCH: flush()
    flush()
    db_conn.commit()
    return inserted


def import_jp_pokemoncardcom(db_conn, *, force: bool = False) -> dict:
    init_unified_schema(db_conn)
    cur = db_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM src_jp_pokemoncardcom")
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
        print(json.dumps(import_jp_pokemoncardcom(conn, force=args.force),
                         indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
