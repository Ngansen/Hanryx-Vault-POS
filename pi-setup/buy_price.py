"""
Buy-side intelligence: "what's the most I should pay for this card?"

Reads `ebay_sold_history` (already populated by ebay_sold.py + the nightly
refresh) and turns the last 90 days of real sold prices into three concrete
buy targets, a trend-adjusted overpay verdict, and a 12-week sparkline so
the operator can eyeball the curve at a glance.

Design goals
------------
* Pure SQL on data we already have. No live HTTP calls. ~1 indexed query.
* Outlier-trimmed (IQR) so a single $500 misfire doesn't poison the median.
* Quartile-based: p25 of 90d trimmed sold prices is the anchor — it's what
  "a healthy buylist offer" would clear at, not the wishful-thinking mean.
* Trend-aware: if the 30-day median is rising vs the prior 30, we nudge
  buy targets up; if falling, we nudge down. Bounded ±15% so a noisy
  spike can't hand you a doubled offer.
* Sample-size honest: < 3 sold = we return null targets and explain why,
  rather than fabricating a number from one $1.99 outlier.

Buy ladder (trim-of-90d, before trend adjust)
---------------------------------------------
    steal_buy   = p25 × 0.50    great deal — flip-grade margin
    fair_buy    = p50 × 0.55    typical buylist offer (~45% margin)
    max_buy     = p25 × 0.75    walk-away ceiling (don't pay above this)

Tunables live at the top of the file. Defaults are calibrated for
Pokémon singles in the $5-$200 band, which is the bulk of buylist
volume; high-end ($500+) cards may want a tighter `MAX_BUY_FRAC`.

Public API
----------
    buy_intelligence(conn, query, *, asking_price=None) → dict
"""
from __future__ import annotations

import datetime
import logging
import statistics
from typing import Iterable

log = logging.getLogger("buy_price")

# ── tunables ──────────────────────────────────────────────────────
STEAL_BUY_FRAC = 0.50   # of trimmed p25
FAIR_BUY_FRAC  = 0.55   # of trimmed median
MAX_BUY_FRAC   = 0.75   # of trimmed p25 — the walk-away ceiling

# Trend nudge: shift all three targets by this fraction of the 30d trend %,
# clamped to ±15 %. So a +20% trend bumps targets +6% (0.30 × 0.20 = 0.06).
TREND_INFLUENCE   = 0.30
TREND_CAP         = 0.15

# Risk bands when asking_price is supplied (relative to max_buy)
RISK_GREAT_DEAL = 0.85   # asking ≤ 85% of max_buy → "great deal"
RISK_FAIR_OK    = 1.00   # asking ≤ 100% of max_buy → "fair, OK to buy"
RISK_OVERPAY    = 1.15   # asking ≤ 115% → "overpay" (above → "walk away")

SPARKLINE_BUCKETS = 12   # ~weekly buckets across 90d
MIN_SAMPLE_SIZE   = 3    # below this we won't fabricate buy targets


# ── helpers ───────────────────────────────────────────────────────

def _trim_outliers(prices: list[float]) -> list[float]:
    """Mild IQR trim. Keeps most rows, drops the obvious flyers."""
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


def _quartiles(prices: list[float]) -> dict:
    """Return p25/p50/p75/min/max/mean of a price list, all rounded to ¢."""
    if not prices:
        return {"p25": None, "p50": None, "p75": None,
                "min": None, "max": None, "mean": None, "n": 0}
    s = sorted(prices)
    n = len(s)
    return {
        "p25":  round(s[n // 4], 2),
        "p50":  round(statistics.median(s), 2),
        "p75":  round(s[(3 * n) // 4], 2),
        "min":  round(s[0], 2),
        "max":  round(s[-1], 2),
        "mean": round(statistics.fmean(s), 2),
        "n":    n,
    }


def _pct_change(now: float | None, prev: float | None) -> float | None:
    if not now or not prev or prev <= 0:
        return None
    return round((now - prev) / prev, 4)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _bucket_weekly(rows: Iterable[tuple], today: datetime.date,
                   buckets: int = SPARKLINE_BUCKETS,
                   days_per_bucket: int = 8) -> list[float]:
    """
    Build `buckets` weekly-ish median price points, oldest → newest.
    Empty weeks fall back to the last known median (carry-forward) so the
    sparkline doesn't drop to zero just because nothing sold that week.
    Returns a list of length `buckets`; 0.0 padding only if the entire
    window has zero sales.
    """
    out: list[float] = []
    last: float = 0.0
    for i in range(buckets):
        days_back_end   = (buckets - i)     * days_per_bucket
        days_back_start = (buckets - i - 1) * days_per_bucket
        end   = today - datetime.timedelta(days=days_back_start)
        start = today - datetime.timedelta(days=days_back_end)
        prices = [
            float(p) for d, p in rows
            if d is not None and p is not None and start <= d < end
        ]
        if prices:
            last = round(statistics.median(prices), 2)
        out.append(last)
    # If we never saw a sale, leave zeros so the SVG can render
    # "No sales data yet".
    return out


def _sparkline_svg(values: list[float], width: int = 320, height: int = 60,
                   color: str = "#10b981") -> str:
    """
    Tiny dependency-free line+area sparkline. Green by default (buy-side).
    Mirrors the look of the dashboard's revenue spark but draws a smooth
    polyline + soft fill rather than bars, which reads better as a trend.
    """
    if not values or max(values) <= 0:
        return (f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
                f'<text x="50%" y="55%" text-anchor="middle" '
                f'fill="#666" font-size="11" font-family="sans-serif">'
                f'No sales data yet</text></svg>')
    pad = 4
    n  = len(values)
    mx = max(values)
    mn = min(v for v in values if v > 0) if any(v > 0 for v in values) else 0
    rng = max(mx - mn, mx * 0.05, 0.01)  # avoid zero-range flat lines
    inner_w = width - pad * 2
    inner_h = height - pad * 2

    def _xy(i: int, v: float) -> tuple[float, float]:
        x = pad + (i / max(n - 1, 1)) * inner_w
        # Higher price → higher on screen (lower y)
        y = pad + inner_h - ((v - mn) / rng) * inner_h if v > 0 else pad + inner_h
        return x, y

    pts  = [_xy(i, v) for i, v in enumerate(values)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    # Area path: line + drop to baseline + close
    area = (f"M {pts[0][0]:.1f},{pad + inner_h:.1f} "
            + " ".join(f"L {x:.1f},{y:.1f}" for x, y in pts)
            + f" L {pts[-1][0]:.1f},{pad + inner_h:.1f} Z")
    last_x, last_y = pts[-1]
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f'<path d="{area}" fill="{color}" opacity="0.18"/>'
        f'<polyline points="{line}" fill="none" stroke="{color}" '
        f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="3" fill="{color}"/>'
        f'</svg>'
    )


def _verdict(asking_price: float | None, max_buy: float | None,
             fair_buy: float | None) -> dict | None:
    """Risk band + human label given a current asking/list price."""
    if asking_price is None or max_buy is None or max_buy <= 0:
        return None
    ratio = asking_price / max_buy
    if ratio <= RISK_GREAT_DEAL:
        band, label, action = "great", "🟢 Great deal", "buy"
    elif ratio <= RISK_FAIR_OK:
        band, label, action = "fair", "🟢 Fair — OK to buy", "buy"
    elif ratio <= RISK_OVERPAY:
        band, label, action = "overpay", "🟡 Slight overpay", "negotiate"
    else:
        band, label, action = "walk", "🔴 Walk away", "skip"
    pct_over_fair = None
    if fair_buy and fair_buy > 0:
        pct_over_fair = round((asking_price - fair_buy) / fair_buy, 4)
    return {
        "band":           band,
        "label":          label,
        "action":         action,
        "asking_price":   round(asking_price, 2),
        "ratio_vs_max":   round(ratio, 3),
        "pct_over_fair":  pct_over_fair,
    }


# ── main entry point ──────────────────────────────────────────────

def buy_intelligence(conn, query: str, *,
                     asking_price: float | None = None) -> dict:
    """
    Compute buy targets, trend-adjusted ceiling, and a 90-day sparkline.

    Args:
      query        Search string ("Pikachu 25/198"). Matched LIKE against
                   ebay_sold_history.query (case-insensitive).
      asking_price If provided, returns a `verdict` dict telling the
                   operator whether buying at that price is great / fair
                   / overpay / walk-away.

    Returns dict with:
      query, asof, sample_size, confidence,
      sold_90d:   {p25, p50, p75, min, max, mean, n}
      sold_30d:   same, restricted to last 30d
      trend:      {pct_30d_vs_prev, direction}
      buy:        {steal, fair, max, trend_adjust_pct}
      verdict:    (only if asking_price supplied)
      sparkline:  {points: [...12], svg: "<svg.../>"}
    """
    if not query or not query.strip():
        return {"error": "empty query"}

    today = datetime.date.today()
    d30   = today - datetime.timedelta(days=30)
    d60   = today - datetime.timedelta(days=60)
    d90   = today - datetime.timedelta(days=90)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT sold_date, price FROM ebay_sold_history "
            "WHERE LOWER(query) LIKE LOWER(%s) "
            "  AND sold_date IS NOT NULL "
            "  AND price IS NOT NULL "
            "  AND sold_date >= %s",
            (f"%{query.strip()}%", d90),
        )
        rows = [(r[0], float(r[1])) for r in cur.fetchall()]

    p90_raw   = [p for d, p in rows if d >= d90]
    p30_raw   = [p for d, p in rows if d >= d30]
    p30p_raw  = [p for d, p in rows if d60 <= d < d30]

    p90  = _trim_outliers(p90_raw)
    p30  = _trim_outliers(p30_raw)
    p30p = _trim_outliers(p30p_raw)

    sold_90d = _quartiles(p90)
    sold_30d = _quartiles(p30)

    # Trend: 30d median vs prior 30d median
    med_30  = sold_30d["p50"]
    med_30p = (statistics.median(p30p) if p30p else None)
    pct_30d = _pct_change(med_30, med_30p)
    direction = "stable"
    if pct_30d is not None:
        if pct_30d >  0.05: direction = "rising"
        elif pct_30d < -0.05: direction = "falling"

    # Confidence buckets
    n = sold_90d["n"]
    if   n >= 30: confidence = "high"
    elif n >= 10: confidence = "medium"
    elif n >=  3: confidence = "low"
    else:         confidence = "insufficient"

    # Buy ladder — only emit numbers if we have at least MIN_SAMPLE_SIZE.
    buy = {
        "steal":            None,
        "fair":             None,
        "max":              None,
        "trend_adjust_pct": 0.0,
        "basis": {
            "p25_trimmed_90d": sold_90d["p25"],
            "p50_trimmed_90d": sold_90d["p50"],
        },
    }
    if n >= MIN_SAMPLE_SIZE and sold_90d["p25"] and sold_90d["p50"]:
        adj = 0.0
        if pct_30d is not None:
            adj = _clamp(pct_30d * TREND_INFLUENCE, -TREND_CAP, TREND_CAP)
        mult = 1.0 + adj
        buy["steal"]            = round(sold_90d["p25"] * STEAL_BUY_FRAC * mult, 2)
        buy["fair"]             = round(sold_90d["p50"] * FAIR_BUY_FRAC  * mult, 2)
        buy["max"]              = round(sold_90d["p25"] * MAX_BUY_FRAC   * mult, 2)
        buy["trend_adjust_pct"] = round(adj, 4)

    spark_pts = _bucket_weekly(rows, today)
    sparkline = {
        "points": spark_pts,
        "svg":    _sparkline_svg(spark_pts),
        "buckets": SPARKLINE_BUCKETS,
        "days_per_bucket": 8,
    }

    out = {
        "query":       query,
        "asof":        today.isoformat(),
        "sample_size": n,
        "confidence":  confidence,
        "sold_90d":    sold_90d,
        "sold_30d":    sold_30d,
        "trend": {
            "pct_30d_vs_prev": pct_30d,
            "direction":       direction,
            "median_30d":      med_30,
            "median_prior_30d": round(med_30p, 2) if med_30p else None,
        },
        "buy":       buy,
        "sparkline": sparkline,
    }

    if asking_price is not None:
        try:
            ap = float(asking_price)
            v = _verdict(ap, buy["max"], buy["fair"])
            if v is not None:
                out["verdict"] = v
        except (TypeError, ValueError):
            pass

    return out
