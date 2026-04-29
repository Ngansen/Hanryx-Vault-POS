"""
Tests for workers/price_refresh.py.

Strategy: inject a fake `quote_fn(conn, **kw) -> dict` so no real
price_aggregator (and therefore no real eBay / pokemontcg.io /
tcgpricelookup.com network) is needed. DB layer = same FakeConn /
FakeCursor pattern as the other worker tests.

The worker is pure orchestration — it doesn't compute prices itself,
so the tests focus on:

  * tier seeding (right SQL keywords, right priorities, right cutoffs)
  * query construction (EN-first, localised fallbacks, set+num appended)
  * process() outcomes (OK, NO_DATA, FETCH_ERROR, NO_LIB, MISSING_CARD)
  * lazy-import behaviour for price_aggregator
  * force_refresh=True is always passed (the whole point of the worker)
"""
from __future__ import annotations

import os
import sys
import unittest
from collections import deque
from pathlib import Path
from typing import Any
from unittest import mock

PI_SETUP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PI_SETUP))

from workers.price_refresh import PriceRefreshWorker  # noqa: E402
from workers.base import WorkerError  # noqa: E402


# ── Fake DB ─────────────────────────────────────────────────────


class FakeCursor:
    def __init__(self, parent: "FakeConn") -> None:
        self.parent = parent
        # Worker uses cur.rowcount after each INSERT...SELECT — the
        # FakeConn lets the test pre-script per-execute rowcounts.
        self.rowcount = 0

    def execute(self, sql: str, params: Any = None) -> None:
        self.parent.executes.append((sql, params))
        # Apply the next scripted rowcount, if any. Defaults to 0.
        if self.parent._rowcount_q:
            self.rowcount = self.parent._rowcount_q.popleft()
        else:
            self.rowcount = 0

    def fetchone(self):
        if not self.parent._fetch_one_q:
            return None
        return self.parent._fetch_one_q.popleft()


class FakeConn:
    def __init__(self) -> None:
        self.executes: list[tuple[str, Any]] = []
        self.commits = 0
        self._fetch_one_q: deque = deque()
        self._rowcount_q: deque = deque()

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def queue_one(self, row):
        self._fetch_one_q.append(row)

    def queue_rowcount(self, n: int):
        self._rowcount_q.append(n)


# ── Fake quote_fn ───────────────────────────────────────────────


class FakeQuoteFn:
    """price_aggregator.get_quote() stand-in.

    Records every call and returns a scripted result. Set
    `raise_with` to make the next call raise instead of returning.
    """

    def __init__(self, result: dict | None = None,
                 raise_with: Exception | None = None):
        self.calls: list[dict] = []
        self.result = result if result is not None else {
            "median_usd": 12.34,
            "sample_count": 7,
            "source_count": 2,
            "sources_used": ["ebay_sold", "tcgplayer"],
        }
        self.raise_with = raise_with

    def __call__(self, conn, **kw):
        self.calls.append({"conn": conn, **kw})
        if self.raise_with is not None:
            raise self.raise_with
        return self.result


# ── Constructor / config ───────────────────────────────────────


class ConstructorTest(unittest.TestCase):
    def test_defaults(self):
        w = PriceRefreshWorker(FakeConn())
        self.assertEqual(w.TASK_TYPE, "price_refresh")
        self.assertEqual(w.BATCH_SIZE, 5)
        self.assertEqual(w.IDLE_SLEEP_S, 300.0)
        self.assertEqual(w.CLAIM_TIMEOUT_S, 600)
        self.assertEqual(w.inventory_recheck_s, 7 * 86400)
        self.assertEqual(w.scanned_recheck_s, 14 * 86400)
        self.assertEqual(w.catalogue_recheck_s, 90 * 86400)
        self.assertIsNone(w.source)
        self.assertEqual(w.condition, "NM")

    def test_priority_constants(self):
        # Lower = sooner, so inventory must be the smallest number.
        self.assertLess(PriceRefreshWorker.PRIORITY_INVENTORY,
                        PriceRefreshWorker.PRIORITY_SCANNED)
        self.assertLess(PriceRefreshWorker.PRIORITY_SCANNED,
                        PriceRefreshWorker.PRIORITY_CATALOGUE)
        # And catalogue must match bg_task_queue's default of 100, so
        # ad-hoc enqueues from elsewhere don't accidentally jump the
        # background tier.
        self.assertEqual(PriceRefreshWorker.PRIORITY_CATALOGUE, 100)

    def test_explicit_recheck_overrides(self):
        w = PriceRefreshWorker(FakeConn(),
                               inventory_recheck_s=3600,
                               scanned_recheck_s=7200,
                               catalogue_recheck_s=86400)
        self.assertEqual(w.inventory_recheck_s, 3600)
        self.assertEqual(w.scanned_recheck_s, 7200)
        self.assertEqual(w.catalogue_recheck_s, 86400)

    def test_zero_recheck_explicitly_allowed(self):
        # 0 means "always refresh" — must not silently fall back to
        # the default. Operators use this for a force-refresh pass.
        w = PriceRefreshWorker(FakeConn(),
                               inventory_recheck_s=0,
                               scanned_recheck_s=0,
                               catalogue_recheck_s=0)
        self.assertEqual(w.inventory_recheck_s, 0)
        self.assertEqual(w.scanned_recheck_s, 0)
        self.assertEqual(w.catalogue_recheck_s, 0)

    def test_explicit_source_and_condition(self):
        w = PriceRefreshWorker(FakeConn(), source="ebay_sold",
                               condition="lp")
        self.assertEqual(w.source, "ebay_sold")
        self.assertEqual(w.condition, "LP")  # always upper-cased

    def test_source_strip_and_blank_to_none(self):
        # Trailing whitespace from a CLI flag would break upstream
        # source matching.
        w = PriceRefreshWorker(FakeConn(), source="  ebay_sold  ")
        self.assertEqual(w.source, "ebay_sold")
        # Empty string == None (no pinning)
        w2 = PriceRefreshWorker(FakeConn(), source="")
        self.assertIsNone(w2.source)
        w3 = PriceRefreshWorker(FakeConn(), source="   ")
        self.assertIsNone(w3.source)

    def test_env_overrides_when_no_explicit(self):
        with mock.patch.dict(os.environ,
                             {"PRICE_REFRESH_SOURCE": "tcgplayer",
                              "PRICE_REFRESH_CONDITION": "mp"}):
            w = PriceRefreshWorker(FakeConn())
        self.assertEqual(w.source, "tcgplayer")
        self.assertEqual(w.condition, "MP")

    def test_explicit_beats_env(self):
        with mock.patch.dict(os.environ,
                             {"PRICE_REFRESH_SOURCE": "tcgplayer",
                              "PRICE_REFRESH_CONDITION": "mp"}):
            w = PriceRefreshWorker(FakeConn(), source="ebay_sold",
                                   condition="nm")
        self.assertEqual(w.source, "ebay_sold")
        self.assertEqual(w.condition, "NM")


# ── Query construction ────────────────────────────────────────


class BuildQueryTest(unittest.TestCase):
    def test_en_first_when_present(self):
        q = PriceRefreshWorker._build_query(
            "피카츄 V", "ピカチュウV", "皮卡丘V", "Pikachu V",
            "sv2", "47")
        # EN should win — most upstream APIs are EN-first.
        self.assertEqual(q, "Pikachu V sv2 47")

    def test_kr_fallback_when_no_en(self):
        q = PriceRefreshWorker._build_query(
            "피카츄 V", "ピカチュウV", "皮卡丘V", "",
            "sv2", "47")
        self.assertEqual(q, "피카츄 V sv2 47")

    def test_jp_fallback_when_no_en_or_kr(self):
        q = PriceRefreshWorker._build_query(
            "", "ピカチュウV", "皮卡丘V", "", "sv2", "47")
        self.assertEqual(q, "ピカチュウV sv2 47")

    def test_chs_fallback_when_only_chs(self):
        q = PriceRefreshWorker._build_query(
            "", "", "皮卡丘V", "", "sv2", "47")
        self.assertEqual(q, "皮卡丘V sv2 47")

    def test_all_empty_falls_back_to_set_num(self):
        # Truly nameless cards (data-import bugs) shouldn't crash
        # the worker — degrade gracefully to set+num search.
        q = PriceRefreshWorker._build_query("", "", "", "", "sv2", "47")
        self.assertEqual(q, "sv2 47")

    def test_whitespace_only_names_treated_as_empty(self):
        q = PriceRefreshWorker._build_query(
            "   ", "  \t ", "", "Pikachu V", "sv2", "47")
        self.assertEqual(q, "Pikachu V sv2 47")

    def test_name_whitespace_stripped(self):
        q = PriceRefreshWorker._build_query(
            "", "", "", "  Pikachu V  ", "sv2", "47")
        self.assertEqual(q, "Pikachu V sv2 47")


# ── seed() ────────────────────────────────────────────────────


class SeedTest(unittest.TestCase):
    def _seed_with_rowcounts(self, n_inv: int, n_scan: int, n_cat: int):
        conn = FakeConn()
        # Three INSERT statements run in order: inventory, scanned, catalogue.
        conn.queue_rowcount(n_inv)
        conn.queue_rowcount(n_scan)
        conn.queue_rowcount(n_cat)
        w = PriceRefreshWorker(conn)
        total = w.seed()
        return total, conn

    def test_returns_total_of_three_tiers(self):
        total, conn = self._seed_with_rowcounts(3, 5, 100)
        self.assertEqual(total, 108)
        self.assertEqual(conn.commits, 1)

    def test_runs_three_insert_statements(self):
        _, conn = self._seed_with_rowcounts(0, 0, 0)
        inserts = [sql for sql, _ in conn.executes
                   if "INSERT INTO bg_task_queue" in sql]
        self.assertEqual(len(inserts), 3)

    def test_all_inserts_target_price_refresh_task_type(self):
        _, conn = self._seed_with_rowcounts(0, 0, 0)
        for sql, _ in conn.executes:
            self.assertIn("'price_refresh'", sql)

    def test_inventory_tier_joins_inventory_and_uses_priority_1(self):
        _, conn = self._seed_with_rowcounts(0, 0, 0)
        sql, params = conn.executes[0]
        self.assertIn("JOIN inventory", sql)
        self.assertIn("'inventory'", sql)
        # First param to the inventory INSERT is the priority constant.
        self.assertEqual(params[0], PriceRefreshWorker.PRIORITY_INVENTORY)

    def test_scanned_tier_uses_scan_log_and_priority_10(self):
        _, conn = self._seed_with_rowcounts(0, 0, 0)
        sql, params = conn.executes[1]
        self.assertIn("scan_log", sql)
        self.assertIn("'scanned'", sql)
        self.assertEqual(params[0], PriceRefreshWorker.PRIORITY_SCANNED)

    def test_catalogue_tier_no_inventory_join_and_priority_100(self):
        _, conn = self._seed_with_rowcounts(0, 0, 0)
        sql, params = conn.executes[2]
        self.assertNotIn("JOIN inventory", sql)
        self.assertNotIn("scan_log", sql)
        self.assertIn("'catalogue'", sql)
        self.assertEqual(params[0], PriceRefreshWorker.PRIORITY_CATALOGUE)

    def test_all_tiers_filter_on_price_quotes_freshness(self):
        # Each tier must skip cards whose price_quotes row is fresher
        # than the tier cutoff — that's the whole rate-limit story.
        _, conn = self._seed_with_rowcounts(0, 0, 0)
        for sql, _ in conn.executes:
            self.assertIn("price_quotes", sql)
            self.assertIn("fetched_at", sql)

    def test_all_tiers_use_on_conflict_do_nothing(self):
        # A card in inventory AND recently scanned should land at the
        # inventory priority (first insert), and the second insert
        # should silently skip it.
        _, conn = self._seed_with_rowcounts(0, 0, 0)
        for sql, _ in conn.executes:
            self.assertIn("ON CONFLICT", sql)
            self.assertIn("DO NOTHING", sql)

    def test_recheck_seconds_become_milliseconds_in_params(self):
        # Worker stores recheck_s in seconds but bg_task / price_quotes
        # use millisecond timestamps — make sure the conversion happens.
        conn = FakeConn()
        for _ in range(3):
            conn.queue_rowcount(0)
        w = PriceRefreshWorker(conn,
                               inventory_recheck_s=1,
                               scanned_recheck_s=2,
                               catalogue_recheck_s=3)
        w.seed()
        # cutoff_ms = (now_s - recheck_s) * 1000  ⇒ very close to now*1000.
        # Check the cutoffs are within a sane window of "now".
        import time
        now_ms = int(time.time()) * 1000
        for i, expected_recheck_s in enumerate([1, 2, 3]):
            params = conn.executes[i][1]
            cutoff_ms = params[-1]  # last param of every INSERT
            self.assertLess(abs(cutoff_ms - (now_ms - expected_recheck_s * 1000)),
                            5000,  # 5s tolerance
                            f"tier {i} cutoff_ms drift too large")


# ── _ensure_quote_fn lazy import ───────────────────────────────


class EnsureQuoteFnTest(unittest.TestCase):
    def test_injected_quote_fn_wins(self):
        fake = FakeQuoteFn()
        w = PriceRefreshWorker(FakeConn(), quote_fn=fake)
        self.assertIs(w._ensure_quote_fn(), fake)

    def test_lazy_import_failure_cached_as_no_lib(self):
        w = PriceRefreshWorker(FakeConn())
        with mock.patch.dict(sys.modules,
                             {"price_aggregator": None}):
            # sys.modules[name] = None makes `import name` raise ImportError
            self.assertIsNone(w._ensure_quote_fn())
            self.assertEqual(w._load_failure, "NO_LIB")
            # Second call must reuse the cached failure, not retry.
            self.assertIsNone(w._ensure_quote_fn())

    def test_successful_lazy_import_is_cached(self):
        fake = FakeQuoteFn()
        fake_module = mock.MagicMock()
        fake_module.get_quote = fake
        w = PriceRefreshWorker(FakeConn())
        with mock.patch.dict(sys.modules,
                             {"price_aggregator": fake_module}):
            got = w._ensure_quote_fn()
        self.assertIs(got, fake)
        self.assertTrue(w._aggregator_loaded)
        # After the cache is populated, a subsequent call returns the
        # same object even without the patch.
        self.assertIs(w._ensure_quote_fn(), fake)


# ── process() ──────────────────────────────────────────────────


class ProcessTest(unittest.TestCase):
    def _task(self, sid="sv2", num="47", tier="inventory", task_id=42):
        return {"task_id": task_id,
                "payload": {"set_id": sid, "card_number": num,
                            "tier": tier}}

    def test_missing_set_id_raises(self):
        w = PriceRefreshWorker(FakeConn(), quote_fn=FakeQuoteFn())
        with self.assertRaises(WorkerError):
            w.process({"task_id": 1, "payload": {"card_number": "47"}})

    def test_missing_card_number_raises(self):
        w = PriceRefreshWorker(FakeConn(), quote_fn=FakeQuoteFn())
        with self.assertRaises(WorkerError):
            w.process({"task_id": 1, "payload": {"set_id": "sv2"}})

    def test_blank_set_id_raises(self):
        w = PriceRefreshWorker(FakeConn(), quote_fn=FakeQuoteFn())
        with self.assertRaises(WorkerError):
            w.process({"task_id": 1,
                       "payload": {"set_id": "  ", "card_number": "47"}})

    def test_missing_cards_master_row_returns_missing_card(self):
        conn = FakeConn()  # fetchone() returns None
        w = PriceRefreshWorker(conn, quote_fn=FakeQuoteFn())
        out = w.process(self._task())
        self.assertEqual(out["status"], "MISSING_CARD")
        self.assertEqual(out["tier"], "inventory")

    def test_no_lib_when_no_quote_fn(self):
        conn = FakeConn()
        conn.queue_one(("피카츄", "", "", "Pikachu"))
        w = PriceRefreshWorker(conn)  # no quote_fn injected
        with mock.patch.dict(sys.modules, {"price_aggregator": None}):
            out = w.process(self._task())
        self.assertEqual(out["status"], "NO_LIB")
        self.assertEqual(out["tier"], "inventory")

    def test_fetch_error_when_quote_fn_raises(self):
        conn = FakeConn()
        conn.queue_one(("피카츄", "", "", "Pikachu"))
        fake = FakeQuoteFn(raise_with=RuntimeError("eBay 503"))
        w = PriceRefreshWorker(conn, quote_fn=fake)
        out = w.process(self._task())
        self.assertEqual(out["status"], "FETCH_ERROR")
        self.assertIn("RuntimeError", out["error"])
        self.assertIn("eBay 503", out["error"])

    def test_no_data_when_sample_count_zero(self):
        conn = FakeConn()
        conn.queue_one(("피카츄", "", "", "Pikachu"))
        fake = FakeQuoteFn(result={"sample_count": 0,
                                   "sources_used": ["ebay_sold"]})
        w = PriceRefreshWorker(conn, quote_fn=fake)
        out = w.process(self._task())
        self.assertEqual(out["status"], "NO_DATA")
        self.assertEqual(out["sources_tried"], ["ebay_sold"])

    def test_ok_when_sample_count_positive(self):
        conn = FakeConn()
        conn.queue_one(("피카츄", "", "", "Pikachu"))
        fake = FakeQuoteFn(result={"sample_count": 12,
                                   "median_usd": 25.50,
                                   "source_count": 3,
                                   "sources_used":
                                       ["ebay_sold", "tcgplayer", "tcgpl"]})
        w = PriceRefreshWorker(conn, quote_fn=fake)
        out = w.process(self._task())
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["median_usd"], 25.50)
        self.assertEqual(out["sample_count"], 12)
        self.assertEqual(out["source_count"], 3)
        self.assertEqual(out["sources_used"],
                         ["ebay_sold", "tcgplayer", "tcgpl"])

    def test_quote_fn_called_with_force_refresh_true(self):
        # The whole point of the worker is to bypass the TTL cache,
        # so force_refresh=True is non-negotiable.
        conn = FakeConn()
        conn.queue_one(("", "", "", "Pikachu"))
        fake = FakeQuoteFn()
        w = PriceRefreshWorker(conn, quote_fn=fake)
        w.process(self._task())
        self.assertEqual(len(fake.calls), 1)
        self.assertTrue(fake.calls[0]["force_refresh"])

    def test_quote_fn_called_with_correct_query(self):
        conn = FakeConn()
        conn.queue_one(("피카츄", "ピカチュウ", "皮卡丘", "Pikachu V"))
        fake = FakeQuoteFn()
        w = PriceRefreshWorker(conn, quote_fn=fake)
        w.process(self._task(sid="sv2", num="47"))
        self.assertEqual(fake.calls[0]["query"], "Pikachu V sv2 47")

    def test_quote_fn_called_with_card_id_set_colon_num(self):
        # card_id format must match price_aggregator's _cache_key
        # convention so subsequent reads find this exact row.
        conn = FakeConn()
        conn.queue_one(("", "", "", "Pikachu"))
        fake = FakeQuoteFn()
        w = PriceRefreshWorker(conn, quote_fn=fake)
        w.process(self._task(sid="sv2", num="47"))
        self.assertEqual(fake.calls[0]["card_id"], "sv2:47")

    def test_quote_fn_called_with_configured_condition_and_source(self):
        conn = FakeConn()
        conn.queue_one(("", "", "", "Pikachu"))
        fake = FakeQuoteFn()
        w = PriceRefreshWorker(conn, quote_fn=fake,
                               source="ebay_sold", condition="LP")
        w.process(self._task())
        self.assertEqual(fake.calls[0]["condition"], "LP")
        self.assertEqual(fake.calls[0]["source"], "ebay_sold")

    def test_localised_only_card_uses_localised_query(self):
        # Korean-only promo with no English name — fall back to KR
        # rather than a useless "sv2 47" search.
        conn = FakeConn()
        conn.queue_one(("피카츄 프로모", "", "", ""))
        fake = FakeQuoteFn()
        w = PriceRefreshWorker(conn, quote_fn=fake)
        w.process(self._task(sid="kor-promo", num="001"))
        self.assertEqual(fake.calls[0]["query"],
                         "피카츄 프로모 kor-promo 001")

    def test_default_tier_when_payload_omits_it(self):
        conn = FakeConn()
        conn.queue_one(("", "", "", "Pikachu"))
        fake = FakeQuoteFn()
        w = PriceRefreshWorker(conn, quote_fn=fake)
        out = w.process({"task_id": 1,
                         "payload": {"set_id": "sv2", "card_number": "47"}})
        # Defaults to 'catalogue' so unscoped tasks don't pretend to
        # be inventory-tier.
        self.assertEqual(out["tier"], "catalogue")


# ─── T021: per-source breakdown helpers ────────────────────────────────


from workers.price_refresh import (  # noqa: E402
    _per_source_breakdown,
    _detect_disagreement,
    _record_breakdown,
    _record_disagreement,
    DEFAULT_DISAGREEMENT_THRESHOLD,
)


class PerSourceBreakdownTests(unittest.TestCase):

    def test_empty_returns_empty(self):
        self.assertEqual(_per_source_breakdown([]), [])
        self.assertEqual(_per_source_breakdown(None), [])  # type: ignore

    def test_single_source_yields_one_row(self):
        sample = [
            {"source": "ebay_sold", "price_usd": 10.0, "currency": "USD"},
            {"source": "ebay_sold", "price_usd": 12.0, "currency": "USD"},
            {"source": "ebay_sold", "price_usd": 14.0, "currency": "USD"},
        ]
        out = _per_source_breakdown(sample)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["source"], "ebay_sold")
        self.assertEqual(out[0]["price_usd"], 12.0)   # median of [10,12,14]
        self.assertEqual(out[0]["sample_count"], 3)
        self.assertEqual(out[0]["currency"], "USD")

    def test_even_count_uses_mean_of_two_middles(self):
        # Median of [10, 12, 14, 16] = (12+14)/2 = 13
        sample = [
            {"source": "ebay_sold", "price_usd": p, "currency": "USD"}
            for p in (10, 12, 14, 16)
        ]
        out = _per_source_breakdown(sample)
        self.assertEqual(out[0]["price_usd"], 13.0)

    def test_multiple_sources_each_get_own_median(self):
        sample = [
            {"source": "ebay_sold", "price_usd": 10.0, "currency": "USD"},
            {"source": "ebay_sold", "price_usd": 14.0, "currency": "USD"},
            {"source": "tcgplayer", "price_usd": 100.0, "currency": "USD"},
            {"source": "tcgplayer", "price_usd": 102.0, "currency": "USD"},
            {"source": "tcgplayer", "price_usd": 104.0, "currency": "USD"},
        ]
        out = _per_source_breakdown(sample)
        # Sorted alphabetically by source — deterministic ordering matters
        # for the operator's eye when scanning the dashboard.
        self.assertEqual([b["source"] for b in out],
                         ["ebay_sold", "tcgplayer"])
        d = {b["source"]: b for b in out}
        self.assertEqual(d["ebay_sold"]["price_usd"], 12.0)
        self.assertEqual(d["tcgplayer"]["price_usd"], 102.0)
        self.assertEqual(d["ebay_sold"]["sample_count"], 2)
        self.assertEqual(d["tcgplayer"]["sample_count"], 3)

    def test_skips_listings_without_source(self):
        sample = [
            {"price_usd": 10.0},                          # no source
            {"source": "", "price_usd": 11.0},            # empty source
            {"source": "ebay_sold", "price_usd": 12.0},
        ]
        out = _per_source_breakdown(sample)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["source"], "ebay_sold")

    def test_skips_zero_or_missing_price(self):
        sample = [
            {"source": "ebay_sold", "price_usd": 0},
            {"source": "ebay_sold", "price_usd": None},
            {"source": "ebay_sold", "price_usd": "not-a-number"},
            {"source": "ebay_sold", "price_usd": 12.0},
        ]
        out = _per_source_breakdown(sample)
        self.assertEqual(out[0]["sample_count"], 1)
        self.assertEqual(out[0]["price_usd"], 12.0)

    def test_modal_currency_used(self):
        # Three USD, one JPY — currency should be USD.
        sample = [
            {"source": "ebay_sold", "price_usd": 10.0, "currency": "USD"},
            {"source": "ebay_sold", "price_usd": 11.0, "currency": "USD"},
            {"source": "ebay_sold", "price_usd": 12.0, "currency": "USD"},
            {"source": "ebay_sold", "price_usd": 13.0, "currency": "JPY"},
        ]
        out = _per_source_breakdown(sample)
        self.assertEqual(out[0]["currency"], "USD")

    def test_missing_currency_defaults_to_usd(self):
        sample = [
            {"source": "ebay_sold", "price_usd": 10.0},  # no currency
        ]
        self.assertEqual(_per_source_breakdown(sample)[0]["currency"], "USD")


class DetectDisagreementTests(unittest.TestCase):

    def test_single_source_never_disagrees(self):
        bd = [{"source": "ebay_sold", "price_usd": 100.0,
               "currency": "USD", "sample_count": 5}]
        self.assertIsNone(_detect_disagreement(bd))

    def test_within_threshold_returns_none(self):
        # 100 vs 140 → ratio 1.4 → within 1.5 threshold
        bd = [
            {"source": "ebay_sold", "price_usd": 100.0,
             "currency": "USD", "sample_count": 5},
            {"source": "tcgplayer", "price_usd": 140.0,
             "currency": "USD", "sample_count": 5},
        ]
        self.assertIsNone(_detect_disagreement(bd))

    def test_exactly_at_threshold_is_not_disagreement(self):
        # ratio == 1.5 → strict greater-than means NOT a disagreement.
        bd = [
            {"source": "a", "price_usd": 100.0,
             "currency": "USD", "sample_count": 1},
            {"source": "b", "price_usd": 150.0,
             "currency": "USD", "sample_count": 1},
        ]
        self.assertIsNone(_detect_disagreement(bd))

    def test_above_threshold_returns_dict(self):
        bd = [
            {"source": "ebay_sold", "price_usd": 100.0,
             "currency": "USD", "sample_count": 5},
            {"source": "tcgkorea", "price_usd": 250.0,
             "currency": "USD", "sample_count": 2},
        ]
        d = _detect_disagreement(bd)
        self.assertIsNotNone(d)
        assert d is not None  # for type checker
        self.assertEqual(d["ratio"], 2.5)
        self.assertEqual(d["min_usd"], 100.0)
        self.assertEqual(d["max_usd"], 250.0)
        self.assertEqual(d["source_count"], 2)
        self.assertEqual({s["source"] for s in d["sources"]},
                         {"ebay_sold", "tcgkorea"})

    def test_zero_min_returns_none_no_div_by_zero(self):
        bd = [
            {"source": "a", "price_usd": 0.0,
             "currency": "USD", "sample_count": 1},
            {"source": "b", "price_usd": 100.0,
             "currency": "USD", "sample_count": 1},
        ]
        # The helper filters zero-priced rows out first → only one
        # valid source left → no disagreement (single source).
        self.assertIsNone(_detect_disagreement(bd))

    def test_custom_threshold(self):
        bd = [
            {"source": "a", "price_usd": 100.0,
             "currency": "USD", "sample_count": 1},
            {"source": "b", "price_usd": 110.0,
             "currency": "USD", "sample_count": 1},
        ]
        # 1.1× ratio → not flagged at default 1.5, but flagged at 1.05
        self.assertIsNone(_detect_disagreement(bd))
        d = _detect_disagreement(bd, threshold=1.05)
        self.assertIsNotNone(d)

    def test_default_threshold_constant_is_1_5(self):
        # Locking the empirical threshold so a future "tighten this"
        # change is a deliberate test update, not a silent shift.
        self.assertEqual(DEFAULT_DISAGREEMENT_THRESHOLD, 1.5)


class RecordHelperTests(unittest.TestCase):

    def test_record_breakdown_inserts_one_row_per_source(self):
        conn = FakeConn()
        bd = [
            {"source": "ebay_sold", "price_usd": 12.0,
             "currency": "USD", "sample_count": 3},
            {"source": "tcgplayer", "price_usd": 14.0,
             "currency": "USD", "sample_count": 5},
        ]
        n = _record_breakdown(conn, card_id="sv2:47",
                              breakdown=bd, fetched_at=1745000000)
        self.assertEqual(n, 2)
        inserts = [(s, p) for s, p in conn.executes
                   if "INSERT INTO price_quote_source" in s]
        self.assertEqual(len(inserts), 2)
        # ON CONFLICT clause must be present so a re-run is a no-op.
        for sql, _ in inserts:
            self.assertIn("ON CONFLICT (card_id, source, fetched_at) "
                          "DO NOTHING", sql)
        # First insert params: card_id, source, currency, price, count, ts
        _, params = inserts[0]
        self.assertEqual(params[0], "sv2:47")
        self.assertEqual(params[1], "ebay_sold")
        self.assertEqual(params[2], "USD")
        self.assertEqual(params[3], 12.0)
        self.assertEqual(params[4], 3)
        self.assertEqual(params[5], 1745000000)

    def test_record_breakdown_empty_is_noop(self):
        conn = FakeConn()
        n = _record_breakdown(conn, card_id="sv2:47",
                              breakdown=[], fetched_at=1)
        self.assertEqual(n, 0)
        self.assertEqual(conn.executes, [])

    def test_record_disagreement_inserts_bg_worker_run_marker(self):
        conn = FakeConn()
        d = {
            "ratio": 2.5, "min_usd": 100.0, "max_usd": 250.0,
            "source_count": 2,
            "sources": [
                {"source": "ebay_sold", "price_usd": 100.0},
                {"source": "tcgkorea", "price_usd": 250.0},
            ],
        }
        _record_disagreement(conn, card_id="sv2:47",
                             disagreement=d, fetched_at=1745000000)
        inserts = [(s, p) for s, p in conn.executes
                   if "INSERT INTO bg_worker_run" in s]
        self.assertEqual(len(inserts), 1)
        sql, params = inserts[0]
        self.assertIn("'price_disagreement'", sql)
        # worker_id holds the card_id so admin can grep by card.
        self.assertEqual(params[0], "sv2:47")
        # started_at == ended_at == fetched_at — these are point-in-
        # time markers, not duration runs.
        self.assertEqual(params[1], 1745000000)
        self.assertEqual(params[2], 1745000000)
        # items_claimed = source_count
        self.assertEqual(params[3], 2)
        # notes is JSON containing both source prices
        import json
        note = json.loads(params[6])
        self.assertEqual(note["card_id"], "sv2:47")
        self.assertEqual(note["ratio"], 2.5)
        self.assertEqual(len(note["sources"]), 2)


# ─── T021: process() integration ────────────────────────────────────────


class ProcessBreakdownIntegrationTests(unittest.TestCase):
    """Verify process() wires breakdown + disagreement detection
    into the DB without breaking any existing return contract."""

    def _task(self, sid="sv2", num="47"):
        return {"task_id": 1, "task_type": "price_refresh",
                "task_key": f"sv2/47:catalogue",
                "payload": {"set_id": sid, "card_number": num,
                            "tier": "catalogue"}, "attempts": 0}

    def _quote(self, *, sample, sample_count=None,
               fetched_at=1745000000_000):
        return {
            "median_usd": 12.0,
            "nm_median_usd": 12.0,
            "sample_count": sample_count if sample_count is not None
                            else len(sample),
            "source_count": len({s["source"] for s in sample
                                 if s.get("source")}),
            "sources_used": sorted({s["source"] for s in sample
                                    if s.get("source")}),
            "listings_sample": sample,
            "fetched_at": fetched_at,
        }

    def test_breakdown_inserted_when_sample_present(self):
        conn = FakeConn()
        # Stub the cards_master name lookup
        conn.queue_one(("Pikachu", "", "", ""))
        sample = [
            {"source": "ebay_sold", "price_usd": 10.0, "currency": "USD"},
            {"source": "ebay_sold", "price_usd": 12.0, "currency": "USD"},
            {"source": "tcgplayer", "price_usd": 14.0, "currency": "USD"},
            {"source": "tcgplayer", "price_usd": 16.0, "currency": "USD"},
        ]
        fake = FakeQuoteFn(result=self._quote(sample=sample))
        w = PriceRefreshWorker(conn, quote_fn=fake)
        out = w.process(self._task())
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["breakdown_sources"], 2)
        # No disagreement: ebay median 11, tcgplayer median 15 → 1.36×
        self.assertIsNone(out["disagreement"])
        inserts = [s for s, _ in conn.executes
                   if "INSERT INTO price_quote_source" in s]
        self.assertEqual(len(inserts), 2)
        # No bg_worker_run marker for the no-disagreement case.
        self.assertFalse(any("'price_disagreement'" in s
                             for s, _ in conn.executes))

    def test_disagreement_inserts_bg_worker_run_marker(self):
        conn = FakeConn()
        conn.queue_one(("Pikachu", "", "", ""))
        sample = [
            {"source": "ebay_sold", "price_usd": 100.0, "currency": "USD"},
            {"source": "tcgkorea", "price_usd": 300.0, "currency": "USD"},
        ]
        fake = FakeQuoteFn(result=self._quote(sample=sample))
        w = PriceRefreshWorker(conn, quote_fn=fake)
        out = w.process(self._task())
        self.assertEqual(out["status"], "OK")
        self.assertIsNotNone(out["disagreement"])
        self.assertEqual(out["disagreement"]["ratio"], 3.0)
        markers = [(s, p) for s, p in conn.executes
                   if "INSERT INTO bg_worker_run" in s
                   and "'price_disagreement'" in s]
        self.assertEqual(len(markers), 1)

    def test_no_data_skips_breakdown_entirely(self):
        # NO_DATA early-return must not even attempt a breakdown
        # write. Belt-and-braces: zero sample listings means there's
        # nothing to break down anyway.
        conn = FakeConn()
        conn.queue_one(("Pikachu", "", "", ""))
        fake = FakeQuoteFn(result={
            "median_usd": None, "sample_count": 0,
            "source_count": 0, "sources_used": ["ebay_sold"],
            "listings_sample": [], "fetched_at": 0,
        })
        w = PriceRefreshWorker(conn, quote_fn=fake)
        out = w.process(self._task())
        self.assertEqual(out["status"], "NO_DATA")
        self.assertFalse(any("INSERT INTO price_quote_source" in s
                             for s, _ in conn.executes))

    def test_breakdown_uses_quote_fetched_at_when_present(self):
        conn = FakeConn()
        conn.queue_one(("Pikachu", "", "", ""))
        sample = [
            {"source": "ebay_sold", "price_usd": 10.0, "currency": "USD"},
            {"source": "ebay_sold", "price_usd": 12.0, "currency": "USD"},
        ]
        # Quote's fetched_at is in milliseconds — worker must convert
        # to seconds for the BIGINT column (matches the convention in
        # the rest of the schema).
        fake = FakeQuoteFn(result=self._quote(
            sample=sample, fetched_at=1745000000_000))
        w = PriceRefreshWorker(conn, quote_fn=fake)
        w.process(self._task())
        inserts = [(s, p) for s, p in conn.executes
                   if "INSERT INTO price_quote_source" in s]
        self.assertEqual(inserts[0][1][5], 1745000000)  # seconds

    def test_breakdown_failure_does_not_fail_the_task(self):
        # If the breakdown insert raises (DB hiccup, weird payload),
        # the worker must STILL return OK for the underlying refresh
        # — observability must never break the parent job.
        conn = FakeConn()
        conn.queue_one(("Pikachu", "", "", ""))
        sample = [
            {"source": "ebay_sold", "price_usd": 10.0, "currency": "USD"},
            {"source": "tcgplayer", "price_usd": 12.0, "currency": "USD"},
        ]
        fake = FakeQuoteFn(result=self._quote(sample=sample))

        original_execute = FakeCursor.execute
        def fail_on_breakdown(self, sql, params=None):
            if "INSERT INTO price_quote_source" in sql:
                raise RuntimeError("simulated DB error")
            return original_execute(self, sql, params)

        with mock.patch.object(FakeCursor, "execute", fail_on_breakdown):
            w = PriceRefreshWorker(conn, quote_fn=fake)
            out = w.process(self._task())
        # The refresh task itself succeeds even though breakdown failed.
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["breakdown_sources"], 2)


# ─── T021: DDL contract ────────────────────────────────────────────────


class PriceQuoteSourceDDLTests(unittest.TestCase):

    def test_ddl_registered(self):
        from unified import schema
        self.assertIn("price_quote_source",
                      [name for name, _ in schema._ALL_DDL])

    def test_ddl_has_required_columns(self):
        from unified import schema
        ddl = schema.DDL_PRICE_QUOTE_SOURCE
        for col in ("card_id", "source", "currency", "price_usd",
                    "sample_count", "fetched_at"):
            self.assertIn(col, ddl)
        # Composite PK so a single fetched_at can hold N source rows
        # AND we keep history across runs.
        self.assertIn("PRIMARY KEY (card_id, source, fetched_at)", ddl)
        # Indexes for the two operator queries: per-card history and
        # global recent activity.
        self.assertIn("idx_price_quote_source_card", ddl)
        self.assertIn("idx_price_quote_source_recent", ddl)


if __name__ == "__main__":
    unittest.main()
