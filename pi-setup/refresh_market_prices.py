"""
refresh_market_prices.py — background market intel backfill (C11).

Picks the top-N most-valuable in-stock inventory cards, fans out to every
scraper in price_scrapers.SCRAPERS (naver / bunjang / hareruya2 / cardmarket),
converts every native-currency hit to USD via fx_rates.to_usd, and writes
one row per source per card into postgres `price_history`.

Why background, not on-demand
-----------------------------
The AI cashier (Qwen 2.5 3B on Pi 5) runs at ~10 tok/sec — it can't
afford a 1-2s scraper fan-out mid-customer-transaction. By writing
results into price_history (which usb_mirror replicates to the SQLite
mirror as price_history_recent), the assistant's existing local-DB
lookup picks up multi-source pricing for free, with a freshness floor
of one orchestrator tick (60 min default).

Why median-per-source, not all hits
-----------------------------------
Each scraper returns up to 10 listings per card; storing all 40 raw
rows per refresh would blow the 200k-row mirror cap in <1 week. We
collapse each source's listings to a single representative row using
the USD-converted median, which is stable against outlier sellers and
matches what the /admin/market lang-grid surfaces.

Idempotency / re-runs
---------------------
Each call APPENDS one row per (card, source) — no UPSERT. The mirror
keeps the most-recent row per source via its `ORDER BY observed_at
DESC LIMIT 200000`, and the AI's lookup_price SQL takes the most
recent observation, so re-running this hourly just keeps a rolling
log without bloat (200k rows / 4 sources / 6 runs/day ≈ 8k cards
covered for ~1 week of history).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Iterable

import psycopg2
import psycopg2.extras

import fx_rates
import price_scrapers

log = logging.getLogger("refresh_market_prices")

# Cap per run. Each card costs ~2-4s wall (sequential 4 scrapers, each
# ~500ms p50 thanks to the 10-min Redis cache absorbing repeats). At
# 200 cards × 3s = 10 min — well under the 60-min orchestrator interval
# but enough to cover the entire trade-show inventory in 2-3 ticks.
TOP_N = int(os.environ.get("MARKET_REFRESH_TOP_N", "200"))


def _connect_pg():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(dsn)


def _select_top_n_cards(conn, n: int) -> list[dict]:
    """Pick the highest-value in-stock inventory cards.

    `sale_price DESC` puts grail cards first (where the lang-grid
    matters most for trade-in offers); `last_updated DESC` tiebreaker
    keeps recently-touched stock on top of dormant sealed product.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT qr_code, name, sale_price, grade
              FROM inventory
             WHERE stock > 0
               AND COALESCE(name, '') <> ''
             ORDER BY sale_price DESC NULLS LAST,
                      last_updated DESC NULLS LAST
             LIMIT %s
            """,
            (n,),
        )
        return list(cur.fetchall())


def _median(values: Iterable[float]) -> float | None:
    vals = sorted(v for v in values if v is not None and v > 0)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def _summarise_source(rows: list[dict]) -> dict | None:
    """Collapse N raw listings from one source to one storable row.

    Uses median price (per source's native currency) + USD-converted
    median. Returns None if no usable rows — caller skips writing so
    we don't spam price_history with empty observations.
    """
    if not rows:
        return None
    enriched = fx_rates.normalize_listings(rows)  # adds price_usd per row
    native_med = _median(r.get("price") for r in enriched)
    usd_med = _median(r.get("price_usd") for r in enriched)
    if native_med is None:
        return None
    # Currency is consistent within a source (naver→KRW, bunjang→KRW,
    # hareruya2→JPY, cardmarket→EUR). Read off the first row.
    currency = (enriched[0].get("currency") or "USD").upper()
    return {
        "price": float(native_med),
        "currency": currency,
        "price_usd": round(usd_med, 4) if usd_med is not None else None,
    }


def _insert_observations(
    conn, card_id: str, card_name: str, grade: str,
    per_source: dict[str, dict], query_used: dict[str, str],
) -> int:
    """One INSERT per source. Returns rows written."""
    written = 0
    with conn.cursor() as cur:
        for source, summary in per_source.items():
            cur.execute(
                """
                INSERT INTO price_history
                    (card_id, card_name, market_price, fetched_ms,
                     source, grade, currency, price_usd,
                     observed_at, query_used)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)
                """,
                (
                    card_id,
                    card_name,
                    # market_price stays USD for back-compat with the
                    # legacy /card/buy_price + trend chart code; falls
                    # back to native price when conversion failed (rare).
                    float(summary["price_usd"] or summary["price"]),
                    int(time.time() * 1000),
                    source,
                    grade or "raw",
                    summary["currency"],
                    summary["price_usd"],
                    (query_used.get(source) or "")[:255],
                ),
            )
            written += 1
    conn.commit()
    return written


def refresh_once(top_n: int = TOP_N) -> dict:
    """Run one full backfill pass. Returns a stats summary."""
    t0 = time.time()
    stats = {
        "cards": 0, "scrapes": 0, "rows_written": 0,
        "empties": 0, "errors": 0,
    }
    conn = _connect_pg()
    try:
        cards = _select_top_n_cards(conn, top_n)
        stats["cards"] = len(cards)
        log.info("[market_refresh] starting: top %d cards", len(cards))

        for i, card in enumerate(cards, 1):
            name = (card["name"] or "").strip()
            if not name:
                continue
            try:
                fanout = price_scrapers.search_all(name)
            except Exception as exc:
                log.warning("[market_refresh] search_all(%r) failed: %s", name, exc)
                stats["errors"] += 1
                continue

            per_source: dict[str, dict] = {}
            for source, listings in (fanout.get("results") or {}).items():
                stats["scrapes"] += 1
                summary = _summarise_source(listings or [])
                if summary is None:
                    stats["empties"] += 1
                    continue
                per_source[source] = summary

            if per_source:
                try:
                    written = _insert_observations(
                        conn,
                        card_id=card["qr_code"],
                        card_name=name,
                        grade=(card.get("grade") or "raw"),
                        per_source=per_source,
                        query_used=(fanout.get("query_used") or {}),
                    )
                    stats["rows_written"] += written
                except Exception as exc:
                    log.warning("[market_refresh] insert(%r) failed: %s", name, exc)
                    stats["errors"] += 1
                    try: conn.rollback()
                    except Exception: pass

            if i % 25 == 0:
                log.info(
                    "[market_refresh] progress %d/%d  rows=%d  empties=%d  err=%d",
                    i, len(cards), stats["rows_written"],
                    stats["empties"], stats["errors"],
                )
    finally:
        try: conn.close()
        except Exception: pass

    stats["elapsed_sec"] = round(time.time() - t0, 1)
    log.info("[market_refresh] done: %s", stats)
    return stats


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    refresh_once()
