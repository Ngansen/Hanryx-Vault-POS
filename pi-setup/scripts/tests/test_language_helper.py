#!/usr/bin/env python3
"""
test_language_helper.py — unit tests for the multilingual enrichment
worker (pi-setup/workers/language_helper.py).

Coverage:
  * romanise_hangul (built-in pure-Python Revised Romanization):
      - empty input → ''
      - returns lowercase ASCII only for Hangul syllables
      - non-Hangul characters pass through unchanged
      - 가 → 'ga'  (sanity: the simplest syllable)
      - 피카츄 → 'pikachyu' (Pikachu)
      - 이브이 → 'ibeui' (Eevee transliterated)
      - mixed Korean + Latin preserves spacing
  * romaji_japanese:
      - empty → ('', 'EMPTY_INPUT')
      - whitespace-only → ('', 'EMPTY_INPUT')
      - pykakasi missing (forced) → ('', 'JP_LIB_MISSING')
  * pinyin_chinese:
      - empty → ('', 'EMPTY_INPUT')
      - pypinyin missing (forced) → ('', 'CN_LIB_MISSING')
  * LanguageEnrichWorker.process:
      - missing payload → WorkerError
      - missing card row → WorkerError
      - happy path: writes one INSERT-on-conflict UPSERT into
        card_language_extra with all expected fields
      - records backfilled_fields when species table provides names
        the card lacks
      - does NOT mutate cards_master (read-only worker on that table)
  * seed enqueues with LEFT JOIN + cutoff guard
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PI_SETUP = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PI_SETUP))

from workers import language_helper as lh  # noqa: E402
from workers.base import WorkerError  # noqa: E402


# ── Fakes ────────────────────────────────────────────────────────


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self.rowcount = 0
        self.executed: list[tuple[str, object]] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self.conn.all_sql.append(sql)
        if self.conn.rowcount_queue:
            self.rowcount = self.conn.rowcount_queue.pop(0)

    def fetchone(self):
        if self.conn.fetchone_queue:
            return self.conn.fetchone_queue.pop(0)
        return None

    def fetchall(self):
        if self.conn.fetchall_queue:
            return self.conn.fetchall_queue.pop(0)
        return []


class FakeConn:
    def __init__(self):
        self.commits = 0
        self.all_sql: list[str] = []
        self.cursors: list[FakeCursor] = []
        self.fetchone_queue: list[object] = []
        self.fetchall_queue: list[list] = []
        self.rowcount_queue: list[int] = []

    def cursor(self):
        c = FakeCursor(self)
        self.cursors.append(c)
        return c

    def commit(self):
        self.commits += 1


# ── Hangul romaniser ─────────────────────────────────────────────


class RomaniseHangulTests(unittest.TestCase):

    def test_empty_returns_empty(self):
        self.assertEqual(lh.romanise_hangul(""), "")

    def test_simplest_syllable(self):
        # 가 = initial ㄱ(g) + medial ㅏ(a) + no final → 'ga'
        # Sanity check that the arithmetic table is wired correctly.
        self.assertEqual(lh.romanise_hangul("가"), "ga")

    def test_pikachu(self):
        # 피카츄 → pi + ka + chyu
        out = lh.romanise_hangul("피카츄")
        self.assertEqual(out, "pikachyu")

    def test_eevee(self):
        # 이브이 → i + beu + i
        out = lh.romanise_hangul("이브이")
        self.assertEqual(out, "ibeui")

    def test_non_hangul_passes_through(self):
        # Latin characters should pass through, lowercased so the
        # result is a uniform search key.
        self.assertEqual(lh.romanise_hangul("Pikachu V"), "pikachu v")

    def test_mixed_korean_and_latin(self):
        # 피카츄 ex → 'pikachyu ex'
        out = lh.romanise_hangul("피카츄 ex")
        self.assertTrue(out.startswith("pikachyu"))
        self.assertIn(" ex", out)

    def test_output_is_ascii_for_pure_hangul(self):
        out = lh.romanise_hangul("리자몽")
        self.assertTrue(out.isascii())
        self.assertEqual(out.lower(), out)


# ── Japanese romaji ──────────────────────────────────────────────


class RomajiJapaneseTests(unittest.TestCase):

    def test_empty_input(self):
        self.assertEqual(lh.romaji_japanese(""), ("", "EMPTY_INPUT"))

    def test_whitespace_input(self):
        self.assertEqual(lh.romaji_japanese("   "), ("", "EMPTY_INPUT"))

    def test_lib_missing_status(self):
        # Force the library-missing branch by clearing the cache and
        # making the load function return None.
        original_tried = lh._PYKAKASI_TRIED
        original_kks   = lh._PYKAKASI_KKS
        try:
            lh._PYKAKASI_TRIED = True
            lh._PYKAKASI_KKS = None
            out, status = lh.romaji_japanese("ピカチュウ")
            self.assertEqual(out, "")
            self.assertEqual(status, "JP_LIB_MISSING")
        finally:
            lh._PYKAKASI_TRIED = original_tried
            lh._PYKAKASI_KKS = original_kks


# ── Chinese pinyin ───────────────────────────────────────────────


class PinyinChineseTests(unittest.TestCase):

    def test_empty_input(self):
        self.assertEqual(lh.pinyin_chinese(""), ("", "EMPTY_INPUT"))

    def test_lib_missing_status(self):
        original_tried = lh._PYPINYIN_TRIED
        original_fn    = lh._PYPINYIN_FN
        try:
            lh._PYPINYIN_TRIED = True
            lh._PYPINYIN_FN = None
            out, status = lh.pinyin_chinese("皮卡丘")
            self.assertEqual(out, "")
            self.assertEqual(status, "CN_LIB_MISSING")
        finally:
            lh._PYPINYIN_TRIED = original_tried
            lh._PYPINYIN_FN = original_fn


# ── LanguageEnrichWorker.process ─────────────────────────────────


class ProcessTests(unittest.TestCase):

    def _force_libs_missing(self):
        # Make lib status deterministic so we can assert exact values.
        lh._PYKAKASI_TRIED = True
        lh._PYKAKASI_KKS = None
        lh._PYPINYIN_TRIED = True
        lh._PYPINYIN_FN = None

    def test_missing_payload_raises(self):
        conn = FakeConn()
        w = lh.LanguageEnrichWorker(conn)
        with self.assertRaises(WorkerError):
            w.process({"task_id": 1, "task_type": "lang_enrich",
                       "task_key": "", "payload": {}, "attempts": 0})

    def test_missing_card_raises(self):
        conn = FakeConn()
        conn.fetchone_queue = [None]   # cards_master SELECT → no row
        w = lh.LanguageEnrichWorker(conn)
        with self.assertRaises(WorkerError):
            w.process({
                "task_id": 1, "task_type": "lang_enrich",
                "task_key": "sv2/47",
                "payload": {"set_id": "sv2", "card_number": "47"},
                "attempts": 0,
            })

    def test_happy_path_upserts_extra_row(self):
        self._force_libs_missing()
        conn = FakeConn()
        # cards_master row: EN+KR+JP+CN names, pokedex_id=25
        conn.fetchone_queue = [
            ("Pikachu", "피카츄", "ピカチュウ", "皮卡丘", 25),
            None,   # ref_pokedex_species lookup → no species row
        ]
        w = lh.LanguageEnrichWorker(conn)
        rv = w.process({
            "task_id": 1, "task_type": "lang_enrich",
            "task_key": "sv2/47",
            "payload": {"set_id": "sv2", "card_number": "47"},
            "attempts": 0,
        })
        self.assertEqual(rv["hangul_roman_status"], "OK")
        self.assertEqual(rv["romaji_jp_status"], "JP_LIB_MISSING")
        self.assertEqual(rv["pinyin_chs_status"], "CN_LIB_MISSING")
        self.assertEqual(rv["backfilled_count"], 0)

        # The third execute() call must be the UPSERT into card_language_extra
        sqls = [c[0] for c in conn.cursors[0].executed]
        joined = "\n".join(sqls)
        self.assertIn("INSERT INTO card_language_extra", joined)
        self.assertIn("ON CONFLICT (set_id, card_number) DO UPDATE", joined)
        # Critical invariant: this worker MUST NOT mutate cards_master.
        self.assertNotIn("UPDATE cards_master", joined)
        self.assertNotIn("INSERT INTO cards_master", joined)

    def test_records_backfills_from_species(self):
        self._force_libs_missing()
        conn = FakeConn()
        # Card has only KR name; species table fills EN/JP/CN.
        conn.fetchone_queue = [
            ("", "피카츄", "", "", 25),                      # cards_master
            ("Pikachu", "피카츄", "ピカチュウ", "皮卡丘"),    # species row
        ]
        w = lh.LanguageEnrichWorker(conn)
        rv = w.process({
            "task_id": 1, "task_type": "lang_enrich",
            "task_key": "sv2/47",
            "payload": {"set_id": "sv2", "card_number": "47"},
            "attempts": 0,
        })
        # 3 fields filled (EN, JP, CN); KR was already present.
        self.assertEqual(rv["backfilled_count"], 3)

        upsert = conn.cursors[0].executed[-1]
        # Find the JSON-encoded backfilled_fields parameter — it must
        # mention all three suggested fields.
        json_params = [p for p in upsert[1]
                       if isinstance(p, str) and p.startswith("{")]
        backfill_blob = next(p for p in json_params if "name_en" in p)
        self.assertIn("Pikachu", backfill_blob)
        self.assertIn("ピカチュウ", backfill_blob)
        self.assertIn("皮卡丘", backfill_blob)


class SeedTests(unittest.TestCase):

    def test_seed_uses_left_join_cutoff(self):
        conn = FakeConn()
        conn.rowcount_queue = [42]
        w = lh.LanguageEnrichWorker(conn)
        n = w.seed()
        self.assertEqual(n, 42)
        sql, _ = conn.cursors[0].executed[0]
        # Expected idempotency mechanics:
        self.assertIn("LEFT JOIN card_language_extra", sql)
        self.assertIn("e.set_id IS NULL", sql)
        self.assertIn("OR COALESCE(e.enriched_at, 0) <", sql)
        self.assertIn("ON CONFLICT (task_type, task_key) DO NOTHING", sql)


if __name__ == "__main__":
    unittest.main()
