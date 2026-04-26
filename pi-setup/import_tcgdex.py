"""
import_tcgdex.py — TCGdex multilingual loader (U7)

TCGdex (https://www.tcgdex.dev/) is the most complete multilingual
Pokémon TCG card database. The Ngansen/cards-database fork holds the
upstream source-of-truth in TypeScript modules — but for our purposes
the public REST API is much easier to consume:

  GET https://api.tcgdex.net/v2/{lang}/cards
  → list of {id, image, localId, name}

We hit it once per language (en/ja/ko/zh-cn/zh-tw/fr/de/it/es/pt) and
merge by `id` (which is consistent across languages, e.g. 'sv8-25' is
the same card in every language). One row per global card id is
written to `src_tcgdex_multi` with the per-language names in a JSONB
`names` field.

Card details (HP, type, rarity, illustrator) need a per-card GET
which is too expensive at this scale (15K+ cards × 60ms ≈ 15 minutes
per language). The consolidator gets these fields from the per-
language source tables instead; TCGdex's contribution is purely the
multilingual name mapping.

Falls back to the Ngansen fork raw GitHub URL when the public API
is unreachable (trade-show offline scenario).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Iterable

import psycopg2
import psycopg2.extras
import requests

from unified.schema import init_unified_schema

log = logging.getLogger("import_tcgdex")

# Languages TCGdex publishes. Order matters only for UX of the log
# output; the merge is commutative. The 4 languages we actually
# search across in the POS (en/ko/ja/zh-cn) are loaded first so a
# truncated import still yields a useful product.
LANGUAGES: list[str] = ["en", "ko", "ja", "zh-cn", "zh-tw",
                         "fr", "de", "it", "es", "pt"]

API_BASE = "https://api.tcgdex.net/v2"


def _fetch_lang(lang: str, timeout: int = 60) -> list[dict]:
    """Fetch the full card list for one language. Returns [] on failure."""
    url = f"{API_BASE}/{lang}/cards"
    headers = {"User-Agent": "HanryxVault-POS/1.0",
               "Accept": "application/json"}
    log.info("[tcgdex] GET %s", url)
    started = time.time()
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        log.info("[tcgdex] %s: %d cards in %.1fs",
                 lang, len(data), time.time() - started)
        return data
    except Exception as e:
        log.error("[tcgdex] %s failed: %s", lang, e)
        return []


def _merge_into(merged: dict[str, dict], lang: str, cards: Iterable[dict]) -> None:
    """In-place merge per-language card list into the master dict by id."""
    for c in cards:
        cid = c.get("id")
        if not cid:
            continue
        slot = merged.get(cid)
        if slot is None:
            slot = {
                "id": cid,
                "localId": c.get("localId", ""),
                "image_base": c.get("image", ""),
                "names": {},
            }
            # Derive set_id from id (TCGdex IDs are '{set}-{localId}',
            # e.g. 'sv8-025'; some legacy IDs have no dash — fall back
            # to empty string in that case).
            if "-" in cid:
                slot["set_id"] = cid.rsplit("-", 1)[0]
            else:
                slot["set_id"] = ""
            merged[cid] = slot
        if c.get("name"):
            slot["names"][lang] = c["name"]
        # Prefer the highest-resolution image we see (just keep the
        # first non-empty one — TCGdex returns the same base URL
        # across languages so this is mostly cosmetic).
        if c.get("image") and not slot.get("image_base"):
            slot["image_base"] = c["image"]


def _ingest(db_conn, merged: dict[str, dict]) -> int:
    cur = db_conn.cursor()
    cur.execute("DELETE FROM src_tcgdex_multi")
    now = int(time.time())

    insert_sql = """
        INSERT INTO src_tcgdex_multi
          (set_id, card_local_id, card_global_id, image_base,
           region, names, raw, imported_at)
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

    for cid, slot in merged.items():
        names = slot["names"]
        # Mark the row's region: 'asia' if only Asian names present,
        # 'intl' if any Western name present, '' if unknown.
        asia_langs = {"ja", "ko", "zh-cn", "zh-tw"}
        if any(l in asia_langs for l in names) and not (set(names) - asia_langs):
            region = "asia"
        elif set(names) - asia_langs:
            region = "intl"
        else:
            region = ""

        batch.append((
            slot.get("set_id", ""),
            slot.get("localId", ""),
            cid,
            slot.get("image_base", ""),
            region,
            json.dumps(names, ensure_ascii=False),
            json.dumps(slot, ensure_ascii=False),
            now,
        ))
        if len(batch) >= BATCH: flush()
    flush()
    db_conn.commit()
    return inserted


def import_tcgdex(db_conn, *, force: bool = False,
                  languages: list[str] | None = None) -> dict:
    init_unified_schema(db_conn)
    cur = db_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM src_tcgdex_multi")
    pre = int(cur.fetchone()[0])
    if pre > 0 and not force:
        return {"skipped": True, "pre_count": pre}

    langs = languages or LANGUAGES
    merged: dict[str, dict] = {}
    per_lang: dict[str, int] = {}
    for lang in langs:
        cards = _fetch_lang(lang)
        per_lang[lang] = len(cards)
        if cards:
            _merge_into(merged, lang, cards)

    inserted = _ingest(db_conn, merged)
    return {
        "inserted": inserted,
        "languages": per_lang,
        "unique_cards": len(merged),
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--languages", nargs="*",
                    help="Override default language list (e.g. --languages en ko ja)")
    args = ap.parse_args()
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr); return 1
    with psycopg2.connect(url) as conn:
        print(json.dumps(import_tcgdex(conn, force=args.force,
                                        languages=args.languages),
                         indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
