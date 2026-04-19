"""
Multi-TCG card importer (Magic, One Piece, Lorcana, Dragon Ball Super)
======================================================================

Populates the unified `cards_multi` table from open public datasets.

Sources picked for offline-friendliness (no API keys required):
  - MTG       → Scryfall bulk-data (`oracle_cards`, ~170 MB) — manual only
  - OnePiece  → BepoTCG/OPTCG-Card-List/OPTCG.json (~3.8 MB) — auto on boot
  - Lorcana   → lorcana-api.com /cards/all (~1.6 MB)         — auto on boot
  - DBS       → no good free dataset; placeholder only

All importers UPSERT keyed on (game, card_id), so re-running them safely
refreshes prices / new prints without duplicating rows.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Iterable

import requests

log = logging.getLogger("import_multi_tcg")

GAMES = ("mtg", "onepiece", "lorcana", "dbs")
_HDR = {"User-Agent": "HanryxVault-POS/1.0"}


# ──────────────────────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────────────────────
def ensure_table(db_conn) -> None:
    cur = db_conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cards_multi (
            game         TEXT NOT NULL,
            card_id      TEXT NOT NULL,
            name         TEXT NOT NULL DEFAULT '',
            set_code     TEXT NOT NULL DEFAULT '',
            set_name     TEXT NOT NULL DEFAULT '',
            card_number  TEXT NOT NULL DEFAULT '',
            rarity       TEXT NOT NULL DEFAULT '',
            image_url    TEXT NOT NULL DEFAULT '',
            language     TEXT NOT NULL DEFAULT 'en',
            price_usd    NUMERIC(10,2),
            raw          JSONB,
            imported_at  BIGINT NOT NULL DEFAULT 0,
            PRIMARY KEY (game, card_id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_multi_name "
                "ON cards_multi (game, name)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_multi_setnum "
                "ON cards_multi (game, set_code, card_number)")
    db_conn.commit()


def cards_count(db_conn, game: str | None = None) -> int:
    cur = db_conn.cursor()
    if game:
        cur.execute("SELECT COUNT(*) FROM cards_multi WHERE game = %s", (game,))
    else:
        cur.execute("SELECT COUNT(*) FROM cards_multi")
    return int(cur.fetchone()[0])


# ──────────────────────────────────────────────────────────────────────────────
# Internal: batched UPSERT
# ──────────────────────────────────────────────────────────────────────────────
def _upsert(db_conn, rows: Iterable[tuple], *, batch: int = 500) -> int:
    cur = db_conn.cursor()
    n = 0
    buf: list = []
    sql = """
        INSERT INTO cards_multi
            (game, card_id, name, set_code, set_name, card_number,
             rarity, image_url, language, price_usd, raw, imported_at)
        VALUES (%s,%s,%s,%s,%s,%s, %s,%s,%s,%s, %s::jsonb, %s)
        ON CONFLICT (game, card_id) DO UPDATE SET
            name        = EXCLUDED.name,
            set_code    = EXCLUDED.set_code,
            set_name    = EXCLUDED.set_name,
            card_number = EXCLUDED.card_number,
            rarity      = EXCLUDED.rarity,
            image_url   = EXCLUDED.image_url,
            language    = EXCLUDED.language,
            price_usd   = EXCLUDED.price_usd,
            raw         = EXCLUDED.raw,
            imported_at = EXCLUDED.imported_at
    """
    for r in rows:
        buf.append(r)
        if len(buf) >= batch:
            cur.executemany(sql, buf); db_conn.commit()
            n += len(buf); buf.clear()
    if buf:
        cur.executemany(sql, buf); db_conn.commit()
        n += len(buf)
    return n


def _truncate_game(db_conn, game: str) -> None:
    cur = db_conn.cursor()
    cur.execute("DELETE FROM cards_multi WHERE game = %s", (game,))
    db_conn.commit()


# ──────────────────────────────────────────────────────────────────────────────
# MTG — Scryfall bulk-data
# ──────────────────────────────────────────────────────────────────────────────
SCRYFALL_BULK_MANIFEST = "https://api.scryfall.com/bulk-data"

def import_mtg(db_conn, *, force: bool = False, kind: str = "oracle_cards") -> dict:
    """
    Pull a Scryfall bulk file and UPSERT into cards_multi.
      kind = 'oracle_cards' (171 MB, recommended — one row per unique card)
           | 'default_cards' (534 MB — every printing of every card)
    """
    ensure_table(db_conn)
    if not force and cards_count(db_conn, "mtg") > 0:
        return {"game": "mtg", "skipped": True,
                "count": cards_count(db_conn, "mtg")}

    log.info("[mtg] fetching Scryfall bulk-data manifest")
    manifest = requests.get(SCRYFALL_BULK_MANIFEST, headers=_HDR, timeout=30).json()
    target = next((b for b in manifest.get("data", []) if b.get("type") == kind), None)
    if not target:
        return {"game": "mtg", "error": f"no bulk type '{kind}'"}

    url = target["download_uri"]
    size_mb = (target.get("size") or 0) / 1e6
    log.info("[mtg] downloading %s (%.0f MB) — be patient", url, size_mb)

    if force:
        _truncate_game(db_conn, "mtg")

    # Stream-parse the JSON array to keep memory flat (the file is one big array).
    resp = requests.get(url, headers=_HDR, timeout=600, stream=True)
    resp.raise_for_status()

    try:
        import ijson  # streaming JSON parser
        items = ijson.items(resp.raw, "item")
        rows = (_mtg_row(c) for c in items)
        n = _upsert(db_conn, (r for r in rows if r))
    except ImportError:
        # Fallback — load whole file (slow + RAM-heavy, but works on tiny installs)
        log.warning("[mtg] ijson not installed, falling back to full load")
        cards = resp.json()
        n = _upsert(db_conn, (r for r in (_mtg_row(c) for c in cards) if r))

    log.info("[mtg] done — imported=%d", n)
    return {"game": "mtg", "imported": n, "kind": kind, "size_mb": round(size_mb, 1)}


def _mtg_row(c: dict) -> tuple | None:
    try:
        cid = c.get("id") or ""
        if not cid:
            return None
        prices = c.get("prices") or {}
        usd = prices.get("usd") or prices.get("usd_foil")
        try: usd = float(usd) if usd else None
        except (TypeError, ValueError): usd = None
        # Scryfall puts art on `image_uris` (single-face) or on `card_faces[*].image_uris`
        img = ""
        if isinstance(c.get("image_uris"), dict):
            img = c["image_uris"].get("normal") or c["image_uris"].get("small") or ""
        elif isinstance(c.get("card_faces"), list) and c["card_faces"]:
            face = c["card_faces"][0]
            if isinstance(face.get("image_uris"), dict):
                img = face["image_uris"].get("normal") or ""
        return (
            "mtg", cid,
            c.get("name") or "",
            (c.get("set") or "").upper(),
            c.get("set_name") or "",
            c.get("collector_number") or "",
            c.get("rarity") or "",
            img,
            c.get("lang") or "en",
            usd,
            json.dumps(c, ensure_ascii=False),
            int(time.time() * 1000),
        )
    except Exception as exc:
        log.warning("[mtg] skip row: %s", exc)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# One Piece — BepoTCG/OPTCG-Card-List
# ──────────────────────────────────────────────────────────────────────────────
OP_JSON_URL = ("https://raw.githubusercontent.com/BepoTCG/OPTCG-Card-List/"
               "main/OPTCG.json")

def import_onepiece(db_conn, *, force: bool = False) -> dict:
    ensure_table(db_conn)
    if not force and cards_count(db_conn, "onepiece") > 0:
        return {"game": "onepiece", "skipped": True,
                "count": cards_count(db_conn, "onepiece")}

    log.info("[onepiece] fetching %s", OP_JSON_URL)
    data = requests.get(OP_JSON_URL, headers=_HDR, timeout=60).json()
    # Schema: list of dicts with id/code, name, rarity, set, image, etc.
    if isinstance(data, dict):
        cards = data.get("cards") or list(data.values())[0]
    else:
        cards = data

    if force:
        _truncate_game(db_conn, "onepiece")

    rows = []
    for c in cards:
        try:
            cid = (c.get("id") or c.get("code") or c.get("Code")
                   or c.get("card_id") or "")
            if not cid:
                continue
            img  = (c.get("image") or c.get("image_url") or c.get("img")
                    or c.get("ImageURL") or "")
            name = c.get("name") or c.get("Name") or ""
            rows.append((
                "onepiece", str(cid), name,
                (c.get("set") or c.get("set_code") or c.get("SetCode") or "").upper(),
                c.get("set_name") or c.get("SetName") or "",
                str(c.get("number") or c.get("card_number") or ""),
                c.get("rarity") or c.get("Rarity") or "",
                img, "en", None,
                json.dumps(c, ensure_ascii=False),
                int(time.time() * 1000),
            ))
        except Exception as exc:
            log.warning("[onepiece] skip row: %s", exc)

    n = _upsert(db_conn, rows)
    log.info("[onepiece] done — imported=%d", n)
    return {"game": "onepiece", "imported": n, "found": len(cards)}


# ──────────────────────────────────────────────────────────────────────────────
# Lorcana — lorcana-api.com
# ──────────────────────────────────────────────────────────────────────────────
LORCANA_URL = "https://api.lorcana-api.com/cards/all"

def import_lorcana(db_conn, *, force: bool = False) -> dict:
    ensure_table(db_conn)
    if not force and cards_count(db_conn, "lorcana") > 0:
        return {"game": "lorcana", "skipped": True,
                "count": cards_count(db_conn, "lorcana")}

    log.info("[lorcana] fetching %s", LORCANA_URL)
    cards = requests.get(LORCANA_URL, headers=_HDR, timeout=60).json()

    if force:
        _truncate_game(db_conn, "lorcana")

    rows = []
    for c in cards:
        try:
            set_num = c.get("Set_Num") or ""
            number  = c.get("Card_Num") or c.get("Number") or ""
            cid = (c.get("Unique_ID") or c.get("ID")
                   or f"{set_num}-{number}-{c.get('Name','')}").strip()
            if not cid:
                continue
            rows.append((
                "lorcana", str(cid),
                c.get("Name") or "",
                str(set_num).upper(),
                c.get("Set_Name") or "",
                str(number),
                c.get("Rarity") or "",
                c.get("Image") or "",
                "en", None,
                json.dumps(c, ensure_ascii=False),
                int(time.time() * 1000),
            ))
        except Exception as exc:
            log.warning("[lorcana] skip row: %s", exc)

    n = _upsert(db_conn, rows)
    log.info("[lorcana] done — imported=%d", n)
    return {"game": "lorcana", "imported": n, "found": len(cards)}


# ──────────────────────────────────────────────────────────────────────────────
# DBS — placeholder.  No good open dataset exists; will be wired once a
# scraper or licensed data source is chosen.
# ──────────────────────────────────────────────────────────────────────────────
def import_dbs(db_conn, *, force: bool = False) -> dict:
    ensure_table(db_conn)
    return {"game": "dbs", "imported": 0, "skipped": True,
            "note": "no open DBS dataset wired yet — see import_multi_tcg.py"}


# ──────────────────────────────────────────────────────────────────────────────
IMPORTERS = {
    "mtg":      import_mtg,
    "onepiece": import_onepiece,
    "lorcana":  import_lorcana,
    "dbs":      import_dbs,
}


if __name__ == "__main__":
    import argparse, psycopg2
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("game", choices=list(IMPORTERS.keys()) + ["all"])
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        targets = list(IMPORTERS.keys()) if args.game == "all" else [args.game]
        for g in targets:
            print(json.dumps(IMPORTERS[g](conn, force=args.force), indent=2))
    finally:
        conn.close()
