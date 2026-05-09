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
            name_jp TEXT, name_en TEXT, rarity TEXT,
            card_type TEXT, image_url TEXT, release_date TEXT,
            _mirror_at INTEGER,
            PRIMARY KEY (set_code, card_number)
        )
        """,
        # C13.6: matched to the actual postgres schema in server.py (init_db
        # CREATE TABLE IF NOT EXISTS cards_jpn → url,set_code,set_name,
        # series,card_number,name_en,name_jp,rarity,card_type,image_url,
        # release_date,raw,imported_at). Earlier pre-C13.5 mirror SELECT
        # referenced `hp` and `artist` which have NEVER existed in
        # postgres — both were guesses. Replaced with the real columns
        # card_type + release_date (also useful for the multi-language
        # browser) and properly dropped `hp` / `artist`.
        """
        SELECT set_code, card_number, set_name, series, name_jp, name_en,
               rarity, card_type, image_url, release_date
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
        # C13.5: postgres cards_chs has duplicate commodity_code rows
        # (the upstream taobao/CCG-CN dump occasionally lists the same
        # SKU under multiple yoren_code entries) — the SQLite PRIMARY
        # KEY (commodity_code) then 4-replicates them. DISTINCT ON +
        # ORDER BY collapses each commodity_code to its first row,
        # preferring entries with a populated image_url so the mirror
        # surfaces the most useful copy.
        """
        SELECT DISTINCT ON (commodity_code)
               commodity_code, commodity_name, collection_number, yoren_code, image_url
          FROM cards_chs
         ORDER BY commodity_code,
                  (CASE WHEN COALESCE(image_url,'') = '' THEN 1 ELSE 0 END),
                  yoren_code
        """,
        f"""
        INSERT INTO cards_chs VALUES (?,?,?,?,?,{_NOW_EPOCH_SQL})
        """,
    ),

    # ── Unified card DB layer ──────────────────────────────────────────────
    # cards_master is the post-consolidator view: one row per logical
    # (set_id, card_number, variant) with every-language name joined in.
    # This is what /tcg/search-multi queries via the new fuzzy_search
    # path. Everything below it (ref_*, src_*) is auxiliary and is
    # mirrored too so the offline POS can still rebuild the master table
    # from the USB stick if the network is gone for weeks.
    "cards_master": (
        """
        CREATE TABLE cards_master (
            master_id INTEGER PRIMARY KEY,
            set_id TEXT, card_number TEXT, variant_code TEXT,
            pokedex_id INTEGER,
            name_en TEXT, name_kr TEXT, name_jp TEXT,
            name_chs TEXT, name_cht TEXT,
            name_fr TEXT, name_de TEXT, name_it TEXT, name_es TEXT,
            card_type TEXT, energy_type TEXT, subtype TEXT, stage TEXT,
            rarity TEXT, rarity_code TEXT, hp INTEGER, artist TEXT,
            ex_serial_codes TEXT, other_pokemon TEXT, promo_source TEXT,
            image_url TEXT, image_url_alt TEXT, source_refs TEXT,
            first_seen INTEGER, last_built INTEGER,
            _mirror_at INTEGER
        )
        """,
        """
        SELECT master_id, set_id, card_number, variant_code, pokedex_id,
               name_en, name_kr, name_jp, name_chs, name_cht,
               name_fr, name_de, name_it, name_es,
               card_type, energy_type, subtype, stage,
               rarity, rarity_code, hp, artist,
               ex_serial_codes::text, other_pokemon, promo_source,
               image_url, image_url_alt::text, source_refs::text,
               first_seen, last_built
          FROM cards_master
        """,
        f"""
        INSERT INTO cards_master VALUES
          (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,{_NOW_EPOCH_SQL})
        """,
    ),

    "ref_set_mapping": (
        """
        CREATE TABLE ref_set_mapping (
            set_id TEXT PRIMARY KEY, era TEXT,
            name_en TEXT, name_kr TEXT, name_jp TEXT,
            name_chs TEXT, name_cht TEXT,
            release_year TEXT, region TEXT, aliases TEXT,
            _mirror_at INTEGER
        )
        """,
        """
        SELECT set_id, era, name_en, name_kr, name_jp,
               name_chs, name_cht, release_year, region, aliases::text
          FROM ref_set_mapping
        """,
        f"""
        INSERT INTO ref_set_mapping VALUES (?,?,?,?,?,?,?,?,?,?,{_NOW_EPOCH_SQL})
        """,
    ),

    "ref_variant_terms": (
        """
        CREATE TABLE ref_variant_terms (
            variant_code TEXT PRIMARY KEY,
            en_term TEXT, kr_term TEXT, jp_term TEXT,
            cht_term TEXT, chs_term TEXT, description TEXT,
            _mirror_at INTEGER
        )
        """,
        """
        SELECT variant_code, en_term, kr_term, jp_term,
               cht_term, chs_term, description
          FROM ref_variant_terms
        """,
        f"""
        INSERT INTO ref_variant_terms VALUES (?,?,?,?,?,?,?,{_NOW_EPOCH_SQL})
        """,
    ),

    "ref_pokedex_species": (
        """
        CREATE TABLE ref_pokedex_species (
            pokedex_no INTEGER PRIMARY KEY,
            name_en TEXT, name_jp TEXT, name_jp_kana TEXT,
            name_kr TEXT, name_chs TEXT, name_cht TEXT,
            name_fr TEXT, name_de TEXT, generation INTEGER,
            _mirror_at INTEGER
        )
        """,
        """
        SELECT pokedex_no, name_en, name_jp, name_jp_kana,
               name_kr, name_chs, name_cht, name_fr, name_de, generation
          FROM ref_pokedex_species
        """,
        f"""
        INSERT INTO ref_pokedex_species VALUES (?,?,?,?,?,?,?,?,?,?,{_NOW_EPOCH_SQL})
        """,
    ),

    "ref_promo_provenance": (
        """
        CREATE TABLE ref_promo_provenance (
            promo_id INTEGER PRIMARY KEY,
            source_category TEXT, set_label TEXT, card_number TEXT,
            name_kr TEXT, name_en TEXT, notes TEXT,
            _mirror_at INTEGER
        )
        """,
        """
        SELECT promo_id, source_category, set_label, card_number,
               name_kr, name_en, notes
          FROM ref_promo_provenance
        """,
        f"""
        INSERT INTO ref_promo_provenance VALUES (?,?,?,?,?,?,?,{_NOW_EPOCH_SQL})
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
        -- C13.3: postgres column is `set_code`, not `set_name` (aliased so the
        -- SQLite consumer side doesn't have to change). The historical `sold`
        -- column was never added to the inventory table; SOLD items just have
        -- stock=0, so we hard-zero the column for now.
        SELECT qr_code, name, set_code AS set_name, language, condition, item_type,
               grade, grading_company, stock, price, sale_price, image_url,
               0 AS sold
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
        -- C13.3: postgres sale_history is name-keyed (no qr_code column —
        -- the table predates the inventory rewrite that introduced QR codes
        -- as the primary key). sold_at is BIGINT epoch-MS, not TIMESTAMPTZ,
        -- so we divide by 1000 to match the SQLite mirror's seconds convention
        -- and compare in epoch-ms against (NOW - 90d).
        SELECT id, '' AS qr_code, name, price AS sold_price,
               (sold_at / 1000)::BIGINT AS sold_at_sec
          FROM sale_history
         WHERE sold_at >= (EXTRACT(EPOCH FROM NOW() - INTERVAL '90 days') * 1000)::BIGINT
        """,
        f"""
        INSERT INTO sale_history_recent VALUES (?,?,?,?,?,{_NOW_EPOCH_SQL})
        """,
    ),

    # price_history_recent — last 90 days. Capped at 200k rows so a
    # single chatty source can't blow the SQLite mirror past USB capacity.
    # C11: added currency + price_usd so the AI cashier can compare across
    # naver(KRW) / bunjang(KRW) / hareruya2(JPY) / cardmarket(EUR) /
    # tcgplayer(USD) without doing FX math at inference time.
    "price_history_recent": (
        """
        CREATE TABLE price_history_recent (
            id INTEGER PRIMARY KEY,
            card_id TEXT, source TEXT, grade TEXT,
            price REAL, currency TEXT, price_usd REAL,
            price_native REAL,
            observed_at INTEGER,
            _mirror_at INTEGER
        )
        """,
        # C13.5: added price_native — the actual native-currency price
        # from the scraper before USD conversion. Pre-C13.5 we only
        # stored market_price (USD) and price_usd (also USD); ai_assistant
        # was reading market_price and labelling it native_price, which
        # produced nonsense like "naver: 35.5 ₩ (~$35.5)" for a card
        # that's actually 47k KRW. COALESCE here so old rows (where
        # price_native is NULL) gracefully fall back to market_price —
        # the legacy display will be wrong but won't crash, and gets
        # corrected on the next refresh_market_prices tick.
        # Postgres market_price is still aliased to `price` for the
        # legacy /admin/market trend chart consumers.
        """
        SELECT id, card_id, source, grade, market_price AS price,
               currency, price_usd,
               COALESCE(price_native, market_price) AS price_native,
               EXTRACT(EPOCH FROM observed_at)::BIGINT
          FROM price_history
         WHERE observed_at >= NOW() - INTERVAL '90 days'
         ORDER BY observed_at DESC
         LIMIT 200000
        """,
        f"""
        INSERT INTO price_history_recent VALUES (?,?,?,?,?,?,?,?,?,{_NOW_EPOCH_SQL})
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
    # C13.4: try/except around the SQLite txn so an INSERT failure (e.g. a
    # UNIQUE constraint violation, or a column-count mismatch between SELECT
    # and INSERT) gets ROLLBACK'd. Without this, the lite_conn stays mid-
    # BEGIN forever and every subsequent _mirror_one call dies with
    # "cannot start a transaction within a transaction" — silently wiping
    # the rest of the mirror cycle (we lost inventory_snapshot + the two
    # *_recent tables this way for months).
    lite_conn.execute("BEGIN IMMEDIATE")
    try:
        lite_conn.execute(f"DROP TABLE IF EXISTS {name}")
        lite_conn.execute(ddl)
        if rows:
            lite_conn.executemany(insert_sql, rows)
        lite_conn.commit()
    except Exception:
        lite_conn.rollback()
        raise
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

    # C13.4: the sync container runs as root but the pos container's Flask
    # process runs as the unprivileged `hanryx` user (see pi-setup/Dockerfile
    # ENTRYPOINT → entrypoint.sh `exec su -s /bin/sh hanryx ...`). With the
    # default 022 umask, root-created sqlite files end up mode 644 → hanryx
    # can read but not write, and SQLite needs write access on the .db file
    # AND its directory to create the -journal/-wal/-shm sidecars even for
    # SELECT queries. Symptom: ai_assistant returns "attempt to write a
    # readonly database" while the orchestrator's mirror succeeds. Forcing
    # 0o002 here makes new files 664 (rw for group, where hanryx and root
    # share the supplementary group via the bind mount) and lets us chmod
    # any pre-existing root-644 file in-place after the cycle completes.
    os.umask(0o002)

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

    # C13.4: chmod the .db (and any -wal/-shm/-journal sidecars SQLite has
    # created by now) to 0o666, plus the parent dir to 0o777, so the
    # unprivileged hanryx user inside the pos container can open them r/w.
    # 0o664 isn't enough — hanryx (created via `useradd -r` in pi-setup/
    # Dockerfile, primary group `hanryx`) is NOT a member of root's group,
    # so a root:root 664 file is effectively read-only to it. SQLite then
    # raises "attempt to write a readonly database" even on SELECT-only
    # workloads because it tries to create -journal/-wal/-shm sidecars in
    # the same directory. World-writable is acceptable here: /mnt/cards is
    # an internal bind mount that's never exposed to untrusted users, and
    # the kiosk runs as a single appliance.
    import glob as _glob
    try:
        os.chmod(os.path.dirname(db_path), 0o777)
    except OSError as _e:
        log.warning("[mirror] chmod dir %s skipped: %s", os.path.dirname(db_path), _e)
    for _p in _glob.glob(db_path + "*"):
        try:
            os.chmod(_p, 0o666)
        except OSError as _e:
            log.warning("[mirror] chmod %s skipped: %s", _p, _e)

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
