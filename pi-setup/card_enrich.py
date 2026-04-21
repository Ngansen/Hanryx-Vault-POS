"""
card_enrich.py — Canonical card identity lookup.

Two free public APIs:
  • Pokémon TCG API (api.pokemontcg.io)  — English Pokémon
  • Scryfall          (api.scryfall.com)  — Magic: The Gathering

Both are no-auth, rate-friendly. Results are cached in Redis for 24 h
when Redis is available, otherwise in a small in-process LRU.

Returned dicts share a stable schema:
    {
      "game":   "pokemon" | "mtg",
      "name":   canonical name,
      "set":    set name,
      "set_code": short set code,
      "number": collector number,
      "rarity": rarity string or None,
      "image":  small/normal image URL,
      "source": "pokemontcg" | "scryfall",
    }
or None when no confident match is found.
"""

from __future__ import annotations

import json
import logging
import time
from functools import lru_cache
from typing import Optional

import requests

log = logging.getLogger("hanryx.card_enrich")

_TCG_URL      = "https://api.pokemontcg.io/v2/cards"
_SCRYFALL_URL = "https://api.scryfall.com/cards/named"

_CACHE_TTL_S = 86_400   # 24 h


# --------------------------------------------------------------------------
# Redis-backed cache (falls back to LRU when Redis is unavailable)
# --------------------------------------------------------------------------
def _cache_get(redis_client, key: str):
    if redis_client is not None:
        try:
            raw = redis_client.get(key)
            if raw:
                return json.loads(raw)
        except Exception as e:
            log.debug("[enrich] redis get failed: %s", e)
    return _lru_cache_get(key)


def _cache_set(redis_client, key: str, value):
    if redis_client is not None:
        try:
            redis_client.setex(key, _CACHE_TTL_S, json.dumps(value))
            return
        except Exception as e:
            log.debug("[enrich] redis set failed: %s", e)
    _lru_cache_set(key, value)


# Tiny in-process fallback (process-local, evicts naturally)
_lru_store: dict[str, tuple[float, object]] = {}
_LRU_MAX = 2048


def _lru_cache_get(key: str):
    item = _lru_store.get(key)
    if not item:
        return None
    expires, value = item
    if time.time() > expires:
        _lru_store.pop(key, None)
        return None
    return value


def _lru_cache_set(key: str, value):
    if len(_lru_store) >= _LRU_MAX:
        # Drop ~10 % of oldest entries
        for k in list(_lru_store.keys())[: _LRU_MAX // 10]:
            _lru_store.pop(k, None)
    _lru_store[key] = (time.time() + _CACHE_TTL_S, value)


# --------------------------------------------------------------------------
# Pokémon TCG API
# --------------------------------------------------------------------------
def enrich_pokemon(name: str, number: Optional[str] = None,
                   redis_client=None) -> Optional[dict]:
    """
    Look up an English Pokémon card by name (and optional collector number).
    Returns the canonical card dict or None.
    """
    name = (name or "").strip()
    if not name:
        return None

    cache_key = f"hv:enrich:pkm:{name.lower()}|{(number or '').lower()}"
    cached = _cache_get(redis_client, cache_key)
    if cached is not None:
        return cached or None   # cached negative match stored as {}

    q = f'name:"{name}"'
    if number:
        q += f' number:"{number.split("/")[0]}"'

    try:
        r = requests.get(
            _TCG_URL,
            params={"q": q, "pageSize": 1},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json().get("data") or []
    except Exception as e:
        log.debug("[enrich] pokemontcg lookup failed for %r: %s", name, e)
        return None

    if not data:
        _cache_set(redis_client, cache_key, {})    # negative cache
        return None

    c = data[0]
    result = {
        "game":     "pokemon",
        "name":     c.get("name"),
        "set":      (c.get("set") or {}).get("name"),
        "set_code": (c.get("set") or {}).get("id"),
        "number":   c.get("number"),
        "rarity":   c.get("rarity"),
        "image":    (c.get("images") or {}).get("small"),
        "source":   "pokemontcg",
    }
    _cache_set(redis_client, cache_key, result)
    return result


# --------------------------------------------------------------------------
# Scryfall (Magic: The Gathering)
# --------------------------------------------------------------------------
_SCRYFALL_LAST_CALL = [0.0]   # rate-limit pacing (Scryfall asks for ~100ms)


def enrich_mtg(name: str, set_code: Optional[str] = None,
               redis_client=None) -> Optional[dict]:
    """
    Look up an MTG card by fuzzy name (and optional 3-letter set code).
    Returns the canonical card dict or None.
    """
    name = (name or "").strip()
    if not name:
        return None

    cache_key = f"hv:enrich:mtg:{name.lower()}|{(set_code or '').lower()}"
    cached = _cache_get(redis_client, cache_key)
    if cached is not None:
        return cached or None

    # Polite pacing — Scryfall asks for ≥ 100 ms between calls.
    delta = time.time() - _SCRYFALL_LAST_CALL[0]
    if delta < 0.1:
        time.sleep(0.1 - delta)
    _SCRYFALL_LAST_CALL[0] = time.time()

    params = {"fuzzy": name}
    if set_code:
        params["set"] = set_code.lower()

    try:
        r = requests.get(_SCRYFALL_URL, params=params, timeout=8)
        if r.status_code == 404:
            _cache_set(redis_client, cache_key, {})
            return None
        r.raise_for_status()
        c = r.json()
    except Exception as e:
        log.debug("[enrich] scryfall lookup failed for %r: %s", name, e)
        return None

    img = (
        (c.get("image_uris") or {}).get("small")
        or ((c.get("card_faces") or [{}])[0].get("image_uris") or {}).get("small")
    )
    result = {
        "game":     "mtg",
        "name":     c.get("name"),
        "set":      c.get("set_name"),
        "set_code": c.get("set"),
        "number":   c.get("collector_number"),
        "rarity":   c.get("rarity"),
        "image":    img,
        "source":   "scryfall",
    }
    _cache_set(redis_client, cache_key, result)
    return result


# --------------------------------------------------------------------------
# Convenience dispatcher
# --------------------------------------------------------------------------
def enrich(game: str, name: str, number: Optional[str] = None,
           set_code: Optional[str] = None, redis_client=None) -> Optional[dict]:
    """
    Game-agnostic entry point. `game` is one of: pokemon, mtg.
    Other games (onepiece, lorcana, dbs) currently return None — no
    free canonical API exists for those at time of writing.
    """
    g = (game or "").lower().strip()
    if g in ("pokemon", "pkm", "pokémon"):
        return enrich_pokemon(name, number, redis_client=redis_client)
    if g in ("mtg", "magic"):
        return enrich_mtg(name, set_code or number, redis_client=redis_client)
    return None
