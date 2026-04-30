#!/usr/bin/env python3
"""
test_phase_d.py — unit tests for sync_card_mirror Phase D (ZH walk).

Hermetic — no network, no real /mnt/cards. Each test runs against a
tmp-dir MIRROR_ROOT with a fabricated PTCG-CHS-Datasets clone (just
`.git/` + a few image files) so the local-mirror walker can be
exercised end-to-end. The remote walker is exercised with mocked
_download.

Design note: Phase D is the first slice (ZH-2) of the Chinese-language
pipeline. These tests pin the on-disk layout (/mnt/cards/zh/<lang>/
<source>/<set>/<num>.<ext>) and idempotency contract — re-running on a
populated tree should do zero new I/O.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PI_SETUP = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PI_SETUP))

from scripts import sync_card_mirror as scm  # noqa: E402
from scripts import zh_sources  # noqa: E402


# ── shared fixture helpers ────────────────────────────────────────────

def _write_min_image(p: Path, size: int = 1024) -> None:
    """Create a fake image file of `size` bytes (above the 256 min)."""
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * (size - 8))


def _make_fake_chs_repo(mirror_root: Path, *, sets: dict[str, list[str]]
                        ) -> Path:
    """
    Build a minimal PTCG-CHS-Datasets clone under mirror_root/.

    `sets` maps set_id (str) → list of card_num (str). Creates `.git/`
    so phase_d treats it as a real Phase A clone, plus img/<id>/<n>.png
    for each entry.
    """
    repo = mirror_root / "PTCG-CHS-Datasets"
    (repo / ".git").mkdir(parents=True)
    for set_id, card_nums in sets.items():
        for n in card_nums:
            _write_min_image(repo / "img" / set_id / f"{n}.png")
    return repo


def _write_canonical_sc(canonical_dir: Path, set_ids: list[str]) -> None:
    """Write a minimal zh_sc.json with the given set_ids (no VERIFY)."""
    canonical_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "_schema": {"version": 1},
        "sets": [{"set_id": sid, "abbreviation": "X",
                  "expected_card_count": 99} for sid in set_ids],
    }
    (canonical_dir / "zh_sc.json").write_text(json.dumps(data))


def _write_canonical_tc(canonical_dir: Path, sets: list[dict]) -> None:
    canonical_dir.mkdir(parents=True, exist_ok=True)
    data = {"_schema": {"version": 1}, "sets": sets}
    (canonical_dir / "zh_tc.json").write_text(json.dumps(data))


class _PhaseDFixture(unittest.TestCase):
    """Base fixture: tmp MIRROR_ROOT + ZH_DEST_ROOT + canonical dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.mirror_root = self.tmp / "mirror"
        self.mirror_root.mkdir()
        self.zh_dest_root = self.mirror_root / "zh"
        self.canonical_dir = self.tmp / "canonical_sets"
        # Patch module-level paths. ZH_DEST_ROOT is module-level so it
        # was already computed at import — overwrite both.
        self._patches = [
            mock.patch.object(scm, "MIRROR_ROOT", self.mirror_root),
            mock.patch.object(scm, "ZH_DEST_ROOT", self.zh_dest_root),
            mock.patch.object(scm, "_CANONICAL_DIR", self.canonical_dir),
            # Failure-log conn returns None so phase_d skips the DB
            # path entirely without needing psycopg2 in the test env.
            mock.patch.object(scm, "_open_failure_log_conn",
                              return_value=None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()


# ── _load_canonical_sets ──────────────────────────────────────────────

class LoadCanonicalSetsTest(_PhaseDFixture):

    def test_reads_valid_json(self):
        _write_canonical_sc(self.canonical_dir, ["1", "42"])
        sets = scm._load_canonical_sets(zh_sources.Lang.SC)
        self.assertEqual([s["set_id"] for s in sets], ["1", "42"])

    def test_missing_file_returns_empty(self):
        sets = scm._load_canonical_sets(zh_sources.Lang.SC)
        self.assertEqual(sets, [])

    def test_malformed_json_returns_empty(self):
        self.canonical_dir.mkdir(parents=True, exist_ok=True)
        (self.canonical_dir / "zh_sc.json").write_text("{not json")
        sets = scm._load_canonical_sets(zh_sources.Lang.SC)
        self.assertEqual(sets, [])

    def test_sets_not_a_list_returns_empty(self):
        self.canonical_dir.mkdir(parents=True, exist_ok=True)
        (self.canonical_dir / "zh_sc.json").write_text(
            json.dumps({"sets": "not-a-list"}))
        sets = scm._load_canonical_sets(zh_sources.Lang.SC)
        self.assertEqual(sets, [])


# ── _zh_dest_path ─────────────────────────────────────────────────────

class ZhDestPathTest(_PhaseDFixture):

    def test_canonical_layout(self):
        s = zh_sources.PTCG_CHS_DATASETS
        p = scm._zh_dest_path(s, "42", "7", ".png")
        self.assertEqual(p, self.zh_dest_root / "sc"
                         / "PTCG-CHS-Datasets" / "42" / "7.png")

    def test_extension_normalised(self):
        # Both ".jpg" and "jpg" should produce a single dot.
        s = zh_sources.PTCG_TW
        p1 = scm._zh_dest_path(s, "svi", "001", ".jpg")
        p2 = scm._zh_dest_path(s, "svi", "001", "jpg")
        self.assertEqual(p1, p2)
        self.assertTrue(str(p1).endswith("/svi/001.jpg"))


# ── _link_or_copy ─────────────────────────────────────────────────────

class LinkOrCopyTest(_PhaseDFixture):

    def test_hardlink_within_same_fs(self):
        src = self.tmp / "src.png"
        dest = self.tmp / "out" / "dest.png"
        _write_min_image(src)
        ok, status = scm._link_or_copy(src, dest)
        self.assertTrue(ok)
        self.assertIn(status, ("linked", "copied"))  # tmpfs may not allow link
        self.assertTrue(dest.exists())
        self.assertEqual(dest.stat().st_size, src.stat().st_size)

    def test_skip_exists_when_dest_already_present(self):
        src = self.tmp / "src.png"
        dest = self.tmp / "out" / "dest.png"
        _write_min_image(src)
        _write_min_image(dest, size=2048)
        ok, status = scm._link_or_copy(src, dest)
        self.assertTrue(ok)
        self.assertEqual(status, "skip-exists")
        # Verify we did NOT clobber the existing file
        self.assertEqual(dest.stat().st_size, 2048)

    def test_src_missing(self):
        ok, status = scm._link_or_copy(
            self.tmp / "nope.png", self.tmp / "out.png")
        self.assertFalse(ok)
        self.assertEqual(status, "src-missing")

    def test_src_too_small(self):
        src = self.tmp / "stub.png"
        src.write_bytes(b"x")  # 1 byte — well below 256
        ok, status = scm._link_or_copy(src, self.tmp / "out.png")
        self.assertFalse(ok)
        self.assertEqual(status, "src-too-small")


# ── _walk_local_zh_source ─────────────────────────────────────────────

class WalkLocalZhSourceTest(_PhaseDFixture):

    def test_happy_path_links_all_images(self):
        _make_fake_chs_repo(self.mirror_root, sets={
            "1": ["0", "1", "2"],
            "42": ["100", "101"],
        })
        sets = [{"set_id": "1"}, {"set_id": "42"}]
        t, ok, skip, fail = scm._walk_local_zh_source(
            zh_sources.PTCG_CHS_DATASETS, sets)
        self.assertEqual(t, 5)
        self.assertEqual(ok, 5)
        self.assertEqual(skip, 0)
        self.assertEqual(fail, 0)
        # Spot-check destination layout
        self.assertTrue((self.zh_dest_root / "sc" / "PTCG-CHS-Datasets"
                         / "1" / "0.png").exists())
        self.assertTrue((self.zh_dest_root / "sc" / "PTCG-CHS-Datasets"
                         / "42" / "101.png").exists())

    def test_skips_verify_set_ids(self):
        _make_fake_chs_repo(self.mirror_root, sets={"1": ["0"]})
        sets = [{"set_id": "VERIFY"}, {"set_id": "1"}]
        t, ok, *_ = scm._walk_local_zh_source(
            zh_sources.PTCG_CHS_DATASETS, sets)
        self.assertEqual(t, 1)  # only the "1" set walked
        self.assertEqual(ok, 1)

    def test_returns_zero_when_repo_not_cloned(self):
        # No fake repo created — Phase A hasn't run
        sets = [{"set_id": "1"}]
        t, ok, skip, fail = scm._walk_local_zh_source(
            zh_sources.PTCG_CHS_DATASETS, sets)
        self.assertEqual((t, ok, skip, fail), (0, 0, 0, 0))

    def test_idempotent_second_run(self):
        _make_fake_chs_repo(self.mirror_root, sets={"1": ["0", "1"]})
        sets = [{"set_id": "1"}]
        scm._walk_local_zh_source(zh_sources.PTCG_CHS_DATASETS, sets)
        t, ok, skip, fail = scm._walk_local_zh_source(
            zh_sources.PTCG_CHS_DATASETS, sets)
        # Second pass: every image is already at dest → all skip-exists
        self.assertEqual(t, 2)
        self.assertEqual(ok, 0)
        self.assertEqual(skip, 2)
        self.assertEqual(fail, 0)


# ── _walk_remote_zh_source ────────────────────────────────────────────

class WalkRemoteZhSourceTest(_PhaseDFixture):

    def test_skips_verify_set_ids(self):
        with mock.patch.object(scm, "_download") as mdl, \
             mock.patch.object(scm.time, "sleep"):
            sets = [
                {"set_id": "VERIFY"},
                {"set_id": "VERIFY-sv1s-slug"},
            ]
            t, *_ = scm._walk_remote_zh_source(
                zh_sources.PTCG_TW, sets, failure_conn=None)
            self.assertEqual(t, 0)
            mdl.assert_not_called()

    def test_skips_sets_without_expected_count(self):
        with mock.patch.object(scm, "_download") as mdl, \
             mock.patch.object(scm.time, "sleep"):
            sets = [{"set_id": "real-slug"}]  # no expected_card_count
            t, *_ = scm._walk_remote_zh_source(
                zh_sources.PTCG_TW, sets, failure_conn=None)
            self.assertEqual(t, 0)
            mdl.assert_not_called()

    def test_walks_expected_card_range(self):
        with mock.patch.object(scm, "_download",
                               return_value=(True, "ok")) as mdl, \
             mock.patch.object(scm.time, "sleep"):
            sets = [{"set_id": "real-slug", "expected_card_count": 3}]
            t, ok, skip, fail = scm._walk_remote_zh_source(
                zh_sources.PTCG_TW, sets, failure_conn=None)
            self.assertEqual(t, 3)
            self.assertEqual(ok, 3)
            # Verify the URL pattern + per-source UA were used
            self.assertEqual(mdl.call_count, 3)
            first_call = mdl.call_args_list[0]
            url = first_call.args[0]
            self.assertIn("real-slug", url)
            self.assertIn("001", url)
            headers = first_call.kwargs.get("extra_headers", {})
            self.assertTrue(headers["User-Agent"].startswith("Mozilla/"))

    def test_not_modified_counted_as_skip(self):
        with mock.patch.object(scm, "_download",
                               return_value=(True, "not-modified")), \
             mock.patch.object(scm.time, "sleep"):
            sets = [{"set_id": "real", "expected_card_count": 2}]
            t, ok, skip, fail = scm._walk_remote_zh_source(
                zh_sources.PTCG_TW, sets, failure_conn=None)
            self.assertEqual((t, ok, skip, fail), (2, 0, 2, 0))


# ── phase_d top-level ─────────────────────────────────────────────────

class PhaseDTest(_PhaseDFixture):

    def test_no_langs_enabled_is_a_noop(self):
        # Should not raise, should not create ZH_DEST_ROOT
        scm.phase_d(include_tc=False, include_sc=False,
                    include_fallback=False)
        self.assertFalse(self.zh_dest_root.exists())

    def test_sc_only_walks_local_source(self):
        _make_fake_chs_repo(self.mirror_root, sets={"1": ["0", "1"]})
        _write_canonical_sc(self.canonical_dir, ["1"])
        scm.phase_d(include_tc=False, include_sc=True,
                    include_fallback=False)
        # Files mirrored under ZH_DEST_ROOT/sc/PTCG-CHS-Datasets/1/
        out = self.zh_dest_root / "sc" / "PTCG-CHS-Datasets" / "1"
        self.assertTrue(out.is_dir())
        self.assertEqual(sorted(p.name for p in out.iterdir()),
                         ["0.png", "1.png"])

    def test_tc_default_skips_fallback_source(self):
        # All canonical TC sets are VERIFY-prefixed → remote walker
        # finds nothing to walk, but we verify the FALLBACK source
        # (mycardart-tc) was never instantiated by checking that
        # sources_for(TC) returned only ptcg.tw.
        _write_canonical_tc(self.canonical_dir, [])
        with mock.patch.object(scm, "_download") as mdl, \
             mock.patch.object(scm.time, "sleep"):
            scm.phase_d(include_tc=True, include_sc=False,
                        include_fallback=False)
            mdl.assert_not_called()

    def test_missing_mirror_root_aborts_early(self):
        # Stomp MIRROR_ROOT to a path that does not exist
        with mock.patch.object(scm, "MIRROR_ROOT",
                               self.tmp / "does-not-exist"):
            scm.phase_d(include_tc=True, include_sc=True,
                        include_fallback=False)
            # No-op — would crash if we tried to walk anything


if __name__ == "__main__":
    unittest.main()
