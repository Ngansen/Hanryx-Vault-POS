"""
TCGplayer pricing without a TCGplayer API key.

TCGplayer closed off public API access in 2023 and stopped issuing new
keys, so the documented `api.tcgplayer.com` route is a dead end for
anyone who isn't already a partner. Fortunately, **pokemontcg.io
re-broadcasts TCGplayer's price feed verbatim** in every Pokémon card
response — that's the same data TCGplayer's own site shows in the price-
guide widget, refreshed daily.

Per-card response from pokemontcg.io includes:
    {
      "tcgplayer": {
        "url":        "https://prices.pokemontcg.io/...",
        "updatedAt":  "2026/04/19",
        "prices": {
          "normal":              {low, mid, high, market, directLow},
          "holofoil":            {low, mid, high, market, directLow},
          "reverseHolofoil":     {low, mid, high, market, directLow},
          "1stEditionHolofoil":  {low, mid, high, market, directLow},
          "1stEditionNormal":    {low, mid, high, market, directLow},
          "unlimitedHolofoil":   {low, mid, high, market, directLow}
        }
      }
    }

We fetch by name + (optional) card-number parsed out of the query
string, then emit ONE listing-shaped dict per variant per card, using
the variant's `market` price as the headline. The aggregator's trim +
median pass deals with the rest.

No key required (works against the public endpoint), but if you ever
set `POKEMONTCG_API_KEY` in the environment we'll send it for the
higher rate-limit tier.

Public API
----------
    tcgplayer_via_pokemontcg(query, *, limit=20) → list[dict]
"""
from __future__ import annotations

import logging
import os
import re
from urllib.parse import quote_plus

import requests

try:
    from scrape_cache import cached
except Exception:  # pragma: no cover
    def cached(_source, **_):
        def deco(fn): return fn
        return deco

log = logging.getLogger("tcgplayer_proxy")

_BASE = "https://api.pokemontcg.io/v2/cards"
_TIMEOUT = 12
_HDR = {"User-Agent": "HanryxVault-POS/1.0"}
_KEY = os.environ.get("POKEMONTCG_API_KEY", "").strip()
if _KEY:
    _HDR["X-Api-Key"] = _KEY

# Accept patterns like "025/198", "25/198", "25", or set-coded "SV1-25".
_RE_FRACTION = re.compile(r"\b(\d{1,4})\s*/\s*\d{1,4}\b")
_RE_BARENUM = re.compile(r"\b(\d{1,4})\b")
_PRICE_FIELDS = ("market", "mid", "low", "directLow", "high")


def _parse_query(query: str) -> tuple[str, str | None]:
    """Pull a card-number out of the query, leave the rest as the name."""
    q = (query or "").strip()
    if not q:
        return "", None
    m = _RE_FRACTION.search(q)
    if m:
        number = str(int(m.group(1)))
        name = (q[:m.start()] + q[m.end():]).strip()
        return name, number
    # Bare trailing number as a fallback ("Pikachu 25")
    parts = q.rsplit(" ", 1)
    if len(parts) == 2 and _RE_BARENUM.fullmatch(parts[1]):
        return parts[0].strip(), str(int(parts[1]))
    return q, None


def _build_pokemontcg_q(name: str, number: str | None) -> str:
    """Construct the pokemontcg.io Lucene-style q= expression."""
    parts = []
    if name:
        # Quote the name so multi-word names like "charizard ex" stay together.
        parts.append(f'name:"{name}"')
    if number:
        parts.append(f'number:"{number}"')
    return " ".join(parts) or '*:*'


def _pick_market_price(variant_prices: dict) -> float | None:
    """Pick the most-trustworthy price field, in order of preference."""
    if not isinstance(variant_prices, dict):
        return None
    for k in _PRICE_FIELDS:
        v = variant_prices.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


@cached("tcgplayer")
def tcgplayer_via_pokemontcg(query: str, *, limit: int = 20) -> list[dict]:
    """
    Fetch up to `limit` TCGplayer-priced variants for `query`.
    Each row matches the uniform listing shape used by every other source.
    """
    if not query:
        return []
    name, number = _parse_query(query)
    q = _build_pokemontcg_q(name, number)
    url = f"{_BASE}?q={quote_plus(q)}&pageSize={min(limit, 50)}"
    try:
        r = requests.get(url, headers=_HDR, timeout=_TIMEOUT)
        if r.status_code != 200:
            log.info("[tcgplayer] %s → HTTP %s", url, r.status_code)
            return []
        cards = (r.json() or {}).get("data") or []
    except (requests.RequestException, ValueError) as exc:
        log.info("[tcgplayer] %s → %s", url, exc)
        return []

    out: list[dict] = []
    for card in cards:
        tcg = (card or {}).get("tcgplayer") or {}
        prices = tcg.get("prices") or {}
        if not isinstance(prices, dict) or not prices:
            continue
        card_name = card.get("name") or ""
        set_name = (card.get("set") or {}).get("name") or ""
        number_disp = card.get("number") or ""
        product_url = tcg.get("url") or ""
        image = ((card.get("images") or {}).get("small")
                 or (card.get("images") or {}).get("large") or "")

        for variant, pricedict in prices.items():
            price = _pick_market_price(pricedict)
            if price is None:
                continue
            title = (f"{card_name} {set_name} {number_disp} "
                     f"({variant})").strip()
            out.append({
                "title":      title,
                "price":      price,
                "currency":   "USD",
                "url":        product_url,
                "image":      image,
                "source":     "tcgplayer",
                "variant":    variant,
                "set_name":   set_name,
                "card_number": number_disp,
            })
            if len(out) >= limit:
                return out
    return out
