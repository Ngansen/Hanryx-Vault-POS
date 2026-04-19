"""
Pokémon TCG Pocket cards importer
==================================

Pulls Ngansen/pokemon-tcg-pocket-database (a fork of flibustier's npm package).
The dataset is a tiny ~370 KB JSON file with every Pocket card — no images,
no git clone needed.  Single HTTP fetch + UPSERT, the whole import takes <2s.

Card schema:  {set, number, rarity, name, image, packs}
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import requests

log = logging.getLogger("import_jpn_pocket_cards")

CARDS_URL  = ("https://raw.githubusercontent.com/Ngansen/"
              "pokemon-tcg-pocket-database/main/dist/cards.min.json")
EXTRA_URL  = ("https://raw.githubusercontent.com/Ngansen/"
              "pokemon-tcg-pocket-database/main/dist/cards.extra.json")
SETS_URL   = ("https://raw.githubusercontent.com/Ngansen/"
              "pokemon-tcg-pocket-database/main/dist/sets.json")
IMG_BASE   = ("https://raw.githubusercontent.com/Ngansen/"
              "pokemon-tcg-pocket-database/main/dist/images/sets")


def ensure_table(db_conn) -> None:
    cur = db_conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cards_jpn_pocket (
            set_code     TEXT NOT NULL,
            card_number  INTEGER NOT NULL,
            name         TEXT NOT NULL,
            rarity       TEXT NOT NULL DEFAULT '',
            image_url    TEXT NOT NULL DEFAULT '',
            packs        TEXT NOT NULL DEFAULT '',
            extra        JSONB,
            imported_at  BIGINT NOT NULL DEFAULT 0,
            PRIMARY KEY (set_code, card_number)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_jpn_pocket_name "
                "ON cards_jpn_pocket (name)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_jpn_pocket_rarity "
                "ON cards_jpn_pocket (rarity)")
    db_conn.commit()


def cards_count(db_conn) -> int:
    cur = db_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM cards_jpn_pocket")
    return int(cur.fetchone()[0])


def import_pocket_cards(db_conn, *, force: bool = False) -> dict:
    ensure_table(db_conn)

    if not force and cards_count(db_conn) > 0:
        return {"imported": 0, "found": 0, "skipped": True}

    headers = {"User-Agent": "HanryxVault-POS/1.0"}

    log.info("[jpn-pocket] fetching %s", CARDS_URL)
    cards_resp = requests.get(CARDS_URL, headers=headers, timeout=30)
    cards_resp.raise_for_status()
    cards: list[dict] = cards_resp.json()

    extras_by_key: dict[tuple, dict] = {}
    try:
        extras_resp = requests.get(EXTRA_URL, headers=headers, timeout=30)
        if extras_resp.ok:
            for e in extras_resp.json():
                key = (e.get("set"), int(e.get("number") or 0))
                extras_by_key[key] = e
    except Exception as exc:
        log.warning("[jpn-pocket] extras fetch failed (non-fatal): %s", exc)

    if force:
        cur = db_conn.cursor()
        cur.execute("TRUNCATE cards_jpn_pocket")
        db_conn.commit()

    rows: list[tuple] = []
    for c in cards:
        try:
            set_code = (c.get("set") or "").strip()
            number   = int(c.get("number") or 0)
            name     = (c.get("name") or "").strip()
            if not (set_code and number and name):
                continue
            image    = (c.get("image") or "").strip()
            image_url = f"{IMG_BASE}/{set_code}/{image}" if image else ""
            packs    = ", ".join(c.get("packs") or [])
            extra    = extras_by_key.get((set_code, number))
            rows.append((
                set_code,
                number,
                name,
                (c.get("rarity") or "").strip(),
                image_url,
                packs,
                json.dumps(extra, ensure_ascii=False) if extra else None,
                int(time.time() * 1000),
            ))
        except Exception as exc:
            log.warning("[jpn-pocket] skip row: %s — %s", exc, c)

    cur = db_conn.cursor()
    cur.executemany("""
        INSERT INTO cards_jpn_pocket
            (set_code, card_number, name, rarity, image_url, packs, extra, imported_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
        ON CONFLICT (set_code, card_number) DO UPDATE SET
            name        = EXCLUDED.name,
            rarity      = EXCLUDED.rarity,
            image_url   = EXCLUDED.image_url,
            packs       = EXCLUDED.packs,
            extra       = EXCLUDED.extra,
            imported_at = EXCLUDED.imported_at
    """, rows)
    db_conn.commit()

    log.info("[jpn-pocket] done — found=%d imported=%d", len(cards), len(rows))
    return {"imported": len(rows), "found": len(cards), "skipped": False}


if __name__ == "__main__":
    import argparse, psycopg2
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    dsn = os.environ["DATABASE_URL"]
    conn = psycopg2.connect(dsn)
    try:
        print(json.dumps(import_pocket_cards(conn, force=args.force), indent=2))
    finally:
        conn.close()
