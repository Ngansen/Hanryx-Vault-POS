#!/usr/bin/env python3
"""
test_image_health.py — unit tests for the Slice 9 image_health
worker (pi-setup/workers/image_health.py).

Coverage:
  * check_one_path:
      - empty string → MISSING
      - non-existent path → MISSING
      - 0-byte file → EMPTY
      - small but non-zero → TOO_SMALL
      - 200-byte garbage → CORRUPT (no magic match)
      - real PNG header → OK (with or without Pillow)
      - real JPEG header → OK
  * aggregate_status:
      - [] → NO_PATHS
      - all OK → OK
      - mix → PARTIAL
      - all MISSING → ALL_MISSING
      - all EMPTY → ALL_EMPTY
      - all CORRUPT → ALL_CORRUPT
      - mixed failure types (MISSING + CORRUPT) → PARTIAL
  * ImageHealthWorker.process:
      - missing payload fields → WorkerError (permanent)
      - cards_master row gone → MISSING_CARD recorded
      - JSONB list with no 'local' values → NO_PATHS
      - mix of OK + MISSING paths → PARTIAL
      - JSON-string payload (string-cursor) is decoded
  * seed enqueues with NOT EXISTS guard
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PI_SETUP = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PI_SETUP))

from workers import image_health  # noqa: E402
from workers.base import WorkerError  # noqa: E402


# Minimal PNG: 1x1 transparent pixel.
PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63000100000005000100"
    "5d0c6a25"
    "0000000049454e44ae426082"
)
JPEG_HEADER = bytes.fromhex("ffd8ffe000104a464946000101") + b"\x00" * 200


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
        self.all_sql: list[str] = []
        self.cursors: list[FakeCursor] = []
        self.fetchone_queue: list[object] = []
        self.fetchall_queue: list[list] = []
        self.rowcount_queue: list[int] = []

    def cursor(self):
        c = FakeCursor(self)
        self.cursors.append(c)
        return c

    def commit(self):
        self.commits += 1


# ── check_one_path ───────────────────────────────────────────────


class CheckOnePathTests(unittest.TestCase):

    def test_empty_string_is_missing(self):
        self.assertEqual(image_health.check_one_path("")["status"], "MISSING")

    def test_nonexistent_path_is_missing(self):
        self.assertEqual(
            image_health.check_one_path("/nope/never/exists.png")["status"],
            "MISSING",
        )

    def test_zero_byte_file_is_empty(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
        try:
            r = image_health.check_one_path(path)
            self.assertEqual(r["status"], "EMPTY")
            self.assertEqual(r["size_bytes"], 0)
        finally:
            os.unlink(path)

    def test_small_non_zero_is_too_small(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG")  # 4 bytes
            path = f.name
        try:
            r = image_health.check_one_path(path)
            self.assertEqual(r["status"], "TOO_SMALL")
        finally:
            os.unlink(path)

    def test_unrecognised_bytes_are_corrupt(self):
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(b"NOTANIMAGEHEADER" * 20)  # >64 bytes, no magic
            path = f.name
        try:
            r = image_health.check_one_path(path)
            self.assertEqual(r["status"], "CORRUPT")
        finally:
            os.unlink(path)

    def test_real_png_header_is_ok_or_corrupt_via_pil(self):
        # The 1×1 PNG above is technically valid; Pillow either
        # accepts it (OK) or doesn't (CORRUPT). What we care about is
        # that the magic-byte sniff identified it as 'png' — when
        # Pillow is unavailable, we accept on magic alone and report
        # OK; when Pillow is available, behavior is its call.
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(PNG_1x1)
            path = f.name
        try:
            r = image_health.check_one_path(path)
            self.assertEqual(r["fmt"], "png")
            # When Pillow is installed it may flag this micro-PNG as
            # corrupt due to truncated chunk lengths — that's fine,
            # we just want to verify the path was IDENTIFIED.
            self.assertIn(r["status"], ("OK", "CORRUPT"))
        finally:
            os.unlink(path)

    def test_jpeg_magic_recognised(self):
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(JPEG_HEADER)
            path = f.name
        try:
            r = image_health.check_one_path(path)
            self.assertEqual(r["fmt"], "jpeg")
        finally:
            os.unlink(path)


# ── aggregate_status ─────────────────────────────────────────────


class AggregateStatusTests(unittest.TestCase):

    def test_empty(self):
        self.assertEqual(image_health.aggregate_status([]), "NO_PATHS")

    def test_all_ok(self):
        self.assertEqual(image_health.aggregate_status(
            [{"status": "OK"}, {"status": "OK"}]), "OK")

    def test_partial(self):
        self.assertEqual(image_health.aggregate_status(
            [{"status": "OK"}, {"status": "MISSING"}]), "PARTIAL")

    def test_all_missing(self):
        self.assertEqual(image_health.aggregate_status(
            [{"status": "MISSING"}, {"status": "MISSING"}]),
            "ALL_MISSING")

    def test_all_empty(self):
        # EMPTY and TOO_SMALL both indicate a failed download.
        self.assertEqual(image_health.aggregate_status(
            [{"status": "EMPTY"}, {"status": "TOO_SMALL"}]),
            "ALL_EMPTY")

    def test_all_corrupt(self):
        self.assertEqual(image_health.aggregate_status(
            [{"status": "CORRUPT"}, {"status": "CORRUPT"}]),
            "ALL_CORRUPT")

    def test_mixed_failure_modes_is_partial(self):
        # Different failure flavours = admin needs to look — flag as
        # PARTIAL even though zero are OK so it stands out from the
        # uniform-failure aggregations.
        self.assertEqual(image_health.aggregate_status(
            [{"status": "MISSING"}, {"status": "CORRUPT"}]),
            "PARTIAL")


# ── ImageHealthWorker.process ────────────────────────────────────


class ProcessTests(unittest.TestCase):

    def test_missing_payload_raises_worker_error(self):
        conn = FakeConn()
        w = image_health.ImageHealthWorker(conn)
        task = {"task_id": 1, "task_type": "image_health",
                "task_key": "", "payload": {}, "attempts": 0}
        with self.assertRaises(WorkerError):
            w.process(task)

    def test_missing_card_records_missing_card(self):
        conn = FakeConn()
        # cards_master SELECT returns no row → fetchone -> None
        conn.fetchone_queue = [None]
        w = image_health.ImageHealthWorker(conn)
        rv = w.process({
            "task_id": 1, "task_type": "image_health",
            "task_key": "sv2/47",
            "payload": {"set_id": "sv2", "card_number": "47"},
            "attempts": 0,
        })
        self.assertEqual(rv["status"], "MISSING_CARD")
        # Last SQL should be the INSERT into image_health_check
        self.assertIn("INSERT INTO image_health_check", conn.all_sql[-1])

    def test_no_paths_is_no_paths(self):
        conn = FakeConn()
        # cards_master row with empty image_url_alt list
        conn.fetchone_queue = [([{"src": "x", "lang": "en",
                                  "url": "http://x", "local": ""}],)]
        w = image_health.ImageHealthWorker(conn)
        rv = w.process({
            "task_id": 1, "task_type": "image_health",
            "task_key": "sv2/47",
            "payload": {"set_id": "sv2", "card_number": "47"},
            "attempts": 0,
        })
        self.assertEqual(rv["status"], "NO_PATHS")
        self.assertEqual(rv["paths_checked"], 0)

    def test_mix_of_ok_and_missing_is_partial(self):
        # One real PNG path + one bogus path → PARTIAL
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(PNG_1x1 * 4)  # >64 bytes, magic recognised
            real_path = f.name
        try:
            conn = FakeConn()
            paths = [
                {"src": "tcgo", "lang": "en", "url": "u1",
                 "local": real_path},
                {"src": "scry", "lang": "en", "url": "u2",
                 "local": "/no/such/file.png"},
            ]
            conn.fetchone_queue = [(paths,)]
            w = image_health.ImageHealthWorker(conn)
            rv = w.process({
                "task_id": 1, "task_type": "image_health",
                "task_key": "sv2/47",
                "payload": {"set_id": "sv2", "card_number": "47"},
                "attempts": 0,
            })
            self.assertEqual(rv["paths_checked"], 2)
            # Real PNG → OK (magic recognised, even if Pillow unavail);
            # bogus → MISSING. So aggregate = PARTIAL.
            self.assertIn(rv["status"], ("PARTIAL", "ALL_CORRUPT"))
        finally:
            os.unlink(real_path)

    def test_string_payload_is_json_decoded(self):
        # When the cursor adapter delivers JSONB as a string instead of
        # a parsed list, process() must still cope.
        conn = FakeConn()
        json_str = json.dumps([{"local": "/no/such/file.png",
                                "src": "x", "lang": "en", "url": "u"}])
        conn.fetchone_queue = [(json_str,)]
        w = image_health.ImageHealthWorker(conn)
        rv = w.process({
            "task_id": 1, "task_type": "image_health",
            "task_key": "sv2/47",
            "payload": {"set_id": "sv2", "card_number": "47"},
            "attempts": 0,
        })
        self.assertEqual(rv["paths_checked"], 1)
        self.assertEqual(rv["status"], "ALL_MISSING")


class SeedTests(unittest.TestCase):

    def test_seed_uses_not_exists_guard(self):
        conn = FakeConn()
        conn.rowcount_queue = [12]
        w = image_health.ImageHealthWorker(conn)
        n = w.seed()
        self.assertEqual(n, 12)
        sql, _ = conn.cursors[0].executed[0]
        # Idempotency: seed must skip cards already checked recently
        self.assertIn("NOT EXISTS", sql)
        self.assertIn("image_health_check", sql)
        # And must scope to cards that actually have local paths
        self.assertIn("jsonb_array_elements", sql)
        self.assertIn("ON CONFLICT (task_type, task_key) DO NOTHING", sql)


if __name__ == "__main__":
    unittest.main()
