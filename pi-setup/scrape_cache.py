"""
Redis-backed cache wrapper for marketplace scrapers.

Why this exists
---------------
Naver / TCGkorea / SnkrDunk / Cardmarket all rate-limit aggressively when
they see repeated traffic from one IP — a busy POS day can easily get the
shop's IP soft-blocked for hours.  This module wraps every scraper with a
per-(source, query, kwargs) Redis cache so we hit each marketplace once
per `ttl` window, no matter how many tablet/website lookups happen.

Falls back to an in-process LRU when Redis is unavailable, so the POS
keeps working off-grid.

Drift detector
--------------
Each (source, query) pair maintains a rolling counter of consecutive
empty results.  Three zero-results in a row on a known-good query is
strong evidence that the site has changed its HTML — we log a WARNING
and (if Sentry is wired) emit a breadcrumb so you find out before the
sales team does.
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
from collections import OrderedDict
from typing import Callable

log = logging.getLogger("scrape_cache")

DEFAULT_TTL = int(os.environ.get("SCRAPER_CACHE_TTL", "600"))   # 10 min
DRIFT_THRESHOLD = int(os.environ.get("SCRAPER_DRIFT_THRESHOLD", "3"))

# Known-good probe queries used to distinguish "nothing matched" (legit)
# from "site CSS broke" (drift). Empty results for these = real drift.
DRIFT_CANARIES = {
    "naver":      ("pikachu", "포켓몬"),
    "tcgkorea":   ("피카츄", "리자몽"),
    "snkrdunk":   ("pikachu", "ピカチュウ"),
    "cardmarket": ("Charizard", "Pikachu"),
    # tcgplayer canary intentionally omitted — tcgdex's tcgplayer field is
    # empirically null on essentially every card (see price_scrapers.py
    # module docstring), so empty results aren't drift, they're expected.
    # Re-add `"tcgplayer": ("Charizard", "Pikachu")` if tcgdex backfills.
}

_inproc: "OrderedDict[str, tuple[float, list]]" = OrderedDict()
_INPROC_MAX = 256


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────
def cached(source: str, *, ttl: int = DEFAULT_TTL):
    """Decorator: wrap a scraper so identical calls hit Redis instead of HTTP.

    Empty-result poisoning guard
    ----------------------------
    Empty list/dict results are NOT cached. Reasons:

      * Transient upstream failures (Cloudflare challenge timeout, naver 401
        from a momentarily-misconfigured key, snkrdunk hiccup) all return
        []. Caching those for the TTL window meant a one-off failure
        silently blocked retries for the next 10 minutes — making config
        fixes appear not to work and forcing manual `redis-cli DEL` after
        every probe (we burnt an hour on this during the C5 rollout).
      * Cost is small: legitimate "no listings for this query" misses
        re-fetch on every call, but the upstream itself is fast (<1s for
        an HTTP 200 with no rows) and the drift counter still fires.
      * Drift detection still works — `_track_drift` is called on every
        result, cached or not, so canary-query empties still increment
        the warning counter as before.

    Truthy results (any non-empty list/dict, or any non-collection value)
    are cached normally for the full TTL.
    """
    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            key = _make_key(source, fn.__name__, args, kwargs)
            hit = _get(key)
            if hit is not None:
                log.debug("[scrape-cache] HIT %s", key)
                return hit
            result = fn(*args, **kwargs)
            # Skip caching empty collections — see docstring rationale.
            # `len()` would TypeError on non-collections, so guard with
            # isinstance first; non-collection return types still cache.
            should_cache = not (isinstance(result, (list, dict, tuple, set))
                                and len(result) == 0)
            if should_cache:
                _set(key, result, ttl)
            else:
                log.debug("[scrape-cache] SKIP empty result for %s", key)
            _track_drift(source, args, kwargs, result)
            return result
        wrapped.__cached_source__ = source  # type: ignore[attr-defined]
        return wrapped
    return deco


def invalidate(source: str | None = None) -> int:
    """Drop cached entries for one source (or everything when source=None)."""
    n = 0
    r = _redis()
    pattern = f"scrape:{source}:*" if source else "scrape:*"
    if r is not None:
        try:
            for k in r.scan_iter(pattern):
                r.delete(k); n += 1
        except Exception as exc:
            log.warning("[scrape-cache] redis invalidate failed: %s", exc)
    # also clear in-process
    keep = {k: v for k, v in _inproc.items() if not k.startswith(pattern.rstrip("*"))}
    n += len(_inproc) - len(keep)
    _inproc.clear(); _inproc.update(keep)
    return n


def stats() -> dict:
    """Return per-source key counts + drift counters — used by /admin/scrape/status."""
    r = _redis()
    counts: dict = {}
    drift:  dict = {}
    for src in DRIFT_CANARIES:
        if r is not None:
            try:
                counts[src] = sum(1 for _ in r.scan_iter(f"scrape:{src}:*"))
                d = r.get(f"scrape:drift:{src}")
                drift[src] = int(d) if d else 0
            except Exception:
                counts[src] = 0; drift[src] = 0
        else:
            counts[src] = sum(1 for k in _inproc if k.startswith(f"scrape:{src}:"))
            drift[src] = 0
    return {
        "ttl_seconds":     DEFAULT_TTL,
        "drift_threshold": DRIFT_THRESHOLD,
        "redis":           r is not None,
        "cached_keys":     counts,
        "drift_counters":  drift,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Internals
# ──────────────────────────────────────────────────────────────────────────────
def _redis():
    """Lazy-borrow the server.py redis client; never raise."""
    try:
        import server  # type: ignore
        return server._redis()
    except Exception:
        return None


def _make_key(source: str, fn_name: str, args: tuple, kwargs: dict) -> str:
    payload = json.dumps({"a": list(args), "k": kwargs},
                         sort_keys=True, default=str, ensure_ascii=False)
    sig = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    return f"scrape:{source}:{fn_name}:{sig}"


def _get(key: str):
    r = _redis()
    if r is not None:
        try:
            v = r.get(key)
            if v is not None:
                return json.loads(v)
        except Exception as exc:
            log.debug("[scrape-cache] redis get error: %s", exc)
    # in-process fallback
    import time
    pair = _inproc.get(key)
    if pair and pair[0] > time.time():
        _inproc.move_to_end(key)
        return pair[1]
    if pair:
        _inproc.pop(key, None)
    return None


def _set(key: str, value, ttl: int):
    r = _redis()
    if r is not None:
        try:
            r.set(key, json.dumps(value, ensure_ascii=False), ex=ttl)
            return
        except Exception as exc:
            log.debug("[scrape-cache] redis set error: %s", exc)
    import time
    _inproc[key] = (time.time() + ttl, value)
    if len(_inproc) > _INPROC_MAX:
        _inproc.popitem(last=False)


def _track_drift(source: str, args: tuple, kwargs: dict, result) -> None:
    """Bump a Redis counter when canary queries return empty."""
    canaries = DRIFT_CANARIES.get(source, ())
    query = ""
    if args: query = str(args[0]).strip()
    if not query: query = str(kwargs.get("query") or kwargs.get("q") or "").strip()
    if not query or query.lower() not in {c.lower() for c in canaries}:
        return  # not a canary — empty result is legit, ignore

    bad = isinstance(result, list) and len(result) == 0
    r = _redis()
    key = f"scrape:drift:{source}"
    if not bad:
        if r is not None:
            try: r.delete(key)
            except Exception: pass
        return

    if r is not None:
        try:
            n = r.incr(key); r.expire(key, 86400)
        except Exception:
            n = 0
    else:
        n = 0
    if n >= DRIFT_THRESHOLD:
        log.warning("[scrape-drift] %s returned 0 results for canary '%s' "
                    "%d times in a row — selectors likely broken",
                    source, query, n)
        try:
            import sentry_sdk
            sentry_sdk.add_breadcrumb(category="scrape-drift", level="warning",
                                       message=f"{source} drift x{n}",
                                       data={"query": query})
            sentry_sdk.capture_message(
                f"Scraper drift detected: {source} (x{n})", level="warning")
        except Exception:
            pass
