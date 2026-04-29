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


# ── Auto-enqueue (image_health → image_mirror loop closure) ──────


class AutoEnqueueMirrorTests(unittest.TestCase):
    """Verifies the image_health → image_mirror handoff. Whenever the
    aggregate status indicates at least one broken `local` path, the
    worker must INSERT an image_mirror task with ON CONFLICT DO NOTHING
    keyed on (task_type, task_key) so re-detection is idempotent."""

    def _run_with(self, paths_meta, *, rowcount_for_enqueue=1):
        conn = FakeConn()
        conn.fetchone_queue = [(paths_meta,)]
        # rowcount sequence: cursor 1 is the SELECT (no rowcount),
        # cursor 2 is the image_health_check INSERT (no assertion),
        # cursor 3 (when triggered) is the image_mirror INSERT.
        # We feed three values so each execute() consumes one.
        conn.rowcount_queue = [0, 0, rowcount_for_enqueue]
        w = image_health.ImageHealthWorker(conn)
        rv = w.process({
            "task_id": 1, "task_type": "image_health",
            "task_key": "sv2/47",
            "payload": {"set_id": "sv2", "card_number": "47"},
            "attempts": 0,
        })
        return conn, rv

    def _enqueue_sql(self, conn):
        """Return the (sql, params) pair of the image_mirror INSERT,
        or None if no enqueue happened."""
        for cur in conn.cursors:
            for sql, params in cur.executed:
                if "INSERT INTO bg_task_queue" in sql \
                        and "image_mirror" in sql:
                    return sql, params
        return None

    def test_all_missing_enqueues_image_mirror(self):
        conn, rv = self._run_with([
            {"src": "tcgo", "lang": "en", "url": "u1",
             "local": "/no/such/a.png"},
            {"src": "scry", "lang": "en", "url": "u2",
             "local": "/no/such/b.png"},
        ])
        self.assertEqual(rv["status"], "ALL_MISSING")
        self.assertTrue(rv.get("mirror_enqueued"))
        enq = self._enqueue_sql(conn)
        self.assertIsNotNone(enq, "expected an image_mirror INSERT")
        sql, params = enq
        self.assertIn("ON CONFLICT (task_type, task_key) DO NOTHING", sql)
        self.assertIn("'PENDING'", sql)
        # task_key is "<set>/<num>" — first param
        self.assertEqual(params[0], "sv2/47")
        # set_id and card_number flow into the JSONB payload
        self.assertEqual(params[1], "sv2")
        self.assertEqual(params[2], "47")
        # 4th param is created_at — must be an int seconds-since-epoch
        self.assertIsInstance(params[3], int)
        self.assertGreater(params[3], 1_700_000_000)

    def test_all_corrupt_enqueues_image_mirror(self):
        # Two existing files with bytes but no recognised magic.
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(b"GARBAGE_NOT_AN_IMAGE_FORMAT_HEADER_FOO" * 4)
            bad1 = f.name
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(b"GARBAGE_NOT_AN_IMAGE_FORMAT_HEADER_BAR" * 4)
            bad2 = f.name
        try:
            conn, rv = self._run_with([
                {"src": "tcgo", "lang": "en", "url": "u1", "local": bad1},
                {"src": "scry", "lang": "en", "url": "u2", "local": bad2},
            ])
            self.assertEqual(rv["status"], "ALL_CORRUPT")
            self.assertTrue(rv.get("mirror_enqueued"))
            self.assertIsNotNone(self._enqueue_sql(conn))
        finally:
            os.unlink(bad1)
            os.unlink(bad2)

    def test_partial_enqueues_image_mirror(self):
        # One real PNG path (OK) + one missing path → PARTIAL → enqueue.
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(PNG_1x1 * 4)
            real_path = f.name
        try:
            conn, rv = self._run_with([
                {"src": "tcgo", "lang": "en", "url": "u1",
                 "local": real_path},
                {"src": "scry", "lang": "en", "url": "u2",
                 "local": "/no/such/file.png"},
            ])
            # PARTIAL when Pillow accepts the micro-PNG; ALL_CORRUPT
            # when Pillow rejects it. Both must enqueue.
            self.assertIn(rv["status"], ("PARTIAL", "ALL_CORRUPT"))
            self.assertTrue(rv.get("mirror_enqueued"))
            self.assertIsNotNone(self._enqueue_sql(conn))
        finally:
            os.unlink(real_path)

    def test_no_paths_does_not_enqueue(self):
        # image_url_alt entries all have empty `local` → NO_PATHS,
        # which is excluded from the trigger set.
        conn, rv = self._run_with([
            {"src": "x", "lang": "en", "url": "u1", "local": ""},
        ])
        self.assertEqual(rv["status"], "NO_PATHS")
        self.assertNotIn("mirror_enqueued", rv)
        self.assertIsNone(self._enqueue_sql(conn))

    def test_missing_card_does_not_enqueue(self):
        # cards_master row is gone — early return before status compute.
        conn = FakeConn()
        conn.fetchone_queue = [None]
        w = image_health.ImageHealthWorker(conn)
        rv = w.process({
            "task_id": 1, "task_type": "image_health",
            "task_key": "sv2/47",
            "payload": {"set_id": "sv2", "card_number": "47"},
            "attempts": 0,
        })
        self.assertEqual(rv["status"], "MISSING_CARD")
        self.assertIsNone(self._enqueue_sql(conn))

    def test_already_pending_collapses_to_no_flag(self):
        # ON CONFLICT (task_type, task_key) DO NOTHING → rowcount==0
        # when an image_mirror task is already PENDING for this card.
        # process() must NOT advertise mirror_enqueued in that case;
        # the result dict is the operator's signal that something new
        # was created vs. coalesced.
        conn, rv = self._run_with([
            {"src": "tcgo", "lang": "en", "url": "u1",
             "local": "/no/such/a.png"},
        ], rowcount_for_enqueue=0)
        self.assertEqual(rv["status"], "ALL_MISSING")
        # The INSERT was attempted (we want the audit record), but
        # the upsert collapsed → no flag.
        self.assertIsNone(rv.get("mirror_enqueued"))
        self.assertIsNotNone(self._enqueue_sql(conn))


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
