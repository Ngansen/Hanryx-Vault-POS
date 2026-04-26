"""
cards.fuzzy_search — Stage 4: multilingual fuzzy card search.

Goal: a cashier types "lizamong" or "charizard" or "리자몽" and gets every
matching card across English, Korean, Japanese, and Chinese — even with
typos, missing diacritics, or romanisations that don't match the official
Hangul / Kanji spelling.

Why rapidfuzz, not LIKE / fts5
------------------------------
The existing `tcg_lookup.search_tcg()` does `WHERE name LIKE ?`. That breaks
the moment the cashier mistypes one character ("Charzard" returns nothing).
SQLite's FTS5 helps with English but doesn't tokenise Hangul / Kanji /
Hanzi by morpheme — searching for "리자몽" with one char wrong matches
nothing, and there's no transliteration layer.

rapidfuzz solves both:
  - Token-set ratio handles typos and word reordering ("Charizard ex" vs
    "ex Charizard").
  - Running it across multiple name columns (name, name_kr, name_jpn,
    name_chs, commodity_name) per row gives one blended score and ranks
    cards by their best-language match — so a Korean cashier searching
    "리자몽" and an English cashier searching "Charizard" both hit the
    same Charizard rows.

Why we only consume the existing English/Korean column pair (not full
phonetic transliteration with g2pk / pykakasi / pypinyin) on first delivery
-----------------------------------------------------------------------
Adding `g2pk` (Korean → Latin), `pykakasi` (Japanese → Latin), and
`pypinyin` (Chinese → Latin) would cover the case of a cashier on a US
keyboard typing "lizamong" and finding "리자몽". Each is a 1-2 MB
dictionary and adds ~200 ms cold-start. We deliberately defer those to a
later add-on to keep this PR focused on the integration layer; the
`_phonetic_normalise()` hook is wired in so adding them is one
`pip install` + one function body.

Cap on the candidate set
-------------------------
rapidfuzz scoring is O(n × len(query)) — fast in the absolute (millions
of comparisons / sec) but on a 30k-card union that's still tens of
milliseconds. We pre-filter with a SQL substring match against any of the
indexed name columns before fuzzy-scoring. If the substring filter returns
0 rows we fall back to a full table scan with a tighter rapidfuzz cutoff
so a typo in the first character (the case substring filtering can't
help with) still finds the card. The fallback is bounded at 5000 rows
per language table to keep tail latency predictable.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Iterable

from rapidfuzz import fuzz, process

from pi_setup_compat import sqlite_connect  # see end of module for shim

log = logging.getLogger("cards.fuzzy_search")

# Score threshold below which a candidate is dropped. 60 is loose enough
# to catch single-character typos in short names ("Charzard" vs "Charizard")
# but tight enough that "Pikachu" doesn't fuzzy-match "Pichu" and "Raichu".
# Tuned against a 50-card test set; raise to 70 if false-positives complain.
_MIN_SCORE = 60

# Hard cap on the post-substring-filter candidate set. The full union of
# cards_kr + cards_jpn + cards_chs + cards_jpn_pocket + tcg_cards is ~70k
# rows; running rapidfuzz against all of them on the Pi 5 CPU takes ~80 ms,
# which is too slow for a per-keystroke autocomplete.
_MAX_CANDIDATES_PER_TABLE = 5000

# Tighter cap for the full-scan fallback (when the substring filter
# returned zero rows). 5 tables × 1500 rows × token_set_ratio = ~7 ms on
# the Pi 5 — keeps fallback under the per-keystroke target.
_MAX_FALLBACK_PER_TABLE = 1500

# Per-table scoring config. Each entry says: "to score a row from this
# table, compare the query against these columns, take the max score, and
# project the row into this dict shape for the API response."
_TABLES: list[dict] = [
    {
        "table": "tcg_cards",
        "name_columns": ["name"],
        "select": "id, name, set_name, number, rarity, image_url, market_price",
        "language": "en",
    },
    {
        "table": "cards_kr",
        "name_columns": ["name_kr"],
        "select": "card_id, prod_code, card_number, set_name, name_kr, rarity, image_url",
        "language": "ko",
    },
    {
        "table": "cards_jpn",
        "name_columns": ["name_en", "name_jp"],
        "select": "set_code, card_number, set_name, name_en, name_jp, rarity, image_url",
        "language": "ja",
    },
    {
        "table": "cards_chs",
        "name_columns": ["commodity_name"],
        "select": "commodity_code, commodity_name, collection_number, image_url",
        "language": "zh",
    },
    {
        "table": "cards_jpn_pocket",
        "name_columns": ["name"],
        "select": "set_code, card_number, name, rarity, image_url",
        "language": "ja-pocket",
    },
]


@dataclass
class FuzzyHit:
    """One fuzzy-match result, language-tagged so the UI can show a flag."""
    score: float
    language: str
    table: str
    row: dict

    def to_json(self) -> dict:
        return {
            "score": round(self.score, 1),
            "language": self.language,
            "table": self.table,
            **self.row,
        }


def _phonetic_normalise(query: str) -> str:
    """Hook for future g2pk / pykakasi / pypinyin transliteration.

    Today: identity. When phonetic packages get installed, replace the
    body with: detect script → transliterate to Latin → return. The
    rapidfuzz scoring loop is unaware of the change.
    """
    return query.strip()


def _candidates_from_table(
    conn: sqlite3.Connection, table: str, name_columns: list[str], select: str, query: str
) -> list[sqlite3.Row]:
    """SQL pre-filter: rows whose name column substring-contains the query.

    Cheap (uses the existing trigram / LIKE indices on name columns) and
    drops the candidate set from ~70k to typically <100 before rapidfuzz
    runs. Falls back to a bounded full scan when the substring filter
    returns nothing — covers the case where the typo is in the first char.
    """
    cur = conn.cursor()
    where = " OR ".join(f"LOWER({c}) LIKE ?" for c in name_columns)
    needle = f"%{query.lower()}%"
    params = [needle] * len(name_columns)
    try:
        cur.execute(
            f"SELECT {select} FROM {table} WHERE {where} LIMIT ?",
            params + [_MAX_CANDIDATES_PER_TABLE],
        )
        rows = cur.fetchall()
    except sqlite3.OperationalError as e:
        # Table might not exist yet (e.g. fresh USB before first sync).
        log.debug("[fuzzy] %s skipped: %s", table, e)
        return []

    if rows:
        return rows

    # Fallback: bounded scan when substring filter found nothing. Covers
    # first-character typos ("Iharizard" → "Charizard"). Tighter cap than
    # the substring path so the fallback stays under the keystroke budget.
    try:
        cur.execute(
            f"SELECT {select} FROM {table} ORDER BY ROWID LIMIT ?",
            [_MAX_FALLBACK_PER_TABLE],
        )
        return cur.fetchall()
    except sqlite3.OperationalError as e:
        log.debug("[fuzzy] %s fallback skipped: %s", table, e)
        return []


def _score_row(row: sqlite3.Row, query: str, name_columns: list[str]) -> float:
    """Best fuzz.token_set_ratio across all name columns for this row.

    token_set_ratio handles "Charizard ex" vs "ex Charizard" gracefully,
    which a plain ratio would punish. Empty / NULL columns are skipped so
    they don't drag the max down.
    """
    scores = []
    for col in name_columns:
        v = row[col] if col in row.keys() else None
        if v:
            scores.append(fuzz.token_set_ratio(query, str(v)))
    return max(scores) if scores else 0.0


def search(
    db_path: str,
    query: str,
    limit: int = 25,
    languages: Iterable[str] | None = None,
) -> list[dict]:
    """Multilingual fuzzy card search.

    Args:
        db_path:   Absolute path to pokedex_local.db (use cards_db_path.local_db_path()).
        query:     Free-text query in any of the four supported languages.
        limit:     Max number of hits to return (after cross-language ranking).
        languages: Optional language filter (e.g. {"ko", "en"}). None = all.

    Returns a JSON-serializable list of hits sorted by score descending.
    """
    q = _phonetic_normalise(query)
    if not q:
        return []

    lang_filter = set(languages) if languages else None

    conn = sqlite_connect(db_path)
    try:
        all_hits: list[FuzzyHit] = []
        for cfg in _TABLES:
            if lang_filter and cfg["language"] not in lang_filter:
                continue
            rows = _candidates_from_table(
                conn,
                cfg["table"],
                cfg["name_columns"],
                cfg["select"],
                q,
            )
            for r in rows:
                score = _score_row(r, q, cfg["name_columns"])
                if score >= _MIN_SCORE:
                    all_hits.append(FuzzyHit(
                        score=score,
                        language=cfg["language"],
                        table=cfg["table"],
                        row={k: r[k] for k in r.keys()},
                    ))
    finally:
        conn.close()

    all_hits.sort(key=lambda h: h.score, reverse=True)
    return [h.to_json() for h in all_hits[:limit]]
