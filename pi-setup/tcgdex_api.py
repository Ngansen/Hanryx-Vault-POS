"""
tcgdex.net public API client — replaces the Cloudflare-blocked cardmarket
scraper and adds tcgplayer (USD) pricing as a new capability.

Why this exists
---------------
By 2026-05, cardmarket.com sits behind Cloudflare bot-fight from our
trade-show egress IP. C5/C6 diagnostics confirmed:

  * Header-spoofed `requests`        → HTTP 403
  * Playwright + stealth             → "Just a moment..." challenge page
  * Playwright + stealth + 15s wait  → IUAM still does not clear
  * Cost per query: 30s of timeouts, then [].

The block is at the network layer (IP fingerprint + JA3) — no amount of
chromium-side stealth defeats it. tcgdex.dev publishes a free public REST
API that EMBEDS Cardmarket EUR pricing AND TCGplayer USD pricing directly
in every Pokemon card response — exactly the data we were trying to
scrape, pre-aggregated, no auth, ~150ms per call. Daily refresh upstream;
our `@cached` Redis layer absorbs the rest.

API surface
-----------
    GET https://api.tcgdex.net/v2/en/cards?name=<q>
        → list of {id, localId, name, image} (no pricing)

    GET https://api.tcgdex.net/v2/en/cards/<id>
        → full card object with .pricing.{cardmarket,tcgplayer}
        cardmarket = {avg, low, trend, avg1, avg7, avg30, avg-holo, ...}
        tcgplayer  = {normal:{lowPrice,midPrice,marketPrice,...},
                      holo:{...}, reverse:{...}}
        Either provider key is omitted when the card isn't listed there.

Wrapper shape
-------------
We expose two scrapers compatible with `price_scrapers.SCRAPERS` dispatch:

    cardmarket(query, *, game="Pokemon", limit=20) -> list[{...}]
    tcgplayer (query, *, limit=20)                 -> list[{...}]

Each returns the same dict shape the existing scrapers do:
    {title, price, currency, url, image, source}

Cost per query: 1 search + top _DETAIL_LIMIT (default 5) detail fetches
≈ 6 calls × 150ms ≈ 1s, well within the trade-show chip budget. Detail
fetches run sequentially — could parallelize with concurrent.futures
if _DETAIL_LIMIT is ever raised significantly, but at N=5 the wall time
is dominated by the search call anyway.

Game support
------------
tcgdex covers Pokemon ONLY. The `game` kwarg on `cardmarket()` is
preserved for backward compatibility with the prior scrape-based
implementation (`game=Magic|Lorcana|OnePiece|...`); for any non-Pokemon
game we return [] and log at debug. Adding non-Pokemon coverage would
require a different upstream (e.g. scryfall for MTG, a separate TCG API
for Lorcana/OnePiece).
"""
from __future__ import annotations

import logging
import os

import requests

from scrape_cache import cached

log = logging.getLogger("tcgdex_api")

_BASE = os.environ.get("TCGDEX_API_BASE", "https://api.tcgdex.net/v2/en").rstrip("/")
_TIMEOUT = float(os.environ.get("TCGDEX_TIMEOUT_S", "8"))
# How many top search hits we fetch detail for. Each detail = 1 HTTP call.
# 5 keeps wall time ~1s while covering the most common card+set combos
# that match a query. Bump for completeness vs. latency tradeoff.
_DETAIL_LIMIT = int(os.environ.get("TCGDEX_DETAIL_LIMIT", "5"))

_session = requests.Session()
_session.headers.update({
    "User-Agent": "HanryxVault-POS/1.0 (+pos@hanryxvault.local)",
    "Accept": "application/json",
})


# ─────────────────────────────────────────────────────────────────────────────
# Low-level API helpers
# ─────────────────────────────────────────────────────────────────────────────
def _search(query: str) -> list[dict]:
    """List cards matching `query` by name. Returns [] on any failure."""
    try:
        r = _session.get(f"{_BASE}/cards", params={"name": query},
                         timeout=_TIMEOUT)
    except Exception as exc:
        log.info("[tcgdex] search %r failed: %s", query, exc)
        return []
    if r.status_code != 200:
        log.info("[tcgdex] search %r → HTTP %d", query, r.status_code)
        return []
    try:
        data = r.json()
    except Exception as exc:
        log.info("[tcgdex] search %r returned non-JSON: %s", query, exc)
        return []
    return data if isinstance(data, list) else []


def _detail(card_id: str) -> dict | None:
    """Fetch full card payload (including .pricing). None on any failure."""
    try:
        r = _session.get(f"{_BASE}/cards/{card_id}", timeout=_TIMEOUT)
    except Exception as exc:
        log.debug("[tcgdex] detail %s failed: %s", card_id, exc)
        return None
    if r.status_code != 200:
        log.debug("[tcgdex] detail %s → HTTP %d", card_id, r.status_code)
        return None
    try:
        return r.json()
    except Exception as exc:
        log.debug("[tcgdex] detail %s returned non-JSON: %s", card_id, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Pricing extraction — provider-specific shape mapping
# ─────────────────────────────────────────────────────────────────────────────
def _format_for_cardmarket(card: dict) -> dict | None:
    """Pull cardmarket EUR pricing out of a tcgdex card payload, or None."""
    pricing = (card.get("pricing") or {}).get("cardmarket")
    if not pricing or not isinstance(pricing, dict):
        return None
    # `trend` is cardmarket's own published "trend price" — closest to what
    # a buyer actually pays today. Falls back to 30-day avg, then plain avg,
    # then low. We deliberately skip avg1 (1-day) because it's noisy on
    # low-liquidity cards and can flip 10x on a single trade.
    price = (pricing.get("trend") or pricing.get("avg30")
             or pricing.get("avg") or pricing.get("low"))
    if not price:
        return None
    name = card.get("name") or "?"
    set_info = card.get("set") if isinstance(card.get("set"), dict) else {}
    set_name = set_info.get("name", "")
    title = f"{name} ({set_name})" if set_name else name
    image = card.get("image") or ""
    if isinstance(image, str) and image and not image.startswith("http"):
        # tcgdex sometimes ships protocol-relative or path-only image URLs;
        # blank them rather than emit broken links to the chip UI.
        image = ""
    card_id = card.get("id", "")
    # Cardmarket itself doesn't expose a stable URL for tcgdex IDs, but the
    # idProduct field (when present) deeplinks correctly.
    id_product = pricing.get("idProduct")
    if id_product:
        url = f"https://www.cardmarket.com/en/Pokemon/Products/Singles/{id_product}"
    else:
        # Fallback: cardmarket's search page seeded with the card name. Less
        # precise but always lands somewhere useful.
        from urllib.parse import quote_plus
        url = (f"https://www.cardmarket.com/en/Pokemon/Products/Search"
               f"?searchString={quote_plus(name)}")
    return {
        "title": title,
        "price": float(price),
        "currency": pricing.get("unit", "EUR"),
        "url": url,
        "image": image,
        "source": "cardmarket",
    }


def _format_for_tcgplayer(card: dict) -> dict | None:
    """Pull tcgplayer USD pricing out of a tcgdex card payload, or None.

    tcgplayer pricing is nested by variant: {normal, holo, reverse}, each
    with {lowPrice, midPrice, highPrice, marketPrice, directLowPrice}. We
    prefer marketPrice (their "fair market" — closest to actual sale) and
    fall back to midPrice. We pick whichever variant has data, in the
    order most TCG buyers care about: normal → holo → reverse.
    """
    pricing = (card.get("pricing") or {}).get("tcgplayer")
    if not pricing or not isinstance(pricing, dict):
        return None
    chosen_price = None
    for variant in ("normal", "holo", "reverse"):
        v = pricing.get(variant) or {}
        chosen_price = v.get("marketPrice") or v.get("midPrice")
        if chosen_price:
            break
    if not chosen_price:
        return None
    name = card.get("name") or "?"
    set_info = card.get("set") if isinstance(card.get("set"), dict) else {}
    set_name = set_info.get("name", "")
    title = f"{name} ({set_name})" if set_name else name
    image = card.get("image") or ""
    if isinstance(image, str) and image and not image.startswith("http"):
        image = ""
    from urllib.parse import quote_plus
    url = (f"https://www.tcgplayer.com/search/pokemon/product"
           f"?productLineName=pokemon&q={quote_plus(name)}")
    return {
        "title": title,
        "price": float(chosen_price),
        "currency": pricing.get("unit", "USD"),
        "url": url,
        "image": image,
        "source": "tcgplayer",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public scrapers (compatible with price_scrapers.SCRAPERS dispatch)
# ─────────────────────────────────────────────────────────────────────────────
def _query_pricing(query: str, formatter, limit: int) -> list[dict]:
    """Shared search → top-N detail-fetch → format pipeline."""
    if not query:
        return []
    hits = _search(query)
    if not hits:
        return []
    out: list[dict] = []
    # Sequential — N=5 × 150ms ≈ 750ms. tcgdex appears unmetered for
    # reasonable use; if we ever raise N significantly, switch to a
    # ThreadPoolExecutor with max_workers≈4 to keep wall time flat.
    for hit in hits[:_DETAIL_LIMIT]:
        if len(out) >= limit:
            break
        card_id = hit.get("id")
        if not card_id:
            continue
        card = _detail(card_id)
        if not card:
            continue
        formatted = formatter(card)
        if formatted:
            out.append(formatted)
    return out


@cached("cardmarket")
def cardmarket(query: str, *, game: str = "Pokemon",
               limit: int = 20) -> list[dict]:
    """Cardmarket EUR pricing via tcgdex.net (replaces the CF-blocked scrape).

    `game` is preserved for backward compatibility with the prior
    scrape-based implementation. tcgdex covers Pokemon only — passing
    any other game returns [] without an upstream call.
    """
    if game and game.lower().strip() not in ("pokemon", "pokémon", ""):
        log.debug("[tcgdex] cardmarket: game=%r not supported "
                  "(tcgdex is Pokemon-only)", game)
        return []
    return _query_pricing(query, _format_for_cardmarket, limit)


@cached("tcgplayer")
def tcgplayer(query: str, *, limit: int = 20) -> list[dict]:
    """TCGplayer USD pricing via tcgdex.net.

    NEW capability — TCGplayer pricing wasn't reachable from this Pi
    before (TCGplayer's own API requires partner OAuth credentials we
    don't have). tcgdex republishes it under their permissive license.
    Pokemon-only.
    """
    return _query_pricing(query, _format_for_tcgplayer, limit)
