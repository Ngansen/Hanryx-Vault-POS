"""
Japanese TCG cards importer (Pokellector via PokeScraper_3.0)
==============================================================

Reads CSV files produced by `Ngansen/PokeScraper_3.0` and UPSERTs them into
the `cards_jpn` table.  PokeScraper itself is a Selenium-based scraper that
needs Chrome — too heavy to ship inside the POS container, so the recommended
flow is:

    1. Run PokeScraper_3.0 on a dev machine (or in a one-shot container)
    2. Drop the resulting CSV files into  ./data/jp_pokellector/  on the Pi
       (this directory is bind-mounted into the POS container as
       /app/data/jp_pokellector — see docker-compose.yml)
    3. POST /admin/jpn-cards/refresh to trigger the import

CSV schema is loose — we look for the columns we recognise and ignore the
rest.  Recognised columns (case-insensitive): URL/CardURL, SetName/Set,
Series, Name/CardName, Number/CardNumber, Rarity, Image/ImageURL, Type,
ReleaseDate, JapaneseName/NameJP.
"""
from __future__ import annotations

import csv
import glob
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("import_jpn_cards")

DATA_DIR = os.environ.get("JPN_DATA_DIR", "/app/data/jp_pokellector")


def ensure_table(db_conn) -> None:
    cur = db_conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cards_jpn (
            url           TEXT PRIMARY KEY,
            set_code      TEXT NOT NULL DEFAULT '',
            set_name      TEXT NOT NULL DEFAULT '',
            series        TEXT NOT NULL DEFAULT '',
            card_number   TEXT NOT NULL DEFAULT '',
            name_en       TEXT NOT NULL DEFAULT '',
            name_jp       TEXT NOT NULL DEFAULT '',
            rarity        TEXT NOT NULL DEFAULT '',
            card_type     TEXT NOT NULL DEFAULT '',
            image_url     TEXT NOT NULL DEFAULT '',
            release_date  TEXT NOT NULL DEFAULT '',
            raw           JSONB,
            imported_at   BIGINT NOT NULL DEFAULT 0
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cards_jpn_name_en "
                "ON cards_jpn (name_en)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cards_jpn_name_jp "
                "ON cards_jpn (name_jp)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cards_jpn_setnum "
                "ON cards_jpn (set_code, card_number)")
    db_conn.commit()


def cards_count(db_conn) -> int:
    cur = db_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM cards_jpn")
    return int(cur.fetchone()[0])


# Fuzzy column name → canonical mapping
_COL_MAP = {
    "url": "url", "cardurl": "url", "card_url": "url",
    "setname": "set_name", "set": "set_name", "set_name": "set_name",
    "setcode": "set_code", "set_code": "set_code", "code": "set_code",
    "series": "series",
    "name": "name_en", "cardname": "name_en", "name_en": "name_en",
    "englishname": "name_en", "card_name": "name_en",
    "japanesename": "name_jp", "name_jp": "name_jp", "jpname": "name_jp",
    "namejp": "name_jp", "japanese": "name_jp",
    "number": "card_number", "cardnumber": "card_number",
    "card_number": "card_number", "no": "card_number", "num": "card_number",
    "rarity": "rarity",
    "type": "card_type", "cardtype": "card_type", "card_type": "card_type",
    "image": "image_url", "imageurl": "image_url", "image_url": "image_url",
    "img": "image_url",
    "releasedate": "release_date", "release_date": "release_date",
    "released": "release_date",
}


def _normalise_row(raw: dict) -> dict:
    """Map arbitrary CSV columns onto our canonical schema."""
    out = {
        "url": "", "set_code": "", "set_name": "", "series": "",
        "card_number": "", "name_en": "", "name_jp": "",
        "rarity": "", "card_type": "", "image_url": "", "release_date": "",
    }
    for k, v in raw.items():
        if v is None:
            continue
        key = k.strip().lower().replace(" ", "_")
        canonical = _COL_MAP.get(key)
        if canonical:
            out[canonical] = str(v).strip()
    if not out["url"]:
        # synthesise a stable key when the CSV has no URL column
        out["url"] = (f"local://{out['set_code']}/{out['card_number']}/"
                      f"{out['name_en'] or out['name_jp']}").strip()
    if not out["set_code"] and out["url"]:
        # try to extract set code from a Pokellector URL
        m = out["url"].rstrip("/").split("/")
        if len(m) >= 2:
            out["set_code"] = m[-2][:50]
    return out


def import_jp_cards(db_conn, *, force: bool = False,
                    data_dir: str | None = None) -> dict:
    ensure_table(db_conn)

    folder = Path(data_dir or DATA_DIR)
    if not folder.exists():
        return {"imported": 0, "found": 0, "files": [],
                "error": f"data dir not found: {folder}"}

    csv_files = sorted(glob.glob(str(folder / "*.csv")))
    if not csv_files:
        return {"imported": 0, "found": 0, "files": [],
                "error": f"no CSV files in {folder}"}

    if force:
        cur = db_conn.cursor()
        cur.execute("TRUNCATE cards_jpn")
        db_conn.commit()

    imported = found = 0
    rows: list[tuple] = []
    seen: set[str] = set()
    BATCH = 500

    def _flush():
        if not rows:
            return
        cur = db_conn.cursor()
        cur.executemany("""
            INSERT INTO cards_jpn (url, set_code, set_name, series, card_number,
                                   name_en, name_jp, rarity, card_type, image_url,
                                   release_date, raw, imported_at)
            VALUES (%s,%s,%s,%s,%s, %s,%s,%s,%s,%s, %s, %s::jsonb, %s)
            ON CONFLICT (url) DO UPDATE SET
                set_code     = EXCLUDED.set_code,
                set_name     = EXCLUDED.set_name,
                series       = EXCLUDED.series,
                card_number  = EXCLUDED.card_number,
                name_en      = EXCLUDED.name_en,
                name_jp      = EXCLUDED.name_jp,
                rarity       = EXCLUDED.rarity,
                card_type    = EXCLUDED.card_type,
                image_url    = EXCLUDED.image_url,
                release_date = EXCLUDED.release_date,
                raw          = EXCLUDED.raw,
                imported_at  = EXCLUDED.imported_at
        """, rows)
        rows.clear()

    for path in csv_files:
        log.info("[jpn-import] reading %s", path)
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            reader = csv.DictReader(fh)
            for raw in reader:
                found += 1
                norm = _normalise_row(raw)
                # require *something* identifying the card
                if not (norm["name_en"] or norm["name_jp"]):
                    continue
                if norm["url"] in seen:
                    continue
                seen.add(norm["url"])
                rows.append((
                    norm["url"], norm["set_code"], norm["set_name"], norm["series"],
                    norm["card_number"], norm["name_en"], norm["name_jp"],
                    norm["rarity"], norm["card_type"], norm["image_url"],
                    norm["release_date"],
                    json.dumps(raw, ensure_ascii=False),
                    int(time.time() * 1000),
                ))
                imported += 1
                if len(rows) >= BATCH:
                    _flush()
    _flush()
    db_conn.commit()

    log.info("[jpn-import] done — files=%d found=%d imported=%d",
             len(csv_files), found, imported)
    return {
        "imported": imported, "found": found,
        "files": [Path(p).name for p in csv_files],
        "data_dir": str(folder),
    }


if __name__ == "__main__":
    import argparse, psycopg2
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dir")
    args = ap.parse_args()
    dsn = os.environ["DATABASE_URL"]
    conn = psycopg2.connect(dsn)
    try:
        print(json.dumps(import_jp_cards(conn, force=args.force,
                                          data_dir=args.dir), indent=2))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Targeted backfill hook for cluster_backfill.
# ---------------------------------------------------------------------------
def backfill_codes(db_conn, set_codes: list[str]) -> dict:
    """Re-run the Japanese CSV import (UPSERT-safe) and log it."""
    try:
        import source_state
        run_id = source_state.begin_run(
            db_conn, source="jpn_cards",
            notes=("backfill: " + ",".join(set_codes[:10])
                   + ("…" if len(set_codes) > 10 else "")),
        )
    except Exception:
        run_id = None
    try:
        before = cards_count(db_conn)
        import_jp_cards(db_conn, force=False)
        after = cards_count(db_conn)
        added = max(0, after - before)
        if run_id is not None:
            source_state.end_run(db_conn, run_id, ok=True,
                                 rows_seen=after, rows_inserted=added)
        return {"ok": True, "added": added, "total": after,
                "set_codes": set_codes}
    except Exception as exc:
        if run_id is not None:
            try: source_state.end_run(db_conn, run_id, ok=False,
                                      errors=1, notes=str(exc)[:300])
            except Exception: pass
        return {"ok": False, "error": str(exc)}
