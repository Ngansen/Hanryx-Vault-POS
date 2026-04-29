#!/usr/bin/env python3
"""
test_data_analyst.py — unit tests for the data-quality analyst worker
(pi-setup/workers/data_analyst.py).

Coverage:
  * REPORTS catalogue:
      - non-empty, every entry is (shape, sql)
      - shape is one of {'one', 'many'}
      - every SQL string is non-empty and includes its primary table
      - completeness query references every name_<lang> column
      - top_gap_sets query produces a per-set rollup (GROUP BY set_id)
      - duplicates query has HAVING COUNT(*) > 1
  * _row_to_dict:
      - tuple + description → column-name dict
      - dict row → returned as a copy
      - None → {}
  * DataAnalystWorker.process:
      - unknown report_kind → WorkerError (permanent)
      - 'completeness' (shape='one'): fetchone result becomes JSONB
        snapshot, INSERT into data_analysis_report
      - 'rarity_distribution' (shape='many'): fetchall result becomes
        JSONB list, rows_examined = len(list)
      - works with custom REPORTS injected via constructor
  * seed enqueues only kinds without a recent snapshot, NOT EXISTS
    + ON CONFLICT DO NOTHING combo
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PI_SETUP = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PI_SETUP))

from workers import data_analyst as da  # noqa: E402
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
        # Apply queued description for the upcoming fetch.
        if self.conn.description_queue:
            self.description = self.conn.description_queue.pop(0)
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
        # Queue of cursor.description tuples that pop on each execute()
        self.description_queue: list[object] = []

    def cursor(self):
        c = FakeCursor(self)
        self.cursors.append(c)
        return c

    def commit(self):
        self.commits += 1


# ── Catalogue invariants ─────────────────────────────────────────


class ReportsCatalogueTests(unittest.TestCase):

    def test_catalogue_non_empty(self):
        self.assertGreater(len(da.REPORTS), 0)

    def test_each_entry_shape(self):
        for kind, entry in da.REPORTS.items():
            self.assertEqual(len(entry), 2,
                msg=f"{kind} entry must be (shape, sql)")
            shape, sql = entry
            self.assertIn(shape, ("one", "many"),
                msg=f"{kind} shape must be 'one' or 'many'")
            self.assertTrue(sql.strip(),
                msg=f"{kind} SQL must be non-empty")

    def test_completeness_covers_all_name_languages(self):
        sql = da.REPORTS["completeness"][1]
        for col in ("name_en", "name_kr", "name_jp", "name_chs"):
            self.assertIn(col, sql,
                msg=f"completeness must reference {col}")

    def test_top_gap_sets_groups_by_set(self):
        shape, sql = da.REPORTS["top_gap_sets"]
        self.assertEqual(shape, "many")
        self.assertIn("GROUP BY set_id", sql)

    def test_duplicates_has_having_count(self):
        sql = da.REPORTS["duplicates"][1]
        self.assertIn("HAVING COUNT(*)", sql)
        self.assertIn("> 1", sql)

    def test_image_coverage_joins_health(self):
        # The image_coverage report would be useless without joining
        # to image_health_check — verify the join is wired up.
        sql = da.REPORTS["image_coverage"][1]
        self.assertIn("image_health_check", sql)
        self.assertIn("LEFT JOIN", sql)


# ── _row_to_dict ─────────────────────────────────────────────────


class RowToDictTests(unittest.TestCase):

    def test_tuple_with_description(self):
        # psycopg2 description rows are (name, type_code, ...) tuples
        desc = [("a",), ("b",), ("c",)]
        out = da._row_to_dict((1, 2, 3), desc)
        self.assertEqual(out, {"a": 1, "b": 2, "c": 3})

    def test_dict_row_passes_through(self):
        d = {"x": 1, "y": 2}
        out = da._row_to_dict(d, None)
        self.assertEqual(out, d)
        self.assertIsNot(out, d, "must return a copy, not the original")

    def test_none_returns_empty(self):
        self.assertEqual(da._row_to_dict(None, None), {})


# ── DataAnalystWorker.process ────────────────────────────────────


class ProcessTests(unittest.TestCase):

    def test_unknown_kind_raises(self):
        conn = FakeConn()
        w = da.DataAnalystWorker(conn)
        with self.assertRaises(WorkerError):
            w.process({
                "task_id": 1, "task_type": "data_analysis",
                "task_key": "no_such_report",
                "payload": {"report_kind": "no_such_report"},
                "attempts": 0,
            })

    def test_completeness_shape_one(self):
        conn = FakeConn()
        # SELECT runs first; queue description + fetchone result.
        conn.description_queue = [
            [("total_cards",), ("with_name_en",), ("with_name_kr",),
             ("with_name_jp",), ("with_name_chs",), ("with_rarity",),
             ("with_artist",), ("with_hp",), ("with_pokedex",),
             ("with_primary_image",), ("with_any_image",)],
            None,   # INSERT step has no description we care about
        ]
        conn.fetchone_queue = [
            (10000, 9500, 9800, 9700, 9100,
             8000, 5000, 7500, 6000, 7800, 8200),
        ]
        w = da.DataAnalystWorker(conn)
        rv = w.process({
            "task_id": 1, "task_type": "data_analysis",
            "task_key": "completeness",
            "payload": {"report_kind": "completeness"},
            "attempts": 0,
        })
        self.assertEqual(rv["report_kind"], "completeness")
        self.assertEqual(rv["rows_examined"], 10000)

        # The INSERT must have happened with a JSON payload that
        # contains the column names.
        sqls = [c[0] for c in conn.cursors[0].executed]
        joined = "\n".join(sqls)
        self.assertIn("INSERT INTO data_analysis_report", joined)
        # Find the JSON payload param
        insert_params = conn.cursors[0].executed[1][1]
        json_str = next(p for p in insert_params
                        if isinstance(p, str) and p.startswith("{"))
        payload = json.loads(json_str)
        self.assertEqual(payload["total_cards"], 10000)
        self.assertEqual(payload["with_name_kr"], 9800)

    def test_many_shape_records_list(self):
        conn = FakeConn()
        conn.description_queue = [
            [("rarity",), ("n",)],
            None,
        ]
        conn.fetchall_queue = [[
            ("Common", 4500),
            ("Rare",   1200),
            ("Holo",    800),
        ]]
        w = da.DataAnalystWorker(conn)
        rv = w.process({
            "task_id": 1, "task_type": "data_analysis",
            "task_key": "rarity_distribution",
            "payload": {"report_kind": "rarity_distribution"},
            "attempts": 0,
        })
        self.assertEqual(rv["rows_examined"], 3)
        # JSON payload must be a list of dicts
        insert_params = conn.cursors[0].executed[1][1]
        json_str = next(p for p in insert_params
                        if isinstance(p, str) and p.startswith("["))
        payload = json.loads(json_str)
        self.assertEqual(payload[0]["rarity"], "Common")
        self.assertEqual(payload[0]["n"], 4500)
        self.assertEqual(len(payload), 3)

    def test_custom_reports_via_constructor(self):
        # Letting tests / future code inject alternative report
        # catalogues keeps the worker pluggable without touching the
        # global REPORTS dict.
        conn = FakeConn()
        conn.description_queue = [[("k",), ("v",)], None]
        conn.fetchone_queue = [("hello", 7)]
        custom = {"hello_world": ("one", "SELECT 1")}
        w = da.DataAnalystWorker(conn, reports=custom)
        rv = w.process({
            "task_id": 1, "task_type": "data_analysis",
            "task_key": "hello_world",
            "payload": {"report_kind": "hello_world"},
            "attempts": 0,
        })
        self.assertEqual(rv["report_kind"], "hello_world")


# ── Seed ─────────────────────────────────────────────────────────


class SeedTests(unittest.TestCase):

    def test_seed_uses_not_exists_and_on_conflict(self):
        conn = FakeConn()
        conn.rowcount_queue = [4]
        w = da.DataAnalystWorker(conn)
        n = w.seed()
        self.assertEqual(n, 4)
        sql, params = conn.cursors[0].executed[0]
        self.assertIn("VALUES", sql)
        self.assertIn("NOT EXISTS", sql)
        self.assertIn("data_analysis_report", sql)
        self.assertIn("ON CONFLICT (task_type, task_key) DO NOTHING", sql)
        # Must have at least one set of params per known report kind
        # (4 params per kind: type/key/payload/created_at) plus the
        # final cutoff param.
        expected_min = len(da.REPORTS) * 4 + 1
        self.assertGreaterEqual(len(params), expected_min)

    def test_seed_with_empty_catalogue_is_noop(self):
        conn = FakeConn()
        w = da.DataAnalystWorker(conn, reports={})
        n = w.seed()
        self.assertEqual(n, 0)
        # No SQL should have been issued for an empty catalogue.
        self.assertEqual(len(conn.cursors), 0)


if __name__ == "__main__":
    unittest.main()
