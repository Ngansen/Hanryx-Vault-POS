#!/usr/bin/env python3
"""
mirror_tcg_to_pg.py — Mirror SQLite tcg_cards into Postgres `cards`.

Bridges the two halves of the PTCG (pokemontcg.io) ingestion path:

  sync_tcg_db.py  →  /mnt/cards/pokedex_local.db  (SQLite, offline kiosk lookup)
                              │
                              │  THIS SCRIPT
                              ▼
  build_cards_master.py reads Postgres `cards` (consolidator's `tcg_api`
  source — _read_tcg_api() in build_cards_master.py).

Without this bridge, `tcg_api: 0 rows` shows up in the consolidator log even
when SQLite is fully populated, because the consolidator only ever talks to
Postgres. After this runs, the consolidator gets PTCG English authoritative
data (HP, abilities, rarity, illustrator, holofoil price tiers) on top of
TCGdex multilingual names and the KR/CHS official sources.

Idempotent: TRUNCATE + bulk INSERT inside one transaction. Re-running is
safe — the row count just reflects the latest sync state.

Usage:
    docker exec -it pi-setup-pos-1 python3 /app/mirror_tcg_to_pg.py
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time

import psycopg2
import psycopg2.extras

from cards_db_path import local_db_path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("mirror_tcg_to_pg")


PG_DDL = """
CREATE TABLE IF NOT EXISTS cards (
    card_id                  TEXT PRIMARY KEY,
    name                     TEXT NOT NULL DEFAULT '',
    supertype                TEXT NOT NULL DEFAULT '',
    subtype                  TEXT NOT NULL DEFAULT '',
    hp                       TEXT NOT NULL DEFAULT '',
    types                    TEXT NOT NULL DEFAULT '',
    evolves_from             TEXT NOT NULL DEFAULT '',
    rarity                   TEXT NOT NULL DEFAULT '',
    artist                   TEXT NOT NULL DEFAULT '',
    number                   TEXT NOT NULL DEFAULT '',
    national_pokedex_numbers TEXT NOT NULL DEFAULT '',
    set_id                   TEXT NOT NULL DEFAULT '',
    set_name                 TEXT NOT NULL DEFAULT '',
    set_series               TEXT NOT NULL DEFAULT '',
    release_date             TEXT NOT NULL DEFAULT '',
    image_url                TEXT NOT NULL DEFAULT '',
    image_large              TEXT NOT NULL DEFAULT '',
    price_market             NUMERIC(10,2),
    raw_prices               JSONB,
    imported_at              BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cards_name        ON cards (LOWER(name));
CREATE INDEX IF NOT EXISTS idx_cards_set_number  ON cards (set_id, number);
"""

INSERT_SQL = """
INSERT INTO cards (
    card_id, name, supertype, subtype, hp, types, evolves_from, rarity,
    artist, number, national_pokedex_numbers, set_id, set_name, set_series,
    release_date, image_url, image_large, price_market, raw_prices, imported_at
) VALUES %s
"""


def _str(v) -> str:
    """Coerce SQLite scalar to plain str (None → '')."""
    return "" if v is None else str(v)


def _num(v):
    if v in (None, "", "null"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _raw_json(v):
    """SQLite stores raw_prices as a JSON string; pass through verbatim."""
    if not v:
        return None
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    s = str(v).strip()
    if not s:
        return None
    try:
        json.loads(s)
        return s
    except json.JSONDecodeError:
        return None


def main() -> int:
    sqlite_path = local_db_path()
    if not os.path.exists(sqlite_path):
        log.error("SQLite DB not found at %s — run sync_tcg_db.py --once --full first", sqlite_path)
        return 1

    pg_url = os.environ.get("DATABASE_URL")
    if not pg_url:
        log.error("DATABASE_URL is not set")
        return 1

    log.info("[mirror] reading from SQLite: %s", sqlite_path)
    sconn = sqlite3.connect(sqlite_path)
    sconn.row_factory = sqlite3.Row
    scur = sconn.cursor()

    try:
        scur.execute("SELECT COUNT(*) AS n FROM tcg_cards")
        src_count = scur.fetchone()["n"]
    except sqlite3.OperationalError as e:
        log.error("SQLite tcg_cards table missing: %s — run import_tcg_db.py --stats then sync_tcg_db.py", e)
        return 1
    log.info("[mirror] source rows: %d", src_count)
    if src_count == 0:
        log.error("Nothing to mirror — sync_tcg_db.py hasn't populated tcg_cards yet")
        return 1

    log.info("[mirror] connecting to Postgres")
    pconn = psycopg2.connect(pg_url)
    pconn.autocommit = False
    pcur = pconn.cursor()

    log.info("[mirror] ensuring schema")
    pcur.execute(PG_DDL)

    log.info("[mirror] truncating target")
    pcur.execute("TRUNCATE TABLE cards")

    now = int(time.time())
    batch: list[tuple] = []
    BATCH_SIZE = 1000
    written = 0

    scur.execute("""
        SELECT id, name, supertype, subtypes, hp, types, evolves_from, rarity,
               artist, number, national_dex, set_id, set_name, set_series,
               release_date, image_small, image_large, market_price, raw_prices
          FROM tcg_cards
    """)

    for row in scur:
        rec = (
            _str(row["id"]),
            _str(row["name"]),
            _str(row["supertype"]),
            _str(row["subtypes"]),       # SQLite "subtypes" → PG "subtype" (consolidator uses singular)
            _str(row["hp"]),
            _str(row["types"]),
            _str(row["evolves_from"]),
            _str(row["rarity"]),
            _str(row["artist"]),
            _str(row["number"]),
            _str(row["national_dex"]),   # SQLite "national_dex" → PG "national_pokedex_numbers"
            _str(row["set_id"]),
            _str(row["set_name"]),
            _str(row["set_series"]),
            _str(row["release_date"]),
            _str(row["image_small"]),    # PG "image_url" mirrors small (consolidator default)
            _str(row["image_large"]),
            _num(row["market_price"]),
            _raw_json(row["raw_prices"]),
            now,
        )
        batch.append(rec)
        if len(batch) >= BATCH_SIZE:
            psycopg2.extras.execute_values(pcur, INSERT_SQL, batch)
            written += len(batch)
            batch.clear()

    if batch:
        psycopg2.extras.execute_values(pcur, INSERT_SQL, batch)
        written += len(batch)

    pconn.commit()

    pcur.execute("SELECT COUNT(*) FROM cards")
    final = pcur.fetchone()[0]
    pcur.execute("SELECT COUNT(DISTINCT set_id) FROM cards WHERE set_id <> ''")
    distinct_sets = pcur.fetchone()[0]

    pcur.close()
    pconn.close()
    sconn.close()

    log.info("[mirror] done: wrote %d rows across %d sets", final, distinct_sets)
    print(json.dumps({"sqlite_rows": src_count, "pg_rows_written": written,
                      "pg_rows_final": final, "distinct_sets": distinct_sets}))
    return 0 if final > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
