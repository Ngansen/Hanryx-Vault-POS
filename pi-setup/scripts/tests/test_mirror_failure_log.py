"""
Tests for scripts/mirror_failure_log.record_mirror_outcome.

Hermetic — no postgres, no network. The helper's contract is small
enough that a hand-rolled FakeConn covers every branch:

  * inserted     — first failure for a URL
  * incremented  — repeat failure (ON CONFLICT path)
  * resolved     — success after a recorded failure
  * noop         — success for a URL we never recorded
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import mirror_failure_log as mfl


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0
        self.executed: list[tuple[str, object]] = []
        self._next_fetchone: object = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self.conn.all_sql.append((sql, params))
        # The fixture controls rowcount and fetchone via queues so
        # each test can drive whatever response shape it wants.
        if self.conn.rowcount_queue:
            self.rowcount = self.conn.rowcount_queue.pop(0)
        if self.conn.fetchone_queue:
            self._next_fetchone = self.conn.fetchone_queue.pop(0)
        else:
            self._next_fetchone = None

    def fetchone(self):
        return self._next_fetchone


class FakeConn:
    def __init__(self):
        self.commits = 0
        self.cursors: list[FakeCursor] = []
        self.all_sql: list[tuple[str, object]] = []
        self.rowcount_queue: list[int] = []
        # Items here are returned by the NEXT execute() that happens
        # to be the upsert (so we can simulate the RETURNING clause).
        self.fetchone_queue: list[object] = []

    def cursor(self):
        c = FakeCursor(self)
        self.cursors.append(c)
        return c

    def commit(self):
        self.commits += 1


class RecordOutcomeFailureTests(unittest.TestCase):
    """Failure path: must INSERT … ON CONFLICT DO UPDATE with the
    +1 attempt-count semantics, force-clear resolved_at, and use the
    RETURNING (xmax=0) trick to discriminate insert from conflict."""

    def test_first_failure_inserts(self):
        conn = FakeConn()
        # RETURNING (xmax=0) → True for a real INSERT
        conn.fetchone_queue = [(True,)]
        rv = mfl.record_mirror_outcome(
            conn, url="https://cdn.example/a.jpg", src="tcgo",
            dest_path="/mnt/cards/a.jpg",
            ok=False, status="http-404", now=1_700_000_000,
        )
        self.assertEqual(rv, "inserted")
        self.assertEqual(conn.commits, 1)
        sql, params = conn.all_sql[0]
        self.assertIn("INSERT INTO mirror_fetch_failure", sql)
        self.assertIn("ON CONFLICT (url) DO UPDATE", sql)
        # Force-clear semantics — re-broken URL must drop out of
        # the unresolved-only filter immediately.
        self.assertIn("resolved_at     = NULL", sql)
        # Increment, not overwrite.
        self.assertIn("attempt_count   = mirror_fetch_failure.attempt_count + 1",
                      sql)
        # Param order matches the VALUES clause.
        self.assertEqual(params[0], "https://cdn.example/a.jpg")
        self.assertEqual(params[1], "tcgo")
        self.assertEqual(params[2], "/mnt/cards/a.jpg")
        self.assertEqual(params[3], "http-404")
        self.assertEqual(params[4], 1_700_000_000)  # first_seen_at
        self.assertEqual(params[5], 1_700_000_000)  # last_attempt_at

    def test_repeat_failure_increments(self):
        conn = FakeConn()
        # RETURNING (xmax=0) → False on a conflict-driven UPDATE
        conn.fetchone_queue = [(False,)]
        rv = mfl.record_mirror_outcome(
            conn, url="https://cdn.example/a.jpg", src="tcgo",
            dest_path="/mnt/cards/a.jpg",
            ok=False, status="err-URLError",
        )
        self.assertEqual(rv, "incremented")
        self.assertEqual(conn.commits, 1)

    def test_failure_uses_wallclock_when_now_omitted(self):
        conn = FakeConn()
        conn.fetchone_queue = [(True,)]
        mfl.record_mirror_outcome(
            conn, url="u", src="s", dest_path="/d",
            ok=False, status="http-500",
        )
        _, params = conn.all_sql[0]
        # Should be a sane epoch second value (not the obvious sentinel
        # 0 or a pre-2024 timestamp).
        self.assertIsInstance(params[4], int)
        self.assertGreater(params[4], 1_700_000_000)
        self.assertLess(params[4], 4_000_000_000)
        # first_seen_at == last_attempt_at on first sight
        self.assertEqual(params[4], params[5])


class RecordOutcomeSuccessTests(unittest.TestCase):
    """Success path: must UPDATE only — NEVER insert a success-only
    row. We don't want to balloon the table to 120k rows on a clean
    Phase C run when the table only needs to track URLs that have
    failed at least once."""

    def test_success_with_no_prior_failure_is_noop(self):
        conn = FakeConn()
        # rowcount=0 means the UPDATE matched zero rows
        conn.rowcount_queue = [0]
        rv = mfl.record_mirror_outcome(
            conn, url="https://cdn.example/clean.jpg", src="scry",
            dest_path="/mnt/cards/clean.jpg",
            ok=True, status="ok",
        )
        self.assertEqual(rv, "noop")
        self.assertEqual(conn.commits, 1)
        sql, params = conn.all_sql[0]
        # Crucially: no INSERT executed for clean URLs
        self.assertEqual(len(conn.all_sql), 1)
        self.assertNotIn("INSERT", sql.upper())
        self.assertIn("UPDATE mirror_fetch_failure", sql)
        self.assertIn("WHERE url = %s", sql)
        self.assertIn("AND resolved_at IS NULL", sql)

    def test_success_with_prior_failure_marks_resolved(self):
        conn = FakeConn()
        conn.rowcount_queue = [1]  # a previously-failed row matched
        rv = mfl.record_mirror_outcome(
            conn, url="https://cdn.example/recovered.jpg", src="tcgo",
            dest_path="/mnt/cards/recovered.jpg",
            ok=True, status="ok", now=1_700_001_000,
        )
        self.assertEqual(rv, "resolved")
        sql, params = conn.all_sql[0]
        # resolved_at, last_status, last_attempt_at, url
        self.assertEqual(params, (1_700_001_000, "ok",
                                  1_700_001_000,
                                  "https://cdn.example/recovered.jpg"))

    def test_skip_exists_counts_as_success(self):
        # `_download` returns "skip-exists" when the file is already
        # on disk and big enough — that's a success, not a failure.
        conn = FakeConn()
        conn.rowcount_queue = [0]
        rv = mfl.record_mirror_outcome(
            conn, url="u", src="s", dest_path="/d",
            ok=True, status="skip-exists",
        )
        self.assertEqual(rv, "noop")

    def test_not_modified_counts_as_success(self):
        # Slice 22 will add HTTP If-Modified-Since / 304 handling;
        # the success-status set already includes 'not-modified' so
        # downstream code is forward-compatible.
        conn = FakeConn()
        conn.rowcount_queue = [0]
        rv = mfl.record_mirror_outcome(
            conn, url="u", src="s", dest_path="/d",
            ok=True, status="not-modified",
        )
        self.assertEqual(rv, "noop")


class IsSuccessStatusTests(unittest.TestCase):
    def test_success_set(self):
        for s in ("ok", "skip-exists", "not-modified"):
            self.assertTrue(mfl.is_success_status(s),
                            f"{s!r} should be a success")

    def test_failure_strings(self):
        for s in ("http-404", "http-500", "too-small",
                  "err-URLError", "err-TimeoutError", ""):
            self.assertFalse(mfl.is_success_status(s),
                             f"{s!r} should NOT be a success")


class TableContractTests(unittest.TestCase):
    """Sanity checks that the helper SQL matches the DDL columns
    declared in unified/schema.py — caught by import time, no
    database needed."""

    def test_ddl_declares_expected_columns(self):
        from unified import schema
        ddl = schema.DDL_MIRROR_FETCH_FAILURE
        for col in ("url", "src", "dest_path", "last_status",
                    "attempt_count", "first_seen_at",
                    "last_attempt_at", "resolved_at"):
            self.assertIn(col, ddl, f"missing column {col!r} in DDL")
        self.assertIn("PRIMARY KEY", ddl)
        self.assertIn("idx_mirror_fetch_failure_unresolved", ddl)
        # Partial index keeps unresolved triage cheap on a giant table.
        self.assertIn("WHERE resolved_at IS NULL", ddl)

    def test_ddl_registered_in_init(self):
        from unified import schema
        names = [name for name, _ in schema._ALL_DDL]
        self.assertIn("mirror_fetch_failure", names)


if __name__ == "__main__":
    unittest.main()
