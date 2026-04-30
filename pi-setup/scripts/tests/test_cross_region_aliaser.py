"""
Tests for workers/cross_region_aliaser.

Hermetic: no network, no real /mnt/cards. We synthesise a tiny ZH
mirror tree on disk + canonical_sets JSONs + manual overrides JSON,
feed them through the worker, and assert on the SQL it emits via a
FakeConn.

Coverage map:
  pure helpers      — _canonical_key, _normalise_number, _zh_card_id,
                      _cosine, _walk_zh_mirror
  loaders           — _load_canonical_sets, _load_manual_overrides
                      (happy + missing + malformed)
  worker.seed       — inserts when empty, skips when PENDING exists
  worker.process    — set_abbrev hit, VERIFY-sentinel skip, CLIP hit,
                      CLIP below-threshold miss, manual override
                      protects auto-write
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

from workers import cross_region_aliaser as cra
from workers.base import WorkerError
from workers.cross_region_aliaser import (
    CrossRegionAliaserWorker,
    _canonical_key,
    _normalise_number,
    _zh_card_id,
    _load_canonical_sets,
    _load_manual_overrides,
    _load_and_validate_manual_overrides,
    _validate_manual_overrides,
    _walk_zh_mirror,
    _cosine,
)


# ── FakeConn / FakeCursor ──────────────────────────────────────────────


class FakeCursor:
    """Records every execute() and serves fetchone()/fetchall() values
    from queues prepared by each test. Indices into the queues advance
    across calls, so a test can prep distinct rows for distinct
    queries."""

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


# ── Tiny synthetic mirror builder ──────────────────────────────────────


def _make_zh_mirror(td: Path, layout: dict[str, dict[str, dict[str, list[str]]]]) -> Path:
    """Build /mnt/cards/zh/<lang>/<source>/<set>/<card_files...> under
    td/zh, then return that path.

    layout format:
      { "zh-tc": { "ptcg.tw": { "SV1S": ["001.jpg", "002.jpg"] } } }
    """
    zh = td / "zh"
    for lang, sources in layout.items():
        for source, sets in sources.items():
            for set_id, files in sets.items():
                d = zh / lang / source / set_id
                d.mkdir(parents=True, exist_ok=True)
                for fname in files:
                    (d / fname).write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 16)
    return zh


def _write_canonical(td: Path, fname: str, sets: list[dict]) -> Path:
    cdir = td / "canonical_sets"
    cdir.mkdir(parents=True, exist_ok=True)
    p = cdir / fname
    p.write_text(json.dumps({"_schema": {"version": 1}, "sets": sets}),
                 encoding="utf-8")
    return cdir


# ── Pure helpers ───────────────────────────────────────────────────────


class CanonicalKeyTests(unittest.TestCase):
    def test_format(self):
        self.assertEqual(_canonical_key("SV1S", "001"), "jp:SV1S:001")

    def test_empty_set_still_well_formed(self):
        # Edge case — empty inputs shouldn't crash. The DB CHECK on
        # match_method protects against an empty key actually being
        # written via the unmatched path; this test just pins behaviour.
        self.assertEqual(_canonical_key("", "1"), "jp::1")


class NormaliseNumberTests(unittest.TestCase):
    def test_strips_leading_zeros(self):
        self.assertEqual(_normalise_number("001"), "1")
        self.assertEqual(_normalise_number("042"), "42")

    def test_already_normalised(self):
        self.assertEqual(_normalise_number("100"), "100")

    def test_empty_becomes_zero(self):
        # Mirrors the kr importer convention: unnumbered promos => "0"
        self.assertEqual(_normalise_number(""), "0")
        self.assertEqual(_normalise_number("000"), "0")


class ZhCardIdTests(unittest.TestCase):
    def test_assembles_id(self):
        # The source must be in the id — same canonical set may come
        # from PTCG-CHS-Datasets primary AND mycardart fallback, and
        # we want the row to record which copy we have on disk.
        self.assertEqual(
            _zh_card_id("zh-tc", "ptcg.tw", "SV1S", "001"),
            "zh-tc:ptcg.tw:SV1S:1",
        )


class CosineTests(unittest.TestCase):
    def test_identical_vectors(self):
        self.assertAlmostEqual(_cosine([1.0, 0.0], [1.0, 0.0]), 1.0)

    def test_orthogonal_vectors(self):
        self.assertAlmostEqual(_cosine([1.0, 0.0], [0.0, 1.0]), 0.0)

    def test_zero_vector_returns_zero(self):
        # We use this to short-circuit the "no embedding" path; if it
        # ever returned NaN we'd silently skip CLIP for the whole run.
        self.assertEqual(_cosine([0.0, 0.0], [1.0, 1.0]), 0.0)
        self.assertEqual(_cosine([], [1.0]), 0.0)

    def test_mismatched_lengths_returns_zero(self):
        self.assertEqual(_cosine([1.0, 0.0], [1.0, 0.0, 0.0]), 0.0)


# ── Loaders ────────────────────────────────────────────────────────────


class LoadCanonicalSetsTests(unittest.TestCase):
    def test_happy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            cdir = _write_canonical(td, "zh_tc.json", [
                {"set_id": "SV1S", "abbreviation": "SV1S",
                 "jp_equivalent_id": "SV1S"},
                {"set_id": "SV1V", "abbreviation": "SV1V",
                 "jp_equivalent_id": "SV1V"},
            ])
            out = _load_canonical_sets(cdir, "zh_tc.json")
            self.assertEqual(set(out), {"SV1S", "SV1V"})
            self.assertEqual(out["SV1S"]["jp_equivalent_id"], "SV1S")

    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_load_canonical_sets(Path(tmp), "nope.json"), {})

    def test_malformed_json_returns_empty(self):
        # Operator hand-edits these files — a stray comma must not
        # take down the whole worker.
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            (td / "zh_tc.json").write_text("{ this is not json", encoding="utf-8")
            self.assertEqual(_load_canonical_sets(td, "zh_tc.json"), {})


class LoadManualOverridesTests(unittest.TestCase):
    def test_list_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "m.json"
            p.write_text(json.dumps([
                {"canonical_key": "jp:SV1S:1", "zh_tc_id": "x"},
            ]), encoding="utf-8")
            out = _load_manual_overrides(p)
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]["zh_tc_id"], "x")

    def test_dict_form_with_overrides_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "m.json"
            p.write_text(json.dumps({"overrides": [
                {"canonical_key": "jp:SV1S:1"},
            ]}), encoding="utf-8")
            self.assertEqual(len(_load_manual_overrides(p)), 1)

    def test_missing_file_returns_empty(self):
        self.assertEqual(_load_manual_overrides(Path("/nonexistent/x.json")), [])


# ── Walk ───────────────────────────────────────────────────────────────


class WalkZhMirrorTests(unittest.TestCase):
    def test_yields_valid_cards(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            zh = _make_zh_mirror(td, {
                "zh-tc": {"ptcg.tw": {"SV1S": ["001.jpg", "002.jpg"]}},
                "zh-sc": {"ptcg-chs": {"1": ["1.png"]}},
            })
            got = sorted(_walk_zh_mirror(zh))
            self.assertEqual(got, [
                ("zh-sc", "ptcg-chs", "1", "1"),
                ("zh-tc", "ptcg.tw", "SV1S", "001"),
                ("zh-tc", "ptcg.tw", "SV1S", "002"),
            ])

    def test_skips_unknown_lang_dir(self):
        # An operator's working dir like /mnt/cards/zh/scratch/ must
        # not be walked as if it were a real lang.
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            zh = _make_zh_mirror(td, {
                "scratch": {"random": {"X": ["1.jpg"]}},
                "zh-tc": {"ptcg.tw": {"SV1S": ["001.jpg"]}},
            })
            got = sorted(_walk_zh_mirror(zh))
            self.assertEqual(got, [("zh-tc", "ptcg.tw", "SV1S", "001")])

    def test_skips_dotfiles_and_text_stems(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            zh = td / "zh"
            d = zh / "zh-tc" / "ptcg.tw" / "SV1S"
            d.mkdir(parents=True)
            (d / "001.jpg").write_bytes(b"x")
            (d / ".DS_Store").write_bytes(b"x")
            (d / "README.txt").write_bytes(b"x")  # no digit in stem
            got = sorted(_walk_zh_mirror(zh))
            self.assertEqual(got, [("zh-tc", "ptcg.tw", "SV1S", "001")])

    def test_missing_root_returns_nothing(self):
        # Brand-new Pi with no /mnt/cards/zh/ yet must not crash.
        self.assertEqual(list(_walk_zh_mirror(Path("/nonexistent/zh"))), [])


# ── Worker.seed ────────────────────────────────────────────────────────


class SeedTests(unittest.TestCase):
    def _make_worker(self, td: Path, conn: FakeConn) -> CrossRegionAliaserWorker:
        return CrossRegionAliaserWorker(
            conn,
            zh_root=td / "zh",
            canonical_dir=td / "canonical_sets",
            manual_overrides_path=td / "manual.json",
            now_fn=lambda: 1700000000,
        )

    def test_inserts_when_queue_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            conn = FakeConn()
            conn.fetchone_queue = [None]   # SELECT 1 returns nothing
            w = self._make_worker(td, conn)
            self.assertEqual(w.seed(), 1)
            inserted = [s for s, _ in conn.all_sql if "INSERT INTO bg_task_queue" in s]
            self.assertEqual(len(inserted), 1)
            self.assertEqual(conn.commits, 1)

    def test_skips_when_pending_exists(self):
        # The bg_task_queue already has a PENDING row → seed must NOT
        # double-enqueue, otherwise multiple worker processes hammering
        # the queue would get N copies of the full-run task.
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            conn = FakeConn()
            conn.fetchone_queue = [(1,)]   # SELECT 1 returns a row
            w = self._make_worker(td, conn)
            self.assertEqual(w.seed(), 0)
            inserted = [s for s, _ in conn.all_sql if "INSERT INTO bg_task_queue" in s]
            self.assertEqual(inserted, [])
            self.assertEqual(conn.commits, 0)


# ── Worker.process — match paths ───────────────────────────────────────


class ProcessTests(unittest.TestCase):
    def _setup(self, td: Path, *, layout, canonical_tc=None, canonical_sc=None,
               manual=None) -> tuple[CrossRegionAliaserWorker, FakeConn]:
        zh = _make_zh_mirror(td, layout)
        cdir = td / "canonical_sets"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "zh_tc.json").write_text(
            json.dumps({"_schema": {"version": 1}, "sets": canonical_tc or []}),
            encoding="utf-8",
        )
        (cdir / "zh_sc.json").write_text(
            json.dumps({"_schema": {"version": 1}, "sets": canonical_sc or []}),
            encoding="utf-8",
        )
        manual_path = td / "manual.json"
        if manual is not None:
            manual_path.write_text(json.dumps(manual), encoding="utf-8")
        conn = FakeConn()
        worker = CrossRegionAliaserWorker(
            conn,
            zh_root=zh,
            canonical_dir=cdir,
            manual_overrides_path=manual_path,
            now_fn=lambda: 1700000000,
        )
        return worker, conn

    def _alias_inserts(self, conn: FakeConn) -> list[tuple[str, object]]:
        return [(s, p) for s, p in conn.all_sql if "INSERT INTO card_alias" in s]

    def test_set_abbrev_happy_path(self):
        # Canonical entry has a real jp_equivalent_id → the worker
        # writes a card_alias row keyed on that JP coordinate with
        # match_method='set_abbrev', confidence 1.0.
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            w, conn = self._setup(
                td,
                layout={"zh-tc": {"ptcg.tw": {"SV1S": ["001.jpg"]}}},
                canonical_tc=[
                    {"set_id": "SV1S", "abbreviation": "SV1S",
                     "jp_equivalent_id": "SV1S"},
                ],
            )
            res = w.process({})
            self.assertEqual(res["set_abbrev_matches"], 1)
            self.assertEqual(res["unmatched"], 0)
            self.assertEqual(res["clip_matches"], 0)
            inserts = self._alias_inserts(conn)
            self.assertEqual(len(inserts), 1)
            params = inserts[0][1]
            # canonical_key, jp_id, region_id, method, confidence, ...
            self.assertEqual(params[0], "jp:SV1S:1")
            self.assertEqual(params[1], "jp:SV1S:1")
            self.assertEqual(params[2], "zh-tc:ptcg.tw:SV1S:1")
            self.assertEqual(params[3], "set_abbrev")
            self.assertAlmostEqual(params[4], 1.0)

    def test_verify_sentinel_does_not_match(self):
        # When the operator hasn't confirmed jp_equivalent_id (the
        # value is the literal "VERIFY"), set_abbrev path must skip
        # so we don't pollute the table with a guess that points
        # at a JP set that may not exist.
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            w, conn = self._setup(
                td,
                layout={"zh-sc": {"ptcg-chs": {"460": ["001.jpg"]}}},
                canonical_sc=[
                    {"set_id": "460", "abbreviation": "30th-P-01",
                     "jp_equivalent_id": "VERIFY"},
                ],
            )
            res = w.process({})
            self.assertEqual(res["set_abbrev_matches"], 0)
            self.assertEqual(res["unmatched"], 1)
            inserts = self._alias_inserts(conn)
            self.assertEqual(len(inserts), 1)
            params = inserts[0][1]
            self.assertTrue(params[0].startswith("unmatched:"))
            self.assertEqual(params[3], "unmatched")

    def test_clip_fallback_match(self):
        # No canonical entry at all → falls through to CLIP. We seed
        # the FakeConn to return a ZH embedding then a JP embedding
        # that perfectly matches it.
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            w, conn = self._setup(
                td,
                layout={"zh-tc": {"ptcg.tw": {"UNKNOWN": ["001.jpg"]}}},
            )
            # 1st fetchone: ZH side embedding lookup returns a row.
            # 1st fetchall:  JP side returns one candidate that matches.
            conn.fetchone_queue = [("clip-vit-b32-onnx-1.0", [1.0, 0.0, 0.0])]
            conn.fetchall_queue = [[("SV1S", "1", [1.0, 0.0, 0.0])]]
            res = w.process({})
            self.assertEqual(res["clip_matches"], 1)
            self.assertEqual(res["set_abbrev_matches"], 0)
            self.assertEqual(res["unmatched"], 0)
            inserts = self._alias_inserts(conn)
            self.assertEqual(len(inserts), 1)
            params = inserts[0][1]
            self.assertEqual(params[0], "jp:SV1S:1")
            self.assertEqual(params[3], "clip")
            self.assertAlmostEqual(params[4], 1.0)

    def test_clip_below_threshold_is_unmatched(self):
        # ZH and JP embeddings exist but cosine sim is < 0.92 → must
        # NOT alias, must record as unmatched. Otherwise the table
        # fills up with low-confidence wrong links that ruin the booth UX.
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            w, conn = self._setup(
                td,
                layout={"zh-tc": {"ptcg.tw": {"UNKNOWN": ["001.jpg"]}}},
            )
            conn.fetchone_queue = [("clip-vit-b32-onnx-1.0", [1.0, 0.0])]
            # Cosine([1,0], [0.5, 0.866]) = 0.5 — well below 0.92.
            conn.fetchall_queue = [[("SV1S", "1", [0.5, 0.866])]]
            res = w.process({})
            self.assertEqual(res["clip_matches"], 0)
            self.assertEqual(res["unmatched"], 1)
            params = self._alias_inserts(conn)[0][1]
            self.assertEqual(params[3], "unmatched")

    def test_clip_skipped_when_no_zh_embedding(self):
        # First aliaser pass on a fresh sync — the CLIP worker hasn't
        # processed any ZH images yet. ZH-side fetchone returns None.
        # Worker must NOT crash and must NOT consult the JP side at
        # all (no fetchall) — just record unmatched and move on.
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            w, conn = self._setup(
                td,
                layout={"zh-tc": {"ptcg.tw": {"UNKNOWN": ["001.jpg"]}}},
            )
            conn.fetchone_queue = [None]
            res = w.process({})
            self.assertEqual(res["unmatched"], 1)
            self.assertEqual(res["clip_matches"], 0)

    def test_manual_override_writes_first_then_protects(self):
        # Manual override pins jp:SV1S:1 → zh-tc:custom:X:1. The same
        # canonical_key is then visited by the disk walk via the
        # set_abbrev path, which would normally UPDATE the row. But
        # the UPSERT's CASE WHEN clause must keep match_method='manual'
        # — we assert by inspecting the SQL: the ON CONFLICT branch
        # MUST contain the protective CASE WHEN.
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            w, conn = self._setup(
                td,
                layout={"zh-tc": {"ptcg.tw": {"SV1S": ["001.jpg"]}}},
                canonical_tc=[
                    {"set_id": "SV1S", "abbreviation": "SV1S",
                     "jp_equivalent_id": "SV1S"},
                ],
                manual=[{
                    "canonical_key": "jp:SV1S:1",
                    "zh_tc_id": "zh-tc:operator:SV1S:1",
                }],
            )
            res = w.process({})
            self.assertEqual(res["manual_overrides_applied"], 1)
            self.assertEqual(res["set_abbrev_matches"], 1)
            # The auto-pass UPSERT must encode manual protection in SQL.
            # Identify the auto-pass insert by the protective CASE WHEN
            # in its ON CONFLICT branch — the manual-override INSERT
            # unconditionally writes 'manual', so it can't contain that
            # clause; only the auto path does.
            inserts = self._alias_inserts(conn)
            protective = [s for s, _ in inserts
                          if "WHEN card_alias.match_method = 'manual'" in s]
            self.assertTrue(
                protective,
                f"expected at least one protective UPSERT, got SQL: "
                f"{[s[:60] for s, _ in inserts]}",
            )

    def test_manual_override_missing_canonical_key_aborts_run(self):
        # FU-1: the lenient pre-validation behaviour silently dropped
        # orphans, which let the auto-aliaser overwrite them on the
        # next pass. The strict loader must abort the WHOLE run with
        # a permanent failure so the operator notices.
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            w, conn = self._setup(
                td,
                layout={},
                manual=[
                    {"zh_tc_id": "orphan"},      # missing canonical_key
                    {"canonical_key": "jp:X:1"}, # pins nothing
                ],
            )
            with self.assertRaises(WorkerError) as cm:
                w.process({})
            msg = str(cm.exception)
            # Both errors should be reported in one shot — the operator
            # shouldn't have to fix-and-rerun N times.
            self.assertIn("missing 'canonical_key'", msg)
            self.assertIn("pins nothing", msg)
            # And NOTHING should have been written to card_alias.
            inserts = self._alias_inserts(conn)
            self.assertEqual(
                inserts, [],
                "expected zero card_alias writes when validation fails",
            )


# ── FU-1: schema validation of /mnt/cards/manual_aliases.json ──────────
#
# These are the failure-mode tests called out in the FU plan. The
# happy-path coverage is the test_manual_override_writes_first_then_protects
# test above (it round-trips a fully-valid override through process()).


class ValidateManualOverridesTests(unittest.TestCase):
    """Pure-function tests on _validate_manual_overrides — fast,
    no FS, no DB. process()-level integration is covered separately."""

    CANONICAL_TC = {
        "SV1S": {"set_id": "SV1S", "jp_equivalent_id": "SV1S",
                 "expected_card_count": 78},
    }
    CANONICAL_SC = {
        "1": {"set_id": "1", "jp_equivalent_id": "BW",
              "expected_card_count": 100},
    }

    def test_happy_path_no_errors(self):
        ovs = [{
            "canonical_key": "jp:SV1S:1",
            "zh_tc_id": "zh-tc:ptcg.tw:SV1S:1",
            "zh_sc_id": "zh-sc:ptcg-chs:1:42",
            "notes": "operator-confirmed 2026-04-30",
        }]
        self.assertEqual(
            _validate_manual_overrides(ovs, self.CANONICAL_TC, self.CANONICAL_SC),
            [],
        )

    def test_unknown_zh_tc_set_id(self):
        ovs = [{
            "canonical_key": "jp:SV1S:1",
            "zh_tc_id": "zh-tc:ptcg.tw:NOPE:1",
        }]
        errs = _validate_manual_overrides(ovs, self.CANONICAL_TC, self.CANONICAL_SC)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("zh_tc_id set_id 'NOPE' not found", errs[0])

    def test_unknown_zh_sc_set_id(self):
        ovs = [{
            "canonical_key": "jp:BW:1",
            "zh_sc_id": "zh-sc:ptcg-chs:9999:1",
        }]
        errs = _validate_manual_overrides(ovs, self.CANONICAL_TC, self.CANONICAL_SC)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("zh_sc_id set_id '9999' not found", errs[0])

    def test_card_number_out_of_range(self):
        # SV1S has expected_card_count=78 in our fixture; pinning #999
        # is almost certainly an operator typo, not a secret rare.
        ovs = [{
            "canonical_key": "jp:SV1S:1",
            "zh_tc_id": "zh-tc:ptcg.tw:SV1S:999",
        }]
        errs = _validate_manual_overrides(ovs, self.CANONICAL_TC, self.CANONICAL_SC)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("outside the expected range", errs[0])
        self.assertIn("1..78", errs[0])

    def test_card_number_in_range_passes(self):
        ovs = [{
            "canonical_key": "jp:SV1S:78",
            "zh_tc_id": "zh-tc:ptcg.tw:SV1S:78",
        }]
        self.assertEqual(
            _validate_manual_overrides(ovs, self.CANONICAL_TC, self.CANONICAL_SC),
            [],
        )

    def test_duplicate_canonical_key(self):
        ovs = [
            {"canonical_key": "jp:SV1S:1",
             "zh_tc_id": "zh-tc:a:SV1S:1"},
            {"canonical_key": "jp:SV1S:1",
             "zh_tc_id": "zh-tc:b:SV1S:1"},
        ]
        errs = _validate_manual_overrides(ovs, self.CANONICAL_TC, self.CANONICAL_SC)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("duplicate canonical_key", errs[0])
        self.assertIn("first seen at override #1", errs[0])

    def test_typo_in_region_key_is_rejected(self):
        # `zh_tcc_id` looks like `zh_tc_id` — would silently no-op
        # under the lenient loader.
        ovs = [{
            "canonical_key": "jp:SV1S:1",
            "zh_tcc_id": "zh-tc:ptcg.tw:SV1S:1",
        }]
        errs = _validate_manual_overrides(ovs, self.CANONICAL_TC, self.CANONICAL_SC)
        # Two errors expected: the typo'd key + "pins nothing" because
        # the only region attempt was the typo.
        self.assertEqual(len(errs), 2, errs)
        self.assertTrue(any("zh_tcc_id" in e for e in errs))
        self.assertTrue(any("pins nothing" in e for e in errs))

    def test_pins_nothing(self):
        ovs = [{"canonical_key": "jp:SV1S:1", "notes": "wip"}]
        errs = _validate_manual_overrides(ovs, self.CANONICAL_TC, self.CANONICAL_SC)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("pins nothing", errs[0])

    def test_canonical_key_wrong_form(self):
        ovs = [{
            "canonical_key": "SV1S/1",  # missing jp: prefix and colons
            "zh_tc_id": "zh-tc:ptcg.tw:SV1S:1",
        }]
        errs = _validate_manual_overrides(ovs, self.CANONICAL_TC, self.CANONICAL_SC)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("does not match", errs[0])

    def test_jp_id_form_mismatch(self):
        ovs = [{
            "canonical_key": "jp:SV1S:1",
            "jp_id": "broken",
            "zh_tc_id": "zh-tc:ptcg.tw:SV1S:1",
        }]
        errs = _validate_manual_overrides(ovs, self.CANONICAL_TC, self.CANONICAL_SC)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("jp_id 'broken'", errs[0])

    def test_region_id_wrong_form(self):
        ovs = [{
            "canonical_key": "jp:SV1S:1",
            "zh_tc_id": "no-colons-here",
        }]
        errs = _validate_manual_overrides(ovs, self.CANONICAL_TC, self.CANONICAL_SC)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("'<lang>:<source>:<set_id>:<card_number>'", errs[0])

    def test_non_dict_entry(self):
        ovs = ["not-a-dict", 42]
        errs = _validate_manual_overrides(ovs, self.CANONICAL_TC, self.CANONICAL_SC)
        self.assertEqual(len(errs), 2, errs)
        self.assertTrue(any("expected an object" in e for e in errs))

    def test_jp_id_derives_canonical_key(self):
        # Operators commonly write `jp_id` as their primary key; the
        # validator should accept that and derive canonical_key from it.
        ovs = [{
            "jp_id": "jp:SV1S:1",
            "zh_tc_id": "zh-tc:ptcg.tw:SV1S:1",
        }]
        self.assertEqual(
            _validate_manual_overrides(ovs, self.CANONICAL_TC, self.CANONICAL_SC),
            [],
        )

    def test_expected_card_count_verify_sentinel_skips_range_check(self):
        # Sets where the operator hasn't confirmed the count yet have
        # expected_card_count="VERIFY"; we shouldn't reject overrides
        # against them — the operator might know more than we do.
        canonical = {"X": {"set_id": "X", "expected_card_count": "VERIFY"}}
        ovs = [{
            "canonical_key": "jp:SV1S:1",
            "zh_tc_id": "zh-tc:src:X:9999",
        }]
        self.assertEqual(
            _validate_manual_overrides(ovs, canonical, self.CANONICAL_SC),
            [],
        )


class LoadAndValidateManualOverridesTests(unittest.TestCase):
    """File-level loader tests — exercise the path that turns a JSON
    file on disk into either a clean override list or a WorkerError."""

    CANONICAL_TC = {"SV1S": {"set_id": "SV1S", "expected_card_count": 78}}
    CANONICAL_SC: dict[str, dict[str, object]] = {}

    def test_missing_file_returns_empty(self):
        out = _load_and_validate_manual_overrides(
            Path("/nonexistent/manual.json"),
            self.CANONICAL_TC, self.CANONICAL_SC,
        )
        self.assertEqual(out, [])

    def test_malformed_json_raises_with_line_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "m.json"
            # Newline before the offending bracket so lineno > 1, proving
            # we surface the actual position.
            p.write_text("{\n  this is not json", encoding="utf-8")
            with self.assertRaises(WorkerError) as cm:
                _load_and_validate_manual_overrides(
                    p, self.CANONICAL_TC, self.CANONICAL_SC,
                )
            msg = str(cm.exception)
            self.assertIn("not valid JSON", msg)
            # JSONDecodeError on this input reports line 2.
            self.assertIn("line 2", msg)

    def test_top_level_shape_wrong_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "m.json"
            p.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
            with self.assertRaises(WorkerError) as cm:
                _load_and_validate_manual_overrides(
                    p, self.CANONICAL_TC, self.CANONICAL_SC,
                )
            self.assertIn("unexpected top-level shape", str(cm.exception))

    def test_overrides_key_not_a_list_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "m.json"
            p.write_text(json.dumps({"overrides": "oops"}), encoding="utf-8")
            with self.assertRaises(WorkerError) as cm:
                _load_and_validate_manual_overrides(
                    p, self.CANONICAL_TC, self.CANONICAL_SC,
                )
            self.assertIn("must be a list", str(cm.exception))

    def test_validation_errors_are_aggregated(self):
        # Multiple bad entries should all show up in the WorkerError —
        # the operator shouldn't fix one, rerun, fix the next, rerun.
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "m.json"
            p.write_text(json.dumps([
                {"canonical_key": "jp:SV1S:1",
                 "zh_tc_id": "zh-tc:src:NOPE:1"},
                {"canonical_key": "jp:SV1S:1",
                 "zh_tc_id": "zh-tc:src:SV1S:2"},
                {"zh_tc_id": "zh-tc:src:SV1S:3"},
            ]), encoding="utf-8")
            with self.assertRaises(WorkerError) as cm:
                _load_and_validate_manual_overrides(
                    p, self.CANONICAL_TC, self.CANONICAL_SC,
                )
            msg = str(cm.exception)
            self.assertIn("3 errors", msg)
            self.assertIn("set_id 'NOPE'", msg)
            self.assertIn("duplicate canonical_key", msg)
            self.assertIn("missing 'canonical_key'", msg)

    def test_happy_path_returns_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "m.json"
            p.write_text(json.dumps([
                {"canonical_key": "jp:SV1S:1",
                 "zh_tc_id": "zh-tc:ptcg.tw:SV1S:1"},
            ]), encoding="utf-8")
            out = _load_and_validate_manual_overrides(
                p, self.CANONICAL_TC, self.CANONICAL_SC,
            )
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]["canonical_key"], "jp:SV1S:1")


if __name__ == "__main__":
    unittest.main()
