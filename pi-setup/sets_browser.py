"""
Cross-language set browser.

Pulls every card belonging to a given set from all five language tables
the system tracks and returns one uniform row per card so the admin UI
(and the Market page) can show "all printings of this set across every
language we have data for" in a single view.

Tables it queries
-----------------
    cards_kr           Korean Pokémon TCG
    cards_chs          Simplified-Chinese Pokémon TCG
    cards_jpn          Japanese Pokémon TCG (full)
    cards_jpn_pocket   Japanese TCG Pocket
    cards_multi        MTG / Lorcana / OnePiece / DBS (+ English Pokémon)

Performance design
------------------
1. **One SQL per table**, never one-per-token. When the alias map
   expands a query into N tokens we pass them to Postgres as an array
   and use ``LOWER(col) LIKE ANY(%s::text[])``. This collapses what
   used to be tables*tokens round-trips into exactly len(tables)
   round-trips.
2. **Trigram-friendly predicates**. Every WHERE clause is
   ``LOWER(col) LIKE ANY(...)`` against columns that have GIN
   ``gin_trgm_ops`` indexes (created idempotently in init_db). On
   large tables this turns a sequential scan into an index lookup.
3. **In-process TTL cache** keyed on ``(query, limit)``. Repeat
   searches return instantly. Cache is bounded and self-evicting.
4. **No early-exit between tables** — every table always runs even
   if an earlier table already filled the global limit, otherwise a
   high-volume language can starve the others.

Public API
----------
    list_sets(conn, *, q=None, limit=200)         → list[dict]
    cards_in_set(conn, set_query, *, limit=600)  → list[dict]
    invalidate_cache()                            → None
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable

log = logging.getLogger("sets_browser")


# ── per-table query builders ─────────────────────────────────────────────────
# Each builder returns (sql, params_tuple) for a single table. The SQL takes
# *one* parameter — a Postgres TEXT[] of `%token%` patterns (lowercased) —
# matched with LIKE ANY against every set-identifying column the table has.

def _kr_q(patterns: list[str], limit: int) -> tuple[str, tuple]:
    return (
        """
        SELECT 'kr' AS language,
               COALESCE(NULLIF(set_name,''), prod_code) AS set_name,
               prod_code      AS set_code,
               card_number,
               name_kr        AS name,
               ''             AS name_en,
               rarity,
               image_url,
               card_id
        FROM cards_kr
        WHERE LOWER(set_name)  LIKE ANY(%s::text[])
           OR LOWER(prod_code) LIKE ANY(%s::text[])
        ORDER BY prod_code, card_number
        LIMIT %s
        """,
        (patterns, patterns, limit),
    )


def _chs_q(patterns: list[str], limit: int) -> tuple[str, tuple]:
    return (
        """
        SELECT 'chs' AS language,
               COALESCE(NULLIF(commodity_name,''), commodity_code) AS set_name,
               commodity_code     AS set_code,
               collection_number  AS card_number,
               name_chs           AS name,
               ''                 AS name_en,
               COALESCE(NULLIF(rarity_text,''), rarity) AS rarity,
               image_url,
               card_id::text      AS card_id
        FROM cards_chs
        WHERE LOWER(commodity_name) LIKE ANY(%s::text[])
           OR LOWER(commodity_code) LIKE ANY(%s::text[])
           OR LOWER(yoren_code)     LIKE ANY(%s::text[])
        ORDER BY commodity_code, collection_number
        LIMIT %s
        """,
        (patterns, patterns, patterns, limit),
    )


def _jpn_q(patterns: list[str], limit: int) -> tuple[str, tuple]:
    return (
        """
        SELECT 'jpn' AS language,
               COALESCE(NULLIF(set_name,''), set_code) AS set_name,
               set_code,
               card_number,
               COALESCE(NULLIF(name_jp,''), name_en) AS name,
               name_en,
               rarity,
               image_url,
               url AS card_id
        FROM cards_jpn
        WHERE LOWER(set_name) LIKE ANY(%s::text[])
           OR LOWER(set_code) LIKE ANY(%s::text[])
           OR LOWER(series)   LIKE ANY(%s::text[])
        ORDER BY set_code, card_number
        LIMIT %s
        """,
        (patterns, patterns, patterns, limit),
    )


def _jpn_pocket_q(patterns: list[str], limit: int) -> tuple[str, tuple]:
    return (
        """
        SELECT 'jpn_pocket' AS language,
               set_code     AS set_name,
               set_code,
               card_number::text AS card_number,
               name,
               '' AS name_en,
               rarity,
               image_url,
               (set_code || '-' || card_number) AS card_id
        FROM cards_jpn_pocket
        WHERE LOWER(set_code) LIKE ANY(%s::text[])
        ORDER BY set_code, card_number
        LIMIT %s
        """,
        (patterns, limit),
    )


def _multi_q(patterns: list[str], limit: int) -> tuple[str, tuple]:
    return (
        """
        SELECT game AS language,
               COALESCE(NULLIF(set_name,''), set_code) AS set_name,
               set_code,
               card_number,
               name,
               name AS name_en,
               rarity,
               image_url,
               card_id
        FROM cards_multi
        WHERE LOWER(set_name) LIKE ANY(%s::text[])
           OR LOWER(set_code) LIKE ANY(%s::text[])
        ORDER BY game, set_code, card_number
        LIMIT %s
        """,
        (patterns, patterns, limit),
    )


_QUERIES: tuple[Callable, ...] = (_kr_q, _chs_q, _jpn_q, _jpn_pocket_q, _multi_q)


# ── natural-key sort (compiled once) ─────────────────────────────────────────
_NUM_SPLIT = re.compile(r"(\d+)")


def _natkey(s: Any) -> tuple:
    """Natural sort key: '2' < '10' instead of lexical '10' < '2'."""
    if s is None:
        return ()
    parts = _NUM_SPLIT.split(str(s))
    return tuple(int(p) if p.isdigit() else p for p in parts)


# ── TTL cache ────────────────────────────────────────────────────────────────
_CACHE_TTL = 60.0          # seconds; trends/inventory rarely change minute-to-minute
_CACHE_MAX = 128           # bounded LRU
_cache_lock = threading.RLock()
_cache: dict[tuple, tuple[float, list[dict]]] = {}


def invalidate_cache() -> None:
    """Wipe the result cache. Call after large card imports."""
    with _cache_lock:
        _cache.clear()


def _cache_get(key: tuple) -> list[dict] | None:
    now = time.time()
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        ts, val = entry
        if now - ts > _CACHE_TTL:
            _cache.pop(key, None)
            return None
        return val


def _cache_put(key: tuple, val: list[dict]) -> None:
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            # Evict oldest insertion (dict preserves insertion order in 3.7+).
            try:
                _cache.pop(next(iter(_cache)))
            except StopIteration:
                pass
        _cache[key] = (time.time(), val)


# ── main entry points ───────────────────────────────────────────────────────
def cards_in_set(conn, set_query: str, *, limit: int = 600,
                 use_aliases: bool = True) -> list[dict]:
    """
    Return every card belonging to `set_query` across all language tables.

    When `use_aliases` is True (default) the query is first expanded
    through `set_aliases.expand()` so typing one regional name (e.g.
    "Twilight Masquerade") fetches every printing under every alias
    that maps to the same logical set ("sv6", "sv6a", "Mask of Change",
    "Night Wanderer", …). All expanded tokens are passed to Postgres
    as an array and matched with one query per table.
    """
    set_query = (set_query or "").strip()
    if not set_query:
        return []

    tokens: list[str] = [set_query]
    cluster_name: str | None = None
    if use_aliases:
        try:
            import set_aliases as _sa
            tokens, cluster_name = _sa.expand(set_query)
            tokens = tokens or [set_query]
        except Exception as exc:
            log.info("[sets_browser] alias expansion skipped: %s", exc)

    cache_key = ("cards", set_query.lower(), int(limit), bool(use_aliases))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # Lowercased, %-wrapped patterns for LIKE ANY(...).
    patterns = list({f"%{t.strip().lower()}%" for t in tokens if t and t.strip()})
    if not patterns:
        return []

    per_table = max(50, limit)  # each table gets up to `limit`; we trim later
    seen: set[tuple] = set()
    out: list[dict] = []
    for builder in _QUERIES:
        sql, params = builder(patterns, per_table)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [d[0] for d in cur.description]
                for row in cur.fetchall():
                    rec = dict(zip(cols, row))
                    key = (str(rec.get("language") or ""),
                           str(rec.get("card_id") or ""))
                    if key in seen:
                        continue
                    seen.add(key)
                    if cluster_name:
                        rec.setdefault("cluster", cluster_name)
                    out.append(rec)
        except Exception as exc:
            log.info("[sets_browser] %s skipped: %s", builder.__name__, exc)

    out.sort(key=lambda r: (
        str(r.get("language") or ""),
        str(r.get("set_code") or r.get("set_name") or ""),
        _natkey(r.get("card_number")),
    ))
    out = out[:limit]
    _cache_put(cache_key, out)
    return out


def list_sets(conn, *, q: str | None = None, limit: int = 200) -> list[dict]:
    """
    Return a deduped list of every set we have in the system, optionally
    filtered by substring `q`. Useful for typeahead on the admin UI.
    """
    q_like = f"%{(q or '').strip().lower()}%" if q else "%"
    cache_key = ("list", q_like, int(limit))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    sql = """
      SELECT 'kr' AS lang,
             COALESCE(NULLIF(set_name,''), prod_code) AS set_name,
             prod_code AS set_code, COUNT(*) AS n
      FROM cards_kr
      WHERE LOWER(set_name) LIKE %s OR LOWER(prod_code) LIKE %s
      GROUP BY 1,2,3
      UNION ALL
      SELECT 'chs', COALESCE(NULLIF(commodity_name,''), commodity_code),
             commodity_code, COUNT(*)
      FROM cards_chs
      WHERE LOWER(commodity_name) LIKE %s OR LOWER(commodity_code) LIKE %s
      GROUP BY 1,2,3
      UNION ALL
      SELECT 'jpn', COALESCE(NULLIF(set_name,''), set_code), set_code, COUNT(*)
      FROM cards_jpn
      WHERE LOWER(set_name) LIKE %s OR LOWER(set_code) LIKE %s
      GROUP BY 1,2,3
      UNION ALL
      SELECT 'jpn_pocket', set_code, set_code, COUNT(*)
      FROM cards_jpn_pocket
      WHERE LOWER(set_code) LIKE %s
      GROUP BY 1,2,3
      UNION ALL
      SELECT game, COALESCE(NULLIF(set_name,''), set_code), set_code, COUNT(*)
      FROM cards_multi
      WHERE LOWER(set_name) LIKE %s OR LOWER(set_code) LIKE %s
      GROUP BY 1,2,3
      ORDER BY 4 DESC
      LIMIT %s
    """
    params = (q_like, q_like, q_like, q_like, q_like, q_like,
              q_like, q_like, q_like, limit)
    out: list[dict[str, Any]] = []
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for lang, set_name, set_code, n in cur.fetchall():
                out.append({
                    "language": lang,
                    "set_name": set_name,
                    "set_code": set_code,
                    "count":    int(n),
                })
    except Exception as exc:
        log.info("[sets_browser] list_sets failed: %s", exc)
    _cache_put(cache_key, out)
    return out
