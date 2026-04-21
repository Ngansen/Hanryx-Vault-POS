"""
tcg_price_lookup.py — Reference-only price lookup via tcgpricelookup.com.

This is a *secondary* data point on the Market screen — never the source
of truth for buylist offers. It exists so the operator can spot-check our
own model against an independent aggregator (TCGPlayer + eBay sold +
graded prices in one call). If this service goes dark tomorrow, our
core pricing pipeline is unaffected.

API:        https://api.tcgpricelookup.com/v1
Auth:       X-API-Key header
SDK ref:    github.com/Ngansen/tcglookup-js (we mirror its endpoint shape)

Plan gating (returned as HTTP 403 by the upstream):
  • Free tier:    raw TCGPlayer prices only
  • Trader+:      eBay sold averages + graded prices + history

Cache strategy:
  • Search results: 6 h Redis (prices change daily, but we only call on
                              explicit "Compare" click anyway)
  • Negative results: 1 h
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

import requests

log = logging.getLogger("hanryx.tcgpl")

_BASE_URL = "https://api.tcgpricelookup.com/v1"

_POS_TTL_S = 6 * 3600
_NEG_TTL_S = 3600

_lru: dict[str, tuple[float, object]] = {}
_LRU_MAX = 2048

# Map our internal game ids → tcgpricelookup slugs.
# Korean Pokémon and Dragon Ball Super are not covered by this service —
# callers will get a None back for those, which is the honest answer.
_GAME_SLUG = {
    "pokemon":       "pokemon",
    "pokemon_en":    "pokemon",
    "pokemon_jp":    "pokemon-jp",
    "pokemon_japan": "pokemon-jp",
    "mtg":           "mtg",
    "magic":         "mtg",
    "yugioh":        "yugioh",
    "onepiece":      "onepiece",
    "one_piece":     "onepiece",
    "lorcana":       "lorcana",
    "swu":           "swu",
    "starwars":      "swu",
    "fab":           "fab",
    # Intentionally NOT mapped — service does not cover these:
    #   pokemon_kr (Korean) → no slug, returns None
    #   dbs / dragonball   → no slug, returns None
}


# --------------------------------------------------------------------------
# Cache (Redis with LRU fallback)
# --------------------------------------------------------------------------
def _cache_get(redis_client, key: str):
    if redis_client is not None:
        try:
            raw = redis_client.get(key)
            if raw:
                return json.loads(raw)
        except Exception as e:
            log.debug("[tcgpl] redis get failed: %s", e)
    item = _lru.get(key)
    if not item:
        return None
    expires, value = item
    if time.time() > expires:
        _lru.pop(key, None)
        return None
    return value


def _cache_set(redis_client, key: str, value, ttl: int):
    if redis_client is not None:
        try:
            redis_client.setex(key, ttl, json.dumps(value))
            return
        except Exception as e:
            log.debug("[tcgpl] redis set failed: %s", e)
    if len(_lru) >= _LRU_MAX:
        for k in list(_lru.keys())[: _LRU_MAX // 10]:
            _lru.pop(k, None)
    _lru[key] = (time.time() + ttl, value)


# --------------------------------------------------------------------------
# Internal request
# --------------------------------------------------------------------------
def _request(path: str, params: Optional[dict] = None) -> tuple[int, dict | None]:
    """
    Returns (status_code, json_body). json_body is None on network failure.
    Caller handles 401/403/404/429 mapping.
    """
    key = os.environ.get("TCGPL_API_KEY", "").strip()
    if not key:
        return 0, None
    try:
        r = requests.get(
            f"{_BASE_URL}{path}",
            headers={
                "X-API-Key": key,
                "Accept":    "application/json",
                "User-Agent": "hanryxvault-pos/1.0 (+reference-only)",
            },
            params={k: v for k, v in (params or {}).items() if v is not None},
            timeout=10,
        )
    except Exception as e:
        log.warning("[tcgpl] request %s failed: %s", path, e)
        return 0, None
    try:
        body = r.json()
    except Exception:
        body = None
    return r.status_code, body


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def search_card(name: str, game: Optional[str] = None,
                number: Optional[str] = None,
                redis_client=None) -> Optional[dict]:
    """
    Search for one card and return the best-matching normalised reference
    block. Returns None when:
      • TCGPL_API_KEY isn't configured
      • Game isn't in their coverage (Korean Pokémon, DBS)
      • No match found
      • Upstream error (logged)

    Returns {"error": "..."} on plan-gated / auth / quota errors so the
    caller can surface a useful message.
    """
    name = (name or "").strip()
    if not name:
        return None
    if not os.environ.get("TCGPL_API_KEY", "").strip():
        return {"error": "no_token"}

    slug = _GAME_SLUG.get((game or "").lower().strip()) if game else None
    # If a game was given but it's not covered, bail honestly
    if game and not slug:
        return None

    # Build a query that biases toward an exact match
    q = name if not number else f"{name} {number}"
    cache_key = f"hv:tcgpl:search:{(slug or '*')}|{q.lower()}"

    cached = _cache_get(redis_client, cache_key)
    if cached is not None:
        return cached or None

    status, body = _request("/cards/search", {
        "q":     q,
        "game":  slug,
        "limit": 5,
    })

    if status == 0:
        return {"error": "upstream"}
    if status == 401:
        return {"error": "bad_token"}
    if status == 429:
        return {"error": "rate_limited"}
    if status == 404 or not body:
        _cache_set(redis_client, cache_key, {}, _NEG_TTL_S)
        return None
    if status >= 400:
        return {"error": "upstream", "status": status}

    cards = (body or {}).get("data") or []
    if not cards:
        _cache_set(redis_client, cache_key, {}, _NEG_TTL_S)
        return None

    # Prefer an exact card-number match when number was supplied
    chosen = cards[0]
    if number:
        for c in cards:
            if (c.get("number") or "").strip() == str(number).strip():
                chosen = c
                break

    result = _normalise(chosen)
    _cache_set(redis_client, cache_key, result, _POS_TTL_S)
    return result


def _normalise(card: dict) -> dict:
    """Flatten the nested SDK shape into something the UI can render directly."""
    prices = card.get("prices") or {}
    raw    = prices.get("raw") or {}
    graded = prices.get("graded") or {}

    # Pull near-mint TCGPlayer + eBay if present
    nm     = raw.get("near_mint") or {}
    tcgp   = (nm.get("tcgplayer") or {})
    ebay   = (nm.get("ebay") or {})

    # Flatten graded into a flat dict like {"psa_10": 185.0, "bgs_9.5": 210.0}
    flat_graded: dict[str, float] = {}
    for grader, grades in graded.items():
        for grade, srcs in (grades or {}).items():
            best = None
            for src in ("ebay", "tcgplayer"):
                row = (srcs or {}).get(src) or {}
                v = row.get("avg_30d") or row.get("avg_7d") or row.get("avg_1d")
                if v is not None:
                    best = v
                    break
            if best is not None:
                flat_graded[f"{grader}_{grade}".lower()] = best

    return {
        "card_id":     card.get("id"),
        "name":        card.get("name"),
        "number":      card.get("number"),
        "set":         (card.get("set") or {}).get("name"),
        "set_slug":    (card.get("set") or {}).get("slug"),
        "game":        (card.get("game") or {}).get("slug"),
        "rarity":      card.get("rarity"),
        "variant":     card.get("variant"),
        "image":       card.get("image_url"),
        "tcgplayer": {
            "market": tcgp.get("market"),
            "low":    tcgp.get("low"),
            "mid":    tcgp.get("mid"),
            "high":   tcgp.get("high"),
        },
        "ebay": {
            "avg_1d":  ebay.get("avg_1d"),
            "avg_7d":  ebay.get("avg_7d"),
            "avg_30d": ebay.get("avg_30d"),
        },
        "graded":         flat_graded,
        "last_update":    card.get("last_price_update") or card.get("updated_at"),
        "source":         "tcgpricelookup",
    }


def compute_delta(reference: Optional[dict], our_market: Optional[float]) -> Optional[float]:
    """
    Return |reference - ours| / ours so the UI can show "agreement: 6%".
    Prefers TCGPlayer market, falls back to eBay 7d avg.
    """
    if not reference or our_market is None or our_market <= 0:
        return None
    ref = (reference.get("tcgplayer") or {}).get("market")
    if ref is None:
        ref = (reference.get("ebay") or {}).get("avg_7d")
    if ref is None or ref <= 0:
        return None
    return round(abs(ref - our_market) / our_market, 4)
