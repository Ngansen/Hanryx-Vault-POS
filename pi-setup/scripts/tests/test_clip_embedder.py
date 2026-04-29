"""
Tests for workers/clip_embedder.py.

Strategy: stub onnxruntime / numpy / Pillow via constructor injection.
The Worker base + DB layer are exercised through the same FakeConn /
FakeCursor pattern as the other worker tests.

We verify:
  * lib + model resolution (NO_LIB / NO_MODEL paths)
  * preprocessing shape, dtype, value range
  * L2 normalisation invariant
  * happy-path INSERT shape (REAL[] cast, 512 dims, norm_before > 0)
  * failure-path INSERT shape (still records a row, doesn't requeue)
  * seed SQL filter mentions image_health AND model_id NOT EXISTS
  * MISSING_CARD path
"""
from __future__ import annotations

import math
import os
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path
from typing import Any
from unittest import mock

PI_SETUP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PI_SETUP))

from workers.clip_embedder import (  # noqa: E402
    ClipEmbedderWorker,
    CLIP_EMBEDDING_DIM,
    CLIP_INPUT_SIZE,
    CLIP_MEAN,
    CLIP_STD,
)
from workers.base import WorkerError  # noqa: E402


# ── Fake DB layer ────────────────────────────────────────────────

class FakeCursor:
    """Records every (sql, params) execute() call. Returns canned
    results from a FIFO queue when fetchone()/fetchall() is called.
    """

    def __init__(self, parent: "FakeConn") -> None:
        self.parent = parent
        self.rowcount = 0
        self._fetch_one_q: deque = parent._fetch_one_q
        self._fetch_all_q: deque = parent._fetch_all_q

    def execute(self, sql: str, params: Any = None) -> None:
        self.parent.executes.append((sql, params))

    def fetchone(self):
        if not self._fetch_one_q:
            return None
        return self._fetch_one_q.popleft()

    def fetchall(self):
        if not self._fetch_all_q:
            return []
        return self._fetch_all_q.popleft()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self) -> None:
        self.executes: list[tuple[str, Any]] = []
        self.commits = 0
        self._fetch_one_q: deque = deque()
        self._fetch_all_q: deque = deque()

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def queue_one(self, row):
        self._fetch_one_q.append(row)

    def queue_all(self, rows):
        self._fetch_all_q.append(rows)


# ── Fake CLIP runtime ───────────────────────────────────────────

class FakeOrtSession:
    """Minimal ONNX-runtime stand-in. get_inputs()[0].name + run()."""

    def __init__(self, output_vec):
        self._output = output_vec
        self.run_calls = []

    def get_inputs(self):
        class _I:
            name = "pixel_values"
        return [_I()]

    def run(self, output_names, feed):
        self.run_calls.append((output_names, feed))
        # Mimic CLIP shape: (1, 512)
        return [self._output]


class FakeImage:
    """Pillow stand-in. Records resize/crop/convert calls and
    returns itself (chainable). For numpy conversion we expose
    `_pixels` so np.asarray can read them via __array_interface__."""

    BICUBIC = 3

    def __init__(self, w=512, h=720, mode="RGB", pixels=None):
        self.size = (w, h)
        self.mode = mode
        self._pixels = pixels  # numpy array, set lazily

    def convert(self, mode):
        return FakeImage(self.size[0], self.size[1], mode=mode,
                         pixels=self._pixels)

    def resize(self, size, resample):
        return FakeImage(size[0], size[1], mode=self.mode,
                         pixels=self._pixels)

    def crop(self, box):
        l, t, r, b = box
        return FakeImage(r - l, b - t, mode=self.mode,
                         pixels=self._pixels)

    @classmethod
    def open(cls, fp):
        return cls()


# ── Tests ───────────────────────────────────────────────────────


class ConstructorTest(unittest.TestCase):
    def test_defaults(self):
        conn = FakeConn()
        w = ClipEmbedderWorker(conn)
        self.assertEqual(w.TASK_TYPE, "clip_embed")
        self.assertEqual(w.model_id, "clip-vit-b32-onnx-1.0")
        self.assertEqual(w.model_path, "/mnt/cards/models/clip-vit-b32.onnx")
        self.assertEqual(w.recheck_after_s, 90 * 86400)
        self.assertEqual(w.BATCH_SIZE, 20)

    def test_env_overrides(self):
        conn = FakeConn()
        with mock.patch.dict(os.environ, {
            "CLIP_MODEL_PATH": "/tmp/x.onnx",
            "CLIP_MODEL_ID":   "clip-test-9.9",
        }):
            w = ClipEmbedderWorker(conn)
        self.assertEqual(w.model_path, "/tmp/x.onnx")
        self.assertEqual(w.model_id, "clip-test-9.9")

    def test_constructor_args_beat_env(self):
        conn = FakeConn()
        with mock.patch.dict(os.environ, {"CLIP_MODEL_PATH": "/env/path"}):
            w = ClipEmbedderWorker(conn, model_path="/explicit/path")
        self.assertEqual(w.model_path, "/explicit/path")

    def test_recheck_after_s_override(self):
        w = ClipEmbedderWorker(FakeConn(), recheck_after_s=3600)
        self.assertEqual(w.recheck_after_s, 3600)


class EnsureSessionTest(unittest.TestCase):
    def test_missing_lib_returns_none(self):
        # No injection → real import → numpy may or may not exist.
        # Force the failure by injecting an object that raises on
        # attribute access. Easier: use the documented hook with a
        # raising factory.
        conn = FakeConn()
        # Inject a bogus np module that is missing required attrs to
        # force the lib path. Easier: patch the lazy import.
        with mock.patch.dict(sys.modules, {"numpy": None}):
            w = ClipEmbedderWorker(conn)
            self.assertIsNone(w._ensure_session())
            self.assertEqual(w._load_failure, "NO_LIB")

    def test_missing_model_file_returns_none(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        conn = FakeConn()
        w = ClipEmbedderWorker(
            conn,
            model_path="/definitely/does/not/exist/model.onnx",
            np_module=np,
            image_module=FakeImage,
        )
        self.assertIsNone(w._ensure_session())
        self.assertEqual(w._load_failure, "NO_MODEL")

    def test_factory_injection_returns_session(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        with tempfile.NamedTemporaryFile(suffix=".onnx", delete=False) as f:
            f.write(b"fake-model")
            model_path = f.name
        try:
            conn = FakeConn()
            sess = FakeOrtSession(np.ones((1, CLIP_EMBEDDING_DIM),
                                          dtype=np.float32))
            w = ClipEmbedderWorker(
                conn,
                model_path=model_path,
                np_module=np,
                image_module=FakeImage,
                ort_session_factory=lambda p: sess,
            )
            self.assertIs(w._ensure_session(), sess)
            self.assertEqual(w._load_failure, "")
        finally:
            os.unlink(model_path)

    def test_factory_failure_records_init_error(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        with tempfile.NamedTemporaryFile(suffix=".onnx", delete=False) as f:
            f.write(b"x")
            model_path = f.name
        try:
            def boom(_):
                raise RuntimeError("bad model")
            w = ClipEmbedderWorker(
                FakeConn(),
                model_path=model_path,
                np_module=np,
                image_module=FakeImage,
                ort_session_factory=boom,
            )
            self.assertIsNone(w._ensure_session())
            self.assertTrue(w._load_failure.startswith("ORT_INIT_ERROR:"))
        finally:
            os.unlink(model_path)


class PreprocessTest(unittest.TestCase):
    def setUp(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        self.np = np

    def _make_real_image(self, w, h):
        # Build a real PIL image so the resize/crop math is exercised
        # against the actual Pillow when available; else use FakeImage.
        try:
            from PIL import Image  # type: ignore
        except ImportError:
            return FakeImage(w, h)
        # Solid mid-gray so the normalised values are predictable.
        return Image.new("RGB", (w, h), color=(128, 128, 128))

    def test_output_shape_and_dtype(self):
        try:
            from PIL import Image  # type: ignore
        except ImportError:
            self.skipTest("Pillow not installed")
        w = ClipEmbedderWorker(FakeConn(), np_module=self.np,
                               image_module=Image)
        img = self._make_real_image(640, 480)
        arr = w._preprocess(img)
        self.assertEqual(arr.shape, (1, 3, CLIP_INPUT_SIZE, CLIP_INPUT_SIZE))
        self.assertEqual(str(arr.dtype), "float32")

    def test_solid_grey_normalises_to_expected_per_channel_value(self):
        try:
            from PIL import Image  # type: ignore
        except ImportError:
            self.skipTest("Pillow not installed")
        w = ClipEmbedderWorker(FakeConn(), np_module=self.np,
                               image_module=Image)
        img = self._make_real_image(300, 300)
        arr = w._preprocess(img)
        # All pixels are 128/255 → after (x-mean)/std the value per
        # channel is constant. Verify channel 0.
        x = 128.0 / 255.0
        expected_c0 = (x - CLIP_MEAN[0]) / CLIP_STD[0]
        # arr is (1, 3, H, W); pick the first pixel of channel 0.
        self.assertAlmostEqual(float(arr[0, 0, 0, 0]),
                               expected_c0, places=4)

    def test_aspect_preserved_then_center_cropped(self):
        try:
            from PIL import Image  # type: ignore
        except ImportError:
            self.skipTest("Pillow not installed")
        w = ClipEmbedderWorker(FakeConn(), np_module=self.np,
                               image_module=Image)
        # Wide image: 448 × 224 → resized to 448×224 (no, shortest
        # edge is h=224 → already 224, but width should be unchanged)
        # → cropped to 224×224.
        img = self._make_real_image(448, 224)
        arr = w._preprocess(img)
        self.assertEqual(arr.shape, (1, 3, 224, 224))


class EmbedTest(unittest.TestCase):
    def setUp(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        self.np = np

    def test_l2_normalises_to_unit_vector(self):
        np = self.np
        # Output a non-unit vector; verify _embed L2-normalises it.
        raw = np.array([[3.0, 4.0] + [0.0] * (CLIP_EMBEDDING_DIM - 2)],
                       dtype=np.float32)
        sess = FakeOrtSession(raw)
        with tempfile.NamedTemporaryFile(suffix=".onnx", delete=False) as f:
            f.write(b"x")
            model_path = f.name
        try:
            w = ClipEmbedderWorker(
                FakeConn(),
                model_path=model_path,
                np_module=np,
                image_module=FakeImage,
                ort_session_factory=lambda p: sess,
            )
            embedding, norm_before = w._embed(np.zeros(
                (1, 3, CLIP_INPUT_SIZE, CLIP_INPUT_SIZE), dtype=np.float32))
            self.assertEqual(len(embedding), CLIP_EMBEDDING_DIM)
            self.assertAlmostEqual(norm_before, 5.0, places=4)
            l2 = math.sqrt(sum(x * x for x in embedding))
            self.assertAlmostEqual(l2, 1.0, places=5)
            # First two components: 3/5, 4/5
            self.assertAlmostEqual(embedding[0], 0.6, places=5)
            self.assertAlmostEqual(embedding[1], 0.8, places=5)
        finally:
            os.unlink(model_path)

    def test_zero_embedding_raises_worker_error(self):
        np = self.np
        raw = np.zeros((1, CLIP_EMBEDDING_DIM), dtype=np.float32)
        sess = FakeOrtSession(raw)
        with tempfile.NamedTemporaryFile(suffix=".onnx", delete=False) as f:
            f.write(b"x")
            model_path = f.name
        try:
            w = ClipEmbedderWorker(
                FakeConn(),
                model_path=model_path,
                np_module=np,
                image_module=FakeImage,
                ort_session_factory=lambda p: sess,
            )
            with self.assertRaises(WorkerError) as ctx:
                w._embed(np.zeros((1, 3, 224, 224), dtype=np.float32))
            self.assertEqual(str(ctx.exception), "ZERO_EMBEDDING")
        finally:
            os.unlink(model_path)


class PickImagePathTest(unittest.TestCase):
    def test_skips_missing_files(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"hello")
            real_path = f.name
        try:
            paths = [
                {"local": "/nope/missing.png", "src": "tcgo", "lang": "en"},
                {"local": real_path, "src": "official", "lang": "kr"},
                {"local": "/also/missing.png", "src": "x", "lang": "jp"},
            ]
            path, tag = ClipEmbedderWorker._pick_image_path(paths)
            self.assertEqual(path, real_path)
            self.assertEqual(tag, "official|kr")
        finally:
            os.unlink(real_path)

    def test_no_paths_returns_empty(self):
        path, tag = ClipEmbedderWorker._pick_image_path([])
        self.assertEqual((path, tag), ("", ""))

    def test_skips_zero_byte_files(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            empty = f.name
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                f.write(b"data")
                good = f.name
            try:
                path, _ = ClipEmbedderWorker._pick_image_path([
                    {"local": empty, "src": "x"},
                    {"local": good,  "src": "y"},
                ])
                self.assertEqual(path, good)
            finally:
                os.unlink(good)
        finally:
            os.unlink(empty)

    def test_skips_non_dict_entries(self):
        path, _ = ClipEmbedderWorker._pick_image_path([
            "string-entry", None, 42, {"local": ""},
        ])
        self.assertEqual(path, "")


class ProcessTest(unittest.TestCase):
    def _make_worker(self, np, image_module, sess=None,
                     model_exists=True):
        if model_exists:
            tmp = tempfile.NamedTemporaryFile(
                suffix=".onnx", delete=False)
            tmp.write(b"x")
            tmp.close()
            mp = tmp.name
        else:
            mp = "/no/such/model.onnx"
        return ClipEmbedderWorker(
            FakeConn(),
            model_path=mp,
            np_module=np,
            image_module=image_module,
            ort_session_factory=(lambda p: sess) if sess else None,
        ), mp

    def test_missing_card_records_failure(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        w, mp = self._make_worker(np, FakeImage)
        try:
            w.conn.queue_one(None)  # cards_master.fetchone -> None
            res = w.process({"task_id": 1,
                             "payload": {"set_id": "S", "card_number": "1"}})
            self.assertEqual(res["status"], "MISSING_CARD")
            insert = [s for s, _ in w.conn.executes
                      if "INSERT INTO card_image_embedding" in s]
            self.assertEqual(len(insert), 1)
        finally:
            os.unlink(mp)

    def test_no_image_records_failure(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        w, mp = self._make_worker(np, FakeImage)
        try:
            w.conn.queue_one((["nonsense"],))  # bad jsonb shape
            res = w.process({"task_id": 1,
                             "payload": {"set_id": "S", "card_number": "1"}})
            self.assertEqual(res["status"], "NO_IMAGE")
            params = [p for s, p in w.conn.executes
                      if "INSERT INTO card_image_embedding" in s][0]
            # failure column is 6th-from-last param (set,num,path,src,model,fail,ts)
            self.assertIn("NO_IMAGE", params)
        finally:
            os.unlink(mp)

    def test_no_model_records_failure(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"data")
            img_path = f.name
        try:
            w, _ = self._make_worker(np, FakeImage, model_exists=False)
            w.conn.queue_one(([{"local": img_path, "src": "x"}],))
            res = w.process({"task_id": 1,
                             "payload": {"set_id": "S", "card_number": "1"}})
            self.assertEqual(res["status"], "NO_MODEL")
            params = [p for s, p in w.conn.executes
                      if "INSERT INTO card_image_embedding" in s][0]
            self.assertIn("NO_MODEL", params)
        finally:
            os.unlink(img_path)

    def test_happy_path_inserts_normalised_embedding(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        try:
            from PIL import Image  # type: ignore
        except ImportError:
            self.skipTest("Pillow not installed")
        # Make a real 1x1 PNG
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp_path = f.name
        Image.new("RGB", (50, 50), color=(200, 50, 100)).save(tmp_path)
        try:
            raw = np.array([[6.0, 8.0] + [0.0] * (CLIP_EMBEDDING_DIM - 2)],
                           dtype=np.float32)
            sess = FakeOrtSession(raw)
            w, mp = self._make_worker(np, Image, sess=sess)
            try:
                w.conn.queue_one(([{"local": tmp_path,
                                    "src": "tcgo",
                                    "lang": "kr"}],))
                res = w.process({"task_id": 1,
                                 "payload": {"set_id": "SV1",
                                             "card_number": "001"}})
                self.assertEqual(res["status"], "OK")
                self.assertEqual(res["dim"], CLIP_EMBEDDING_DIM)
                self.assertAlmostEqual(res["norm_before"], 10.0, places=4)
                inserts = [(s, p) for s, p in w.conn.executes
                           if "INSERT INTO card_image_embedding" in s
                           and "DO UPDATE SET image_path" in s
                           and "embedding     = EXCLUDED.embedding" in s]
                self.assertEqual(len(inserts), 1)
                _, params = inserts[0]
                # params order: sid, num, path, src, model, embedding,
                # dim, norm_before, ts
                embedding = params[5]
                self.assertEqual(len(embedding), CLIP_EMBEDDING_DIM)
                l2 = math.sqrt(sum(x * x for x in embedding))
                self.assertAlmostEqual(l2, 1.0, places=5)
                self.assertEqual(params[6], CLIP_EMBEDDING_DIM)
                self.assertAlmostEqual(params[7], 10.0, places=4)
            finally:
                os.unlink(mp)
        finally:
            os.unlink(tmp_path)

    def test_missing_payload_raises_worker_error(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        w, mp = self._make_worker(np, FakeImage)
        try:
            with self.assertRaises(WorkerError):
                w.process({"task_id": 1, "payload": {}})
        finally:
            os.unlink(mp)


class SeedTest(unittest.TestCase):
    def test_seed_sql_filters_image_health_and_excludes_existing(self):
        conn = FakeConn()
        w = ClipEmbedderWorker(conn)
        n = w.seed()
        self.assertEqual(n, 0)
        sql, params = conn.executes[0]
        self.assertIn("INSERT INTO bg_task_queue", sql)
        self.assertIn("FROM image_health_check", sql)
        self.assertIn("status IN ('OK','PARTIAL')", sql)
        self.assertIn("NOT EXISTS", sql)
        self.assertIn("card_image_embedding", sql)
        self.assertIn("ON CONFLICT (task_type, task_key) DO NOTHING", sql)
        # model_id appears 3 times: task_key prefix, payload, NOT EXISTS
        self.assertEqual(params.count(w.model_id), 3)

    def test_seed_uses_latest_check_only(self):
        conn = FakeConn()
        w = ClipEmbedderWorker(conn)
        w.seed()
        sql = conn.executes[0][0]
        # The latest-check-per-card guard prevents enqueueing one task
        # per historical health row.
        self.assertIn("MAX(h2.checked_at)", sql)


if __name__ == "__main__":
    unittest.main()
