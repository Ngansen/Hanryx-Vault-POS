"""
Tests for workers/zh_set_audit.

Hermetic: synthesises a tiny ZH mirror, canonical_sets dir, and
ptcg_chs_infos.json upstream file. No network, no /mnt access.

Coverage:
  pure helpers  — _normalise_number, _num_sort_key, _walk_sets,
                  _load_canonical, _read_sc_infos
  refresh       — preserve operator-confirmed, refresh VERIFY,
                  append new, no-op when nothing changes,
                  malformed upstream → no crash
  audit + UPSERT — TC complete, TC missing, SC complete, SC extras,
                   unknown set creates row with expected=0
  worker.seed   — insert/skip
  schema check  — lang_variant CHECK enforced (string-level)
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workers import zh_set_audit
from workers.zh_set_audit import (
    ZhSetAuditWorker,
    _normalise_number,
    _num_sort_key,
    _walk_sets,
    _load_canonical,
    _read_sc_infos,
    _refresh_sc_canonical,
    VERIFY_SENTINEL,
)
from unified.schema import DDL_ZH_SET_GAP


# ── FakeConn / FakeCursor ──────────────────────────────────────────────


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        self.conn.all_sql.append((sql, params))

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
        self.all_sql: list[tuple[str, object]] = []
        self.fetchone_queue: list[object] = []
        self.fetchall_queue: list[list] = []

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1


# ── Builders ───────────────────────────────────────────────────────────


def _make_zh_mirror(td: Path, layout: dict) -> Path:
    zh = td / "zh"
    for lang, sources in layout.items():
        for source, sets in sources.items():
            for set_id, files in sets.items():
                d = zh / lang / source / set_id
                d.mkdir(parents=True, exist_ok=True)
                for fn in files:
                    (d / fn).write_bytes(b"x")
    return zh


def _write_canonical(td: Path, fname: str, sets: list[dict]) -> Path:
    cdir = td / "canonical_sets"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / fname).write_text(
        json.dumps({"_schema": {"version": 1}, "sets": sets}),
        encoding="utf-8",
    )
    return cdir


def _write_sc_infos(td: Path, collections: list[dict]) -> Path:
    p = td / "ptcg_chs_infos.json"
    p.write_text(json.dumps({"collections": collections}), encoding="utf-8")
    return p


# ── Pure helpers ───────────────────────────────────────────────────────


class HelperTests(unittest.TestCase):
    def test_normalise_strips_zeros(self):
        self.assertEqual(_normalise_number("001"), "1")
        self.assertEqual(_normalise_number(""), "0")

    def test_num_sort_key_orders_short_first(self):
        # '1' (len 1) sorts before '10' (len 2) sorts before '100'.
        # Also stable for non-numeric like 'TG01'.
        items = ["10", "1", "100", "2", "TG01"]
        items.sort(key=_num_sort_key)
        self.assertEqual(items, ["1", "2", "10", "100", "TG01"])

    def test_walk_sets_yields_normalised_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            zh = _make_zh_mirror(td, {
                "zh-tc": {"ptcg.tw": {"SV1S": ["001.jpg", "002.jpg"]}},
                "zh-sc": {"ptcg-chs": {"1": ["1.png", "2.png"]}},
            })
            tc = _walk_sets(zh, "zh-tc")
            self.assertEqual(tc, {"SV1S": {"1", "2"}})
            sc = _walk_sets(zh, "zh-sc")
            self.assertEqual(sc, {"1": {"1", "2"}})

    def test_walk_sets_missing_lang_returns_empty(self):
        # Brand-new Pi: /mnt/cards/zh/zh-sc/ doesn't exist yet because
        # PTCG-CHS-Datasets hasn't synced. Don't crash.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_walk_sets(Path(tmp) / "zh", "zh-sc"), {})

    def test_load_canonical_skips_malformed_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            cdir = _write_canonical(td, "zh_tc.json", [
                {"set_id": "SV1S", "abbreviation": "SV1S"},
                "not-a-dict",     # ← malformed entry; must be skipped
                {"abbreviation": "no-id"},     # ← no set_id; skip
            ])
            self.assertEqual(set(_load_canonical(cdir, "zh_tc.json")), {"SV1S"})

    def test_read_sc_infos_missing_file_returns_empty(self):
        self.assertEqual(_read_sc_infos(Path("/nope/x.json")), [])

    def test_read_sc_infos_filters_non_dict_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            p = _write_sc_infos(td, [
                {"id": "1", "name": "a"},
                "garbage",
                {"id": "2", "name": "b"},
            ])
            out = _read_sc_infos(p)
            self.assertEqual([c["id"] for c in out], ["1", "2"])


# ── Auto-refresh ───────────────────────────────────────────────────────


class RefreshSCTests(unittest.TestCase):
    def test_preserves_operator_jp_en_equivalents_only(self):
        # Existing entry has jp_equivalent_id='SV1S' and
        # en_equivalent_id='SVI' (operator-confirmed mapping).
        # Upstream brings a different commodityCode and a different
        # name. The refresh MUST:
        #   * leave jp_equivalent_id and en_equivalent_id alone (the
        #     operator's confirmed mapping is sacred — they spent
        #     human time deciding "SC set 1 == JP SV1S")
        #   * REFRESH the upstream-derivable fields (abbreviation,
        #     name_zh_sc, release_date, expected_card_count) — those
        #     are upstream-canonical and must reflect today's truth,
        #     not the snapshot the operator confirmed against last
        #     quarter, otherwise card_count drift would be permanent.
        # Architect-flagged regression: the original ZH-4 commit
        # bailed entirely on non-VERIFY entries and lost upstream
        # refresh value forever once an operator confirmed.
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            cdir = _write_canonical(td, "zh_sc.json", [
                {"set_id": "1", "abbreviation": "OLD",
                 "jp_equivalent_id": "SV1S",
                 "en_equivalent_id": "SVI",
                 "expected_card_count": 99,
                 "release_date": "1999-01-01",
                 "name_zh_sc": "operator-curated"},
            ])
            wrote, preserved, refreshed, appended = _refresh_sc_canonical(
                canonical_dir=cdir, fname="zh_sc.json",
                upstream=[{"id": "1", "commodityCode": "NEW",
                           "name": "upstream-name",
                           "salesDate": "2024-12-01",
                           "cards": [{"number": "1"}, {"number": "2"}]}],
            )
            self.assertTrue(wrote)
            self.assertEqual(refreshed, 1)
            doc = json.loads((cdir / "zh_sc.json").read_text())
            entry = doc["sets"][0]
            # Upstream-derivable fields refreshed:
            self.assertEqual(entry["abbreviation"], "NEW")
            self.assertEqual(entry["name_zh_sc"], "upstream-name")
            self.assertEqual(entry["release_date"], "2024-12-01")
            self.assertEqual(entry["expected_card_count"], 2)
            # Operator-decision fields preserved verbatim:
            self.assertEqual(entry["jp_equivalent_id"], "SV1S")
            self.assertEqual(entry["en_equivalent_id"], "SVI")

    def test_curated_entry_with_no_upstream_diff_is_noop(self):
        # Same operator-confirmed entry, but upstream values match
        # what we already had. Must be a no-op (don't bump mtime).
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            cdir = _write_canonical(td, "zh_sc.json", [
                {"set_id": "1", "abbreviation": "SV1",
                 "jp_equivalent_id": "SV1S",
                 "en_equivalent_id": "SVI",
                 "expected_card_count": 2,
                 "release_date": "2023-07-01",
                 "name_zh_sc": "朱紫"},
            ])
            mtime_before = (cdir / "zh_sc.json").stat().st_mtime
            time.sleep(0.05)
            wrote, preserved, refreshed, appended = _refresh_sc_canonical(
                canonical_dir=cdir, fname="zh_sc.json",
                upstream=[{"id": "1", "commodityCode": "SV1",
                           "name": "朱紫", "salesDate": "2023-07-01",
                           "cards": [{"number": "1"}, {"number": "2"}]}],
            )
            self.assertFalse(wrote)
            self.assertEqual(preserved, 1)
            self.assertEqual(refreshed, 0)
            self.assertEqual((cdir / "zh_sc.json").stat().st_mtime, mtime_before)

    def test_refreshes_VERIFY_entries_from_upstream(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            cdir = _write_canonical(td, "zh_sc.json", [
                {"set_id": "1", "abbreviation": "VERIFY",
                 "jp_equivalent_id": "VERIFY",
                 "en_equivalent_id": "VERIFY",
                 "expected_card_count": "VERIFY",
                 "release_date": "VERIFY",
                 "name_zh_sc": "VERIFY (first SC release)"},
            ])
            wrote, preserved, refreshed, appended = _refresh_sc_canonical(
                canonical_dir=cdir, fname="zh_sc.json",
                upstream=[{"id": "1", "commodityCode": "SV1",
                           "name": "朱紫", "salesDate": "2023-07-01",
                           "cards": [{"number": "1"}, {"number": "2"}]}],
            )
            self.assertTrue(wrote)
            self.assertEqual(refreshed, 1)
            doc = json.loads((cdir / "zh_sc.json").read_text())
            entry = doc["sets"][0]
            # Upstream-derivable fields refreshed:
            self.assertEqual(entry["abbreviation"], "SV1")
            self.assertEqual(entry["release_date"], "2023-07-01")
            self.assertEqual(entry["expected_card_count"], 2)
            self.assertEqual(entry["name_zh_sc"], "朱紫")
            # jp/en equivalents stay VERIFY (operator decision):
            self.assertEqual(entry["jp_equivalent_id"], "VERIFY")
            self.assertEqual(entry["en_equivalent_id"], "VERIFY")

    def test_appends_new_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            cdir = _write_canonical(td, "zh_sc.json", [
                {"set_id": "1", "abbreviation": "SV1",
                 "jp_equivalent_id": "SV1S"},
            ])
            wrote, preserved, refreshed, appended = _refresh_sc_canonical(
                canonical_dir=cdir, fname="zh_sc.json",
                upstream=[
                    {"id": "1", "commodityCode": "SV1"},
                    {"id": "999", "commodityCode": "NEW",
                     "name": "新セット"},
                ],
            )
            self.assertTrue(wrote)
            self.assertEqual(appended, 1)
            doc = json.loads((cdir / "zh_sc.json").read_text())
            ids = sorted(s["set_id"] for s in doc["sets"])
            self.assertEqual(ids, ["1", "999"])
            new_entry = next(s for s in doc["sets"] if s["set_id"] == "999")
            self.assertEqual(new_entry["jp_equivalent_id"], VERIFY_SENTINEL)
            self.assertEqual(new_entry["en_equivalent_id"], VERIFY_SENTINEL)

    def test_noop_when_nothing_changes(self):
        # Upstream identical to local. Must NOT bump file mtime —
        # otherwise nightly cron looks like "something changed every
        # night" in the operator's git diff view.
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            cdir = _write_canonical(td, "zh_sc.json", [
                {"set_id": "1", "abbreviation": "SV1",
                 "jp_equivalent_id": "VERIFY",
                 "en_equivalent_id": "VERIFY",
                 "expected_card_count": 2,
                 "release_date": "2023-07-01",
                 "name_zh_sc": "朱紫"},
            ])
            mtime_before = (cdir / "zh_sc.json").stat().st_mtime
            time.sleep(0.05)
            wrote, _, refreshed, appended = _refresh_sc_canonical(
                canonical_dir=cdir, fname="zh_sc.json",
                upstream=[{"id": "1", "commodityCode": "SV1",
                           "name": "朱紫", "salesDate": "2023-07-01",
                           "cards": [{"number": "1"}, {"number": "2"}]}],
            )
            self.assertFalse(wrote)
            self.assertEqual(refreshed, 0)
            self.assertEqual(appended, 0)
            self.assertEqual((cdir / "zh_sc.json").stat().st_mtime, mtime_before)

    def test_malformed_upstream_no_crash(self):
        # Read returned []; refresh loop is a no-op, file untouched.
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            cdir = _write_canonical(td, "zh_sc.json", [
                {"set_id": "1", "jp_equivalent_id": "VERIFY"},
            ])
            wrote, *_ = _refresh_sc_canonical(
                canonical_dir=cdir, fname="zh_sc.json", upstream=[],
            )
            self.assertFalse(wrote)


# ── Audit + UPSERT ─────────────────────────────────────────────────────


class AuditTests(unittest.TestCase):
    def _make_worker(self, td, conn, *, sc_infos=None):
        return ZhSetAuditWorker(
            conn,
            zh_root=td / "zh",
            canonical_dir=td / "canonical_sets",
            sc_infos_path=sc_infos or (td / "no_infos.json"),
            skip_sc_refresh=True,            # most audit tests skip refresh
            now_fn=lambda: 1700000000,
        )

    def _gap_inserts(self, conn: FakeConn) -> list[tuple[str, object]]:
        return [(s, p) for s, p in conn.all_sql if "INSERT INTO zh_set_gap" in s]

    def test_tc_set_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            _make_zh_mirror(td, {
                "zh-tc": {"ptcg.tw": {"SV1S": ["1.jpg", "2.jpg", "3.jpg"]}},
            })
            _write_canonical(td, "zh_tc.json", [
                {"set_id": "SV1S", "abbreviation": "SV1S",
                 "jp_equivalent_id": "SV1S",
                 "expected_card_count": 3},
            ])
            _write_canonical(td, "zh_sc.json", [])
            conn = FakeConn()
            w = self._make_worker(td, conn)
            res = w.process({})
            self.assertEqual(res["sets_audited"], 1)
            inserts = self._gap_inserts(conn)
            self.assertEqual(len(inserts), 1)
            params = inserts[0][1]
            # set_id, lang_variant, expected, actual, missing, extras, audited_at
            self.assertEqual(params[0], "SV1S")
            self.assertEqual(params[1], "TC")
            self.assertEqual(params[2], 3)
            self.assertEqual(params[3], 3)
            self.assertEqual(json.loads(params[4]), [])
            self.assertEqual(json.loads(params[5]), [])

    def test_tc_set_missing_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            _make_zh_mirror(td, {
                "zh-tc": {"ptcg.tw": {"SV1S": ["1.jpg", "3.jpg"]}},
            })
            _write_canonical(td, "zh_tc.json", [
                {"set_id": "SV1S", "expected_card_count": 5,
                 "jp_equivalent_id": "SV1S"},
            ])
            _write_canonical(td, "zh_sc.json", [])
            conn = FakeConn()
            w = self._make_worker(td, conn)
            w.process({})
            params = self._gap_inserts(conn)[0][1]
            self.assertEqual(params[3], 2)                    # actual
            self.assertEqual(json.loads(params[4]), ["2", "4", "5"])  # missing

    def test_sc_extras_when_disk_has_more_than_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            _make_zh_mirror(td, {
                "zh-sc": {"ptcg-chs": {"1": ["1.png", "2.png", "3.png", "4.png"]}},
            })
            _write_canonical(td, "zh_tc.json", [])
            _write_canonical(td, "zh_sc.json", [
                {"set_id": "1", "expected_card_count": 2,
                 "jp_equivalent_id": "VERIFY"},
            ])
            conn = FakeConn()
            w = self._make_worker(td, conn)
            w.process({})
            params = self._gap_inserts(conn)[0][1]
            self.assertEqual(params[1], "SC")
            self.assertEqual(params[2], 2)
            self.assertEqual(params[3], 4)
            self.assertEqual(json.loads(params[5]), ["3", "4"])     # extras

    def test_unknown_set_on_disk_writes_row_with_expected_zero(self):
        # Disk has a set that's not in canonical at all. We still
        # write a row (expected=0, actual=N) so the dashboard shows
        # "untracked set with N cards" instead of silently ignoring
        # it — the operator needs to either add it to canonical or
        # delete the rogue dir.
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            _make_zh_mirror(td, {
                "zh-tc": {"ptcg.tw": {"MYSTERY": ["1.jpg"]}},
            })
            _write_canonical(td, "zh_tc.json", [])
            _write_canonical(td, "zh_sc.json", [])
            conn = FakeConn()
            w = self._make_worker(td, conn)
            w.process({})
            params = self._gap_inserts(conn)[0][1]
            self.assertEqual(params[0], "MYSTERY")
            self.assertEqual(params[2], 0)
            self.assertEqual(params[3], 1)
            # No expected_nums known → extras stays empty (we can't
            # call any card "extra" if we don't know what was expected).
            self.assertEqual(json.loads(params[5]), [])

    def test_seed_inserts_when_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            _write_canonical(td, "zh_tc.json", [])
            _write_canonical(td, "zh_sc.json", [])
            conn = FakeConn()
            conn.fetchone_queue = [None]
            w = self._make_worker(td, conn)
            self.assertEqual(w.seed(), 1)
            seeded = [s for s, _ in conn.all_sql if "INSERT INTO bg_task_queue" in s]
            self.assertEqual(len(seeded), 1)

    def test_seed_skips_when_pending_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            _write_canonical(td, "zh_tc.json", [])
            _write_canonical(td, "zh_sc.json", [])
            conn = FakeConn()
            conn.fetchone_queue = [(1,)]
            w = self._make_worker(td, conn)
            self.assertEqual(w.seed(), 0)


class SchemaTests(unittest.TestCase):
    def test_lang_variant_check_constraint_in_ddl(self):
        # We don't have a real PG to verify the CONSTRAINT runs, but
        # the DDL string must encode it. A future DB migration MUST
        # never silently drop this check — we'd start writing 'KR' or
        # '' rows that confuse the dashboard.
        self.assertIn("CHECK (lang_variant IN ('TC', 'SC'))", DDL_ZH_SET_GAP)

    def test_composite_pk_in_ddl(self):
        # PRIMARY KEY (set_id, lang_variant) — same numeric set_id
        # may exist in BOTH TC and SC namespaces.
        self.assertIn("PRIMARY KEY (set_id, lang_variant)", DDL_ZH_SET_GAP)


if __name__ == "__main__":
    unittest.main()
