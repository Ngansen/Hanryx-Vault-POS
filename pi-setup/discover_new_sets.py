#!/usr/bin/env python3
"""
discover_new_sets.py — multi-language new-set probe (D2).

Hits TCGdex's per-language set endpoints in parallel and enqueues every
set ID we don't already know about into `discovery_queue` for the
dispatcher to pick up on its next tick.

Why per-language? TCGdex's English endpoint deliberately omits sets that
have no English release — most JP-exclusive promo bundles, Pokemon Korea
exclusives, and the Simplified-Chinese reprints all fall into this bucket.
A trade-show POS that only catches English releases would miss exactly
the cards its multilingual customers are most likely to bring in.

Source: TCGdex public REST API (https://api.tcgdex.net/v2). No key,
no auth, no rate limit at our volume (~1 req per language per day).

CLI:
    python3 discover_new_sets.py                  # default 5 languages
    python3 discover_new_sets.py --languages en ja ko
    python3 discover_new_sets.py --force          # ignore short-circuit

Idempotent — safe to run on every orchestrator tick. The unique partial
index `uq_discovery_queue_pending_set` prevents duplicate pending rows
for the same set_id.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg2
import requests

from unified.schema import init_unified_schema

log = logging.getLogger("discover_new_sets")

API_BASE = "https://api.tcgdex.net/v2"

# Languages we probe by default. EN/JA/KO/ZH-TW are the four real-world
# trade-show audiences; ZH-CN catches the (small but growing) Simplified
# Chinese exclusives. Order is irrelevant — fetched in parallel.
DEFAULT_LANGUAGES: list[str] = ["en", "ja", "ko", "zh-tw", "zh-cn"]

# Per-language fetch timeout. TCGdex is fast (~300 ms typical), but trade-show
# WiFi is sometimes terrible; 30 s is the right ceiling for a daily probe.
HTTP_TIMEOUT_SEC = 30


def _fetch_sets(lang: str) -> list[dict]:
    """Fetch the full set list for one language. Returns [] on failure.

    Returns rows of the shape:
        {id: 'sv9', name: 'Battle Partners', cardCount: {total, official},
         releaseDate: '2025-01-24', logo: '...', symbol: '...'}
    """
    url = f"{API_BASE}/{lang}/sets"
    headers = {
        "User-Agent": "HanryxVault-POS/discover/1.0",
        "Accept": "application/json",
    }
    started = time.time()
    try:
        r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT_SEC)
        r.raise_for_status()
        data = r.json()
        log.info("[probe] %s: %d sets in %.1fs",
                 lang, len(data), time.time() - started)
        return data if isinstance(data, list) else []
    except Exception as e:
        log.warning("[probe] %s failed (%s) — other languages will continue", lang, e)
        return []


def _existing_known_set_ids(cur) -> set[str]:
    """Collect set IDs we already know about — both fully imported and queued.

    A set is 'known' if it appears in:
      * `ref_set_mapping` (canonical set list, fully imported)
      * `discovery_queue` with a non-failed status (pending/running/resolved)

    Failed rows ARE re-probed — gives us a chance to re-discover after a
    transient upstream outage without operator action.
    """
    known: set[str] = set()
    cur.execute("SELECT set_id FROM ref_set_mapping")
    known.update(row[0] for row in cur.fetchall() if row[0])

    cur.execute(
        """
        SELECT payload->>'set_id'
          FROM discovery_queue
         WHERE kind = 'set'
           AND status IN ('pending','running','resolved')
        """
    )
    known.update(row[0] for row in cur.fetchall() if row[0])
    return known


def _aggregate_by_set_id(per_lang: dict[str, list[dict]]) -> dict[str, dict]:
    """Collapse per-language responses into one row per set_id with all
    language names attached and a list of which languages returned it.

    Output shape per set_id:
        {set_id, name_en, name_ja, name_ko, name_zh_tw, name_zh_cn,
         release_date, card_count_total, card_count_official, languages}
    """
    out: dict[str, dict] = {}
    for lang, sets in per_lang.items():
        for s in sets:
            sid = (s.get("id") or "").strip()
            if not sid:
                continue
            row = out.setdefault(sid, {
                "set_id": sid,
                "name_en": "", "name_ja": "", "name_ko": "",
                "name_zh_tw": "", "name_zh_cn": "",
                "release_date": "",
                "card_count_total": 0,
                "card_count_official": 0,
                "languages": [],
            })
            # First non-empty value wins per language; later passes don't
            # overwrite — TCGdex sometimes returns the EN name in non-EN
            # responses as a fallback, which we don't want.
            name_key = {
                "en": "name_en", "ja": "name_ja", "ko": "name_ko",
                "zh-tw": "name_zh_tw", "zh-cn": "name_zh_cn",
            }.get(lang)
            if name_key and not row[name_key]:
                row[name_key] = (s.get("name") or "").strip()

            if not row["release_date"]:
                row["release_date"] = (s.get("releaseDate") or "").strip()

            cc = s.get("cardCount") or {}
            if isinstance(cc, dict):
                row["card_count_total"] = max(
                    row["card_count_total"], int(cc.get("total") or 0)
                )
                row["card_count_official"] = max(
                    row["card_count_official"], int(cc.get("official") or 0)
                )

            if lang not in row["languages"]:
                row["languages"].append(lang)
    return out


def discover(db_conn, *, languages: list[str] | None = None,
             force: bool = False) -> dict:
    """Probe TCGdex per-language set endpoints, enqueue every unknown set.

    Returns a summary dict with counts and the new set IDs.
    """
    languages = languages or DEFAULT_LANGUAGES

    init_unified_schema(db_conn)  # idempotent — ensures discovery_queue exists

    log.info("[discover] probing %d languages: %s", len(languages), languages)

    # Parallel per-language fetch — one slow language doesn't slow the rest.
    per_lang: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=len(languages)) as pool:
        futures = {pool.submit(_fetch_sets, lang): lang for lang in languages}
        for fut in as_completed(futures):
            lang = futures[fut]
            per_lang[lang] = fut.result()

    aggregated = _aggregate_by_set_id(per_lang)
    log.info("[discover] %d unique set IDs aggregated across all languages",
             len(aggregated))

    cur = db_conn.cursor()
    known = _existing_known_set_ids(cur)
    log.info("[discover] %d set IDs already known (ref_set_mapping + queue)",
             len(known))

    new_set_ids = sorted(set(aggregated.keys()) - known)
    log.info("[discover] %d brand-new set IDs to enqueue", len(new_set_ids))

    inserted = 0
    skipped = 0
    now_ms = int(time.time() * 1000)
    for sid in new_set_ids:
        payload = aggregated[sid]
        try:
            cur.execute(
                """
                INSERT INTO discovery_queue
                    (kind, payload, source, reporter, status,
                     discovered_at, next_attempt_at)
                VALUES ('set', %s::jsonb, 'tcgdex', 'worker', 'pending',
                        %s, %s)
                """,
                (json.dumps(payload, ensure_ascii=False), now_ms, now_ms),
            )
            inserted += 1
        except psycopg2.errors.UniqueViolation:
            # Race: another probe instance enqueued the same set_id between
            # our SELECT and INSERT. Safe to ignore — the unique partial
            # index is doing exactly what we want.
            db_conn.rollback()
            skipped += 1
        except Exception as e:
            db_conn.rollback()
            log.error("[discover] failed to enqueue %s: %s", sid, e)
        else:
            db_conn.commit()

    summary = {
        "languages_probed": languages,
        "sets_seen_total":  len(aggregated),
        "already_known":    len(known),
        "newly_queued":     inserted,
        "race_skipped":     skipped,
        "new_set_ids":      new_set_ids[:50],  # truncate log output
    }
    log.info("[discover] done: %s", summary)
    return summary


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--languages", nargs="*",
                    help=f"Override default language list "
                         f"(default: {' '.join(DEFAULT_LANGUAGES)})")
    ap.add_argument("--force", action="store_true",
                    help="Currently a no-op — discovery is always idempotent. "
                         "Reserved for future short-circuit logic.")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    with psycopg2.connect(url) as conn:
        result = discover(conn, languages=args.languages, force=args.force)

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
