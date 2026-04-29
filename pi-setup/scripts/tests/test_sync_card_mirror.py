#!/usr/bin/env python3
"""
test_sync_card_mirror.py — unit tests for sync_card_mirror._download.

Slice 22 added HTTP If-Modified-Since revalidation to the downloader so
re-running Phase C against a fully-mirrored drive becomes thousands of
free 304s instead of either thousands of zero-cost skip-exists shortcuts
(which never catch upstream churn) or thousands of full-body re-fetches
(which would melt the booth WiFi).

These tests cover the full _download contract — fresh download, atomic
write semantics, IMS header injection, 304 handling via both branches
of urllib (response status AND HTTPError), Last-Modified mtime
stamping, and the revalidate=False escape hatch — using a hermetic
fake urlopen so the suite never touches the network.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import time
import unittest
from email.utils import formatdate
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

PI_SETUP = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PI_SETUP))

from scripts import sync_card_mirror as scm  # noqa: E402


class _FakeResponse:
    """Minimal urlopen response stand-in. Context-manager + .read()."""

    def __init__(self, *, status: int = 200, body: bytes = b"",
                 headers: dict | None = None):
        self.status = status
        self._body = body
        # Mimic urllib's HTTPMessage just enough — .get() is all we use.
        self.headers = headers or {}

    def read(self, n: int = -1) -> bytes:
        if n < 0 or n >= len(self._body):
            out, self._body = self._body, b""
            return out
        out, self._body = self._body[:n], self._body[n:]
        return out

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_urlopen(fn):
    """Patch urllib.request.urlopen on the sync_card_mirror module."""
    return mock.patch.object(scm.urllib.request, "urlopen", fn)


class DownloadFreshTests(unittest.TestCase):
    """Behaviour when `dest` does not yet exist — the simple path."""

    def test_writes_body_atomically(self):
        body = b"x" * 1024
        captured: dict = {}

        def fake_urlopen(req, timeout=30):
            captured["req"] = req
            return _FakeResponse(body=body)

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "sub" / "img.png"
            with _patch_urlopen(fake_urlopen):
                ok, status = scm._download("https://x/img.png", dest)
            self.assertTrue(ok)
            self.assertEqual(status, "ok")
            self.assertTrue(dest.exists())
            self.assertEqual(dest.read_bytes(), body)
            # tmp must be cleaned up by the rename
            self.assertFalse(dest.with_suffix(".png.tmp").exists())

    def test_no_ims_header_when_dest_missing(self):
        # First-time fetch must NOT send If-Modified-Since — there's
        # nothing to compare against, and some servers 412 on a
        # malformed conditional.
        captured: dict = {}

        def fake_urlopen(req, timeout=30):
            captured["headers"] = dict(req.header_items())
            return _FakeResponse(body=b"x" * 1024)

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "img.png"
            with _patch_urlopen(fake_urlopen):
                scm._download("https://x/img.png", dest)
        # urllib title-cases header names
        keys_lower = {k.lower() for k in captured["headers"]}
        self.assertNotIn("if-modified-since", keys_lower)
        self.assertIn("user-agent", keys_lower)

    def test_user_agent_set(self):
        captured: dict = {}

        def fake_urlopen(req, timeout=30):
            captured["headers"] = dict(req.header_items())
            return _FakeResponse(body=b"x" * 1024)

        with tempfile.TemporaryDirectory() as td:
            with _patch_urlopen(fake_urlopen):
                scm._download("https://x/img.png", Path(td) / "img.png")
        ua = next(v for k, v in captured["headers"].items()
                  if k.lower() == "user-agent")
        self.assertIn("HanryxVault-mirror", ua)


class DownloadFailureTests(unittest.TestCase):
    """Negative-path behaviour — failures must NEVER leave a tmp file."""

    def test_too_small_body_treated_as_failed(self):
        # Default min_size=256 — a 10-byte body is the classic
        # "CDN returned a 1×1 placeholder" failure mode.
        def fake_urlopen(req, timeout=30):
            return _FakeResponse(body=b"x" * 10)

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "img.png"
            with _patch_urlopen(fake_urlopen):
                ok, status = scm._download("https://x/img.png", dest)
            self.assertFalse(ok)
            self.assertEqual(status, "too-small")
            self.assertFalse(dest.exists())
            self.assertFalse(dest.with_suffix(".png.tmp").exists())

    def test_http_404(self):
        def fake_urlopen(req, timeout=30):
            raise HTTPError(req.full_url, 404, "Not Found", {}, None)

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "img.png"
            with _patch_urlopen(fake_urlopen):
                ok, status = scm._download("https://x/img.png", dest)
            self.assertFalse(ok)
            self.assertEqual(status, "http-404")
            self.assertFalse(dest.exists())

    def test_network_exception(self):
        def fake_urlopen(req, timeout=30):
            raise ConnectionResetError("upstream RST")

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "img.png"
            with _patch_urlopen(fake_urlopen):
                ok, status = scm._download("https://x/img.png", dest)
            self.assertFalse(ok)
            self.assertEqual(status, "err-ConnectionResetError")
            self.assertFalse(dest.exists())


class DownloadRevalidateTests(unittest.TestCase):
    """Slice 22 surface — IMS revalidation when `dest` already exists."""

    def _make_existing(self, size: int = 1024) -> Path:
        td = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(td,
                                                            ignore_errors=True))
        dest = Path(td) / "img.png"
        dest.write_bytes(b"y" * size)
        # Pin mtime to a known historical timestamp so we can assert
        # the IMS header is correct without race-ing the clock.
        os.utime(dest, (1700000000, 1700000000))
        return dest

    def test_skip_exists_short_circuits_when_revalidate_false(self):
        # The opt-out path: existing file, revalidate=False → no
        # network at all.
        called = {"n": 0}

        def fake_urlopen(req, timeout=30):
            called["n"] += 1
            return _FakeResponse(body=b"x" * 1024)

        dest = self._make_existing()
        with _patch_urlopen(fake_urlopen):
            ok, status = scm._download("https://x/img.png", dest,
                                       revalidate=False)
        self.assertTrue(ok)
        self.assertEqual(status, "skip-exists")
        self.assertEqual(called["n"], 0)

    def test_sends_if_modified_since_header_when_revalidate_true(self):
        captured: dict = {}

        def fake_urlopen(req, timeout=30):
            captured["headers"] = dict(req.header_items())
            return _FakeResponse(status=304)

        dest = self._make_existing()
        with _patch_urlopen(fake_urlopen):
            scm._download("https://x/img.png", dest)

        ims = next(v for k, v in captured["headers"].items()
                   if k.lower() == "if-modified-since")
        # Expected IMF-fixdate form for our pinned mtime.
        self.assertEqual(ims, formatdate(1700000000, usegmt=True))

    def test_304_returns_not_modified_no_write(self):
        # Body must NOT be touched — the file is bytewise unchanged
        # and we shouldn't even create the tmp file on a 304.
        original = b"y" * 1024

        def fake_urlopen(req, timeout=30):
            return _FakeResponse(status=304)

        dest = self._make_existing()
        with _patch_urlopen(fake_urlopen):
            ok, status = scm._download("https://x/img.png", dest)
        self.assertTrue(ok)
        self.assertEqual(status, "not-modified")
        self.assertEqual(dest.read_bytes(), original)
        self.assertFalse(dest.with_suffix(".png.tmp").exists())

    def test_304_via_HTTPError_branch(self):
        # Older nginx/apache configs surface 304 as an HTTPError
        # rather than a normal response. Same outcome required.
        def fake_urlopen(req, timeout=30):
            raise HTTPError(req.full_url, 304, "Not Modified", {}, None)

        dest = self._make_existing()
        with _patch_urlopen(fake_urlopen):
            ok, status = scm._download("https://x/img.png", dest)
        self.assertTrue(ok)
        self.assertEqual(status, "not-modified")
        self.assertEqual(dest.read_bytes(), b"y" * 1024)

    def test_304_bumps_local_mtime_to_now(self):
        # After a 304 we update the file's mtime so the next IMS uses
        # a fresh floor — otherwise the same stale mtime would force
        # the upstream to hand us 304s forever even after fixing a
        # real upstream change.
        def fake_urlopen(req, timeout=30):
            return _FakeResponse(status=304)

        dest = self._make_existing()
        before = dest.stat().st_mtime
        with _patch_urlopen(fake_urlopen):
            scm._download("https://x/img.png", dest)
        after = dest.stat().st_mtime
        self.assertGreater(after, before)

    def test_200_replaces_file_and_stamps_last_modified(self):
        new_body = b"z" * 2048
        # A Last-Modified ~ 2024-01-01 — must end up as dest mtime.
        lm = "Mon, 01 Jan 2024 00:00:00 GMT"

        def fake_urlopen(req, timeout=30):
            return _FakeResponse(status=200, body=new_body,
                                 headers={"Last-Modified": lm})

        dest = self._make_existing()
        with _patch_urlopen(fake_urlopen):
            ok, status = scm._download("https://x/img.png", dest)
        self.assertTrue(ok)
        self.assertEqual(status, "ok")
        self.assertEqual(dest.read_bytes(), new_body)
        # 2024-01-01 UTC → 1704067200
        self.assertAlmostEqual(dest.stat().st_mtime, 1704067200.0,
                               delta=1.0)

    def test_200_without_last_modified_leaves_default_mtime(self):
        # No Last-Modified header → we don't pretend; the file gets
        # the current wallclock from the OS, which means the next
        # IMS round-trip uses "now" as the floor (one extra full
        # refresh cycle worst-case).
        new_body = b"z" * 2048

        def fake_urlopen(req, timeout=30):
            return _FakeResponse(status=200, body=new_body, headers={})

        dest = self._make_existing()
        before = time.time() - 1
        with _patch_urlopen(fake_urlopen):
            scm._download("https://x/img.png", dest)
        self.assertGreaterEqual(dest.stat().st_mtime, before)

    def test_200_with_unparseable_last_modified_is_silent(self):
        # Garbage Last-Modified must NOT raise — file is still good,
        # we just lose the IMS optimisation for one cycle.
        new_body = b"z" * 2048

        def fake_urlopen(req, timeout=30):
            return _FakeResponse(status=200, body=new_body,
                                 headers={"Last-Modified": "not-a-date"})

        dest = self._make_existing()
        with _patch_urlopen(fake_urlopen):
            ok, status = scm._download("https://x/img.png", dest)
        self.assertTrue(ok)
        self.assertEqual(status, "ok")
        self.assertEqual(dest.read_bytes(), new_body)

    def test_200_atomic_tmp_cleaned_on_too_small(self):
        # Small body during a revalidation must NOT corrupt the
        # existing good file — the rename never happens, the tmp
        # is unlinked, dest stays exactly as it was.
        original = b"y" * 1024

        def fake_urlopen(req, timeout=30):
            return _FakeResponse(status=200, body=b"tiny",
                                 headers={})

        dest = self._make_existing()
        with _patch_urlopen(fake_urlopen):
            ok, status = scm._download("https://x/img.png", dest)
        self.assertFalse(ok)
        self.assertEqual(status, "too-small")
        # Existing file untouched
        self.assertEqual(dest.read_bytes(), original)
        self.assertFalse(dest.with_suffix(".png.tmp").exists())


class DownloadEdgeCaseTests(unittest.TestCase):

    def test_existing_below_min_size_treated_as_missing(self):
        # An existing 5-byte file (CDN error stub from a previous
        # broken run) must not trigger IMS — we want a fresh
        # unconditional GET to replace it.
        captured: dict = {}

        def fake_urlopen(req, timeout=30):
            captured["headers"] = dict(req.header_items())
            return _FakeResponse(status=200, body=b"x" * 1024)

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "img.png"
            dest.write_bytes(b"stub")  # 4 bytes < min_size 256
            with _patch_urlopen(fake_urlopen):
                ok, status = scm._download("https://x/img.png", dest)
        self.assertTrue(ok)
        self.assertEqual(status, "ok")
        keys_lower = {k.lower() for k in captured["headers"]}
        self.assertNotIn("if-modified-since", keys_lower)


if __name__ == "__main__":
    unittest.main()
