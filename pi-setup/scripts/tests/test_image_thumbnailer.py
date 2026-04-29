"""
Tests for workers/image_thumbnailer.

Hermetic — uses real Pillow when available (the sandbox has it),
falls back to a FakePIL when explicitly injected. Tests that need
NO_LIB pass pil_module=False.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workers import image_thumbnailer
from workers.base import WorkerError


# Try to import Pillow once at module load. Tests that NEED it skip
# if it's missing on the host; tests that PROVE the NO_LIB branch
# don't care.
try:
    from PIL import Image as _PIL  # type: ignore
    HAS_PIL = True
except ImportError:
    _PIL = None
    HAS_PIL = False


# ── FakeConn / FakeCursor — mirrors the pattern in other workers.


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
        self.all_sql: list[tuple[str, object]] = []
        self.fetchone_queue: list[object] = []
        self.fetchall_queue: list[list] = []
        self.rowcount_queue: list[int] = []

    def cursor(self):
        c = FakeCursor(self)
        self.cursors.append(c)
        return c

    def commit(self):
        self.commits += 1


# ── Helpers


def _make_png(path: Path, w: int = 600, h: int = 800) -> None:
    """Write a real PNG at the given path. Requires Pillow."""
    if not HAS_PIL:
        raise unittest.SkipTest("Pillow not installed in test env")
    img = _PIL.new("RGB", (w, h), color=(180, 60, 60))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")


def _make_task(sid="sv2", num="47"):
    return {"task_id": 1, "task_type": "image_thumbnail",
            "task_key": f"{sid}/{num}",
            "payload": {"set_id": sid, "card_number": num},
            "attempts": 0}


def _png_size(path: Path) -> tuple[int, int]:
    with _PIL.open(path) as im:
        return im.size


# ── _thumb_path / _is_thumb_fresh


class HelperTests(unittest.TestCase):

    def test_thumb_path_layout(self):
        root = Path("/mnt/cards/thumbs")
        self.assertEqual(
            image_thumbnailer._thumb_path(root, 200, "sv2", "47"),
            Path("/mnt/cards/thumbs/200/sv2/47.webp"),
        )
        self.assertEqual(
            image_thumbnailer._thumb_path(root, 800, "sv5kpre", "PRE-001"),
            Path("/mnt/cards/thumbs/800/sv5kpre/PRE-001.webp"),
        )

    def test_is_thumb_fresh_missing(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(image_thumbnailer._is_thumb_fresh(
                Path(td) / "nope.webp", time.time()))

    def test_is_thumb_fresh_newer_source(self):
        with tempfile.NamedTemporaryFile(suffix=".webp", delete=False) as f:
            f.write(b"x" * 10)
            tp = Path(f.name)
        try:
            os.utime(tp, (1_700_000_000, 1_700_000_000))
            # Source is newer than thumb → NOT fresh
            self.assertFalse(image_thumbnailer._is_thumb_fresh(
                tp, 1_700_001_000))
        finally:
            tp.unlink()

    def test_is_thumb_fresh_equal_or_newer_thumb(self):
        with tempfile.NamedTemporaryFile(suffix=".webp", delete=False) as f:
            f.write(b"x" * 10)
            tp = Path(f.name)
        try:
            os.utime(tp, (1_700_001_000, 1_700_001_000))
            # Equal mtime → fresh (FAT32 2-second-resolution
            # avoidance documented in the helper).
            self.assertTrue(image_thumbnailer._is_thumb_fresh(
                tp, 1_700_001_000))
            # Older source → fresh
            self.assertTrue(image_thumbnailer._is_thumb_fresh(
                tp, 1_700_000_000))
        finally:
            tp.unlink()


# ── Worker contract


class WorkerWiringTests(unittest.TestCase):

    def test_task_type_is_image_thumbnail(self):
        self.assertEqual(image_thumbnailer.ImageThumbnailerWorker.TASK_TYPE,
                         "image_thumbnail")

    def test_default_sizes_are_200_and_800(self):
        # Documented in the docstring; locking it in so a future
        # refactor that drops a tier requires a deliberate test
        # update rather than a silent UI regression.
        self.assertEqual(image_thumbnailer.DEFAULT_SIZES, (200, 800))

    def test_explicit_false_disables_pil(self):
        w = image_thumbnailer.ImageThumbnailerWorker(
            FakeConn(), pil_module=False)
        self.assertTrue(w._pil_disabled)
        self.assertIsNone(w._ensure_pil())


# ── Process — error / short-circuit branches


class ProcessShortCircuitTests(unittest.TestCase):

    def _w(self, **kw):
        return image_thumbnailer.ImageThumbnailerWorker(FakeConn(), **kw)

    def test_missing_payload_raises(self):
        w = self._w(pil_module=False)
        with self.assertRaises(WorkerError):
            w.process({"task_id": 1, "payload": {}})

    def test_missing_set_id_raises(self):
        w = self._w(pil_module=False)
        with self.assertRaises(WorkerError):
            w.process({"task_id": 1,
                       "payload": {"set_id": "", "card_number": "47"}})

    def test_no_lib_short_circuits_before_db(self):
        # When Pillow is unavailable, the worker must NOT touch the
        # database — the queue should mark the task COMPLETED with
        # NO_LIB and move on. This keeps dead Pi-without-Pillow runs
        # cheap.
        conn = FakeConn()
        w = image_thumbnailer.ImageThumbnailerWorker(
            conn, pil_module=False)
        rv = w.process(_make_task())
        self.assertEqual(rv, {"status": "NO_LIB"})
        self.assertEqual(conn.all_sql, [])
        self.assertEqual(conn.commits, 0)

    def test_missing_card_returns_missing_card(self):
        if not HAS_PIL:
            self.skipTest("Pillow required for this branch")
        conn = FakeConn()
        conn.fetchone_queue = [None]  # cards_master row gone
        w = image_thumbnailer.ImageThumbnailerWorker(
            conn, pil_module=_PIL)
        rv = w.process(_make_task())
        self.assertEqual(rv["status"], "MISSING_CARD")

    def test_no_usable_source_when_all_local_paths_missing(self):
        if not HAS_PIL:
            self.skipTest("Pillow required for this branch")
        conn = FakeConn()
        conn.fetchone_queue = [([
            {"src": "x", "lang": "en", "url": "u",
             "local": "/no/such/a.png"},
            {"src": "y", "lang": "en", "url": "u",
             "local": "/no/such/b.png"},
        ],)]
        w = image_thumbnailer.ImageThumbnailerWorker(
            conn, pil_module=_PIL)
        rv = w.process(_make_task())
        self.assertEqual(rv["status"], "NO_USABLE_SOURCE")
        self.assertEqual(rv["paths_seen"], 2)

    def test_too_small_source_is_skipped(self):
        if not HAS_PIL:
            self.skipTest("Pillow required for this branch")
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG")  # 4 bytes, well under min
            tiny = f.name
        try:
            conn = FakeConn()
            conn.fetchone_queue = [([
                {"src": "x", "lang": "en", "url": "u", "local": tiny},
            ],)]
            w = image_thumbnailer.ImageThumbnailerWorker(
                conn, pil_module=_PIL)
            rv = w.process(_make_task())
            self.assertEqual(rv["status"], "NO_USABLE_SOURCE")
        finally:
            os.unlink(tiny)


# ── Process — happy paths (require real Pillow)


@unittest.skipUnless(HAS_PIL, "Pillow not installed in test env")
class ProcessHappyPathTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.thumb_root = Path(self.tmp) / "thumbs"
        self.source = Path(self.tmp) / "src" / "card.png"
        _make_png(self.source, 600, 800)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _conn(self, source_path):
        conn = FakeConn()
        conn.fetchone_queue = [([
            {"src": "tcgo", "lang": "en", "url": "u",
             "local": str(source_path)},
        ],)]
        return conn

    def _worker(self, conn, **kw):
        return image_thumbnailer.ImageThumbnailerWorker(
            conn, thumb_root=self.thumb_root, pil_module=_PIL, **kw)

    def test_generates_both_sizes(self):
        conn = self._conn(self.source)
        rv = self._worker(conn).process(_make_task())
        self.assertEqual(rv["status"], "OK")
        self.assertEqual(sorted(rv["generated"]), [200, 800])
        self.assertEqual(rv["skipped"], [])
        self.assertEqual(rv["failed"], [])
        # Files exist with correct on-disk layout
        thumb200 = self.thumb_root / "200" / "sv2" / "47.webp"
        thumb800 = self.thumb_root / "800" / "sv2" / "47.webp"
        self.assertTrue(thumb200.is_file())
        self.assertTrue(thumb800.is_file())
        # Longest-edge resize, aspect preserved
        w200, h200 = _png_size(thumb200)
        self.assertEqual(max(w200, h200), 200)
        # 600×800 source → 150×200 thumb (aspect 3:4)
        self.assertEqual((w200, h200), (150, 200))
        w800, h800 = _png_size(thumb800)
        self.assertEqual((w800, h800), (600, 800))

    def test_no_upscale_on_small_source(self):
        # 100×100 source is smaller than both thumb tiers — must not
        # be upscaled. Both thumbs should be 100×100, not 200×200.
        small = Path(self.tmp) / "src" / "small.png"
        _make_png(small, 100, 100)
        conn = self._conn(small)
        rv = self._worker(conn).process(_make_task())
        self.assertEqual(rv["status"], "OK")
        self.assertEqual(_png_size(self.thumb_root / "200" / "sv2" / "47.webp"),
                         (100, 100))
        self.assertEqual(_png_size(self.thumb_root / "800" / "sv2" / "47.webp"),
                         (100, 100))

    def test_idempotent_when_thumbs_are_fresh(self):
        # First run generates both. Second run with the same source
        # mtime must skip both — not a single byte rewritten.
        conn1 = self._conn(self.source)
        rv1 = self._worker(conn1).process(_make_task())
        self.assertEqual(sorted(rv1["generated"]), [200, 800])
        thumb200 = self.thumb_root / "200" / "sv2" / "47.webp"
        first_mtime = thumb200.stat().st_mtime
        # Sleep enough to detect a real rewrite (mtime resolution).
        time.sleep(0.05)
        conn2 = self._conn(self.source)
        rv2 = self._worker(conn2).process(_make_task())
        self.assertEqual(rv2["status"], "OK")
        self.assertEqual(sorted(rv2["skipped"]), [200, 800])
        self.assertEqual(rv2["generated"], [])
        # Mtime unchanged — no rewrite happened.
        self.assertEqual(thumb200.stat().st_mtime, first_mtime)

    def test_regenerates_when_source_is_newer(self):
        conn1 = self._conn(self.source)
        self._worker(conn1).process(_make_task())
        thumb200 = self.thumb_root / "200" / "sv2" / "47.webp"
        # Backdate the thumb to BEFORE the source mtime → must regen.
        old = self.source.stat().st_mtime - 100
        os.utime(thumb200, (old, old))
        conn2 = self._conn(self.source)
        rv = self._worker(conn2).process(_make_task())
        self.assertIn(200, rv["generated"])
        # 800 thumb was untouched, so still fresh.
        self.assertIn(800, rv["skipped"])

    def test_one_size_already_present(self):
        # Pre-create only the 200 thumb (fresh). Worker must skip
        # 200 and generate 800.
        thumb200 = self.thumb_root / "200" / "sv2" / "47.webp"
        thumb200.parent.mkdir(parents=True, exist_ok=True)
        # Use Pillow to write a valid 50×50 webp so the file is real.
        _PIL.new("RGB", (50, 50)).save(thumb200, format="WEBP")
        # Future-date the thumb so it counts as fresh against the
        # source mtime regardless of how fast the test ran.
        future = self.source.stat().st_mtime + 1000
        os.utime(thumb200, (future, future))
        conn = self._conn(self.source)
        rv = self._worker(conn).process(_make_task())
        self.assertEqual(rv["status"], "OK")
        self.assertEqual(rv["skipped"], [200])
        self.assertEqual(rv["generated"], [800])

    def test_directory_created_for_new_set(self):
        # A brand-new set_id has no /thumbs/<size>/<set>/ directory
        # yet — _render must mkdir parents.
        new_thumb = self.thumb_root / "200" / "sv2" / "47.webp"
        self.assertFalse(new_thumb.parent.exists())
        conn = self._conn(self.source)
        rv = self._worker(conn).process(_make_task())
        self.assertEqual(rv["status"], "OK")
        self.assertTrue(new_thumb.is_file())

    def test_string_payload_jsonb_decoded(self):
        # When the cursor adapter delivers JSONB as a string, the
        # worker still finds the local source.
        conn = FakeConn()
        conn.fetchone_queue = [(json.dumps([
            {"src": "tcgo", "lang": "en", "url": "u",
             "local": str(self.source)},
        ]),)]
        rv = self._worker(conn).process(_make_task())
        self.assertEqual(rv["status"], "OK")
        self.assertEqual(sorted(rv["generated"]), [200, 800])

    def test_atomic_write_no_tmp_left_on_pil_failure(self):
        # Simulate a PIL save failure by injecting a fake module
        # whose open() returns an object that raises on save().
        class BadImg:
            mode = "RGB"
            size = (600, 800)
            def thumbnail(self, *a, **kw): pass
            def convert(self, *a, **kw): return self
            def save(self, *a, **kw):
                raise RuntimeError("disk full simulation")

        class BadPIL:
            @staticmethod
            def open(buf):
                return BadImg()
            @staticmethod
            def new(*a, **kw):
                return _PIL.new(*a, **kw)

        conn = self._conn(self.source)
        rv = image_thumbnailer.ImageThumbnailerWorker(
            conn, thumb_root=self.thumb_root, pil_module=BadPIL,
        ).process(_make_task())
        self.assertEqual(rv["status"], "FAILED")
        self.assertEqual(len(rv["failed"]), 2)
        # No leftover .tmp.* in either size dir
        for size in (200, 800):
            d = self.thumb_root / str(size) / "sv2"
            if d.exists():
                leftover = [p for p in d.iterdir()
                            if p.name.startswith(".tmp.")]
                self.assertEqual(leftover, [],
                                 f"tmp file leaked in {d}")
        # And no real thumb created
        self.assertFalse((self.thumb_root / "200" / "sv2" / "47.webp").exists())

    def test_partial_failure_marks_partial(self):
        # First size succeeds, second size fails. We achieve this by
        # making the destination directory for size 800 unwritable.
        if os.geteuid() == 0:
            self.skipTest("Permission test meaningless as root")
        conn = self._conn(self.source)
        # Pre-create the parent of the 800 destination as a FILE so
        # mkdir(parents=True) inside _render fails.
        bad_parent = self.thumb_root / "800" / "sv2"
        bad_parent.parent.mkdir(parents=True, exist_ok=True)
        bad_parent.write_bytes(b"sentinel: not a directory")
        rv = self._worker(conn).process(_make_task())
        self.assertEqual(rv["status"], "PARTIAL")
        self.assertEqual(rv["generated"], [200])
        self.assertEqual(len(rv["failed"]), 1)
        self.assertEqual(rv["failed"][0]["size"], 800)


# ── Seed


class SeedTests(unittest.TestCase):

    def test_seed_joins_image_health_and_filters_to_ok(self):
        conn = FakeConn()
        conn.rowcount_queue = [42]
        w = image_thumbnailer.ImageThumbnailerWorker(
            conn, pil_module=False)
        n = w.seed()
        self.assertEqual(n, 42)
        sql, _ = conn.cursors[0].executed[0]
        self.assertIn("INSERT INTO bg_task_queue", sql)
        self.assertIn("'image_thumbnail'", sql)
        # Latest health verdict only (no enqueueing on stale OK)
        self.assertIn("ORDER BY h.checked_at DESC", sql)
        self.assertIn("LIMIT 1", sql)
        self.assertIn("latest.status = 'OK'", sql)
        self.assertIn("jsonb_array_elements", sql)
        self.assertIn("ON CONFLICT (task_type, task_key) DO NOTHING", sql)


if __name__ == "__main__":
    unittest.main()
