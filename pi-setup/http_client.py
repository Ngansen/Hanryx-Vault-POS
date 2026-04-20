"""
Shared HTTP client for every importer / scraper / price source.

Why
---
Each importer used to roll its own request loop with ad-hoc retry, no
backoff, no rate limit, and no awareness of whether the upstream was
already failing. One bad endpoint could lock up a long-running import
for tens of minutes.

This module provides a single ``request()`` entry point that gives
every caller four things uniformly:

1. **Per-host token-bucket rate limiting** — declared up-front in
   ``RATE_LIMITS`` (e.g. ``api.pokemontcg.io`` = 1000/day on the free
   tier, 20 000/day with an API key, 30 burst).  Requests block
   briefly when the bucket is empty rather than getting 429'd.

2. **Exponential backoff with jitter** on 429/5xx and connection
   errors — 3 retries with 0.5 s, 1.5 s, 4.5 s ± 30 % jitter by
   default.  Honours an upstream ``Retry-After`` header when present.

3. **Per-host circuit breaker** — after 5 consecutive failures within
   60 s the breaker opens and any subsequent calls to that host fail
   fast (returning ``None`` instead of hanging) until a 30 s cooldown
   elapses.  A long import job that hits an outage moves on instead of
   stalling.

4. **Priority queue** — callers tag requests as ``priority="critical"``
   (e.g. operator-triggered gap fill) or ``priority="background"``
   (e.g. nightly refresh).  Critical requests bypass the rate-limit
   wait queue and pre-empt the bucket.

Read state at any time with ``health_snapshot()`` for the admin
``/admin/sources/health`` page.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import urllib.error
import urllib.request

log = logging.getLogger("http_client")

# ── per-host configuration ──────────────────────────────────────────────────
# Rate limits expressed as a (capacity, refill_seconds_per_token) pair.
# capacity = max burst; tokens regenerate one every refill_seconds.
RATE_LIMITS: dict[str, tuple[int, float]] = {
    # pokemontcg.io: 1000/day free, 20 000/day keyed.
    # Conservative default: 1000/day = 1 token / 86.4 s, burst 30.
    "api.pokemontcg.io":   (30,  86.4),
    # eBay scraper (HTML): self-imposed gentle pace, 1 req every 2 s.
    "www.ebay.com":        (10,  2.0),
    "ebay.com":            (10,  2.0),
    # TCGplayer pricing — we proxy via pokemontcg.io, so its limit applies
    # there.  Direct hits to tcgplayer.com (rare) get a polite 1/s.
    "www.tcgplayer.com":   (5,   1.0),
    # Korean / Chinese / Japanese mirrors: 1 token / 0.5 s.
    "naver.com":           (10,  0.5),
    "shopping.naver.com":  (10,  0.5),
    "tcgkorea.com":        (10,  0.5),
    "snkrdunk.com":        (10,  0.5),
    "www.cardmarket.com":  (10,  0.5),
    # Default for everything else: 20 burst, 0.25 s refill.
    "*":                   (20,  0.25),
}

CIRCUIT_FAIL_THRESHOLD = 5      # consecutive failures
CIRCUIT_WINDOW_SEC = 60         # within this window
CIRCUIT_COOLDOWN_SEC = 30       # before half-open retry

DEFAULT_TIMEOUT = 15
DEFAULT_RETRIES = 3
RETRY_BASE_DELAY = 0.5
RETRY_JITTER = 0.30
USER_AGENT = "HanryxVault/1.0 (+pi-pos)"


# ── token-bucket implementation (thread-safe) ──────────────────────────────
@dataclass
class _Bucket:
    capacity: int
    refill_period: float    # seconds per token
    tokens: float = 0.0
    last_refill: float = field(default_factory=time.time)

    def take(self, *, critical: bool = False) -> float:
        """
        Block as needed and consume one token.  Returns the wait time spent.
        Critical callers may borrow against the bucket (go negative by 1)
        so operator-triggered actions never stall behind background jobs.
        """
        wait = 0.0
        while True:
            now = time.time()
            self._refill(now)
            if self.tokens >= 1.0 or (critical and self.tokens > -1.0):
                self.tokens -= 1.0
                return wait
            need = 1.0 - self.tokens
            sleep = need * self.refill_period
            wait += sleep
            time.sleep(min(sleep, 5.0))

    def _refill(self, now: float) -> None:
        elapsed = now - self.last_refill
        if elapsed <= 0:
            return
        gained = elapsed / self.refill_period
        self.tokens = min(float(self.capacity), self.tokens + gained)
        self.last_refill = now


@dataclass
class _Breaker:
    fails: deque = field(default_factory=lambda: deque(maxlen=CIRCUIT_FAIL_THRESHOLD))
    open_until: float = 0.0
    state: str = "closed"   # closed | open | half_open
    total_ok: int = 0
    total_err: int = 0

    def record_ok(self) -> None:
        self.fails.clear()
        self.state = "closed"
        self.open_until = 0.0
        self.total_ok += 1

    def record_err(self) -> None:
        now = time.time()
        self.fails.append(now)
        self.total_err += 1
        recent = [t for t in self.fails if (now - t) <= CIRCUIT_WINDOW_SEC]
        if len(recent) >= CIRCUIT_FAIL_THRESHOLD:
            self.open_until = now + CIRCUIT_COOLDOWN_SEC
            self.state = "open"

    def is_open(self) -> bool:
        if self.state != "open":
            return False
        if time.time() >= self.open_until:
            self.state = "half_open"
            return False
        return True


_LOCK = threading.RLock()
_BUCKETS: dict[str, _Bucket] = {}
_BREAKERS: dict[str, _Breaker] = {}


def _host_of(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower() or "*"
    except Exception:
        return "*"


def _bucket_for(host: str) -> _Bucket:
    with _LOCK:
        b = _BUCKETS.get(host)
        if b is None:
            cap, refill = RATE_LIMITS.get(host) or RATE_LIMITS["*"]
            b = _Bucket(capacity=cap, refill_period=refill, tokens=float(cap))
            _BUCKETS[host] = b
        return b


def _breaker_for(host: str) -> _Breaker:
    with _LOCK:
        br = _BREAKERS.get(host)
        if br is None:
            br = _Breaker()
            _BREAKERS[host] = br
        return br


# ── public API ─────────────────────────────────────────────────────────────
def request(url: str, *, method: str = "GET",
            headers: dict | None = None, body: bytes | None = None,
            timeout: float = DEFAULT_TIMEOUT,
            retries: int = DEFAULT_RETRIES,
            priority: str = "background") -> tuple[int, bytes, dict] | None:
    """
    Perform an HTTP request with shared retry/backoff/rate-limit/breaker.

    Returns ``(status, body_bytes, response_headers)`` on success.
    Returns ``None`` if the host's circuit breaker is open or every retry
    failed.  Never raises — callers handle the ``None`` sentinel.

    ``priority="critical"`` lets the request borrow against the rate-limit
    bucket so operator-driven actions don't stall behind background sync.
    """
    host = _host_of(url)
    breaker = _breaker_for(host)
    if breaker.is_open():
        log.info("[http_client] circuit open for %s; failing fast", host)
        return None

    bucket = _bucket_for(host)
    bucket.take(critical=(priority == "critical"))

    hdrs = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    if headers:
        hdrs.update(headers)

    last_err: Any = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, method=method, headers=hdrs, data=body)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                breaker.record_ok()
                return resp.status, data, dict(resp.headers)
        except urllib.error.HTTPError as exc:
            last_err = exc
            # Honour Retry-After when the server gives one.
            ra = exc.headers.get("Retry-After") if exc.headers else None
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                delay = _backoff(attempt, ra)
                log.info("[http_client] %s %s → HTTP %s, sleeping %.1fs (attempt %d)",
                         method, url, exc.code, delay, attempt + 1)
                time.sleep(delay)
                continue
            breaker.record_err()
            log.info("[http_client] %s %s gave HTTP %s — giving up", method, url, exc.code)
            return None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_err = exc
            if attempt < retries:
                delay = _backoff(attempt, None)
                log.info("[http_client] %s %s connection error %s, sleeping %.1fs",
                         method, url, exc, delay)
                time.sleep(delay)
                continue
            breaker.record_err()
            log.info("[http_client] %s %s exhausted retries: %s", method, url, exc)
            return None
        except Exception as exc:
            last_err = exc
            breaker.record_err()
            log.info("[http_client] %s %s unexpected: %s", method, url, exc)
            return None

    breaker.record_err()
    log.info("[http_client] %s %s gave up after %d retries; last err=%s",
             method, url, retries, last_err)
    return None


def _backoff(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass
    base = RETRY_BASE_DELAY * (3 ** attempt)
    return base * (1.0 + random.uniform(-RETRY_JITTER, RETRY_JITTER))


def health_snapshot() -> dict:
    """Admin-page friendly snapshot of every host we've talked to."""
    with _LOCK:
        out = {}
        for host in sorted(set(list(_BUCKETS.keys()) + list(_BREAKERS.keys()))):
            b = _BUCKETS.get(host)
            br = _BREAKERS.get(host)
            out[host] = {
                "tokens":          round(b.tokens, 2) if b else None,
                "capacity":        b.capacity if b else None,
                "refill_seconds":  b.refill_period if b else None,
                "circuit_state":   br.state if br else "closed",
                "circuit_opens_in": (
                    max(0, int(br.open_until - time.time()))
                    if br and br.state == "open" else 0
                ),
                "total_ok":   br.total_ok if br else 0,
                "total_err":  br.total_err if br else 0,
            }
        return out


def reset_breaker(host: str) -> bool:
    """Force a breaker closed (admin override)."""
    with _LOCK:
        br = _BREAKERS.get(host.lower())
        if not br:
            return False
        br.state = "closed"
        br.fails.clear()
        br.open_until = 0.0
        return True
