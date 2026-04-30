"""
Tests for workers/cards_master_audit.

Hermetic: no DB, no network. Mirrors the en_set_audit FakeConn pattern
but with a fetchone_queue too (the cards_master audit uses fetchone()
for the cheap LIMIT-1 probe and for each COUNT(*) query, fetchall()
only for the bounded sample queries).
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workers import cards_master_audit
from workers.cards_master_audit import (
    CardsMasterAuditWorker, SAMPLE_LIMIT, VIOLATIONS, _has_any_rows,
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

    def fetchone(self):
        if self.conn.fetchone_queue:
            return self.conn.fetchone_queue.pop(0)
        return None


class FakeConn:
    def __init__(self):
        self.commits = 0
        self.cursors: list[FakeCursor] = []
        self.all_sql: list[tuple[str, object]] = []
        self.fetchall_queue: list[list] = []
        self.fetchone_queue: list[object] = []
        self.rowcount_queue: list[int] = []

    def cursor(self):
        c = FakeCursor(self)
        self.cursors.append(c)
        return c

    def commit(self):
        self.commits += 1


# ── VIOLATIONS contract ──────────────────────────────────────────


class ViolationsContractTests(unittest.TestCase):
    """The audit only matters if the violation list itself is right.
    These tests guard the literal SQL shape so a refactor can't
    silently weaken what we audit."""

    def test_four_violation_types_registered(self):
        types = [v[0] for v in VIOLATIONS]
        self.assertEqual(sorted(types), [
            "all_names_blank",
            "duplicate_identity",
            "no_card_number",
            "no_set_id",
        ])

    def test_all_names_blank_checks_all_five_user_facing_cols(self):
        v = next(x for x in VIOLATIONS if x[0] == "all_names_blank")
        # Both the count and sample SQL must check the SAME 5 columns
        # — drift between them would misreport.
        for sql in (v[1], v[2]):
            for col in ("name_en", "name_kr", "name_jp",
                        "name_chs", "name_cht"):
                self.assertIn(f"{col}", sql,
                              f"{col} missing from all_names_blank")
            # Critically does NOT check fr/de/it/es — those are
            # back-of-house extras, not the resolver's surface.
            for unrelated in ("name_fr", "name_de", "name_it", "name_es"):
                self.assertNotIn(unrelated, sql,
                                 f"{unrelated} should not gate the violation")

    def test_no_set_id_predicate_is_empty_string_not_null(self):
        # The column is NOT NULL DEFAULT '' so the audit must check
        # for empty string, not IS NULL — otherwise it'd silently
        # report 0 forever.
        v = next(x for x in VIOLATIONS if x[0] == "no_set_id")
        self.assertIn("set_id = ''", v[1])
        self.assertIn("set_id = ''", v[2])

    def test_no_card_number_predicate_is_empty_string_not_null(self):
        v = next(x for x in VIOLATIONS if x[0] == "no_card_number")
        self.assertIn("card_number = ''", v[1])
        self.assertIn("card_number = ''", v[2])

    def test_duplicate_identity_groups_by_unique_constraint_columns(self):
        v = next(x for x in VIOLATIONS if x[0] == "duplicate_identity")
        # GROUP BY must match the cards_master UNIQUE constraint
        # (set_id, card_number, variant_code) — otherwise the canary
        # is checking the wrong invariant.
        for sql in (v[1], v[2]):
            self.assertIn("set_id", sql)
            self.assertIn("card_number", sql)
            self.assertIn("variant_code", sql)
            self.assertIn("HAVING COUNT(*) > 1", sql)

    def test_sample_limit_is_twenty(self):
        # Limit small so JSONB doesn't bloat the gap row, but big
        # enough for the operator to spot a pattern.
        self.assertEqual(SAMPLE_LIMIT, 20)


# ── _has_any_rows ────────────────────────────────────────────────


class HasAnyRowsTests(unittest.TestCase):

    def test_returns_true_when_probe_returns_a_row(self):
        conn = FakeConn()
        conn.fetchone_queue = [(1,)]
        self.assertTrue(_has_any_rows(conn.cursor()))

    def test_returns_false_when_probe_returns_none(self):
        conn = FakeConn()
        conn.fetchone_queue = [None]
        self.assertFalse(_has_any_rows(conn.cursor()))

    def test_uses_limit_one_not_full_count(self):
        # Critical: a fresh-Pi probe must NOT do COUNT(*) over the
        # whole table. cards_master can be 100k+ rows on a populated
        # Pi and the audit runs every 5 minutes when idle.
        conn = FakeConn()
        conn.fetchone_queue = [(1,)]
        _has_any_rows(conn.cursor())
        sql, _ = conn.all_sql[0]
        self.assertIn("LIMIT 1", sql)
        self.assertNotIn("COUNT(*)", sql.upper())


# ── seed ─────────────────────────────────────────────────────────


class SeedTests(unittest.TestCase):

    def test_seed_uses_today_date_as_task_key(self):
        conn = FakeConn()
        conn.rowcount_queue = [1]
        w = CardsMasterAuditWorker(conn, today_fn=lambda: "2026-04-30")
        n = w.seed()
        self.assertEqual(n, 1)
        sql, params = conn.cursors[0].executed[0]
        self.assertIn("INSERT INTO bg_task_queue", sql)
        self.assertIn("'cards_master_audit'", sql)
        self.assertIn("ON CONFLICT (task_type, task_key) DO NOTHING", sql)
        self.assertEqual(params[0], "2026-04-30")
        self.assertEqual(conn.commits, 1)

    def test_seed_idempotent_when_row_already_present(self):
        # ON CONFLICT DO NOTHING returns rowcount=0 when today's
        # task already exists. The orchestrator polls every 5 min;
        # we must not silently double-enqueue.
        conn = FakeConn()
        conn.rowcount_queue = [0]
        w = CardsMasterAuditWorker(conn, today_fn=lambda: "2026-04-30")
        self.assertEqual(w.seed(), 0)


# ── process — short circuit ──────────────────────────────────────


class ProcessShortCircuitTests(unittest.TestCase):

    def test_empty_cards_master_returns_empty_source_no_writes(self):
        # Fresh Pi, no importer has run. Refusing to audit beats
        # writing 4 zeros to cards_master_gap — the dashboard would
        # then claim "0 violations of every kind" which is misleading.
        conn = FakeConn()
        conn.fetchone_queue = [None]   # _has_any_rows → False
        w = CardsMasterAuditWorker(conn)
        rv = w.process({"task_id": 1, "payload": {}})
        self.assertEqual(rv["status"], "EMPTY_SOURCE")
        self.assertEqual(rv["violations_audited"], 0)
        # No INSERT into cards_master_gap should have fired.
        self.assertFalse(any("cards_master_gap" in s for s, _ in conn.all_sql))
        self.assertEqual(conn.commits, 0)


# ── process — happy paths ────────────────────────────────────────


class ProcessHappyPathTests(unittest.TestCase):

    def _run_with_counts(self, counts: dict, samples: dict | None = None):
        """Drive process() with per-violation-type count and sample
        responses. counts maps violation_type → integer; samples maps
        violation_type → list of fake cursor rows."""
        samples = samples or {}
        conn = FakeConn()
        # Probe first.
        conn.fetchone_queue = [(1,)]
        # Then for each violation in registration order: COUNT first
        # (fetchone), then optionally samples (fetchall) iff count>0.
        for vtype, _, _, _ in VIOLATIONS:
            conn.fetchone_queue.append((counts.get(vtype, 0),))
            if counts.get(vtype, 0) > 0:
                conn.fetchall_queue.append(samples.get(vtype, []))
        w = CardsMasterAuditWorker(conn)
        rv = w.process({"task_id": 1, "payload": {}})
        return rv, conn

    def test_all_zero_violations_writes_one_row_per_type(self):
        rv, conn = self._run_with_counts({})
        self.assertEqual(rv["status"], "OK")
        self.assertEqual(rv["violations_audited"], 4)
        self.assertEqual(rv["total_violations"], 0)
        upserts = [(s, p) for s, p in conn.all_sql
                   if "INSERT INTO cards_master_gap" in s]
        self.assertEqual(len(upserts), 4,
                         "every violation_type must get a row, even at 0 — "
                         "that's how the dashboard renders 'currently clean'")
        # Each upsert row at count=0 should also have an empty sample list
        # — never carry stale samples forward.
        for _, params in upserts:
            self.assertEqual(params[1], 0)        # violation_count
            self.assertEqual(json.loads(params[2]), [])
        self.assertEqual(conn.commits, 1)

    def test_violations_sample_keys_recorded_with_master_id(self):
        rv, conn = self._run_with_counts(
            counts={"all_names_blank": 3,
                    "no_set_id": 0,
                    "no_card_number": 0,
                    "duplicate_identity": 0},
            samples={"all_names_blank": [(101,), (102,), (103,)]},
        )
        self.assertEqual(rv["status"], "OK")
        self.assertEqual(rv["total_violations"], 3)
        upserts = {p[0]: p for _, p in conn.all_sql
                   if isinstance(p, tuple) and len(p) >= 1
                   and p[0] in {v[0] for v in VIOLATIONS}}
        anb = upserts["all_names_blank"]
        self.assertEqual(anb[1], 3)
        # master_ids stored as ints, not stringified.
        self.assertEqual(json.loads(anb[2]), [101, 102, 103])

    def test_duplicate_identity_sample_key_is_pipe_joined_coords(self):
        # Composite key needs a different shape than the master_id
        # samples — the operator can't paste a master_id for a
        # duplicate, they need the (set, num, variant) coords.
        rv, conn = self._run_with_counts(
            counts={"duplicate_identity": 2,
                    "all_names_blank": 0,
                    "no_set_id": 0,
                    "no_card_number": 0},
            samples={"duplicate_identity": [
                ("sv2", "001", "STD"),
                ("sv2", "002", "RH"),
            ]},
        )
        upserts = {p[0]: p for _, p in conn.all_sql
                   if isinstance(p, tuple) and len(p) >= 1
                   and p[0] in {v[0] for v in VIOLATIONS}}
        dup = upserts["duplicate_identity"]
        self.assertEqual(json.loads(dup[2]),
                         ["sv2|001|STD", "sv2|002|RH"])

    def test_sample_query_only_runs_when_count_positive(self):
        # Optimization that matters at scale: skipping the sample
        # SELECT when count is 0 saves 4 round trips per audit on a
        # clean booth.
        rv, conn = self._run_with_counts(
            counts={"all_names_blank": 0, "no_set_id": 5,
                    "no_card_number": 0, "duplicate_identity": 0},
            samples={"no_set_id": [(7,), (8,)]},
        )
        sample_selects = [s for s, _ in conn.all_sql
                          if "ORDER BY master_id" in s
                          or "ORDER BY set_id, card_number" in s]
        self.assertEqual(len(sample_selects), 1,
                         "exactly one sample SELECT — the one with count>0")

    def test_sample_query_passes_sample_limit_as_param(self):
        rv, conn = self._run_with_counts(
            counts={"all_names_blank": 50, "no_set_id": 0,
                    "no_card_number": 0, "duplicate_identity": 0},
            samples={"all_names_blank": [(i,) for i in range(20)]},
        )
        sample_calls = [(s, p) for s, p in conn.all_sql
                        if "ORDER BY master_id" in s and "LIMIT %s" in s]
        self.assertEqual(len(sample_calls), 1)
        _, params = sample_calls[0]
        self.assertEqual(params, (SAMPLE_LIMIT,))

    def test_upsert_uses_on_conflict_violation_type(self):
        rv, conn = self._run_with_counts({})
        upserts = [s for s, _ in conn.all_sql
                   if "INSERT INTO cards_master_gap" in s]
        for sql in upserts:
            self.assertIn("ON CONFLICT (violation_type) DO UPDATE", sql)

    def test_returns_per_type_breakdown_in_by_type(self):
        rv, conn = self._run_with_counts(
            counts={"all_names_blank": 7, "no_set_id": 2,
                    "no_card_number": 0, "duplicate_identity": 1},
            samples={"all_names_blank": [(1,)] * 7,
                     "no_set_id": [(9,), (10,)],
                     "duplicate_identity": [("sv1", "1", "STD")]},
        )
        self.assertEqual(rv["by_type"], {
            "all_names_blank": 7,
            "no_set_id": 2,
            "no_card_number": 0,
            "duplicate_identity": 1,
        })
        self.assertEqual(rv["total_violations"], 10)

    def test_one_commit_at_end_not_per_violation(self):
        # Atomic snapshot semantics — partial-failure shouldn't
        # leave half-stale gap rows.
        rv, conn = self._run_with_counts(
            counts={"all_names_blank": 1, "no_set_id": 1,
                    "no_card_number": 1, "duplicate_identity": 1},
            samples={"all_names_blank": [(1,)],
                     "no_set_id": [(2,)],
                     "no_card_number": [(3,)],
                     "duplicate_identity": [("sv1", "1", "STD")]},
        )
        self.assertEqual(conn.commits, 1)

    def test_no_truncate_or_delete_used(self):
        # UPSERT contract — no TRUNCATE so a midway crash can't
        # leave the table empty.
        rv, conn = self._run_with_counts({})
        for sql, _ in conn.all_sql:
            self.assertNotIn("TRUNCATE", sql.upper())
            self.assertNotIn("DELETE FROM cards_master_gap", sql)


# ── DDL contract ─────────────────────────────────────────────────


class DDLContractTests(unittest.TestCase):

    def test_ddl_registered_in_schema(self):
        from unified import schema
        self.assertIn("cards_master_gap",
                      [name for name, _ in schema._ALL_DDL])
        ddl = schema.DDL_CARDS_MASTER_GAP
        for col in ("violation_type", "violation_count",
                    "sample_keys", "audited_at"):
            self.assertIn(col, ddl, f"missing column {col} in DDL")
        # PK on violation_type so the worker's UPSERT works.
        self.assertIn("violation_type   TEXT PRIMARY KEY", ddl)
        # Index on audited_at for "show me the latest gap report".
        self.assertIn("idx_cards_master_gap_audited", ddl)

    def test_worker_registered_in_run_dispatcher(self):
        # The operator's CLI (`workers/run.py cards_master_audit`)
        # must be wired up. Inspect the source rather than importing
        # workers/run.py — that module imports psycopg2 which isn't
        # in dev-machine test envs.
        run_src = (ROOT / "workers" / "run.py").read_text(encoding="utf-8")
        self.assertIn(
            "from workers.cards_master_audit import CardsMasterAuditWorker",
            run_src,
            "CardsMasterAuditWorker import missing from workers/run.py",
        )
        self.assertIn(
            '"cards_master_audit": CardsMasterAuditWorker',
            run_src,
            "cards_master_audit not registered in WORKERS dict",
        )


if __name__ == "__main__":
    unittest.main()
