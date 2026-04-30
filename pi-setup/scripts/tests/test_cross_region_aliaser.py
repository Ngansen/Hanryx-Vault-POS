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
from workers.cross_region_aliaser import (
    CrossRegionAliaserWorker,
    _canonical_key,
    _normalise_number,
    _zh_card_id,
    _load_canonical_sets,
    _load_manual_overrides,
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
                manual=[{"canonical_key": "jp:SV1S:1", "zh_tc_id": "pinned"}],
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

    def test_manual_override_missing_canonical_key_skipped(self):
        # An override missing both canonical_key AND jp_id must be
        # logged & skipped, not inserted with a NULL key (would
        # violate PK).
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            w, conn = self._setup(
                td,
                layout={},
                manual=[{"zh_tc_id": "orphan"}, {"canonical_key": "jp:X:1"}],
            )
            res = w.process({})
            self.assertEqual(res["manual_overrides_applied"], 1)


if __name__ == "__main__":
    unittest.main()
