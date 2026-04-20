"""
Multi-source price aggregator with trimmed median, condition multipliers,
volatility flag, and a Postgres-backed quote cache.

Pipeline
--------
    1. Fan-out across all enabled sources (eBay sold + naver + tcgkorea +
       snkrdunk + cardmarket).
    2. Normalize every listing to USD via fx_rates.
    3. Drop outliers using a 10/90-percentile trim — eBay sold listings in
       particular have wild outliers (mis-listed cards, lots, sealed boxes
       in the same search).
    4. Compute trimmed median, p25, p75, source-count, sample-count.
    5. Apply a condition multiplier to produce condition-specific quotes.
    6. Compute a volatility score (IQR / median); flag noisy results.
    7. Cache the canonical quote in `price_quotes` for the next call.

Public API
----------
    bootstrap(conn)
    get_quote(conn, *, query, game="Pokemon", condition="NM",
              card_id=None, source=None, max_age_sec=21_600,
              force_refresh=False) → dict
    invalidate(conn, card_id, source)

The cache key is (card_id, source) when those are provided (precise), else
("__query__", sha1(query)) for ad-hoc lookups. TTL defaults to 6 hours.
"""
from __future__ import annotations

import hashlib
import json
import logging
import statistics
import time
from typing import Any

log = logging.getLogger("price_aggregator")

# Condition multipliers applied to the canonical NM (Near Mint) median.
# Calibrated against TCGplayer condition pricing for ungraded singles.
CONDITION_MULTIPLIERS: dict[str, float] = {
    "NM":   1.00,   # Near Mint
    "LP":   0.85,   # Lightly Played
    "MP":   0.70,   # Moderately Played
    "HP":   0.55,   # Heavily Played
    "DMG":  0.35,   # Damaged
    "PSA10": 4.00,  # very rough placeholder for graded slabs
    "PSA9":  2.00,
}

DEFAULT_TTL_SEC = 6 * 3600
TRIM_LOW_PCT = 0.10
TRIM_HIGH_PCT = 0.90
VOLATILITY_THRESHOLD = 0.45  # IQR / median

_DDL = """
CREATE TABLE IF NOT EXISTS price_quotes (
    id              BIGSERIAL PRIMARY KEY,
    cache_key       TEXT         NOT NULL,
    card_id         TEXT         NOT NULL DEFAULT '',
    source_hint     TEXT         NOT NULL DEFAULT '',
    query           TEXT         NOT NULL DEFAULT '',
    game            TEXT         NOT NULL DEFAULT '',
    condition       TEXT         NOT NULL DEFAULT 'NM',
    median_usd      REAL,
    p25_usd         REAL,
    p75_usd         REAL,
    sample_count    INTEGER      NOT NULL DEFAULT 0,
    source_count    INTEGER      NOT NULL DEFAULT 0,
    sources_used    JSONB        NOT NULL DEFAULT '[]'::jsonb,
    volatility      REAL,
    volatile_flag   BOOLEAN      NOT NULL DEFAULT FALSE,
    listings_sample JSONB        NOT NULL DEFAULT '[]'::jsonb,
    fetched_at      BIGINT       NOT NULL DEFAULT 0,
    UNIQUE (cache_key, condition)
);

CREATE INDEX IF NOT EXISTS idx_price_quotes_cardid    ON price_quotes (card_id);
CREATE INDEX IF NOT EXISTS idx_price_quotes_fetchedat ON price_quotes (fetched_at DESC);
"""


def bootstrap(conn) -> None:
    with conn.cursor() as cur:
        for stmt in [s.strip() for s in _DDL.split(";") if s.strip()]:
            cur.execute(stmt)
    conn.commit()


def _cache_key(card_id: str | None, source: str | None,
               query: str) -> str:
    if card_id:
        return f"card:{source or 'any'}:{card_id}"
    return "q:" + hashlib.sha1((query or "").encode("utf-8")).hexdigest()


def _trimmed(vals: list[float]) -> list[float]:
    if len(vals) < 4:
        return list(vals)  # too few to trim meaningfully
    vals = sorted(vals)
    lo = int(len(vals) * TRIM_LOW_PCT)
    hi = int(len(vals) * TRIM_HIGH_PCT) or len(vals)
    return vals[lo:hi] or vals


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _aggregate(listings_usd: list[dict]) -> dict:
    """
    Compute median/IQR/volatility from a flat list of USD-normalized rows.

    Delegates to ``price_weighting.aggregate`` (smart aggregator that
    weights by source reliability, listing recency, and per-source
    sample count, plus rejects sources whose median lies far outside
    the cross-source consensus).  Falls back to the legacy trimmed
    median if the weighting module is unavailable.
    """
    try:
        import price_weighting
        return price_weighting.aggregate(listings_usd)
    except Exception as exc:
        log.info("[agg] price_weighting unavailable, using trimmed median: %s", exc)

    vals = [r["price_usd"] for r in listings_usd
            if r.get("price_usd") is not None and r["price_usd"] > 0]
    if not vals:
        return {"median_usd": None, "p25_usd": None, "p75_usd": None,
                "sample_count": 0, "volatility": None, "volatile_flag": False}

    trimmed = sorted(_trimmed(vals))
    median = round(statistics.median(trimmed), 2)
    p25 = round(_percentile(trimmed, 0.25), 2)
    p75 = round(_percentile(trimmed, 0.75), 2)
    iqr = max(0.0, p75 - p25)
    volatility = round(iqr / median, 4) if median else None
    return {
        "median_usd":    median,
        "p25_usd":       p25,
        "p75_usd":       p75,
        "sample_count":  len(trimmed),
        "volatility":    volatility,
        "volatile_flag": (volatility is not None and
                          volatility > VOLATILITY_THRESHOLD),
    }


def _fan_out(query: str, game: str) -> tuple[list[dict], list[str]]:
    """Call every available source; return (listings, sources_used)."""
    listings: list[dict] = []
    used: list[str] = []

    # eBay sold (the most important signal — actual transaction prices).
    try:
        from ebay_sold import ebay_sold
        rows = ebay_sold(query, limit=80, pages=1)
        if rows:
            listings.extend(rows)
            used.append("ebay_sold")
    except Exception as exc:
        log.info("[agg] ebay_sold skipped: %s", exc)

    # TCGplayer pricing via pokemontcg.io (no API key needed; pokemontcg.io
    # rebroadcasts TCGplayer's official price feed). Pokémon-only.
    if game.lower().startswith("pokemon"):
        try:
            from tcgplayer_proxy import tcgplayer_via_pokemontcg
            rows = tcgplayer_via_pokemontcg(query, limit=20)
            if rows:
                listings.extend(rows)
                used.append("tcgplayer")
        except Exception as exc:
            log.info("[agg] tcgplayer skipped: %s", exc)

    # Asian + EU sources
    try:
        from price_scrapers import naver_shopping, tcgkorea, snkrdunk, cardmarket
        for name, fn in (("naver", naver_shopping),
                         ("tcgkorea", tcgkorea),
                         ("snkrdunk", snkrdunk)):
            try:
                rows = fn(query, limit=20)
                if rows:
                    listings.extend(rows)
                    used.append(name)
            except Exception as exc:
                log.info("[agg] %s skipped: %s", name, exc)
        try:
            rows = cardmarket(query, limit=20, game=game)
            if rows:
                listings.extend(rows)
                used.append("cardmarket")
        except Exception as exc:
            log.info("[agg] cardmarket skipped: %s", exc)
    except Exception as exc:
        log.info("[agg] price_scrapers unavailable: %s", exc)

    return listings, used


def _normalize(listings: list[dict]) -> list[dict]:
    try:
        from fx_rates import normalize_listings
        return normalize_listings(listings)
    except Exception as exc:
        log.info("[agg] fx unavailable, prices stay in source currency: %s", exc)
        # Last-ditch: assume USD on missing rate so we at least return *something*
        out = []
        for r in listings:
            row = dict(r)
            row["price_usd"] = (float(row["price"])
                                if row.get("currency") == "USD" else None)
            out.append(row)
        return out


def _read_cached(conn, key: str, condition: str, max_age_sec: int) -> dict | None:
    cutoff = int((time.time() - max_age_sec) * 1000)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT median_usd, p25_usd, p75_usd, sample_count, source_count,
                   sources_used, volatility, volatile_flag, listings_sample,
                   fetched_at, query, game
            FROM price_quotes
            WHERE cache_key = %s AND condition = %s AND fetched_at >= %s
        """, (key, condition, cutoff))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "median_usd":      row[0], "p25_usd": row[1], "p75_usd": row[2],
            "sample_count":    int(row[3] or 0),
            "source_count":    int(row[4] or 0),
            "sources_used":    row[5] or [],
            "volatility":      row[6], "volatile_flag": bool(row[7]),
            "listings_sample": row[8] or [],
            "fetched_at":      int(row[9] or 0),
            "query":           row[10] or "", "game": row[11] or "",
            "from_cache":      True,
            "condition":       condition,
        }


def _write_cache(conn, *, key: str, card_id: str, source_hint: str,
                 query: str, game: str, condition: str, agg: dict,
                 sources_used: list[str], sample: list[dict]) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO price_quotes
              (cache_key, card_id, source_hint, query, game, condition,
               median_usd, p25_usd, p75_usd, sample_count, source_count,
               sources_used, volatility, volatile_flag,
               listings_sample, fetched_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (cache_key, condition) DO UPDATE SET
              median_usd      = EXCLUDED.median_usd,
              p25_usd         = EXCLUDED.p25_usd,
              p75_usd         = EXCLUDED.p75_usd,
              sample_count    = EXCLUDED.sample_count,
              source_count    = EXCLUDED.source_count,
              sources_used    = EXCLUDED.sources_used,
              volatility      = EXCLUDED.volatility,
              volatile_flag   = EXCLUDED.volatile_flag,
              listings_sample = EXCLUDED.listings_sample,
              fetched_at      = EXCLUDED.fetched_at
        """, (
            key, card_id or "", source_hint or "", query, game, condition,
            agg["median_usd"], agg["p25_usd"], agg["p75_usd"],
            agg["sample_count"], len(sources_used),
            json.dumps(sources_used),
            agg["volatility"], agg["volatile_flag"],
            json.dumps(sample[:30]),
            int(time.time() * 1000),
        ))
    conn.commit()


def get_quote(conn, *, query: str, game: str = "Pokemon",
              condition: str = "NM",
              card_id: str | None = None, source: str | None = None,
              max_age_sec: int = DEFAULT_TTL_SEC,
              force_refresh: bool = False) -> dict:
    """
    Return a canonical USD quote for one card. See module docstring for the
    pipeline. The response shape:

        {
          "median_usd":    12.34,        # condition-adjusted
          "nm_median_usd": 12.34,        # always Near-Mint baseline
          "p25_usd": 9.50, "p75_usd": 15.10,
          "sample_count": 42, "source_count": 3,
          "sources_used": ["ebay_sold", "tcgkorea"],
          "volatility": 0.31, "volatile_flag": false,
          "listings_sample": [ ...up to 30 rows... ],
          "from_cache":   true | false,
          "condition":    "NM",
          "condition_multiplier": 1.0,
          "fetched_at":   1745176800000,
          "query": "...", "game": "Pokemon",
        }
    """
    bootstrap(conn)
    condition = (condition or "NM").upper()
    mult = CONDITION_MULTIPLIERS.get(condition, 1.0)
    key = _cache_key(card_id, source, query)

    if not force_refresh:
        cached = _read_cached(conn, key, "NM", max_age_sec)
        if cached and cached["median_usd"] is not None:
            cached["condition"] = condition
            cached["condition_multiplier"] = mult
            cached["nm_median_usd"] = cached["median_usd"]
            cached["median_usd"] = round(cached["median_usd"] * mult, 2)
            return cached

    listings, sources_used = _fan_out(query, game)
    listings_usd = _normalize(listings)
    agg = _aggregate(listings_usd)

    sample = sorted(
        [r for r in listings_usd if r.get("price_usd")],
        key=lambda r: r["price_usd"],
    )

    _write_cache(conn, key=key, card_id=card_id or "",
                 source_hint=source or "", query=query, game=game,
                 condition="NM", agg=agg, sources_used=sources_used,
                 sample=sample)

    nm_median = agg["median_usd"]
    return {
        "median_usd":         (round(nm_median * mult, 2)
                               if nm_median is not None else None),
        "nm_median_usd":      nm_median,
        "p25_usd":            agg["p25_usd"],
        "p75_usd":            agg["p75_usd"],
        "sample_count":       agg["sample_count"],
        "source_count":       len(sources_used),
        "sources_used":       sources_used,
        "volatility":         agg["volatility"],
        "volatile_flag":      agg["volatile_flag"],
        "listings_sample":    sample[:30],
        "from_cache":         False,
        "condition":          condition,
        "condition_multiplier": mult,
        "fetched_at":         int(time.time() * 1000),
        "query":              query,
        "game":               game,
    }


def invalidate(conn, card_id: str | None = None,
               source: str | None = None) -> int:
    """Drop cached quotes for a single card (or for everything if both None)."""
    with conn.cursor() as cur:
        if card_id:
            key = _cache_key(card_id, source, "")
            cur.execute("DELETE FROM price_quotes WHERE cache_key = %s",
                        (key,))
        else:
            cur.execute("DELETE FROM price_quotes")
        n = cur.rowcount
    conn.commit()
    return int(n)
