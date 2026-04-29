"""
Tests for workers/kr_set_audit.

Hermetic: no network, no real ptcg-kr-db checkout. We synthesise a
tiny repo on disk and feed it through the worker.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workers import kr_set_audit
from workers.kr_set_audit import (
    KrSetAuditWorker, _walk_canonical, _normalise_number, _num_sort_key,
)


# ── FakeConn / FakeCursor


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


# ── Tiny synthetic repo builder


def _make_repo(td: Path,
               pokemon: list[dict] | None = None,
               trainers: list[dict] | None = None,
               energy: list[dict] | None = None,
               include_garbage: bool = False) -> Path:
    """Synthesise a ptcg-kr-db-shaped checkout under td/repo."""
    repo = td / "repo"
    for sub, cards in (("pokemon", pokemon),
                       ("trainers", trainers),
                       ("energy", energy)):
        if cards is None:
            continue
        sd = repo / sub
        sd.mkdir(parents=True, exist_ok=True)
        for i, c in enumerate(cards):
            (sd / f"card_{i}.json").write_text(
                json.dumps(c, ensure_ascii=False), encoding="utf-8")
    if include_garbage:
        # Garbage file in pokemon/ — must be skipped, must not abort
        # the audit of the other 47 healthy files.
        gd = repo / "pokemon"
        gd.mkdir(parents=True, exist_ok=True)
        (gd / "broken.json").write_text("{not json", encoding="utf-8")
    return repo


def _card(prod_code: str, number: str, name: str = "테스트") -> dict:
    """Shape mirrors what's in ptcg-kr-db: top-level card with
    version_infos[] holding (prodCode, number)."""
    return {
        "id": f"{prod_code}-{number}",
        "name": name,
        "version_infos": [
            {"prodCode": prod_code, "number": number,
             "prodName": "Set", "rarity": "C"},
        ],
    }


# ── Helpers


class HelperTests(unittest.TestCase):

    def test_normalise_number_strips_leading_zeros(self):
        self.assertEqual(_normalise_number("001"), "1")
        self.assertEqual(_normalise_number("47"), "47")
        self.assertEqual(_normalise_number(" 010 "), "10")
        # Empty-or-zero collapses to '0' (matches importer).
        self.assertEqual(_normalise_number(""), "0")
        self.assertEqual(_normalise_number("000"), "0")

    def test_num_sort_key_numeric_then_alpha(self):
        nums = ["10", "2", "1", "PRE-001", "100", "9"]
        nums.sort(key=_num_sort_key)
        self.assertEqual(nums, ["1", "2", "9", "10", "100", "PRE-001"])


# ── _walk_canonical


class WalkCanonicalTests(unittest.TestCase):

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def test_missing_root_returns_empty(self):
        self.assertEqual(_walk_canonical(self.td / "absent"), {})

    def test_root_exists_but_no_subdirs_returns_empty(self):
        (self.td / "repo").mkdir()
        self.assertEqual(_walk_canonical(self.td / "repo"), {})

    def test_collects_all_three_subdirs(self):
        repo = _make_repo(
            self.td,
            pokemon=[_card("sv2", "47"), _card("sv2", "48")],
            trainers=[_card("sv2", "150")],
            energy=[_card("sv1", "200")],
        )
        result = _walk_canonical(repo)
        self.assertEqual(result, {"sv2": {"47", "48", "150"},
                                  "sv1": {"200"}})

    def test_lowercases_prod_code(self):
        # Upstream sometimes uses uppercase prodCode (e.g. "SV2K");
        # cards_master normalises to lowercase, so the audit must too.
        repo = _make_repo(self.td,
                          pokemon=[_card("SV2K", "1"),
                                   _card("Sv2K", "2")])
        result = _walk_canonical(repo)
        self.assertEqual(result, {"sv2k": {"1", "2"}})

    def test_normalises_card_numbers(self):
        # Same set, two cards written with different zero-padding —
        # canonical set must dedupe to a single number per real card.
        repo = _make_repo(self.td,
                          pokemon=[_card("sv2", "001"),
                                   _card("sv2", "1")])
        result = _walk_canonical(repo)
        self.assertEqual(result, {"sv2": {"1"}})

    def test_malformed_json_is_skipped(self):
        # The audit must keep going past one broken JSON file.
        repo = _make_repo(
            self.td,
            pokemon=[_card("sv2", "1"), _card("sv2", "2")],
            include_garbage=True,
        )
        result = _walk_canonical(repo)
        self.assertEqual(result, {"sv2": {"1", "2"}})

    def test_card_without_version_infos_skipped(self):
        repo = _make_repo(self.td, pokemon=[
            {"id": "x-1", "name": "no versions"},
            _card("sv2", "1"),
        ])
        # Skipped (no canonical mapping possible) — only the well-
        # formed sv2/1 entry survives.
        self.assertEqual(_walk_canonical(repo), {"sv2": {"1"}})

    def test_card_with_empty_prod_code_skipped(self):
        repo = _make_repo(self.td, pokemon=[
            _card("", "1"),       # no prodCode → silently skipped
            _card("sv2", "2"),
        ])
        self.assertEqual(_walk_canonical(repo), {"sv2": {"2"}})

    def test_array_or_object_root_both_accepted(self):
        # ptcg-kr-db's per-card files are arrays of reprints, but
        # some legacy files are bare objects. Both must work.
        repo = self.td / "repo"
        (repo / "pokemon").mkdir(parents=True)
        (repo / "pokemon" / "as_array.json").write_text(json.dumps(
            [_card("sv2", "1")]), encoding="utf-8")
        (repo / "pokemon" / "as_object.json").write_text(json.dumps(
            _card("sv2", "2")), encoding="utf-8")
        self.assertEqual(_walk_canonical(repo), {"sv2": {"1", "2"}})


# ── seed


class SeedTests(unittest.TestCase):

    def test_seed_uses_today_date_as_task_key(self):
        conn = FakeConn()
        conn.rowcount_queue = [1]
        w = KrSetAuditWorker(
            conn, kr_db_root=Path("/dev/null"),
            today_fn=lambda: "2026-04-29")
        n = w.seed()
        self.assertEqual(n, 1)
        sql, params = conn.cursors[0].executed[0]
        self.assertIn("INSERT INTO bg_task_queue", sql)
        self.assertIn("'kr_set_audit'", sql)
        self.assertIn("ON CONFLICT (task_type, task_key) DO NOTHING", sql)
        # Date is the FIRST positional param (task_key).
        self.assertEqual(params[0], "2026-04-29")
        self.assertEqual(conn.commits, 1)


# ── process — short circuits


class ProcessShortCircuitTests(unittest.TestCase):

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def test_no_repo_returns_no_repo_status_and_no_writes(self):
        conn = FakeConn()
        w = KrSetAuditWorker(conn, kr_db_root=self.td / "absent")
        rv = w.process({"task_id": 1, "payload": {}})
        self.assertEqual(rv["status"], "NO_REPO")
        self.assertEqual(rv["sets_audited"], 0)
        # Critically: no SQL touched, no commit. Don't blow away the
        # previous gap report just because the repo is temporarily
        # unmounted (USB drive popped out mid-night).
        self.assertEqual(conn.all_sql, [])
        self.assertEqual(conn.commits, 0)

    def test_empty_repo_returns_empty_repo_status_and_no_writes(self):
        # Repo exists but no parseable cards — same don't-blow-away
        # invariant as NO_REPO.
        repo = self.td / "repo"
        repo.mkdir()
        conn = FakeConn()
        w = KrSetAuditWorker(conn, kr_db_root=repo)
        rv = w.process({"task_id": 1, "payload": {}})
        self.assertEqual(rv["status"], "EMPTY_REPO")
        self.assertEqual(conn.all_sql, [])
        self.assertEqual(conn.commits, 0)


# ── process — happy paths


class ProcessHappyPathTests(unittest.TestCase):

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def _run(self, repo_cards, cards_master_rows):
        repo = _make_repo(self.td, **repo_cards)
        conn = FakeConn()
        conn.fetchall_queue = [cards_master_rows]
        w = KrSetAuditWorker(conn, kr_db_root=repo)
        rv = w.process({"task_id": 1, "payload": {}})
        return rv, conn

    def test_complete_set_records_zero_missing_zero_extra(self):
        rv, conn = self._run(
            {"pokemon": [_card("sv2", "1"), _card("sv2", "2"),
                         _card("sv2", "3")]},
            [("sv2", ["1", "2", "3"])],
        )
        self.assertEqual(rv["status"], "OK")
        self.assertEqual(rv["sets_audited"], 1)
        self.assertEqual(rv["total_missing"], 0)
        self.assertEqual(rv["total_extra"], 0)
        # Find the kr_set_gap UPSERT
        upserts = [(s, p) for s, p in conn.all_sql
                   if "INSERT INTO kr_set_gap" in s]
        self.assertEqual(len(upserts), 1)
        sql, params = upserts[0]
        self.assertIn("ON CONFLICT (set_id) DO UPDATE", sql)
        self.assertEqual(params[0], "sv2")
        self.assertEqual(params[1], 3)        # expected_count
        self.assertEqual(params[2], 3)        # actual_count
        self.assertEqual(json.loads(params[3]), [])  # missing_numbers
        self.assertEqual(json.loads(params[4]), [])  # extra_numbers

    def test_missing_numbers_recorded_sorted(self):
        rv, conn = self._run(
            {"pokemon": [_card("sv2", "1"), _card("sv2", "2"),
                         _card("sv2", "3"), _card("sv2", "10")]},
            [("sv2", ["1", "10"])],   # 2 and 3 missing
        )
        self.assertEqual(rv["status"], "OK")
        self.assertEqual(rv["total_missing"], 2)
        upserts = [(s, p) for s, p in conn.all_sql
                   if "INSERT INTO kr_set_gap" in s]
        _, params = upserts[0]
        self.assertEqual(json.loads(params[3]), ["2", "3"])

    def test_extra_numbers_recorded(self):
        # cards_master has a number the canonical doesn't — usually
        # an alias drift bug worth surfacing.
        rv, conn = self._run(
            {"pokemon": [_card("sv2", "1"), _card("sv2", "2")]},
            [("sv2", ["1", "2", "999"])],
        )
        self.assertEqual(rv["total_extra"], 1)
        upserts = [(s, p) for s, p in conn.all_sql
                   if "INSERT INTO kr_set_gap" in s]
        _, params = upserts[0]
        self.assertEqual(json.loads(params[4]), ["999"])

    def test_only_canonical_sets_audited(self):
        # cards_master has rows for an English set 'bw1' that the KR
        # repo knows nothing about. The audit must NOT generate a
        # phantom gap row for bw1 — that would fill the table with
        # noise that doesn't belong in a KR audit.
        rv, conn = self._run(
            {"pokemon": [_card("sv2", "1")]},
            [("sv2", ["1"])],   # only canonical sets queried
        )
        self.assertEqual(rv["sets_audited"], 1)
        # SELECT clause must be parameterised on the canonical set
        # list — i.e. ANY(%s) with sorted(['sv2']).
        selects = [(s, p) for s, p in conn.all_sql
                   if "FROM cards_master" in s]
        self.assertEqual(len(selects), 1)
        _, params = selects[0]
        self.assertEqual(params[0], ["sv2"])

    def test_set_with_no_cards_master_rows_marks_all_missing(self):
        # The set exists upstream but cards_master has zero rows for
        # it — every canonical number is missing.
        rv, conn = self._run(
            {"pokemon": [_card("sv2", "1"), _card("sv2", "2")]},
            [],   # no cards_master rows at all
        )
        self.assertEqual(rv["total_missing"], 2)
        self.assertEqual(rv["total_extra"], 0)
        upserts = [(s, p) for s, p in conn.all_sql
                   if "INSERT INTO kr_set_gap" in s]
        _, params = upserts[0]
        self.assertEqual(params[1], 2)  # expected
        self.assertEqual(params[2], 0)  # actual
        self.assertEqual(json.loads(params[3]), ["1", "2"])

    def test_multiple_sets_one_upsert_per_set(self):
        rv, conn = self._run(
            {"pokemon": [_card("sv2", "1"), _card("sv1", "1"),
                         _card("sv1", "2")]},
            [("sv2", ["1"]), ("sv1", ["1"])],   # sv1 missing '2'
        )
        self.assertEqual(rv["sets_audited"], 2)
        self.assertEqual(rv["total_missing"], 1)
        upserts = [(s, p) for s, p in conn.all_sql
                   if "INSERT INTO kr_set_gap" in s]
        self.assertEqual(len(upserts), 2)
        # And exactly ONE commit at the end — not one per set.
        self.assertEqual(conn.commits, 1)

    def test_uses_upsert_not_truncate(self):
        # Important contract: we UPSERT, we don't TRUNCATE. A failed
        # audit on a sub-set shouldn't wipe the whole table.
        rv, conn = self._run(
            {"pokemon": [_card("sv2", "1")]},
            [("sv2", ["1"])],
        )
        self.assertFalse(any("TRUNCATE" in s.upper()
                             for s, _ in conn.all_sql))
        self.assertFalse(any("DELETE FROM kr_set_gap" in s
                             for s, _ in conn.all_sql))


# ── DDL contract


class DDLContractTests(unittest.TestCase):

    def test_ddl_registered_in_schema(self):
        from unified import schema
        self.assertIn("kr_set_gap",
                      [name for name, _ in schema._ALL_DDL])
        ddl = schema.DDL_KR_SET_GAP
        # Required columns
        for col in ("set_id", "expected_count", "actual_count",
                    "missing_numbers", "extra_numbers", "audited_at"):
            self.assertIn(col, ddl, f"missing column {col} in DDL")
        # Primary key on set_id (so the worker's UPSERT works)
        self.assertIn("set_id           TEXT PRIMARY KEY", ddl)
        # Index on audited_at for "show me the latest gap report"
        self.assertIn("idx_kr_set_gap_audited", ddl)


if __name__ == "__main__":
    unittest.main()
