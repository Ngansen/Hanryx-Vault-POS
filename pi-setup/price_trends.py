"""
Price-trend & velocity signals from data we already collect.

Reads two tables that are already populated by other parts of the system:

  ebay_sold_history(query, title, price, sold_date, score, scraped_at)
      → market signal — what real buyers paid on eBay
  sale_history(name, price, quantity, sold_at)
      → your own POS sales — *your* velocity, which can diverge from eBay's

For a given card query we return three buckets:

  medians       7-day / 30-day / 90-day median sale price (USD)
                with sample counts and a confidence flag
  trend         % change last 7d vs prior 7d, last 30d vs prior 30d,
                direction tag (rising / stable / falling), and a
                human-readable badge string
  velocity      eBay sales/week + your-shop sales/week, plus a
                temperature label (hot / warm / cool / cold) so the
                buy-list can adjust offers automatically

Pure SQL. No external HTTP calls. Cached in Redis for 1 hour because
the underlying eBay history only refreshes when the operator clicks
"Live Refresh" or the nightly job runs.

Public API
----------
    trends(conn, query, *, your_name=None) → dict
"""
from __future__ import annotations

import datetime
import logging
import statistics
from typing import Iterable

log = logging.getLogger("price_trends")

# Hot/warm/cool/cold thresholds in eBay sales-per-week.
# Tuned for Pokémon singles; tweak via constants if a different game
# distribution shifts the curve.
_HOT_PER_WEEK  = 7.0   # ≥ 1 a day on average
_WARM_PER_WEEK = 2.0   # a few a week
_COOL_PER_WEEK = 0.5   # 1-2 a month
# Anything below _COOL is cold

# Trend direction thresholds (% change). 5% noise floor — anything
# inside ±5% reads as "stable" so we don't flap between rising and
# falling on tiny week-to-week wiggles.
_TREND_NOISE = 0.05


# ── small helpers ──────────────────────────────────────────────────

def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def _trim_outliers(prices: list[float]) -> list[float]:
    """IQR trim: drop anything outside 1.5×IQR. Mild — keeps most rows."""
    if len(prices) < 6:
        return prices
    s = sorted(prices)
    q1 = s[len(s) // 4]
    q3 = s[(3 * len(s)) // 4]
    iqr = q3 - q1
    if iqr <= 0:
        return prices
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    return [p for p in prices if lo <= p <= hi]


def _pct_change(now: float | None, prev: float | None) -> float | None:
    if not now or not prev or prev <= 0:
        return None
    return round((now - prev) / prev, 4)  # 4 decimals = 0.01% precision


def _direction(pct: float | None) -> str:
    if pct is None:
        return "unknown"
    if pct >  _TREND_NOISE:
        return "rising"
    if pct < -_TREND_NOISE:
        return "falling"
    return "stable"


def _temperature(per_week: float) -> str:
    if per_week >= _HOT_PER_WEEK:  return "hot"
    if per_week >= _WARM_PER_WEEK: return "warm"
    if per_week >= _COOL_PER_WEEK: return "cool"
    return "cold"


def _bucket(rows: Iterable[tuple], cutoff: datetime.date,
            window_end: datetime.date | None = None) -> list[float]:
    """Pull prices whose sold_date falls inside [cutoff, window_end]."""
    out: list[float] = []
    for sold_date, price in rows:
        if sold_date is None or price is None:
            continue
        if sold_date < cutoff:
            continue
        if window_end is not None and sold_date > window_end:
            continue
        try:
            out.append(float(price))
        except (TypeError, ValueError):
            continue
    return out


# ── main entry point ──────────────────────────────────────────────

def trends(conn, query: str, *, your_name: str | None = None) -> dict:
    """
    Compute the trend & velocity report for `query`.

    `query` is matched against `ebay_sold_history.query` (case-insensitive
    LIKE). `your_name`, if provided, is matched against `sale_history.name`
    the same way; pass it when the in-shop product name differs from the
    eBay search query.
    """
    if not query or not query.strip():
        return {"error": "empty query"}
    your_name = (your_name or query).strip()

    today = datetime.date.today()
    d7   = today - datetime.timedelta(days=7)
    d14  = today - datetime.timedelta(days=14)
    d30  = today - datetime.timedelta(days=30)
    d60  = today - datetime.timedelta(days=60)
    d90  = today - datetime.timedelta(days=90)

    # ── pull eBay sold history once (cheap; index on query+sold_date) ──
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sold_date, price FROM ebay_sold_history "
            "WHERE LOWER(query) LIKE LOWER(%s) "
            "  AND sold_date IS NOT NULL "
            "  AND sold_date >= %s",
            (f"%{query.strip()}%", d90),
        )
        ebay_rows = [(r[0], r[1]) for r in cur.fetchall()]

    p7    = _trim_outliers(_bucket(ebay_rows, d7))
    p7_p  = _trim_outliers(_bucket(ebay_rows, d14, d7))   # prior 7d
    p30   = _trim_outliers(_bucket(ebay_rows, d30))
    p30_p = _trim_outliers(_bucket(ebay_rows, d60, d30))  # prior 30d
    p90   = _trim_outliers(_bucket(ebay_rows, d90))

    medians = {
        "7d":  {"median": _median(p7),  "count": len(p7)},
        "30d": {"median": _median(p30), "count": len(p30)},
        "90d": {"median": _median(p90), "count": len(p90)},
    }

    pct_7d  = _pct_change(_median(p7),  _median(p7_p))
    pct_30d = _pct_change(_median(p30), _median(p30_p))
    direction = _direction(pct_30d if pct_30d is not None else pct_7d)
    if direction == "rising":
        badge = f"📈 +{round((pct_30d or pct_7d or 0) * 100)}% trending up"
    elif direction == "falling":
        badge = f"📉 {round((pct_30d or pct_7d or 0) * 100)}% cooling off"
    elif direction == "stable":
        badge = "➡️ Stable"
    else:
        badge = "—"

    trend = {
        "pct_7d_vs_prev":  pct_7d,
        "pct_30d_vs_prev": pct_30d,
        "direction":       direction,
        "badge":           badge,
    }

    # ── velocity ────────────────────────────────────────────────────
    ebay_sales_30d = len([r for r in ebay_rows if r[0] >= d30])
    ebay_per_week  = round(ebay_sales_30d / 30 * 7, 2)

    your_per_week = 0.0
    your_sales_30d = 0
    cutoff_ms = int(datetime.datetime.combine(d30, datetime.time()).timestamp() * 1000)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM sale_history "
            "WHERE LOWER(name) LIKE LOWER(%s) AND sold_at >= %s",
            (f"%{your_name}%", cutoff_ms),
        )
        row = cur.fetchone()
        if row:
            your_sales_30d = int(row[0] or 0)
            your_per_week = round(your_sales_30d / 30 * 7, 2)

    # Temperature uses eBay velocity (broad market signal); your own
    # velocity is reported alongside but doesn't drive the label.
    velocity = {
        "ebay_per_week":   ebay_per_week,
        "ebay_sales_30d":  ebay_sales_30d,
        "your_per_week":   your_per_week,
        "your_sales_30d":  your_sales_30d,
        "label":           _temperature(ebay_per_week),
    }

    # Confidence is a function of total sample size across the 90d window.
    n = len(p90)
    if n >= 30:   confidence = "high"
    elif n >= 10: confidence = "medium"
    elif n >= 3:  confidence = "low"
    else:         confidence = "insufficient"

    return {
        "query":      query,
        "asof":       today.isoformat(),
        "medians":    medians,
        "trend":      trend,
        "velocity":   velocity,
        "confidence": confidence,
        "sample_size": n,
    }
