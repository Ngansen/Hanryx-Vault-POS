"""
workers/price_refresh.py — bulk price refresher for offline POS.

Wraps `price_aggregator.get_quote()` in the Worker framework so the
inventory's prices stay fresh enough that the POS can still quote
sane numbers when the booth WiFi drops mid-show. The worker does
NO pricing math itself — every aggregation, weighting, and
condition multiplier already lives in price_aggregator + the
per-source clients (ebay_sold, tcgplayer_proxy, tcg_price_lookup).
This file is pure orchestration.

Why a separate background worker rather than a cron / on-demand?
----------------------------------------------------------------
* Three-tier priority. Cards we actually have on the shelf get
  refreshed weekly; cards a customer recently scanned get
  refreshed bi-weekly; the long tail of the catalogue gets a
  quarterly refresh. A flat cron can't express that without a
  whole second layer of scheduling — bg_task_queue.priority
  already gives us this for free (lower = sooner).
* Rate-limit aware. eBay's free Finding API quota is 5000/day
  and a naive "walk every card" job would burn it in hours
  (which is exactly why the legacy DISABLE_BG_PRICING_PREWARM=1
  env switch exists in docker-compose.yml). BATCH_SIZE=5 with
  a 5-minute idle pause keeps the daily call count well under
  budget while still keeping the inventory tier fresh.
* Per-card retry tracking. A transient eBay 503 doesn't poison
  the cache — the task gets re-claimed on the next pass. Failures
  surface in bg_worker_run logs so the operator can see "37
  catalogue cards failed today, all FETCH_ERROR" at a glance
  via the data_analyst report.
* Test-friendly. price_aggregator.get_quote is injected via
  quote_fn so unit tests don't need real API keys or network.

Why no new schema?
------------------
price_aggregator already bootstraps the `price_quotes` table
(cache_key, condition, median_usd, sources_used, fetched_at, …)
and the seed checks fetched_at to know what's stale. Adding a
parallel "card_price" table would just duplicate state and
require a second source of truth.

Lang multipliers (JP=0.55, KR=0.40, …) live in server.py's
_LANGUAGE_PRICE_RULES and are applied at display time — the
worker stores raw USD/Near-Mint baselines so the aggregator
cache is language-agnostic and the operator can tune the
multipliers without invalidating any cached prices.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable

from .base import Worker, WorkerError

log = logging.getLogger("workers.price_refresh")


class PriceRefreshWorker(Worker):
    TASK_TYPE = "price_refresh"

    # Each get_quote() fans out to up to 3 upstream APIs (eBay sold,
    # TCGplayer via pokemontcg.io, tcgpricelookup.com). On a Pi 5
    # over a typical booth uplink this is 1-3s per card. Smaller
    # batches keep commits frequent so a power blip doesn't lose
    # an hour of work, and lets the reaper unstick CLAIMED tasks
    # without too much wasted re-work.
    BATCH_SIZE = 5

    # 5-minute idle pause keeps the eBay quota math sane:
    #   5 cards/batch × ~12 batches/hour × 24 hours = ~1440 calls/day
    # well under the 5000/day cap, and per-card it's a refresh every
    # ~7 days for a 10k-card inventory tier (matches the inventory
    # recheck cadence below).
    IDLE_SLEEP_S = 300.0

    # 10 min — eBay's Finding API can stall under load; we don't
    # want the reaper killing tasks that are just slow.
    CLAIM_TIMEOUT_S = 600

    # Tier recheck cadences. Operator can override via constructor
    # args or CLI flags. The values match a "weekly inventory check,
    # quarterly long-tail" rhythm that's worked well at past shows.
    DEFAULT_INVENTORY_RECHECK_S = 7 * 86400        # weekly
    DEFAULT_SCANNED_RECHECK_S = 14 * 86400         # bi-weekly
    DEFAULT_CATALOGUE_RECHECK_S = 90 * 86400       # quarterly

    # Only scan_log entries from the last 30 days count for the
    # "recently scanned" tier — a card someone scanned 6 months ago
    # isn't actively interesting and should drop back to the
    # catalogue-cadence refresh.
    SCAN_LOOKBACK_DAYS = 30

    # bg_task_queue.priority — lower = sooner. The default for new
    # rows is 100 (catalogue tier), so inventory and scanned tiers
    # naturally jump the queue.
    PRIORITY_INVENTORY = 1
    PRIORITY_SCANNED = 10
    PRIORITY_CATALOGUE = 100

    DEFAULT_CONDITION = "NM"

    def __init__(self, conn, *,
                 inventory_recheck_s: int | None = None,
                 scanned_recheck_s: int | None = None,
                 catalogue_recheck_s: int | None = None,
                 source: str | None = None,
                 condition: str | None = None,
                 quote_fn: Callable[..., dict] | None = None,
                 **kw):
        super().__init__(conn, **kw)

        self.inventory_recheck_s = (inventory_recheck_s
                                    if inventory_recheck_s is not None
                                    else self.DEFAULT_INVENTORY_RECHECK_S)
        self.scanned_recheck_s = (scanned_recheck_s
                                  if scanned_recheck_s is not None
                                  else self.DEFAULT_SCANNED_RECHECK_S)
        self.catalogue_recheck_s = (catalogue_recheck_s
                                    if catalogue_recheck_s is not None
                                    else self.DEFAULT_CATALOGUE_RECHECK_S)

        # `source` pins all fan-out to a single upstream client (e.g.
        # 'ebay_sold' to do a pure eBay sweep, useful for back-fill
        # passes). None = let price_aggregator hit every source.
        env_source = (os.environ.get("PRICE_REFRESH_SOURCE") or "").strip()
        self.source = (source.strip() if source else env_source) or None
        self.condition = (condition or
                          os.environ.get("PRICE_REFRESH_CONDITION") or
                          self.DEFAULT_CONDITION).upper()

        # Lazy-loaded reference to price_aggregator.get_quote.
        # Tests inject quote_fn directly to avoid the import.
        self._injected_quote_fn = quote_fn
        self._aggregator_loaded = False
        self._load_failure: str = ""

    # ── Lazy import of the aggregator engine ───────────────────────

    def _ensure_quote_fn(self) -> Callable[..., dict] | None:
        """Return the get_quote callable or None on lib failure.

        The aggregator imports the per-source HTTP clients which in
        turn import requests / lxml / etc — keep that off the import
        path until the first task actually runs, so a fresh Pi
        without the price stack still drains the queue cleanly.
        """
        if self._injected_quote_fn is not None:
            return self._injected_quote_fn
        if self._aggregator_loaded:
            return self._real_quote_fn  # type: ignore[attr-defined]
        if self._load_failure:
            return None
        try:
            from price_aggregator import get_quote  # type: ignore
        except ImportError as e:
            self._load_failure = "NO_LIB"
            log.warning("[price_refresh] price_aggregator not importable: "
                        "%s — install runtime deps or run inside the "
                        "pos/sync container", e)
            return None
        self._real_quote_fn = get_quote  # type: ignore[attr-defined]
        self._aggregator_loaded = True
        return get_quote

    # ── Query construction ────────────────────────────────────────

    @staticmethod
    def _build_query(name_kr: str, name_jp: str, name_chs: str,
                     name_en: str, set_id: str, card_number: str) -> str:
        """Compose the upstream search query.

        EN-first because every upstream API has the best coverage in
        English (eBay sold-listings work everywhere; pokemontcg.io
        is EN-only; tcgpricelookup is multilingual but EN gives the
        most hits). Localised names are the fallback when an English
        name is missing — common for Korean-only promos.

        set_id + card_number is always appended for disambiguation
        (Pikachu has 60+ printings; "Pikachu sv2/47" gets exactly the
        Surging Sparks Pikachu).
        """
        for name in (name_en, name_kr, name_jp, name_chs):
            n = (name or "").strip()
            if n:
                return f"{n} {set_id} {card_number}".strip()
        # Truly nameless cards (data-import bugs) still get a
        # set/number-based search — better than nothing.
        return f"{set_id} {card_number}".strip()

    # ── Worker contract ───────────────────────────────────────────

    def seed(self) -> int:
        """Three-tier seed: inventory > scanned > catalogue.

        Each tier is a single INSERT...SELECT against bg_task_queue
        with ON CONFLICT DO NOTHING — a card in BOTH inventory and
        recently scanned will land at the inventory priority (since
        that tier seeds first and the conflict skips the second
        insert). Per-tier `priority` is set explicitly so the
        existing idx_bg_task_pending(task_type, priority, created_at)
        index orders the drain correctly.

        Cards whose price_quotes row is fresher than the tier's
        recheck cutoff are skipped — that's how we throttle the
        upstream API budget without a separate scheduler.
        """
        now_s = int(time.time())
        inventory_cutoff_ms = (now_s - self.inventory_recheck_s) * 1000
        scanned_cutoff_ms = (now_s - self.scanned_recheck_s) * 1000
        catalogue_cutoff_ms = (now_s - self.catalogue_recheck_s) * 1000
        scan_lookback_ms = (now_s - self.SCAN_LOOKBACK_DAYS * 86400) * 1000

        cur = self.conn.cursor()

        # Tier 1: inventory cards (priority 1, weekly recheck).
        # inventory.tcg_id is "<set_id>/<card_number>" by convention.
        cur.execute("""
            INSERT INTO bg_task_queue
                (task_type, task_key, payload, priority, status, created_at)
            SELECT 'price_refresh',
                   'price_refresh:' || c.set_id || '/' || c.card_number,
                   jsonb_build_object('set_id',      c.set_id,
                                      'card_number', c.card_number,
                                      'tier',        'inventory'),
                   %s, 'PENDING', %s
              FROM cards_master c
              JOIN inventory i
                ON i.tcg_id = (c.set_id || '/' || c.card_number)
             WHERE NOT EXISTS (
                 SELECT 1 FROM price_quotes q
                  WHERE q.card_id = (c.set_id || ':' || c.card_number)
                    AND q.fetched_at > %s
             )
            ON CONFLICT (task_type, task_key) DO NOTHING
        """, (self.PRIORITY_INVENTORY, now_s, inventory_cutoff_ms))
        n_inv = cur.rowcount or 0

        # Tier 2: recently scanned cards (priority 10, bi-weekly).
        # scan_log.qr_code is the same "<set_id>/<card_number>" key.
        cur.execute("""
            INSERT INTO bg_task_queue
                (task_type, task_key, payload, priority, status, created_at)
            SELECT 'price_refresh',
                   'price_refresh:' || c.set_id || '/' || c.card_number,
                   jsonb_build_object('set_id',      c.set_id,
                                      'card_number', c.card_number,
                                      'tier',        'scanned'),
                   %s, 'PENDING', %s
              FROM cards_master c
             WHERE EXISTS (
                 SELECT 1 FROM scan_log s
                  WHERE s.qr_code = (c.set_id || '/' || c.card_number)
                    AND s.scanned_at > %s
             )
               AND NOT EXISTS (
                 SELECT 1 FROM price_quotes q
                  WHERE q.card_id = (c.set_id || ':' || c.card_number)
                    AND q.fetched_at > %s
             )
            ON CONFLICT (task_type, task_key) DO NOTHING
        """, (self.PRIORITY_SCANNED, now_s, scan_lookback_ms,
              scanned_cutoff_ms))
        n_scan = cur.rowcount or 0

        # Tier 3: long-tail catalogue (priority 100, quarterly).
        cur.execute("""
            INSERT INTO bg_task_queue
                (task_type, task_key, payload, priority, status, created_at)
            SELECT 'price_refresh',
                   'price_refresh:' || c.set_id || '/' || c.card_number,
                   jsonb_build_object('set_id',      c.set_id,
                                      'card_number', c.card_number,
                                      'tier',        'catalogue'),
                   %s, 'PENDING', %s
              FROM cards_master c
             WHERE NOT EXISTS (
                 SELECT 1 FROM price_quotes q
                  WHERE q.card_id = (c.set_id || ':' || c.card_number)
                    AND q.fetched_at > %s
             )
            ON CONFLICT (task_type, task_key) DO NOTHING
        """, (self.PRIORITY_CATALOGUE, now_s, catalogue_cutoff_ms))
        n_cat = cur.rowcount or 0

        total = n_inv + n_scan + n_cat
        self.conn.commit()
        log.info("[price_refresh] seed enqueued %d task(s): "
                 "inventory=%d scanned=%d catalogue=%d",
                 total, n_inv, n_scan, n_cat)
        return total

    def process(self, task: dict) -> dict:
        payload = task.get("payload") or {}
        sid = (payload.get("set_id") or "").strip()
        num = (payload.get("card_number") or "").strip()
        tier = (payload.get("tier") or "catalogue").strip()
        if not sid or not num:
            raise WorkerError(
                f"price_refresh task {task.get('task_id')} missing "
                f"set_id/card_number: {payload!r}"
            )

        cur = self.conn.cursor()
        cur.execute("""
            SELECT name_kr, name_jp, name_chs, name_en
              FROM cards_master
             WHERE set_id = %s AND card_number = %s
        """, (sid, num))
        row = cur.fetchone()
        if row is None:
            log.warning("[price_refresh] no cards_master row for %s/%s "
                        "— dropping task", sid, num)
            return {"status": "MISSING_CARD", "tier": tier}

        name_kr, name_jp, name_chs, name_en = row[0], row[1], row[2], row[3]
        query = self._build_query(name_kr or "", name_jp or "",
                                  name_chs or "", name_en or "",
                                  sid, num)
        # card_id chosen to match price_aggregator's _cache_key:
        # `card:any:<set_id>:<card_number>` — a stable identity that
        # survives across condition-tier refreshes.
        card_id = f"{sid}:{num}"

        quote_fn = self._ensure_quote_fn()
        if quote_fn is None:
            return {"status": self._load_failure or "NO_LIB",
                    "tier": tier}

        # force_refresh=True because we're explicitly here to update —
        # the aggregator's TTL-based cache hit would skip the upstream
        # call and defeat the whole point of the worker.
        try:
            quote = quote_fn(self.conn,
                             query=query,
                             card_id=card_id,
                             condition=self.condition,
                             source=self.source,
                             force_refresh=True)
        except Exception as e:  # noqa: BLE001 — upstream raises many things
            log.error("[price_refresh] get_quote failed for %s/%s: %s",
                      sid, num, e)
            return {"status": "FETCH_ERROR", "tier": tier,
                    "error": f"{type(e).__name__}:{e}"}

        sample_count = int(quote.get("sample_count") or 0)
        sources_used = list(quote.get("sources_used") or [])
        median_usd = quote.get("median_usd")

        # No listings at all = "we tried, every source returned 0
        # results". Not a failure (the upstream calls succeeded),
        # just a card we have no market data for. Surfaced in the
        # worker_run log so the operator can see which cards lack
        # coverage and consider adding sources.
        if sample_count <= 0:
            log.info("[price_refresh] no listings for %s/%s "
                     "(query=%r) — sources tried: %s",
                     sid, num, query, sources_used)
            return {"status": "NO_DATA", "tier": tier,
                    "sources_tried": sources_used}

        return {"status": "OK", "tier": tier,
                "median_usd": median_usd,
                "sample_count": sample_count,
                "source_count": int(quote.get("source_count") or 0),
                "sources_used": sources_used}
