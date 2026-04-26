"""
usb_mirror.py — Postgres → SQLite read-replica sync.

Mirrors the multi-language card tables (and recent price history) from the
authoritative Postgres database into a single SQLite file on the USB drive
at `/mnt/cards/pokedex_local.db`. Once the mirror is built, the POS, the
fuzzy search, and the AI assistant can answer queries with zero Postgres
dependency — useful at trade shows when the WiFi dies and the Pi loses
all upstream connectivity to anything that's not on the same LAN.

Why mirror, not move
--------------------
The earlier version of the plan considered moving the entire Postgres
data directory (`pgdata` Docker volume) onto the USB drive. We rejected
that because:

  1. Postgres holds inventory, sales, laybys, P&L — the live business
     state. Tying that to USB-plugged-in-ness creates a single point of
     failure where pulling the USB at the wrong moment crashes every
     write. The shop loses transactions, not just cached card data.
  2. Postgres on a slow USB stick is meaningfully slower than Postgres
     on the SD card under WAL pressure.
  3. The portability we actually want is "carry the card data and recent
     prices to a different machine for backup / debugging / a second
     kiosk", which a SQLite snapshot does perfectly without any of the
     concurrency story Postgres needs.

So: live writes go to Postgres on the SD card (fast, durable, Docker
volume managed). The mirror runs every N minutes and projects the
read-only card / price tables into one portable SQLite file on USB.

Tables mirrored (all with a `_mirror_at` epoch column added):
    cards_kr            from Postgres `cards_kr`
    cards_jpn           from Postgres `cards_jpn`
    cards_jpn_pocket    from Postgres `cards_jpn_pocket`
    cards_chs           from Postgres `cards_chs`
    inventory_snapshot  from Postgres `inventory`  (subset of columns)
    sale_history_recent from Postgres `sale_history`  (last 90 days)
    price_history_recent from Postgres `price_history`  (last 90 days, capped)

The mirror is idempotent and safe to run mid-write: each table is
rebuilt inside one transaction (DROP + CREATE + bulk INSERT), so a
reader that races sees either the old fully-populated table or the new
fully-populated table — never a half-built one.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterable

import psycopg2
import psycopg2.extras

from cards_db_path import (
    assert_usb_configured,
    local_db_path,
    sync_log_dir,
)

log = logging.getLogger("usb_mirror")

# ── Connection helpers ─────────────────────────────────────────────────────────


def _pg_url() -> str:
    """Postgres connection URL.

    Reads DATABASE_URL (set on the pos container by docker-compose) so the
    orchestrator container can share the same env var without duplicating
    the secret in two places.
    """
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set — the sync orchestrator needs Postgres "
            "credentials to read from. Inherit the var from the pos service "
            "in docker-compose.yml."
        )
    return url


@contextmanager
def _pg_conn():
    conn = psycopg2.connect(_pg_url())
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _sqlite_conn(path: str):
    conn = sqlite3.connect(path)
    # WAL gives us readers-don't-block-writers semantics, which is exactly
    # what we need: the mirror writes for ~1s every N minutes; the POS
    # reads constantly. Without WAL the POS sees SQLITE_BUSY on every
    # mirror tick.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    try:
        yield conn
    finally:
        conn.close()


# ── Per-table mirrors ──────────────────────────────────────────────────────────
# Each entry: (sqlite_create_sql, pg_select_sql, sqlite_insert_sql)
# Each tuple is one fully-described table. Adding a new mirror is one entry.

_NOW_EPOCH_SQL = "CAST(strftime('%s','now') AS INTEGER)"

_MIRRORS: dict[str, tuple[str, str, str]] = {
    # cards_kr — Korean card metadata (no images blobbed; image_url stays as
    # an external CDN reference to pokemonkorea.co.kr). 14MB JSON imported
    # by import_kr_cards.py.
    "cards_kr": (
        """
        CREATE TABLE cards_kr (
            card_id TEXT, prod_code TEXT, card_number TEXT,
            set_name TEXT, name_kr TEXT, pokedex_no INTEGER,
            supertype TEXT, subtype TEXT, hp INTEGER, type_kr TEXT,
            rarity TEXT, artist TEXT, prod_number TEXT,
            image_url TEXT, flavor_text TEXT,
            _mirror_at INTEGER,
            PRIMARY KEY (card_id, prod_code, card_number)
        )
        """,
        """
        SELECT card_id, prod_code, card_number, set_name, name_kr,
               pokedex_no, supertype, subtype, hp, type_kr, rarity,
               artist, prod_number, image_url, flavor_text
          FROM cards_kr
        """,
        f"""
        INSERT INTO cards_kr VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,{_NOW_EPOCH_SQL})
        """,
    ),

    # cards_jpn — full Japanese card pool from import_jpn_cards.py.
    "cards_jpn": (
        """
        CREATE TABLE cards_jpn (
            set_code TEXT, card_number TEXT, set_name TEXT, series TEXT,
            name_jp TEXT, name_en TEXT, rarity TEXT, hp INTEGER,
            artist TEXT, image_url TEXT,
            _mirror_at INTEGER,
            PRIMARY KEY (set_code, card_number)
        )
        """,
        """
        SELECT set_code, card_number, set_name, series, name_jp, name_en,
               rarity, hp, artist, image_url
          FROM cards_jpn
        """,
        f"""
        INSERT INTO cards_jpn VALUES (?,?,?,?,?,?,?,?,?,?,{_NOW_EPOCH_SQL})
        """,
    ),

    # cards_jpn_pocket — TCG Pocket app cards (different release cadence
    # to physical Japanese cards, hence its own table).
    "cards_jpn_pocket": (
        """
        CREATE TABLE cards_jpn_pocket (
            set_code TEXT, card_number TEXT, name TEXT, rarity TEXT,
            image_url TEXT,
            _mirror_at INTEGER,
            PRIMARY KEY (set_code, card_number)
        )
        """,
        """
        SELECT set_code, card_number, name, rarity, image_url
          FROM cards_jpn_pocket
        """,
        f"""
        INSERT INTO cards_jpn_pocket VALUES (?,?,?,?,?,{_NOW_EPOCH_SQL})
        """,
    ),

    # cards_chs — Chinese card pool (covers both Simplified and Traditional;
    # commodity_code is the official 商品编码 from cn.pokemon.com).
    "cards_chs": (
        """
        CREATE TABLE cards_chs (
            commodity_code TEXT, commodity_name TEXT,
            collection_number TEXT, yoren_code TEXT,
            image_url TEXT,
            _mirror_at INTEGER,
            PRIMARY KEY (commodity_code)
        )
        """,
        """
        SELECT commodity_code, commodity_name, collection_number, yoren_code, image_url
          FROM cards_chs
        """,
        f"""
        INSERT INTO cards_chs VALUES (?,?,?,?,?,{_NOW_EPOCH_SQL})
        """,
    ),

    # inventory_snapshot — what's currently in stock and for sale. Subset
    # of columns; full inventory stays in Postgres because the POS writes
    # to it constantly and the mirror would lag the live view.
    "inventory_snapshot": (
        """
        CREATE TABLE inventory_snapshot (
            qr_code TEXT PRIMARY KEY, name TEXT, set_name TEXT,
            language TEXT, condition TEXT, item_type TEXT,
            grade TEXT, grading_company TEXT, stock INTEGER,
            price REAL, sale_price REAL, image_url TEXT, sold INTEGER,
            _mirror_at INTEGER
        )
        """,
        """
        SELECT qr_code, name, set_name, language, condition, item_type,
               grade, grading_company, stock, price, sale_price, image_url,
               COALESCE(sold, 0)
          FROM inventory
        """,
        f"""
        INSERT INTO inventory_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,{_NOW_EPOCH_SQL})
        """,
    ),

    # sale_history_recent — last 90 days, used by the AI assistant for
    # "what sold lately" questions. Full history stays in Postgres.
    "sale_history_recent": (
        """
        CREATE TABLE sale_history_recent (
            id INTEGER PRIMARY KEY,
            qr_code TEXT, name TEXT, sold_price REAL, sold_at INTEGER,
            _mirror_at INTEGER
        )
        """,
        """
        SELECT id, qr_code, name, sold_price,
               EXTRACT(EPOCH FROM sold_at)::BIGINT
          FROM sale_history
         WHERE sold_at >= NOW() - INTERVAL '90 days'
        """,
        f"""
        INSERT INTO sale_history_recent VALUES (?,?,?,?,?,{_NOW_EPOCH_SQL})
        """,
    ),

    # price_history_recent — last 90 days. Capped at 200k rows so a
    # single chatty source can't blow the SQLite mirror past USB capacity.
    "price_history_recent": (
        """
        CREATE TABLE price_history_recent (
            id INTEGER PRIMARY KEY,
            card_id TEXT, source TEXT, grade TEXT,
            price REAL, observed_at INTEGER,
            _mirror_at INTEGER
        )
        """,
        """
        SELECT id, card_id, source, grade, price,
               EXTRACT(EPOCH FROM observed_at)::BIGINT
          FROM price_history
         WHERE observed_at >= NOW() - INTERVAL '90 days'
         ORDER BY observed_at DESC
         LIMIT 200000
        """,
        f"""
        INSERT INTO price_history_recent VALUES (?,?,?,?,?,?,{_NOW_EPOCH_SQL})
        """,
    ),
}


def _mirror_one(pg_conn, lite_conn: sqlite3.Connection, name: str, ddl: str, select_sql: str, insert_sql: str) -> int:
    """Mirror one table inside a single SQLite transaction. Returns row count.

    A fresh psycopg2 cursor is opened per table — sharing one named
    server-side cursor across multiple SELECTs raises ProgrammingError
    after the first iteration. Client-side cursors are fine here because
    even our largest mirrored table (price_history_recent, capped at 200k
    rows) fits in ~10 MB of RAM.
    """
    # The Postgres SELECT may legitimately fail if the source table doesn't
    # exist yet (fresh Pi where the importers haven't run). We log and
    # skip rather than aborting the whole mirror cycle.
    pg_cur = pg_conn.cursor(cursor_factory=psycopg2.extras.NamedTupleCursor)
    try:
        try:
            pg_cur.execute(select_sql)
            rows = pg_cur.fetchall()
        except psycopg2.errors.UndefinedTable:
            log.warning("[mirror] %s skipped: source table does not exist in Postgres", name)
            pg_conn.rollback()
            return 0
        except Exception as e:
            log.error("[mirror] %s SELECT failed: %s", name, e)
            pg_conn.rollback()
            raise
    finally:
        pg_cur.close()

    # SQLite WAL mode + BEGIN IMMEDIATE: this entire DROP+CREATE+INSERT
    # block is atomic to readers — they either see the OLD fully-populated
    # table or the NEW fully-populated table, never a half-built one. The
    # short window where the table doesn't exist (between DROP and the
    # next reader's query) is irrelevant because the writer holds the
    # write lock for the whole transaction.
    lite_conn.execute("BEGIN IMMEDIATE")
    lite_conn.execute(f"DROP TABLE IF EXISTS {name}")
    lite_conn.execute(ddl)
    if rows:
        lite_conn.executemany(insert_sql, rows)
    lite_conn.commit()
    return len(rows)


def run_mirror() -> dict:
    """Run one full mirror cycle. Returns per-table row counts + duration.

    Idempotent and safe to call serially from sync_orchestrator.py. Not
    safe to call from two processes simultaneously — see the pidfile lock
    in sync_orchestrator.py for the cross-process guarantee.
    """
    assert_usb_configured()
    started = time.time()
    db_path = local_db_path()
    counts: dict[str, int] = {}

    with _pg_conn() as pg, _sqlite_conn(db_path) as lite:
        for name, (ddl, select_sql, insert_sql) in _MIRRORS.items():
            try:
                counts[name] = _mirror_one(pg, lite, name, ddl, select_sql, insert_sql)
                log.info("[mirror] %s: %d rows", name, counts[name])
            except Exception as e:
                # One table failing does not abort the others; we want a
                # partial mirror to be usable. The orchestrator's status
                # endpoint reports per-table errors.
                log.error("[mirror] %s failed: %s", name, e)
                counts[name] = -1

    elapsed = time.time() - started
    summary = {"counts": counts, "elapsed_sec": round(elapsed, 2), "ts": int(time.time())}

    # Write the latest result to /mnt/cards/logs/mirror_status.json so the
    # /admin/usb-sync/status endpoint can read it without poking inside the
    # orchestrator container.
    status_path = sync_log_dir() / "mirror_status.json"
    status_path.write_text(json.dumps(summary, indent=2))

    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(json.dumps(run_mirror(), indent=2))
