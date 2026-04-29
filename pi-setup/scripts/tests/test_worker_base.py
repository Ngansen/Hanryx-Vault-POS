#!/usr/bin/env python3
"""
test_worker_base.py — unit tests for the Slice 9 background worker
framework defined in pi-setup/workers/base.py.

Coverage:
  * Worker subclass without TASK_TYPE raises ValueError on construct
  * worker_id defaults to '<hostname>:<pid>' when not provided
  * batch_size kwarg overrides class BATCH_SIZE
  * enqueue: emits INSERT … ON CONFLICT DO NOTHING with the right
    task_type / task_key / payload / priority / max_attempts
  * claim_batch:
      - SQL contains 'FOR UPDATE SKIP LOCKED' (no-double-claim invariant)
      - SQL contains 'attempts < max_attempts' so exhausted tasks are
        excluded from the claim
      - SQL filters to PENDING + sets CLAIMED + bumps attempts
      - tuple-row result is normalised to {'task_id', 'task_type',
        'task_key', 'payload', 'attempts'}
      - dict-row result is also normalised (RealDictCursor compat)
  * complete: sets DONE + completed_at + clears last_error
  * fail(permanent=True): forces attempts up to max_attempts and FAILED
  * fail(permanent=False): SQL CASE leaves PENDING until exhausted
  * reap_stale: SQL has correct cutoff + only touches CLAIMED rows
  * run_once: calls reap_stale → claim → process → complete; records
    a run in bg_worker_run with correct counts
  * run_once: WorkerError → permanent fail (single fail() call with True)
  * run_once: bare Exception → transient fail (single fail() call with False)
  * run_once: empty claim → still records a run with 0/0/0
  * run_forever: max_idle_passes=N exits after N consecutive empty passes
  * run_forever: items reset the idle counter
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PI_SETUP = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PI_SETUP))

from workers.base import Worker, WorkerError, DEFAULT_CLAIM_TIMEOUT_S  # noqa: E402


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
        # Pop next rowcount the conn has queued.
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
        self.cursors: list[FakeCursor] = []
        self.all_sql: list[str] = []
        self.fetchone_queue: list[object] = []
        self.fetchall_queue: list[list] = []
        self.rowcount_queue: list[int] = []

    def cursor(self):
        c = FakeCursor(self)
        self.cursors.append(c)
        return c

    def commit(self):
        self.commits += 1


class _ConcreteWorker(Worker):
    """Minimal subclass for testing the framework. Tracks process()
    calls and can be configured to raise."""
    TASK_TYPE = "test_worker"
    BATCH_SIZE = 5

    def __init__(self, conn, raise_on=None, **kw):
        super().__init__(conn, **kw)
        self.processed: list[dict] = []
        self.raise_on = raise_on or {}   # task_id -> Exception

    def process(self, task):
        self.processed.append(task)
        if task["task_id"] in self.raise_on:
            raise self.raise_on[task["task_id"]]
        return {"ok": True}


class _NoTypeWorker(Worker):
    TASK_TYPE = ""
    def process(self, task):  # pragma: no cover
        return None


# ── Tests ────────────────────────────────────────────────────────


class WorkerConstructionTests(unittest.TestCase):

    def test_missing_task_type_raises(self):
        with self.assertRaises(ValueError):
            _NoTypeWorker(FakeConn())

    def test_default_worker_id_has_pid(self):
        w = _ConcreteWorker(FakeConn())
        self.assertIn(":", w.worker_id)
        # Must be parseable as host:pid
        host, pid = w.worker_id.rsplit(":", 1)
        self.assertTrue(host)
        self.assertTrue(pid.isdigit())

    def test_explicit_worker_id_used(self):
        w = _ConcreteWorker(FakeConn(), worker_id="test-id-7")
        self.assertEqual(w.worker_id, "test-id-7")

    def test_batch_size_override(self):
        w = _ConcreteWorker(FakeConn(), batch_size=42)
        self.assertEqual(w.BATCH_SIZE, 42)

    def test_default_claim_timeout(self):
        # Confirm the documented default — protection against
        # accidental refactors.
        self.assertEqual(DEFAULT_CLAIM_TIMEOUT_S, 600)


class EnqueueTests(unittest.TestCase):

    def test_enqueue_emits_on_conflict(self):
        conn = FakeConn()
        # Insert returns one row → newly inserted.
        conn.fetchone_queue = [(101,)]
        w = _ConcreteWorker(conn)
        rv = w.enqueue("set/01", {"hello": "world"},
                       priority=50, max_attempts=7)
        self.assertTrue(rv)
        sql, params = conn.cursors[0].executed[0]
        self.assertIn("INSERT INTO bg_task_queue", sql)
        self.assertIn("ON CONFLICT (task_type, task_key) DO NOTHING", sql)
        self.assertEqual(params[0], "test_worker")
        self.assertEqual(params[1], "set/01")
        self.assertIn('"hello"', params[2])  # JSON-encoded
        self.assertEqual(params[3], 50)
        self.assertEqual(params[4], 7)

    def test_enqueue_returns_false_on_conflict(self):
        conn = FakeConn()
        conn.fetchone_queue = [None]   # nothing returned → already exists
        w = _ConcreteWorker(conn)
        self.assertFalse(w.enqueue("set/01"))


class ClaimBatchTests(unittest.TestCase):

    def test_claim_sql_invariants(self):
        conn = FakeConn()
        conn.fetchall_queue = [[]]
        w = _ConcreteWorker(conn)
        w.claim_batch(10)
        sql = conn.cursors[0].executed[0][0]
        # The four invariants we care about most:
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)
        self.assertIn("attempts  < max_attempts", sql)
        self.assertIn("status    = 'PENDING'", sql)
        self.assertIn("'CLAIMED'", sql)

    def test_claim_normalises_tuple_rows(self):
        conn = FakeConn()
        conn.fetchall_queue = [[
            (7, "test_worker", "k1", {"a": 1}, 0),
            (8, "test_worker", "k2", '{"b": 2}', 1),  # JSON string payload
        ]]
        w = _ConcreteWorker(conn)
        out = w.claim_batch(2)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["task_id"], 7)
        self.assertEqual(out[0]["task_key"], "k1")
        self.assertEqual(out[0]["payload"], {"a": 1})
        self.assertEqual(out[0]["attempts"], 0)
        # JSON-string payload is decoded
        self.assertEqual(out[1]["payload"], {"b": 2})

    def test_claim_normalises_dict_rows(self):
        conn = FakeConn()
        conn.fetchall_queue = [[
            {"task_id": 9, "task_type": "test_worker",
             "task_key": "k3", "payload": {"x": 99}, "attempts": 2},
        ]]
        w = _ConcreteWorker(conn)
        out = w.claim_batch(1)
        self.assertEqual(out[0]["task_id"], 9)
        self.assertEqual(out[0]["payload"], {"x": 99})

    def test_claim_commits(self):
        conn = FakeConn()
        conn.fetchall_queue = [[]]
        w = _ConcreteWorker(conn)
        w.claim_batch(1)
        self.assertEqual(conn.commits, 1)


class CompleteFailTests(unittest.TestCase):

    def test_complete(self):
        conn = FakeConn()
        w = _ConcreteWorker(conn)
        w.complete(123)
        sql, params = conn.cursors[0].executed[0]
        self.assertIn("status       = 'DONE'", sql)
        self.assertIn("last_error   = ''", sql)
        self.assertEqual(params[1], 123)
        self.assertEqual(conn.commits, 1)

    def test_fail_permanent_forces_failed(self):
        conn = FakeConn()
        w = _ConcreteWorker(conn)
        w.fail(123, "boom", permanent=True)
        sql, _params = conn.cursors[0].executed[0]
        self.assertIn("status       = 'FAILED'", sql)
        self.assertIn("attempts     = max_attempts", sql)

    def test_fail_transient_uses_case(self):
        conn = FakeConn()
        w = _ConcreteWorker(conn)
        w.fail(123, "transient", permanent=False)
        sql, _params = conn.cursors[0].executed[0]
        # Transient failures must NOT force FAILED — they leave room
        # for retry until attempts >= max_attempts.
        self.assertIn("CASE", sql)
        self.assertIn("WHEN attempts >= max_attempts", sql)
        self.assertIn("THEN 'FAILED'", sql)
        self.assertIn("ELSE 'PENDING'", sql)

    def test_fail_truncates_long_error(self):
        conn = FakeConn()
        w = _ConcreteWorker(conn)
        w.fail(1, "x" * 5000, permanent=False)
        _sql, params = conn.cursors[0].executed[0]
        # last_error param is index 1 in the transient branch.
        # The exact index depends on the SQL but the string must
        # have been truncated to <= 4000 chars.
        self.assertTrue(any(isinstance(p, str) and len(p) == 4000
                            for p in params))


class ReapStaleTests(unittest.TestCase):

    def test_reap_uses_claim_timeout(self):
        conn = FakeConn()
        conn.rowcount_queue = [3]
        w = _ConcreteWorker(conn)
        w.CLAIM_TIMEOUT_S = 100
        n = w.reap_stale()
        self.assertEqual(n, 3)
        sql, params = conn.cursors[0].executed[0]
        self.assertIn("status     = 'PENDING'", sql)
        self.assertIn("status    = 'CLAIMED'", sql)
        # task_type, cutoff
        self.assertEqual(params[0], "test_worker")
        # cutoff = now - 100; param is an int well below current epoch
        import time as _t
        self.assertLess(params[1], int(_t.time()))


class RunOnceTests(unittest.TestCase):

    def _setup_run(self, conn, claimed_rows, run_id=42):
        # Order of cursor() / fetch* calls in run_once:
        #   1. reap_stale         — UPDATE, rowcount popped (none queued = 0)
        #   2. _record_run_start  — INSERT … RETURNING run_id (fetchone)
        #   3. claim_batch        — UPDATE … RETURNING (fetchall)
        #   4. (per task) process → complete OR fail (UPDATE)
        #   5. _record_run_end    — UPDATE
        conn.rowcount_queue = [0]            # reap_stale: 0 reaped
        conn.fetchone_queue = [(run_id,)]    # _record_run_start
        conn.fetchall_queue = [claimed_rows] # claim_batch result

    def test_happy_path(self):
        conn = FakeConn()
        rows = [(1, "test_worker", "a", {}, 0),
                (2, "test_worker", "b", {}, 0)]
        self._setup_run(conn, rows)
        w = _ConcreteWorker(conn)
        stats = w.run_once()
        self.assertEqual(stats["claimed"], 2)
        self.assertEqual(stats["ok"], 2)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(len(w.processed), 2)
        # Final SQL must be the run-end UPDATE
        last_sql = conn.all_sql[-1]
        self.assertIn("UPDATE bg_worker_run", last_sql)
        self.assertIn("items_ok", last_sql)

    def test_worker_error_is_permanent(self):
        conn = FakeConn()
        rows = [(1, "test_worker", "a", {}, 0)]
        self._setup_run(conn, rows)
        w = _ConcreteWorker(conn,
                            raise_on={1: WorkerError("bad payload")})
        stats = w.run_once()
        self.assertEqual(stats["ok"], 0)
        self.assertEqual(stats["failed"], 1)
        # Must contain a permanent fail SQL
        joined = "\n".join(conn.all_sql)
        self.assertIn("attempts     = max_attempts", joined)

    def test_generic_exception_is_transient(self):
        conn = FakeConn()
        rows = [(1, "test_worker", "a", {}, 0)]
        self._setup_run(conn, rows)
        w = _ConcreteWorker(conn,
                            raise_on={1: RuntimeError("transient")})
        stats = w.run_once()
        self.assertEqual(stats["failed"], 1)
        joined = "\n".join(conn.all_sql)
        # Transient branch uses CASE … ELSE 'PENDING'
        self.assertIn("ELSE 'PENDING'", joined)

    def test_empty_claim_still_records_run(self):
        conn = FakeConn()
        self._setup_run(conn, [])
        w = _ConcreteWorker(conn)
        stats = w.run_once()
        self.assertEqual(stats["claimed"], 0)
        # Both run_start INSERT and run_end UPDATE must be present.
        joined = "\n".join(conn.all_sql)
        self.assertIn("INSERT INTO bg_worker_run", joined)
        self.assertIn("UPDATE bg_worker_run", joined)


class RunForeverTests(unittest.TestCase):

    def test_max_idle_exits(self):
        # Three back-to-back empty passes; max_idle=2 should exit
        # after the second one.
        conn = FakeConn()
        # Each run_once needs: reap rowcount + run_start fetchone + claim fetchall
        conn.rowcount_queue = [0, 0, 0]
        conn.fetchone_queue = [(1,), (2,), (3,)]
        conn.fetchall_queue = [[], [], []]

        w = _ConcreteWorker(conn)
        w.IDLE_SLEEP_S = 0          # don't actually sleep
        totals = w.run_forever(max_idle_passes=2)
        self.assertEqual(totals["passes"], 2)
        self.assertEqual(totals["claimed"], 0)

    def test_items_reset_idle_counter(self):
        # Pattern: empty, populated, empty, empty → with max_idle=2
        # we should exit on the 4th pass (2 consecutive empties at
        # the end) for a total of 4 passes.
        conn = FakeConn()
        conn.rowcount_queue = [0, 0, 0, 0]
        conn.fetchone_queue = [(1,), (2,), (3,), (4,)]
        conn.fetchall_queue = [
            [],
            [(10, "test_worker", "k", {}, 0)],
            [],
            [],
        ]
        w = _ConcreteWorker(conn)
        w.IDLE_SLEEP_S = 0
        totals = w.run_forever(max_idle_passes=2)
        self.assertEqual(totals["passes"], 4)
        self.assertEqual(totals["claimed"], 1)
        self.assertEqual(totals["ok"], 1)


if __name__ == "__main__":
    unittest.main()
