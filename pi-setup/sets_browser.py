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

Each table has a different schema, so this module's job is to collapse
them into the shape the UI expects:

    {
      "language":    "kr" | "chs" | "jpn" | "jpn_pocket" | "<game>",
      "set":         human-readable set name,
      "set_code":    short set code where available,
      "card_number": printed card number,
      "name":        display name (native script),
      "name_en":     English name when known (only some sources have it),
      "rarity":      rarity string,
      "image_url":   thumbnail URL,
      "card_id":     stable identifier within its language,
    }

Public API
----------
    list_sets(conn, *, q=None, limit=200)        → list[dict]
    cards_in_set(conn, set_query, *, limit=600) → list[dict]
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("sets_browser")


# Each entry: (language_label, table, columns SELECT'd, set-match expression)
# We always LIKE-match case-insensitively against multiple "set" columns so
# users can type either the human name ("Twilight Masquerade"), the prod
# code ("svp"), or a partial substring.
def _kr_q(set_query: str, limit: int) -> tuple[str, tuple]:
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
        WHERE LOWER(set_name)  LIKE LOWER(%s)
           OR LOWER(prod_code) LIKE LOWER(%s)
        ORDER BY prod_code, card_number
        LIMIT %s
        """,
        (f"%{set_query}%", f"%{set_query}%", limit),
    )


def _chs_q(set_query: str, limit: int) -> tuple[str, tuple]:
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
        WHERE LOWER(commodity_name) LIKE LOWER(%s)
           OR LOWER(commodity_code) LIKE LOWER(%s)
           OR LOWER(yoren_code)     LIKE LOWER(%s)
        ORDER BY commodity_code, collection_number
        LIMIT %s
        """,
        (f"%{set_query}%", f"%{set_query}%", f"%{set_query}%", limit),
    )


def _jpn_q(set_query: str, limit: int) -> tuple[str, tuple]:
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
        WHERE LOWER(set_name) LIKE LOWER(%s)
           OR LOWER(set_code) LIKE LOWER(%s)
           OR LOWER(series)   LIKE LOWER(%s)
        ORDER BY set_code, card_number
        LIMIT %s
        """,
        (f"%{set_query}%", f"%{set_query}%", f"%{set_query}%", limit),
    )


def _jpn_pocket_q(set_query: str, limit: int) -> tuple[str, tuple]:
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
        WHERE LOWER(set_code) LIKE LOWER(%s)
        ORDER BY set_code, card_number
        LIMIT %s
        """,
        (f"%{set_query}%", limit),
    )


def _multi_q(set_query: str, limit: int) -> tuple[str, tuple]:
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
        WHERE LOWER(set_name) LIKE LOWER(%s)
           OR LOWER(set_code) LIKE LOWER(%s)
        ORDER BY game, set_code, card_number
        LIMIT %s
        """,
        (f"%{set_query}%", f"%{set_query}%", limit),
    )


_QUERIES = (_kr_q, _chs_q, _jpn_q, _jpn_pocket_q, _multi_q)


def cards_in_set(conn, set_query: str, *, limit: int = 600) -> list[dict]:
    """
    Return every card belonging to `set_query` across all language tables.

    `set_query` is a substring (case-insensitive). To get all printings of
    a given Pokémon set across all languages, search for a token shared by
    every language's set name — e.g. "twilight masquerade" matches the
    English / multi side, while "twm" or the prod_code matches the KR /
    CHS / JPN sides. The UI surfaces both result groups together.
    """
    set_query = (set_query or "").strip()
    if not set_query:
        return []
    per_lang = max(50, limit // len(_QUERIES))
    out: list[dict] = []
    for builder in _QUERIES:
        sql, params = builder(set_query, per_lang)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [d[0] for d in cur.description]
                for row in cur.fetchall():
                    out.append(dict(zip(cols, row)))
        except Exception as exc:
            log.info("[sets_browser] %s skipped: %s", builder.__name__, exc)
    # Stable sort: language label, then set, then card number (naturally).
    out.sort(key=lambda r: (
        str(r.get("language") or ""),
        str(r.get("set_code") or r.get("set_name") or ""),
        _natkey(r.get("card_number") or ""),
    ))
    return out[:limit]


def _natkey(s: str) -> tuple:
    """Natural sort key: '2' < '10' instead of lexical '10' < '2'."""
    s = str(s or "")
    head, num = "", ""
    for ch in s:
        if ch.isdigit():
            num += ch
        else:
            head += ch
    return (head, int(num) if num else 0, s)


def list_sets(conn, *, q: str | None = None, limit: int = 200) -> list[dict]:
    """
    Return a deduped list of every set we have in the system, optionally
    filtered by substring `q`. Useful for typeahead on the admin UI.
    """
    q_like = f"%{(q or '').strip()}%" if q else "%"
    sql = """
      SELECT 'kr' AS lang,
             COALESCE(NULLIF(set_name,''), prod_code) AS set_name,
             prod_code AS set_code, COUNT(*) AS n
      FROM cards_kr
      WHERE LOWER(set_name) LIKE LOWER(%s) OR LOWER(prod_code) LIKE LOWER(%s)
      GROUP BY 1,2,3
      UNION ALL
      SELECT 'chs', COALESCE(NULLIF(commodity_name,''), commodity_code),
             commodity_code, COUNT(*)
      FROM cards_chs
      WHERE LOWER(commodity_name) LIKE LOWER(%s) OR LOWER(commodity_code) LIKE LOWER(%s)
      GROUP BY 1,2,3
      UNION ALL
      SELECT 'jpn', COALESCE(NULLIF(set_name,''), set_code), set_code, COUNT(*)
      FROM cards_jpn
      WHERE LOWER(set_name) LIKE LOWER(%s) OR LOWER(set_code) LIKE LOWER(%s)
      GROUP BY 1,2,3
      UNION ALL
      SELECT 'jpn_pocket', set_code, set_code, COUNT(*)
      FROM cards_jpn_pocket
      WHERE LOWER(set_code) LIKE LOWER(%s)
      GROUP BY 1,2,3
      UNION ALL
      SELECT game, COALESCE(NULLIF(set_name,''), set_code), set_code, COUNT(*)
      FROM cards_multi
      WHERE LOWER(set_name) LIKE LOWER(%s) OR LOWER(set_code) LIKE LOWER(%s)
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
    return out
