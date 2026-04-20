#!/usr/bin/env python3
"""
Build the recognizer's perceptual-hash index from existing card datasets.

Walks every cards_* table that's been imported, downloads each card's
artwork (cached to disk), computes a 64-bit perceptual hash, and writes
the result to the `card_hashes` table that the recognizer service loads
into memory at startup.

This is idempotent and resumable: rows already present in card_hashes are
skipped, so re-running just picks up newly-imported cards.

Usage (inside the pos container, or any host with the same env):

    # Hash everything, 10 parallel downloads:
    python import_artwork_hashes.py

    # Just one source, useful for testing:
    python import_artwork_hashes.py --source kr --limit 50

    # Force re-hash (e.g. after upgrading the hash algorithm):
    python import_artwork_hashes.py --rehash

Sources walked: kr, chs, jpn, jpn_pocket, multi (one row per game).
"""
from __future__ import annotations

import argparse
import hashlib
import io
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import imagehash
import psycopg2
import psycopg2.extras
import requests
from PIL import Image

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("import_hashes")

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://vaultpos:vaultpos@db:5432/vaultpos",
)
CACHE_DIR = Path(os.environ.get("CARD_IMAGE_CACHE", "/app/card-images"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
HTTP_TIMEOUT = 20
USER_AGENT = "HanRyxVault-ArtworkHasher/1.0"


SOURCES = {
    # source key → (table, select-sql returning the columns we need)
    "kr": ("cards_kr",
           "SELECT card_id, set_name AS set_code, card_number, "
           "name_kr AS name, image_url, 'kr' AS language "
           "FROM cards_kr WHERE image_url <> ''"),
    "chs": ("cards_chs",
            "SELECT card_id, set_name AS set_code, card_number, "
            "name_chs AS name, image_url, 'chs' AS language "
            "FROM cards_chs WHERE image_url <> ''"),
    "jpn": ("cards_jpn",
            "SELECT card_id, set_name AS set_code, card_number, "
            "name_jp AS name, image_url, 'jpn' AS language "
            "FROM cards_jpn WHERE image_url <> ''"),
    "jpn_pocket": ("cards_jpn_pocket",
                   "SELECT card_id, set_code, card_number, name, "
                   "image_url, 'jpn' AS language "
                   "FROM cards_jpn_pocket WHERE image_url <> ''"),
    "multi": ("cards_multi",
              "SELECT 'multi:' || game AS source_key, card_id, set_code, "
              "card_number, name, image_url, language "
              "FROM cards_multi WHERE image_url <> ''"),
}


def _connect():
    return psycopg2.connect(DB_URL, connect_timeout=10)


def _ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS card_hashes (
                source       TEXT NOT NULL,
                card_id      TEXT NOT NULL,
                set_code     TEXT NOT NULL DEFAULT '',
                card_number  TEXT NOT NULL DEFAULT '',
                name         TEXT NOT NULL DEFAULT '',
                language     TEXT NOT NULL DEFAULT '',
                image_url    TEXT NOT NULL DEFAULT '',
                phash        BIGINT NOT NULL,
                image_sha    TEXT NOT NULL DEFAULT '',
                created_at   BIGINT NOT NULL DEFAULT 0,
                PRIMARY KEY (source, card_id)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_card_hashes_setnum "
                    "ON card_hashes (set_code, card_number)")
    conn.commit()


def _table_exists(conn, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (table,))
        return cur.fetchone()[0] is not None


def _existing_keys(conn, source: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT card_id FROM card_hashes WHERE source = %s",
                    (source,))
        return {row[0] for row in cur.fetchall()}


def _download(url: str) -> bytes:
    """Fetch an artwork URL, caching by URL hash on disk."""
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    cache = CACHE_DIR / f"{key}.bin"
    if cache.exists() and cache.stat().st_size > 0:
        return cache.read_bytes()
    r = requests.get(url, timeout=HTTP_TIMEOUT,
                     headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    cache.write_bytes(r.content)
    return r.content


def _phash_bytes(data: bytes) -> int:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    return int(str(imagehash.phash(img)), 16)


def _hash_one(row: dict, source: str) -> tuple | None:
    """Worker — download, hash, return a tuple ready for INSERT (or None)."""
    url = row.get("image_url") or ""
    if not url:
        return None
    try:
        data = _download(url)
        phash_int = _phash_bytes(data)
        # Postgres BIGINT is signed 64-bit; map down so the value fits.
        if phash_int >= (1 << 63):
            phash_signed = phash_int - (1 << 64)
        else:
            phash_signed = phash_int
        sha = hashlib.sha256(data).hexdigest()
        return (
            row.get("source_key") or source,
            row["card_id"],
            row.get("set_code", "") or "",
            row.get("card_number", "") or "",
            row.get("name", "") or "",
            row.get("language", "") or "",
            url,
            phash_signed,
            sha,
            int(time.time()),
        )
    except Exception as exc:
        log.warning("[%s/%s] hash failed: %s", source, row["card_id"], exc)
        return None


def _process_source(conn, source: str, limit: int | None,
                    rehash: bool, workers: int) -> tuple[int, int]:
    table, sql = SOURCES[source]
    if not _table_exists(conn, table):
        log.info("[%s] table '%s' missing — skipping", source, table)
        return (0, 0)

    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute(sql)
    rows = [dict(r) for r in cur.fetchall()]
    if limit:
        rows = rows[:limit]
    if not rows:
        log.info("[%s] no rows with image_url", source)
        return (0, 0)

    skip = set() if rehash else _existing_keys(conn, source)
    todo = [r for r in rows if r["card_id"] not in skip]
    log.info("[%s] %d total, %d already hashed, %d to do",
             source, len(rows), len(rows) - len(todo), len(todo))
    if not todo:
        return (0, 0)

    inserted, failed = 0, 0
    batch: list[tuple] = []
    BATCH = 100

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_hash_one, r, source): r for r in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            if res is None:
                failed += 1
            else:
                batch.append(res)
            if len(batch) >= BATCH or i == len(futures):
                if batch:
                    with conn.cursor() as ic:
                        psycopg2.extras.execute_values(ic, """
                            INSERT INTO card_hashes
                              (source, card_id, set_code, card_number, name,
                               language, image_url, phash, image_sha, created_at)
                            VALUES %s
                            ON CONFLICT (source, card_id) DO UPDATE SET
                              phash       = EXCLUDED.phash,
                              image_sha   = EXCLUDED.image_sha,
                              image_url   = EXCLUDED.image_url,
                              created_at  = EXCLUDED.created_at
                        """, batch)
                    conn.commit()
                    inserted += len(batch)
                    batch.clear()
            if i % 50 == 0 or i == len(futures):
                log.info("[%s] %d / %d (%d failed)",
                         source, i, len(futures), failed)
    return (inserted, failed)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=list(SOURCES) + ["all"],
                    default="all")
    ap.add_argument("--limit", type=int, default=None,
                    help="Process at most N cards per source (debug)")
    ap.add_argument("--rehash", action="store_true",
                    help="Re-hash cards even if already in card_hashes")
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()

    conn = _connect()
    _ensure_schema(conn)

    targets = list(SOURCES) if args.source == "all" else [args.source]
    grand_inserted = grand_failed = 0
    for src in targets:
        ins, fail = _process_source(conn, src, args.limit,
                                    args.rehash, args.workers)
        grand_inserted += ins
        grand_failed += fail

    conn.close()
    log.info("DONE — inserted/updated %d hashes, %d failed",
             grand_inserted, grand_failed)
    return 0 if grand_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
