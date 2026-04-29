#!/usr/bin/env python3
"""
test_image_mirror.py — unit tests for workers/image_mirror.py.

Covers _check_path, _atomic_download, and ImageMirrorWorker.process
end to end, with a hermetic fake urlopen so the suite never hits the
network and stays deterministic on CI / sandbox.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

PI_SETUP = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PI_SETUP))

from workers import image_mirror  # noqa: E402
from workers.base import WorkerError  # noqa: E402


# Minimal valid 1×1 PNG — same one image_health tests use.
PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63000100000005000100"
    "5d0c6a25"
    "0000000049454e44ae426082"
)
JPEG_HEADER = bytes.fromhex("ffd8ffe000104a464946000101") + b"\x00" * 300


# ── Fakes ───────────────────────────────────────────────────────


class FakeResponse:
    """Mimics the file-like context manager urlopen returns."""
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status
        self._buf = io.BytesIO(body)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self._buf.read(n)

    def readinto(self, buf):
        data = self._buf.read(len(buf))
        buf[:len(data)] = data
        return len(data)


def make_urlopen(responses: dict[str, object]):
    """Build an urlopen_fn that returns a FakeResponse keyed by URL,
    or raises if `responses[url]` is an Exception."""
    calls: list[str] = []

    def _urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        calls.append(url)
        r = responses.get(url)
        if isinstance(r, BaseException):
            raise r
        if r is None:
            raise HTTPError(url, 404, "not found", {}, None)
        return r

    _urlopen.calls = calls  # type: ignore[attr-defined]
    return _urlopen


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.executed: list[tuple[str, object]] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        if self.conn.fetchone_queue:
            return self.conn.fetchone_queue.pop(0)
        return None


class FakeConn:
    def __init__(self):
        self.fetchone_queue: list[object] = []
        self.cursors: list[FakeCursor] = []
        self.commits = 0

    def cursor(self):
        c = FakeCursor(self)
        self.cursors.append(c)
        return c

    def commit(self):
        self.commits += 1


class FakePILGood:
    """Pillow stand-in whose verify() always succeeds."""
    @staticmethod
    def open(_buf):
        class _I:
            def verify(self):
                return None
        return _I()


class FakePILBad:
    """Pillow stand-in whose verify() always raises."""
    @staticmethod
    def open(_buf):
        class _I:
            def verify(self):
                raise OSError("decode failed")
        return _I()


# ── _check_path ─────────────────────────────────────────────────


class TestCheckPath(unittest.TestCase):
    def test_empty_string_returns_missing(self):
        self.assertEqual(image_mirror._check_path(""), "MISSING")

    def test_nonexistent_returns_missing(self):
        self.assertEqual(image_mirror._check_path("/nope/abc.png"), "MISSING")

    def test_zero_byte_returns_empty(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            p = f.name
        self.addCleanup(os.unlink, p)
        self.assertEqual(image_mirror._check_path(p), "EMPTY")

    def test_too_small_returns_too_small(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"x" * 10)
            p = f.name
        self.addCleanup(os.unlink, p)
        self.assertEqual(image_mirror._check_path(p), "TOO_SMALL")

    def test_garbage_no_magic_returns_corrupt(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"not an image at all but big enough" * 10)
            p = f.name
        self.addCleanup(os.unlink, p)
        self.assertEqual(image_mirror._check_path(p), "CORRUPT")

    def test_png_returns_ok_no_pil(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as f:
            f.write(PNG_1x1)
            p = f.name
        self.addCleanup(os.unlink, p)
        self.assertEqual(image_mirror._check_path(p), "OK")

    def test_jpeg_returns_ok_no_pil(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
            f.write(JPEG_HEADER)
            p = f.name
        self.addCleanup(os.unlink, p)
        self.assertEqual(image_mirror._check_path(p), "OK")

    def test_pil_verify_ok_returns_ok(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as f:
            f.write(PNG_1x1)
            p = f.name
        self.addCleanup(os.unlink, p)
        self.assertEqual(
            image_mirror._check_path(p, pil_module=FakePILGood), "OK")

    def test_pil_verify_failure_returns_corrupt(self):
        # Magic-byte sniff would pass, but PIL.verify throws.
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as f:
            f.write(PNG_1x1 + b"\x00" * 100)
            p = f.name
        self.addCleanup(os.unlink, p)
        self.assertEqual(
            image_mirror._check_path(p, pil_module=FakePILBad), "CORRUPT")


# ── _atomic_download ────────────────────────────────────────────


class TestAtomicDownload(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._rmtree, self.tmpdir)

    @staticmethod
    def _rmtree(p):
        import shutil
        shutil.rmtree(p, ignore_errors=True)

    def test_happy_path_writes_file(self):
        url = "https://cdn.example.com/a.png"
        dest = os.path.join(self.tmpdir, "subdir", "a.png")
        urlopen_fn = make_urlopen({url: FakeResponse(PNG_1x1 * 20)})
        ok, status = image_mirror._atomic_download(
            url, dest, urlopen_fn=urlopen_fn, min_size=64)
        self.assertTrue(ok, status)
        self.assertEqual(status, "ok")
        self.assertTrue(os.path.exists(dest))
        # No leftover .tmp file in the parent.
        leftovers = [f for f in os.listdir(os.path.dirname(dest))
                     if f.startswith(".im_mirror.")]
        self.assertEqual(leftovers, [])

    def test_too_small_returns_failure_and_no_dest(self):
        url = "https://cdn.example.com/tiny.png"
        dest = os.path.join(self.tmpdir, "tiny.png")
        urlopen_fn = make_urlopen({url: FakeResponse(b"x" * 8)})
        ok, status = image_mirror._atomic_download(
            url, dest, urlopen_fn=urlopen_fn, min_size=256)
        self.assertFalse(ok)
        self.assertEqual(status, "too-small")
        self.assertFalse(os.path.exists(dest))

    def test_http_error_returns_status_no_dest(self):
        url = "https://cdn.example.com/missing.png"
        dest = os.path.join(self.tmpdir, "missing.png")
        urlopen_fn = make_urlopen(
            {url: HTTPError(url, 404, "not found", {}, None)})
        ok, status = image_mirror._atomic_download(
            url, dest, urlopen_fn=urlopen_fn)
        self.assertFalse(ok)
        self.assertEqual(status, "http-404")
        self.assertFalse(os.path.exists(dest))

    def test_network_exception_cleans_tmp(self):
        url = "https://cdn.example.com/timeout.png"
        dest = os.path.join(self.tmpdir, "timeout.png")
        urlopen_fn = make_urlopen({url: TimeoutError("slow")})
        ok, status = image_mirror._atomic_download(
            url, dest, urlopen_fn=urlopen_fn)
        self.assertFalse(ok)
        self.assertTrue(status.startswith("err-"))
        self.assertFalse(os.path.exists(dest))
        leftovers = [f for f in os.listdir(self.tmpdir)
                     if f.startswith(".im_mirror.")]
        self.assertEqual(leftovers, [])

    def test_creates_parent_dir(self):
        url = "https://cdn.example.com/nested.png"
        dest = os.path.join(self.tmpdir, "a", "b", "c", "nested.png")
        urlopen_fn = make_urlopen({url: FakeResponse(PNG_1x1 * 20)})
        ok, status = image_mirror._atomic_download(
            url, dest, urlopen_fn=urlopen_fn, min_size=64)
        self.assertTrue(ok, status)
        self.assertTrue(os.path.exists(dest))

    def test_overwrites_existing_dest(self):
        url = "https://cdn.example.com/over.png"
        dest = os.path.join(self.tmpdir, "over.png")
        # Pre-existing corrupt file
        with open(dest, "wb") as f:
            f.write(b"GARBAGE" * 50)
        urlopen_fn = make_urlopen({url: FakeResponse(PNG_1x1 * 20)})
        ok, _ = image_mirror._atomic_download(
            url, dest, urlopen_fn=urlopen_fn, min_size=64)
        self.assertTrue(ok)
        with open(dest, "rb") as f:
            head = f.read(8)
        # Now starts with PNG magic, not garbage.
        self.assertEqual(head, b"\x89PNG\r\n\x1a\n")


# ── ImageMirrorWorker.process ──────────────────────────────────


class TestImageMirrorWorker(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._rmtree, self.tmpdir)
        self.conn = FakeConn()

    @staticmethod
    def _rmtree(p):
        import shutil
        shutil.rmtree(p, ignore_errors=True)

    def _make_worker(self, urlopen_fn=None, pil_module=False):
        # pil_module defaults to False (explicitly disabled) so the
        # test result doesn't depend on whether the runtime has Pillow
        # installed. PNG_1x1 * N is a valid magic match but NOT a
        # valid decodable PNG, and we want the magic-only path here.
        # The dedicated PIL test passes a fake module to override this.
        if urlopen_fn is None:
            urlopen_fn = make_urlopen({})
        return image_mirror.ImageMirrorWorker(
            self.conn,
            urlopen_fn=urlopen_fn,
            pil_module=pil_module,
            min_size=64,
        )

    def test_missing_payload_raises_workererror(self):
        w = self._make_worker()
        with self.assertRaises(WorkerError):
            w.process({"task_id": 1, "payload": {"set_id": ""}})

    def test_no_card_row_returns_missing_card(self):
        w = self._make_worker()
        # fetchone returns None → MISSING_CARD
        out = w.process({"task_id": 2,
                         "payload": {"set_id": "sv2", "card_number": "47"}})
        self.assertEqual(out["status"], "MISSING_CARD")

    def test_no_local_paths_returns_no_paths(self):
        # row has alt entries but none have a 'local' set
        self.conn.fetchone_queue.append(
            ([{"src": "x", "url": "https://e/x.png", "local": ""}],))
        w = self._make_worker()
        out = w.process({"task_id": 3,
                         "payload": {"set_id": "sv2", "card_number": "47"}})
        self.assertEqual(out["status"], "NO_PATHS")

    def test_already_ok_skips_fetch(self):
        # Pre-create an OK file so _check_path returns OK
        local = os.path.join(self.tmpdir, "ok.png")
        with open(local, "wb") as f:
            f.write(PNG_1x1 * 20)
        self.conn.fetchone_queue.append(
            ([{"src": "x", "url": "https://e/x.png", "local": local}],))
        urlopen_fn = make_urlopen({})  # would raise if called
        w = self._make_worker(urlopen_fn=urlopen_fn)
        out = w.process({"task_id": 4,
                         "payload": {"set_id": "sv2", "card_number": "47"}})
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["paths_already_ok"], 1)
        self.assertEqual(out["paths_attempted"], 0)
        self.assertEqual(urlopen_fn.calls, [])

    def test_recovers_missing_file(self):
        local = os.path.join(self.tmpdir, "missing.png")
        url = "https://cdn.example.com/missing.png"
        self.conn.fetchone_queue.append(
            ([{"src": "jp_pcc", "url": url, "local": local}],))
        urlopen_fn = make_urlopen({url: FakeResponse(PNG_1x1 * 20)})
        w = self._make_worker(urlopen_fn=urlopen_fn)
        out = w.process({"task_id": 5,
                         "payload": {"set_id": "sv2", "card_number": "47"}})
        self.assertEqual(out["status"], "RECOVERED")
        self.assertEqual(out["paths_recovered"], 1)
        self.assertEqual(out["paths_still_broken"], 0)
        self.assertTrue(os.path.exists(local))

    def test_partial_one_recovered_one_still_broken(self):
        ok_local = os.path.join(self.tmpdir, "rec.png")
        bad_local = os.path.join(self.tmpdir, "bad.png")
        ok_url = "https://e/rec.png"
        bad_url = "https://e/bad.png"
        self.conn.fetchone_queue.append((
            [
                {"src": "a", "url": ok_url,  "local": ok_local},
                {"src": "b", "url": bad_url, "local": bad_local},
            ],
        ))
        urlopen_fn = make_urlopen({
            ok_url: FakeResponse(PNG_1x1 * 20),
            bad_url: HTTPError(bad_url, 500, "boom", {}, None),
        })
        w = self._make_worker(urlopen_fn=urlopen_fn)
        out = w.process({"task_id": 6,
                         "payload": {"set_id": "sv2", "card_number": "47"}})
        self.assertEqual(out["status"], "PARTIAL")
        self.assertEqual(out["paths_recovered"], 1)
        self.assertEqual(out["paths_still_broken"], 1)

    def test_no_url_for_broken_file_marked_still_broken(self):
        local = os.path.join(self.tmpdir, "noUrl.png")  # missing on disk
        self.conn.fetchone_queue.append(
            ([{"src": "x", "url": "", "local": local}],))
        urlopen_fn = make_urlopen({})  # would raise if called
        w = self._make_worker(urlopen_fn=urlopen_fn)
        out = w.process({"task_id": 7,
                         "payload": {"set_id": "sv2", "card_number": "47"}})
        self.assertEqual(out["status"], "FAILED")
        self.assertEqual(out["paths_still_broken"], 1)
        self.assertEqual(urlopen_fn.calls, [])

    def test_failed_fetch_marks_still_broken(self):
        local = os.path.join(self.tmpdir, "bad.png")
        url = "https://cdn.example.com/bad.png"
        self.conn.fetchone_queue.append(
            ([{"src": "x", "url": url, "local": local}],))
        urlopen_fn = make_urlopen(
            {url: HTTPError(url, 404, "nf", {}, None)})
        w = self._make_worker(urlopen_fn=urlopen_fn)
        out = w.process({"task_id": 8,
                         "payload": {"set_id": "sv2", "card_number": "47"}})
        self.assertEqual(out["status"], "FAILED")
        self.assertEqual(out["paths_still_broken"], 1)

    def test_fetch_returns_garbage_marks_still_broken(self):
        local = os.path.join(self.tmpdir, "garb.png")
        url = "https://cdn.example.com/garb.png"
        self.conn.fetchone_queue.append(
            ([{"src": "x", "url": url, "local": local}],))
        # Body is big enough to pass min-size, but no magic bytes.
        urlopen_fn = make_urlopen(
            {url: FakeResponse(b"not an image" * 50)})
        w = self._make_worker(urlopen_fn=urlopen_fn)
        out = w.process({"task_id": 9,
                         "payload": {"set_id": "sv2", "card_number": "47"}})
        self.assertEqual(out["status"], "FAILED")
        self.assertEqual(out["paths_still_broken"], 1)
        # The downloaded bytes ARE on disk now (atomic rename
        # happened) — the next image_health pass will see CORRUPT
        # and re-enqueue.
        self.assertTrue(os.path.exists(local))

    def test_string_image_url_alt_payload_decoded(self):
        local = os.path.join(self.tmpdir, "str.png")
        url = "https://cdn.example.com/str.png"
        self.conn.fetchone_queue.append((
            json.dumps([{"src": "x", "url": url, "local": local}]),
        ))
        urlopen_fn = make_urlopen({url: FakeResponse(PNG_1x1 * 20)})
        w = self._make_worker(urlopen_fn=urlopen_fn)
        out = w.process({"task_id": 10,
                         "payload": {"set_id": "sv2", "card_number": "47"}})
        self.assertEqual(out["status"], "RECOVERED")

    def test_pil_injected_used_for_verify(self):
        local = os.path.join(self.tmpdir, "pil.png")
        # Pre-existing PNG passes magic but the bad PIL will reject it.
        with open(local, "wb") as f:
            f.write(PNG_1x1 * 20)
        url = "https://cdn.example.com/pil.png"
        # Source is "OK" via magic but FakePILBad will report CORRUPT,
        # which triggers a re-fetch attempt.
        self.conn.fetchone_queue.append(
            ([{"src": "x", "url": url, "local": local}],))
        urlopen_fn = make_urlopen({url: FakeResponse(PNG_1x1 * 20)})
        w = self._make_worker(urlopen_fn=urlopen_fn,
                              pil_module=FakePILBad)
        out = w.process({"task_id": 11,
                         "payload": {"set_id": "sv2", "card_number": "47"}})
        # Re-fetch succeeded but PIL still rejects → still broken.
        self.assertEqual(out["status"], "FAILED")
        self.assertEqual(out["paths_attempted"], 1)
        self.assertEqual(urlopen_fn.calls, [url])

    def test_lazy_pil_import_when_not_injected(self):
        # When pil_module is None (auto-detect mode), _ensure_pil
        # should attempt the import once, cache the outcome, and
        # never raise. We don't need Pillow installed to assert this:
        # subsequent calls return the cached value via _pil_tried.
        w = self._make_worker(pil_module=None)
        first = w._ensure_pil()
        second = w._ensure_pil()
        # Both calls return the same object (None or a real Image
        # module — both are valid in the sandbox).
        self.assertIs(first, second)
        self.assertTrue(w._pil_tried)
        self.assertFalse(w._pil_disabled)

    def test_explicit_false_disables_pil(self):
        w = self._make_worker(pil_module=False)
        self.assertTrue(w._pil_disabled)
        self.assertIsNone(w._ensure_pil())
        # Even after a call, still disabled — never tries to import.
        self.assertIsNone(w._ensure_pil())


# ── Worker class wiring ────────────────────────────────────────


class TestWorkerWiring(unittest.TestCase):
    def test_task_type_set(self):
        self.assertEqual(image_mirror.ImageMirrorWorker.TASK_TYPE,
                         "image_mirror")

    def test_batch_size_and_timeouts_reasonable(self):
        cls = image_mirror.ImageMirrorWorker
        # 1..50 inclusive — slow downloads keep batches small.
        self.assertGreaterEqual(cls.BATCH_SIZE, 1)
        self.assertLessEqual(cls.BATCH_SIZE, 50)
        # At least 1 minute claim timeout — CDN can be slow.
        self.assertGreaterEqual(cls.CLAIM_TIMEOUT_S, 60)


if __name__ == "__main__":
    unittest.main()
