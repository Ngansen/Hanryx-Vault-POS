"""
Smart price aggregation — replaces the naive trimmed median.

Why this exists
---------------
The original ``price_aggregator._aggregate`` simply trimmed outliers and
took the median across every listing it received.  That gives equal
weight to a 6-month-old eBay sale and a yesterday's TCGplayer market
price, and lets a single source with 50 noisy listings drown out three
sources that agree closely.

The smart aggregator scores every listing on three axes and weights it
accordingly, then drops sources whose own median sits well outside the
cross-source consensus before computing the final number.

Three weighting axes
--------------------
1. **Recency** — sales from the last 7 days carry full weight; weight
   decays exponentially with age (half-life = 21 days).  Listings
   without a date are treated as "moderately old" (default ~30 days).

2. **Source reliability** — hand-tuned multipliers (TCGplayer market =
   1.0, eBay sold = 0.95, scrapers = 0.7…).  These reflect how much
   the source's headline number tracks real transaction prices.

3. **Sample volume per source** — a source with 1 listing gets a
   confidence haircut; a source with 10+ listings gets full weight.
   Curve: ``min(1.0, sqrt(n)/3)``.

Outlier source rejection
------------------------
After per-source medians are computed, any source whose median is more
than 2× the cross-source IQR away from the cross-source median is
dropped entirely (with the dropped sources surfaced in the response so
the operator can see *why* the number is what it is).
"""
from __future__ import annotations

import logging
import math
import statistics
import time
from typing import Any, Iterable

log = logging.getLogger("price_weighting")


SOURCE_RELIABILITY: dict[str, float] = {
    "tcgplayer":   1.00,
    "tcgplayer_market": 1.00,
    "ebay_sold":   0.95,
    "ebay":        0.85,
    "cardmarket":  0.85,
    "naver":       0.70,
    "shopping.naver.com": 0.70,
    "tcgkorea":    0.70,
    "snkrdunk":    0.70,
}
DEFAULT_SOURCE_RELIABILITY = 0.60

RECENCY_HALF_LIFE_DAYS = 21.0
DEFAULT_AGE_DAYS = 30.0

OUTLIER_IQR_MULTIPLIER = 2.0
MIN_SOURCES_FOR_OUTLIER_DROP = 3   # need ≥3 sources to define a consensus


def _age_days(sold_at: Any) -> float:
    if not sold_at:
        return DEFAULT_AGE_DAYS
    if isinstance(sold_at, (int, float)):
        ts = float(sold_at)
        return max(0.0, (time.time() - ts) / 86400.0)
    if isinstance(sold_at, str):
        # Best-effort ISO date parse without bringing in dateutil.
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d"):
            try:
                from datetime import datetime
                t = datetime.strptime(sold_at[:len(fmt)+5], fmt)
                return max(0.0, (time.time() - t.timestamp()) / 86400.0)
            except Exception:
                continue
    return DEFAULT_AGE_DAYS


def _recency_weight(age_days: float) -> float:
    return math.exp(-math.log(2) * age_days / RECENCY_HALF_LIFE_DAYS)


def _source_reliability(source: str) -> float:
    if not source:
        return DEFAULT_SOURCE_RELIABILITY
    s = source.lower()
    return SOURCE_RELIABILITY.get(s, DEFAULT_SOURCE_RELIABILITY)


def _volume_weight(n: int) -> float:
    if n <= 0:
        return 0.0
    return min(1.0, math.sqrt(n) / 3.0)


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _weighted_median(pairs: list[tuple[float, float]]) -> float:
    """``pairs`` = [(value, weight), …].  Classic weighted median."""
    if not pairs:
        return 0.0
    total = sum(w for _, w in pairs) or 1.0
    cum = 0.0
    for v, w in sorted(pairs):
        cum += w
        if cum >= total / 2.0:
            return v
    return pairs[-1][0]


def aggregate(listings: list[dict]) -> dict:
    """
    Drop-in smart replacement for ``price_aggregator._aggregate``.

    Expects each listing as
      {"price_usd": float, "source": str, "sold_at": Optional[str|ts]}

    Returns the same shape the old aggregator returned, plus a
    ``per_source`` breakdown and ``dropped_sources`` for transparency.
    """
    rows = [r for r in listings
            if r.get("price_usd") is not None and r["price_usd"] > 0]
    if not rows:
        return {"median_usd": None, "p25_usd": None, "p75_usd": None,
                "sample_count": 0, "source_count": 0,
                "volatility": None, "volatile_flag": False,
                "per_source": {}, "dropped_sources": [],
                "method": "smart_weighted"}

    # Group by source and pre-compute per-source medians.
    by_src: dict[str, list[dict]] = {}
    for r in rows:
        by_src.setdefault((r.get("source") or "?").lower(), []).append(r)

    per_source: dict[str, dict] = {}
    for src, items in by_src.items():
        vals = sorted(float(i["price_usd"]) for i in items)
        per_source[src] = {
            "n":      len(vals),
            "median": round(statistics.median(vals), 2),
            "p25":    round(_percentile(vals, 0.25), 2),
            "p75":    round(_percentile(vals, 0.75), 2),
            "reliability": _source_reliability(src),
        }

    # Outlier-source rejection (only when enough sources to vote).
    dropped: list[str] = []
    src_medians = sorted(s["median"] for s in per_source.values())
    if len(src_medians) >= MIN_SOURCES_FOR_OUTLIER_DROP:
        m = statistics.median(src_medians)
        iqr = max(0.01, _percentile(src_medians, 0.75)
                       - _percentile(src_medians, 0.25))
        threshold = OUTLIER_IQR_MULTIPLIER * iqr
        for src, info in list(per_source.items()):
            if abs(info["median"] - m) > threshold:
                dropped.append(src)
                per_source.pop(src, None)
                by_src.pop(src, None)

    # Build (value, weight) pairs from the surviving listings.
    pairs: list[tuple[float, float]] = []
    for src, items in by_src.items():
        v_weight = _volume_weight(len(items))
        rel = _source_reliability(src)
        for r in items:
            age = _age_days(r.get("sold_at"))
            w = v_weight * rel * _recency_weight(age)
            if w > 0:
                pairs.append((float(r["price_usd"]), w))

    if not pairs:
        return {"median_usd": None, "p25_usd": None, "p75_usd": None,
                "sample_count": 0, "source_count": 0,
                "volatility": None, "volatile_flag": False,
                "per_source": per_source, "dropped_sources": dropped,
                "method": "smart_weighted"}

    sorted_vals = sorted(v for v, _ in pairs)
    p25 = round(_percentile(sorted_vals, 0.25), 2)
    p75 = round(_percentile(sorted_vals, 0.75), 2)
    median = round(_weighted_median(pairs), 2)
    iqr = max(0.0, p75 - p25)
    volatility = round(iqr / median, 4) if median else None

    return {
        "median_usd":    median,
        "p25_usd":       p25,
        "p75_usd":       p75,
        "sample_count":  len(pairs),
        "source_count":  len(by_src),
        "volatility":    volatility,
        "volatile_flag": volatility is not None and volatility > 0.45,
        "per_source":    per_source,
        "dropped_sources": dropped,
        "method":        "smart_weighted",
    }
