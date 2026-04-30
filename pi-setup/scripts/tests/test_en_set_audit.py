"""
Tests for workers/en_set_audit.

Hermetic: no DB, no network. The EN audit reads canonical from
`src_tcgdex_multi` (a DB table), not a filesystem walk like KR/ZH —
so the FakeConn here delivers two fetchall responses in order:
canonical first, then actual.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workers import en_set_audit
from workers.en_set_audit import (
    EnSetAuditWorker, _normalise_number, _num_sort_key,
    _read_canonical, _read_actual,
)


# ── FakeConn / FakeCursor ────────────────────────────────────────


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0
        self.executed: list[tuple[str, object]] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self.conn.all_sql.append((sql, params))
        if self.conn.rowcount_queue:
            self.rowcount = self.conn.rowcount_queue.pop(0)

    def fetchall(self):
        if self.conn.fetchall_queue:
            return self.conn.fetchall_queue.pop(0)
        return []


class FakeConn:
    def __init__(self):
        self.commits = 0
        self.cursors: list[FakeCursor] = []
        self.all_sql: list[tuple[str, object]] = []
        self.fetchall_queue: list[list] = []
        self.rowcount_queue: list[int] = []

    def cursor(self):
        c = FakeCursor(self)
        self.cursors.append(c)
        return c

    def commit(self):
        self.commits += 1


# ── _normalise_number ────────────────────────────────────────────


class NormaliseNumberTests(unittest.TestCase):

    def test_strips_leading_zeros_on_pure_digits(self):
        self.assertEqual(_normalise_number("001"), "1")
        self.assertEqual(_normalise_number("042"), "42")
        self.assertEqual(_normalise_number("100"), "100")

    def test_empty_becomes_zero(self):
        self.assertEqual(_normalise_number(""), "0")
        self.assertEqual(_normalise_number("   "), "0")
        self.assertEqual(_normalise_number(None), "0")  # type: ignore[arg-type]

    def test_zero_stays_zero(self):
        self.assertEqual(_normalise_number("0"), "0")
        self.assertEqual(_normalise_number("000"), "0")

    def test_alphanumeric_kept_verbatim(self):
        # Promo / TG / SV-P style codes never strip — those aren't
        # leading-zero-padded ints, they're identifiers.
        self.assertEqual(_normalise_number("TG01"), "TG01")
        self.assertEqual(_normalise_number("SV-P-001"), "SV-P-001")
        self.assertEqual(_normalise_number("GG10"), "GG10")

    def test_whitespace_trimmed(self):
        self.assertEqual(_normalise_number("  005  "), "5")
        self.assertEqual(_normalise_number("\tTG01\n"), "TG01")


# ── _num_sort_key ────────────────────────────────────────────────


class NumSortKeyTests(unittest.TestCase):

    def test_pure_ints_sort_numerically(self):
        nums = ["10", "2", "1", "100", "20"]
        self.assertEqual(sorted(nums, key=_num_sort_key),
                         ["1", "2", "10", "20", "100"])

    def test_alphanumeric_sort_after_ints(self):
        nums = ["TG01", "5", "1", "SV-P-001"]
        # ints first (sorted), then strings (lexical).
        self.assertEqual(sorted(nums, key=_num_sort_key),
                         ["1", "5", "SV-P-001", "TG01"])


# ── _read_canonical ──────────────────────────────────────────────


class ReadCanonicalTests(unittest.TestCase):

    def test_groups_per_set_with_normalisation(self):
        conn = FakeConn()
        conn.fetchall_queue = [[
            ("sv2", ["001", "2", "10"]),
            ("sv1", ["1"]),
        ]]
        out = _read_canonical(conn.cursor())
        self.assertEqual(out, {"sv2": {"1", "2", "10"},
                               "sv1": {"1"}})
        # Verify the SQL filters to EN-bearing rows only.
        sql, _ = conn.all_sql[0]
        self.assertIn("names ? 'en'", sql)
        # And excludes empty set_id, otherwise we'd group nameless
        # rows under '' and produce a phantom audit bucket.
        self.assertIn("set_id <> ''", sql)

    def test_empty_response_yields_empty_dict(self):
        conn = FakeConn()
        conn.fetchall_queue = [[]]
        self.assertEqual(_read_canonical(conn.cursor()), {})

    def test_skips_rows_with_empty_set_id(self):
        # If the SQL filter ever regresses, the function still defends
        # in code (skip falsy sid before populating the dict).
        conn = FakeConn()
        conn.fetchall_queue = [[("", ["1"]), ("sv2", ["1"])]]
        self.assertEqual(_read_canonical(conn.cursor()), {"sv2": {"1"}})


# ── _read_actual ─────────────────────────────────────────────────


class ReadActualTests(unittest.TestCase):

    def test_filters_by_set_ids_and_returns_normalised_numbers(self):
        conn = FakeConn()
        conn.fetchall_queue = [[
            ("sv2", ["001", "002", "010"]),
        ]]
        out = _read_actual(conn.cursor(), ["sv2", "sv1"])
        self.assertEqual(out, {"sv2": {"1", "2", "10"}})
        sql, params = conn.all_sql[0]
        # Critical: the SQL must filter on name_en being set, not just
        # on the set_id — otherwise we'd count JP-only rows as EN.
        self.assertIn("name_en IS NOT NULL", sql)
        self.assertIn("name_en <> ''", sql)
        # Set-id list is parameterised.
        self.assertEqual(params[0], ["sv2", "sv1"])

    def test_empty_set_ids_returns_empty_without_querying(self):
        conn = FakeConn()
        out = _read_actual(conn.cursor(), [])
        self.assertEqual(out, {})
        # No SQL should fire — we'd just be wasting a round trip.
        self.assertEqual(conn.all_sql, [])


# ── seed ─────────────────────────────────────────────────────────


class SeedTests(unittest.TestCase):

    def test_seed_uses_today_date_as_task_key(self):
        conn = FakeConn()
        conn.rowcount_queue = [1]
        w = EnSetAuditWorker(conn, today_fn=lambda: "2026-04-30")
        n = w.seed()
        self.assertEqual(n, 1)
        sql, params = conn.cursors[0].executed[0]
        self.assertIn("INSERT INTO bg_task_queue", sql)
        self.assertIn("'en_set_audit'", sql)
        self.assertIn("ON CONFLICT (task_type, task_key) DO NOTHING", sql)
        # task_key (the per-day key) is the FIRST positional param.
        self.assertEqual(params[0], "2026-04-30")
        self.assertEqual(conn.commits, 1)


# ── process — short circuit ──────────────────────────────────────


class ProcessShortCircuitTests(unittest.TestCase):

    def test_empty_canonical_returns_empty_source_status_no_writes(self):
        # src_tcgdex_multi has zero EN rows. We must NOT blow away
        # the previous en_set_gap report just because the operator
        # hasn't refreshed TCGdex yet.
        conn = FakeConn()
        conn.fetchall_queue = [[]]   # _read_canonical → empty
        w = EnSetAuditWorker(conn)
        rv = w.process({"task_id": 1, "payload": {}})
        self.assertEqual(rv["status"], "EMPTY_SOURCE")
        self.assertEqual(rv["sets_audited"], 0)
        # No INSERT into en_set_gap should have fired.
        self.assertFalse(any("en_set_gap" in s for s, _ in conn.all_sql))
        self.assertEqual(conn.commits, 0)


# ── process — happy paths ────────────────────────────────────────


class ProcessHappyPathTests(unittest.TestCase):

    def _run(self, canonical_rows, actual_rows):
        """Drive process() with canonical and actual fetchall responses."""
        conn = FakeConn()
        conn.fetchall_queue = [canonical_rows, actual_rows]
        w = EnSetAuditWorker(conn)
        rv = w.process({"task_id": 1, "payload": {}})
        return rv, conn

    def test_complete_set_records_zero_missing_zero_extra(self):
        rv, conn = self._run(
            [("sv2", ["1", "2", "3"])],
            [("sv2", ["1", "2", "3"])],
        )
        self.assertEqual(rv["status"], "OK")
        self.assertEqual(rv["sets_audited"], 1)
        self.assertEqual(rv["total_missing"], 0)
        self.assertEqual(rv["total_extra"], 0)
        upserts = [(s, p) for s, p in conn.all_sql
                   if "INSERT INTO en_set_gap" in s]
        self.assertEqual(len(upserts), 1)
        sql, params = upserts[0]
        self.assertIn("ON CONFLICT (set_id) DO UPDATE", sql)
        self.assertEqual(params[0], "sv2")        # set_id
        self.assertEqual(params[1], 3)            # expected_count
        self.assertEqual(params[2], 3)            # actual_count
        self.assertEqual(json.loads(params[3]), [])  # missing_numbers
        self.assertEqual(json.loads(params[4]), [])  # extra_numbers
        self.assertEqual(conn.commits, 1)

    def test_missing_numbers_recorded_sorted_numerically(self):
        rv, conn = self._run(
            [("sv2", ["1", "2", "3", "10"])],
            [("sv2", ["1", "10"])],   # missing 2 and 3
        )
        self.assertEqual(rv["status"], "OK")
        self.assertEqual(rv["total_missing"], 2)
        upserts = [(s, p) for s, p in conn.all_sql
                   if "INSERT INTO en_set_gap" in s]
        _, params = upserts[0]
        # Sorted numerically, not lexically (so '10' comes after '2').
        self.assertEqual(json.loads(params[3]), ["2", "3"])

    def test_extra_numbers_recorded(self):
        # cards_master has a number TCGdex doesn't list — usually
        # an alias drift bug worth surfacing.
        rv, conn = self._run(
            [("sv2", ["1", "2"])],
            [("sv2", ["1", "2", "999"])],
        )
        self.assertEqual(rv["total_extra"], 1)
        upserts = [(s, p) for s, p in conn.all_sql
                   if "INSERT INTO en_set_gap" in s]
        _, params = upserts[0]
        self.assertEqual(json.loads(params[4]), ["999"])

    def test_only_canonical_sets_audited_no_phantom_rows(self):
        # cards_master has rows for set 'bw1' that TCGdex doesn't
        # know about. Phantom 'bw1: 0 expected, N extra' rows would
        # be noise that doesn't belong in an EN coverage report —
        # they'd come from a non-EN spine row.
        rv, conn = self._run(
            [("sv2", ["1"])],
            [("sv2", ["1"])],   # only canonical sets queried
        )
        self.assertEqual(rv["sets_audited"], 1)
        # The cards_master SELECT must be parameterised with the
        # canonical set list, not unconstrained.
        selects = [(s, p) for s, p in conn.all_sql
                   if "FROM cards_master" in s]
        self.assertEqual(len(selects), 1)
        _, params = selects[0]
        self.assertEqual(params[0], ["sv2"])

    def test_set_with_no_cards_master_rows_marks_all_missing(self):
        rv, conn = self._run(
            [("sv9", ["1", "2", "3"])],
            [],   # no cards_master rows for sv9
        )
        self.assertEqual(rv["sets_audited"], 1)
        self.assertEqual(rv["total_missing"], 3)
        upserts = [(s, p) for s, p in conn.all_sql
                   if "INSERT INTO en_set_gap" in s]
        _, params = upserts[0]
        self.assertEqual(params[1], 3)            # expected_count
        self.assertEqual(params[2], 0)            # actual_count
        self.assertEqual(json.loads(params[3]), ["1", "2", "3"])  # all missing

    def test_multiple_sets_one_upsert_per_set_one_commit(self):
        rv, conn = self._run(
            [("sv1", ["1", "2"]), ("sv2", ["1"])],
            [("sv1", ["1", "2"]), ("sv2", [])],
        )
        self.assertEqual(rv["sets_audited"], 2)
        self.assertEqual(rv["total_missing"], 1)  # sv2 missing '1'
        upserts = [(s, p) for s, p in conn.all_sql
                   if "INSERT INTO en_set_gap" in s]
        self.assertEqual(len(upserts), 2)
        # Exactly ONE commit at the end — not one per set.
        self.assertEqual(conn.commits, 1)

    def test_uses_upsert_not_truncate_or_delete(self):
        # Important contract: we UPSERT, we don't TRUNCATE. A failed
        # audit on a sub-set shouldn't wipe the whole table.
        rv, conn = self._run(
            [("sv2", ["1"])],
            [("sv2", ["1"])],
        )
        self.assertFalse(any("TRUNCATE" in s.upper()
                             for s, _ in conn.all_sql))
        self.assertFalse(any("DELETE FROM en_set_gap" in s
                             for s, _ in conn.all_sql))

    def test_normalisation_aligns_canonical_and_actual(self):
        # Canonical comes back as "001" (zero-padded TCGdex localId);
        # actual comes back as "1" (cards_master stripped form).
        # Without normalisation the diff would be {missing:["001"],
        # extra:["1"]} — a phantom 100% miss. With normalisation
        # both sides are "1" and the set is complete.
        rv, conn = self._run(
            [("sv2", ["001", "002"])],
            [("sv2", ["1", "2"])],
        )
        self.assertEqual(rv["total_missing"], 0)
        self.assertEqual(rv["total_extra"], 0)


# ── DDL contract ─────────────────────────────────────────────────


class DDLContractTests(unittest.TestCase):

    def test_ddl_registered_in_schema(self):
        from unified import schema
        self.assertIn("en_set_gap",
                      [name for name, _ in schema._ALL_DDL])
        ddl = schema.DDL_EN_SET_GAP
        for col in ("set_id", "expected_count", "actual_count",
                    "missing_numbers", "extra_numbers", "audited_at"):
            self.assertIn(col, ddl, f"missing column {col} in DDL")
        # PK on set_id so the worker's UPSERT works.
        self.assertIn("set_id           TEXT PRIMARY KEY", ddl)
        # Index on audited_at for "show me the latest gap report".
        self.assertIn("idx_en_set_gap_audited", ddl)

    def test_worker_registered_in_run_dispatcher(self):
        # The operator's CLI (`workers/run.py en_set_audit`) must
        # be wired up. If this assertion fires, `EnSetAuditWorker`
        # was added but the WORKERS dict wasn't updated.
        #
        # Inspect the source rather than importing workers/run.py:
        # that module imports psycopg2 which is only present on the
        # Pi container, not in dev-machine test envs.
        run_src = (ROOT / "workers" / "run.py").read_text(encoding="utf-8")
        self.assertIn(
            "from workers.en_set_audit import EnSetAuditWorker",
            run_src,
            "EnSetAuditWorker import missing from workers/run.py",
        )
        self.assertIn(
            '"en_set_audit":    EnSetAuditWorker',
            run_src,
            "en_set_audit not registered in WORKERS dict",
        )


if __name__ == "__main__":
    unittest.main()
