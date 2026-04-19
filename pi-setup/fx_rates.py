"""
Foreign-exchange rates with daily Redis cache.

We use exchangerate.host as the primary (free, no key) and fall back to
open.er-api.com if the first source ever vanishes.  Rates are cached for
24 hours by default — currency moves on a long enough timescale that
minute-by-minute precision doesn't matter for retail card pricing.

Public API
----------
    to_usd(amount, currency)       → float USD value (or original on miss)
    convert(amount, src, dst)      → float in dst currency
    rates(base="USD")              → {"KRW": 1335.4, "JPY": 156.7, …}

All functions never raise — failures return the input or {}.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Mapping

import requests

log = logging.getLogger("fx_rates")

_TTL = int(os.environ.get("FX_RATE_TTL", "86400"))     # 24 h
_REDIS_KEY = "fx:rates:USD"
_HDR = {"User-Agent": "HanryxVault-POS/1.0"}

# In-process fallback when Redis is missing
_mem_rates: dict = {"ts": 0, "data": {}}


def _redis():
    try:
        import server  # type: ignore
        return server._redis()
    except Exception:
        return None


def _fetch_remote() -> dict:
    """Pull fresh rates from exchangerate.host, then open.er-api.com."""
    for url in (
        "https://api.exchangerate.host/latest?base=USD",
        "https://open.er-api.com/v6/latest/USD",
    ):
        try:
            r = requests.get(url, headers=_HDR, timeout=10)
            if r.status_code != 200:
                continue
            j = r.json()
            rates = j.get("rates") or j.get("conversion_rates") or {}
            if rates:
                rates = {k: float(v) for k, v in rates.items()
                         if isinstance(v, (int, float))}
                rates["USD"] = 1.0
                log.info("[fx] refreshed %d rates from %s", len(rates), url)
                return rates
        except Exception as exc:
            log.info("[fx] %s failed: %s", url, exc)
    return {}


def rates(base: str = "USD") -> dict:
    """Return {currency: rate-vs-base}.  Cached in Redis for 24 h."""
    base = (base or "USD").upper()

    # 1) Redis
    r = _redis()
    if r is not None:
        try:
            v = r.get(_REDIS_KEY)
            if v:
                cached = json.loads(v)
                return _rebase(cached, base)
        except Exception as exc:
            log.debug("[fx] redis get failed: %s", exc)

    # 2) In-process
    if _mem_rates["data"] and (time.time() - _mem_rates["ts"]) < _TTL:
        return _rebase(_mem_rates["data"], base)

    # 3) Network
    fresh = _fetch_remote()
    if fresh:
        if r is not None:
            try: r.set(_REDIS_KEY, json.dumps(fresh), ex=_TTL)
            except Exception: pass
        _mem_rates["ts"] = time.time(); _mem_rates["data"] = fresh
        return _rebase(fresh, base)

    return _mem_rates["data"] or {}


def _rebase(usd_rates: Mapping[str, float], base: str) -> dict:
    base = base.upper()
    if base == "USD":
        return dict(usd_rates)
    if base not in usd_rates:
        return dict(usd_rates)
    factor = 1.0 / usd_rates[base]
    return {c: round(v * factor, 6) for c, v in usd_rates.items()}


def convert(amount: float, src: str, dst: str) -> float | None:
    """Convert `amount` from src→dst currency.  Returns None on failure."""
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return None
    src = (src or "USD").upper(); dst = (dst or "USD").upper()
    if src == dst:
        return round(amount, 4)
    rs = rates("USD")
    if src not in rs or dst not in rs or rs[src] == 0:
        return None
    usd = amount / rs[src]
    return round(usd * rs[dst], 4)


def to_usd(amount: float, currency: str) -> float | None:
    return convert(amount, currency, "USD")


def normalize_listings(listings: list[dict]) -> list[dict]:
    """Add a `price_usd` field to every row (None if conversion fails)."""
    out = []
    for row in listings or []:
        new = dict(row)
        cur = new.get("currency") or "USD"
        new["price_usd"] = to_usd(new.get("price"), cur)
        out.append(new)
    return out


def median_usd(listings: list[dict]) -> float | None:
    """Median USD price across a flat list of listings, ignoring nulls."""
    vals = [r.get("price_usd") for r in (listings or [])
            if r.get("price_usd") is not None]
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    return round(vals[n // 2] if n % 2 else (vals[n//2 - 1] + vals[n//2]) / 2, 2)
