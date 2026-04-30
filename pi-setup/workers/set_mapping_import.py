"""
workers/set_mapping_import.py — refresh ref_set_mapping from TCGdex.

Why this exists
---------------
The EN-edition resolver in `unified.en_match.resolve_set_id_with_source`
canonicalises operator-typed set strings against `ref_set_mapping`. The
quality of every "Matched as" header strip and every cards_master tier-1
lookup is bounded by the freshness and breadth of that table.

Today the table gets seeded from two unrelated paths:

  * `notion_master_import.upsert_set_mapping()` populates rows incidentally
    while importing a Notion CSV — but only for sets that appear in the
    operator's CSV, and only with `name_en`. Korean / Japanese / Chinese
    name columns stay blank, which is exactly the case the alias branch
    is supposed to rescue. Operator alias curation should be the *third*
    line of defence, not the first.
  * `import_tcgdex.py` populates `src_tcgdex_multi` (per-card data) but
    deliberately skips `ref_set_mapping` because the per-card pass would
    redo the same set-list fetch hundreds of thousands of times.

This worker fills the gap. It hits TCGdex's per-language /sets endpoint
once per language and UPSERTs name_en / name_kr / name_jp / name_chs /
name_cht in one pass. Roughly 5 languages × ~250 sets ≈ 1250 rows
written per run; the API call itself is a handful of seconds.

Preserving operator-curated state
---------------------------------
Three columns are *not* clobbered on conflict:

  * `aliases`   — operator-curated abbreviations (e.g. "PAL" → "sv2").
                  Critical: the alias branch in en_match is the only
                  recourse when the operator pastes a code TCGdex has
                  never heard of. Re-importing must never wipe these.
  * `era`       — operator-tagged grouping ("Sword & Shield", etc.) used
                  by reporting; TCGdex doesn't expose it on the list
                  endpoint.
  * `region`    — same: regional spinoff disambiguation that's a
                  superset of TCGdex's notion of `id`.

Per-language `name_*` columns are also preserved when TCGdex returns an
empty string for that language (COALESCE/NULLIF pattern). TCGdex
publishes some sets in EN-only at first; we don't want to overwrite a
manually-typed `name_kr` just because the upstream feed hasn't filled
it in yet.

Offline-first
-------------
This is a network-bound worker. If every language fetch returns empty
(transient outage, booth on a flaky uplink, the API behind a captive
portal), the worker logs EMPTY_SOURCE and writes nothing — the table
keeps its prior contents and the resolver keeps working. There is no
'partial wipe' state.
"""
from __future__ import annotations

import datetime
import json
import logging
import time
from typing import Callable, Optional

# `requests` is intentionally NOT imported at module top. The Pi has it
# installed; the workers test runner doesn't always (the worker is
# importable without network deps so the registration / merge / UPSERT
# paths can be exercised hermetically). _fetch_sets imports it lazily.
from .base import Worker, WorkerError

log = logging.getLogger("workers.set_mapping_import")

# TCGdex's per-language sets endpoint. Mirrors the language list in
# import_tcgdex.LANGUAGES but trimmed to the 5 we actually expose in
# ref_set_mapping columns. Adding a 6th column here means adding it
# to the schema first.
_LANG_TO_COL: dict[str, str] = {
    "en":    "name_en",
    "ko":    "name_kr",
    "ja":    "name_jp",
    "zh-cn": "name_chs",
    "zh-tw": "name_cht",
}

_API_BASE = "https://api.tcgdex.net/v2"

# Cap on per-set detail GETs in a single run. The per-language /sets
# endpoint doesn't expose serie.name (era) or releaseDate, so to fill
# those we have to GET /{lang}/sets/{set_id} once per set. There are
# ~250 sets total; on a fresh Pi the first run would burn 250 round
# trips back-to-back if uncapped. This cap means full backfill takes
# at most ceil(250/50) = 5 runs (i.e. 5 days at the daily cadence),
# which is a fine trade for a steady booth uplink. After backfill
# is complete the cap is irrelevant — only newly-discovered sets
# need a detail GET, usually 0-2 per run.
_MAX_DETAIL_FETCHES_PER_RUN = 50


def _fetch_sets(lang: str, *, timeout: int = 30) -> list[dict]:
    """Fetch the full set list for one language. Returns [] on failure
    so the merge loop can keep going for the other languages."""
    # Lazy import so the rest of the worker (registration, merge, UPSERT)
    # stays importable in environments without `requests`. The Pi has it.
    import requests
    url = f"{_API_BASE}/{lang}/sets"
    headers = {
        "User-Agent": "HanryxVault-POS/1.0 (set-mapping-import)",
        "Accept":     "application/json",
    }
    log.info("[set-import] GET %s", url)
    started = time.time()
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            log.warning("[set-import] %s: expected list, got %s",
                        lang, type(data).__name__)
            return []
        log.info("[set-import] %s: %d sets in %.1fs",
                 lang, len(data), time.time() - started)
        return data
    except Exception as e:
        # No raise: caller treats per-language failure as 'no rows from
        # this language' and moves on. A blanket all-empty result
        # triggers the EMPTY_SOURCE short-circuit.
        log.error("[set-import] %s failed: %s", lang, e)
        return []


def _fetch_set_detail(
    set_id: str, *, lang: str = "en", timeout: int = 30,
) -> Optional[dict]:
    """Fetch one set's detail document. Returns None on any failure
    so the backfill loop can keep going to the next set.

    The /sets list endpoint only returns id + name + cardCount + logo.
    The era + release date live on the per-set detail document
    (`serie.name` and `releaseDate`). EN is the safe default for the
    detail GET — TCGdex publishes EN serie names for every set, even
    JP-only ones (the serie metadata is shared across regions).
    """
    import requests
    url = f"{_API_BASE}/{lang}/sets/{set_id}"
    headers = {
        "User-Agent": "HanryxVault-POS/1.0 (set-mapping-import)",
        "Accept":     "application/json",
    }
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            log.warning("[set-import] detail %s: expected dict, got %s",
                        set_id, type(data).__name__)
            return None
        return data
    except Exception as e:
        # Per-set failure is normal — TCGdex 404s on a few obscure
        # legacy sets. We log at info to keep the daily noise down.
        log.info("[set-import] detail %s failed: %s", set_id, e)
        return None


def _extract_era_and_year(detail: dict) -> tuple[str, str]:
    """Pull (era, release_year) from a /sets/{id} detail document.

    era ← `serie.name` if present and non-empty (TCGdex's serie is
    its closest analogue to our era column — "Scarlet & Violet",
    "Sword & Shield", "Sun & Moon", etc.).
    release_year ← first 4 chars of `releaseDate` if it parses as
    YYYY-MM-DD, else ''. We deliberately keep release_year as text
    in the schema so '2026' and 'TBD' both fit.

    Both default to '' so callers can OR-merge with COALESCE.
    """
    era = ""
    serie = detail.get("serie") if isinstance(detail, dict) else None
    if isinstance(serie, dict):
        era = (serie.get("name") or "").strip()

    year = ""
    rel = (detail.get("releaseDate") or "").strip() if isinstance(detail, dict) else ""
    if len(rel) >= 4 and rel[:4].isdigit():
        year = rel[:4]
    return era, year


def _merge_languages(
    fetch_fn: Callable[[str], list[dict]],
) -> tuple[dict[str, dict], dict[str, int]]:
    """Hit each language and collapse to one row per set_id with all
    available name_* columns populated.

    Returns (merged_by_set_id, per_lang_counts).
    """
    merged: dict[str, dict] = {}
    per_lang_counts: dict[str, int] = {}
    for lang, col in _LANG_TO_COL.items():
        sets = fetch_fn(lang) or []
        per_lang_counts[lang] = len(sets)
        for s in sets:
            sid = (s.get("id") or "").strip()
            if not sid:
                continue
            slot = merged.setdefault(sid, {})
            name = (s.get("name") or "").strip()
            if name:
                # Only write non-empty names. The COALESCE/NULLIF
                # pattern in the UPSERT preserves a prior non-empty
                # column when this run sees an empty one.
                slot[col] = name
    return merged, per_lang_counts


# Backfill UPDATE for the era + release_year columns. Run AFTER the
# main per-language UPSERT loop so it only touches rows that already
# exist. UPDATE-not-UPSERT means a transient detail-GET success can't
# accidentally insert a phantom row that the main loop never saw.
# COALESCE/NULLIF preserves an operator-curated era when TCGdex
# returns ''; we only ever fill blanks, never overwrite curated text.
_BACKFILL_UPDATE_SQL = """
UPDATE ref_set_mapping
   SET era          = COALESCE(NULLIF(%s, ''), era),
       release_year = COALESCE(NULLIF(%s, ''), release_year),
       imported_at  = %s
 WHERE set_id = %s
"""


def _backfill_era_year(
    cur,
    fetch_detail_fn: Callable[[str], Optional[dict]],
    *,
    max_sets: int = _MAX_DETAIL_FETCHES_PER_RUN,
    now_fn: Callable[[], int] = lambda: int(time.time()),
) -> dict:
    """Fill blank era / release_year columns from TCGdex per-set details.

    Selects up to `max_sets` rows whose era OR release_year is blank,
    fetches /{lang}/sets/{set_id} for each, extracts metadata, and
    UPDATEs only blank columns (operator-curated values are preserved
    via COALESCE/NULLIF in _BACKFILL_UPDATE_SQL).

    Per-set fetch failures are tolerated and counted (`backfill_failed`).
    Returns a per-run summary the caller can fold into its task result.
    """
    cur.execute("""
        SELECT set_id
          FROM ref_set_mapping
         WHERE era = '' OR release_year = ''
         ORDER BY imported_at DESC, set_id
         LIMIT %s
    """, (max_sets,))
    candidates: list[str] = []
    for row in (cur.fetchall() or []):
        sid = row[0] if not isinstance(row, dict) else row.get("set_id")
        if sid:
            candidates.append(sid)

    fetched = 0
    updated = 0
    failed = 0
    for sid in candidates:
        detail = fetch_detail_fn(sid)
        fetched += 1
        if not detail:
            failed += 1
            continue
        era, year = _extract_era_and_year(detail)
        if not era and not year:
            # TCGdex returned the detail but neither field was usable.
            # Don't UPDATE (we'd only churn imported_at).
            continue
        cur.execute(_BACKFILL_UPDATE_SQL, (era, year, now_fn(), sid))
        if (cur.rowcount or 0) > 0:
            updated += 1

    return {
        "backfill_candidates": len(candidates),
        "backfill_fetched":    fetched,
        "backfill_updated":    updated,
        "backfill_failed":     failed,
    }


# UPSERT pinned out so the column order stays in lockstep with the
# parameter tuple in process(). The COALESCE/NULLIF dance preserves
# operator-curated names when TCGdex has nothing better, and leaves
# aliases / era / region untouched on every re-run.
_UPSERT_SQL = """
INSERT INTO ref_set_mapping
    (set_id, name_en, name_kr, name_jp, name_chs, name_cht,
     raw, imported_at)
VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
ON CONFLICT (set_id) DO UPDATE SET
    name_en     = COALESCE(NULLIF(EXCLUDED.name_en,  ''), ref_set_mapping.name_en),
    name_kr     = COALESCE(NULLIF(EXCLUDED.name_kr,  ''), ref_set_mapping.name_kr),
    name_jp     = COALESCE(NULLIF(EXCLUDED.name_jp,  ''), ref_set_mapping.name_jp),
    name_chs    = COALESCE(NULLIF(EXCLUDED.name_chs, ''), ref_set_mapping.name_chs),
    name_cht    = COALESCE(NULLIF(EXCLUDED.name_cht, ''), ref_set_mapping.name_cht),
    raw         = EXCLUDED.raw,
    imported_at = EXCLUDED.imported_at
"""


class SetMappingImportWorker(Worker):
    TASK_TYPE = "set_mapping_import"
    BATCH_SIZE = 1
    # One full refresh per UTC day is plenty — TCGdex publishes new
    # sets at most every few weeks, and the en-match resolver tolerates
    # a stale day fine. The orchestrator ticks faster than this; the
    # ON CONFLICT in seed() collapses repeated ticks to a no-op.
    IDLE_SLEEP_S = 3600.0
    # 30-minute ceiling: 5 GETs at ~5s each + ~250 small UPSERTs is
    # well under a minute on the Pi, but the booth uplink can be
    # punitive and we don't want a stuck task to hold the queue forever.
    CLAIM_TIMEOUT_S = 1800

    def __init__(
        self,
        conn,
        *,
        today_fn: Optional[Callable[[], str]] = None,
        fetch_fn: Optional[Callable[[str], list[dict]]] = None,
        fetch_detail_fn: Optional[Callable[[str], Optional[dict]]] = None,
        max_detail_fetches_per_run: int = _MAX_DETAIL_FETCHES_PER_RUN,
        **kw,
    ):
        super().__init__(conn, **kw)
        # All three are injectable for deterministic tests:
        #   * today_fn   — pin the seed task_key
        #   * fetch_fn   — replace live /sets HTTP calls with canned data
        #   * fetch_detail_fn — replace live /sets/{id} HTTP calls
        self._today_fn = today_fn or (
            lambda: datetime.datetime.utcnow().strftime("%Y-%m-%d"))
        self._fetch_fn = fetch_fn or _fetch_sets
        self._fetch_detail_fn = fetch_detail_fn or _fetch_set_detail
        self._max_detail_fetches_per_run = max_detail_fetches_per_run

    # ── Worker contract ──────────────────────────────────────────

    def seed(self) -> int:
        """Enqueue one import task per UTC day. ON CONFLICT collapses
        re-seed runs to a no-op so the orchestrator's polling tick
        doesn't pile up duplicate imports."""
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO bg_task_queue
                (task_type, task_key, payload, status, created_at)
            VALUES ('set_mapping_import', %s, '{}'::jsonb, 'PENDING', %s)
            ON CONFLICT (task_type, task_key) DO NOTHING
        """, (self._today_fn(), int(time.time())))
        n = cur.rowcount or 0
        self.conn.commit()
        log.info("[set-import] seed enqueued %d task(s)", n)
        return n

    def process(self, task: dict) -> dict:
        merged, per_lang_counts = _merge_languages(self._fetch_fn)

        if not merged:
            # All-empty signal: every language fetch failed (offline,
            # captive portal, API hiccup) or the API returned empty
            # lists. Either way the safe move is to write nothing —
            # the resolver keeps using yesterday's mapping unchanged.
            log.warning(
                "[set-import] no sets returned by any language — skip "
                "(per-lang counts: %s)", per_lang_counts)
            return {
                "status":          "EMPTY_SOURCE",
                "sets_imported":   0,
                "per_lang_counts": per_lang_counts,
            }

        cur = self.conn.cursor()
        now = int(time.time())
        written = 0
        for sid, names in merged.items():
            raw_blob = {
                "source": "tcgdex_sets",
                "names":  names,
            }
            cur.execute(_UPSERT_SQL, (
                sid,
                names.get("name_en",  ""),
                names.get("name_kr",  ""),
                names.get("name_jp",  ""),
                names.get("name_chs", ""),
                names.get("name_cht", ""),
                json.dumps(raw_blob, ensure_ascii=False),
                now,
            ))
            written += 1

        # Backfill era + release_year for the rows we just touched.
        # Runs AFTER the main UPSERT loop so the backfill always sees
        # at least the freshest set list. Bounded by
        # _max_detail_fetches_per_run so a fresh Pi spreads detail
        # fetches over a handful of runs instead of stalling for
        # minutes on the first one. Safe to skip on per-set fetch
        # failures — we just retry next run.
        backfill_stats = _backfill_era_year(
            cur,
            self._fetch_detail_fn,
            max_sets=self._max_detail_fetches_per_run,
        )

        # ONE commit at the end — the main UPSERT and any backfill
        # UPDATEs land atomically together so a Pi-side power loss
        # in the middle either leaves last-run state or this-run
        # state, never half-and-half.
        self.conn.commit()

        log.info(
            "[set-import] wrote %d set(s) (per-lang counts: %s, backfill: %s)",
            written, per_lang_counts, backfill_stats)
        return {
            "status":          "OK",
            "sets_imported":   written,
            "per_lang_counts": per_lang_counts,
            **backfill_stats,
        }
