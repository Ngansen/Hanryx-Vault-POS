"""
Tests for workers/ocr_pipeline.py — snapshot → preprocess → OCR.

Both the preprocessor and the OCR engine are injectable, so we
swap in fakes that return canned dicts and record the bytes/path
they were called with. No cv2 or PaddleOCR required.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
)

from workers.ocr_pipeline import OcrPipeline  # noqa: E402


# ── Fakes ─────────────────────────────────────────────────────────


class FakePreprocessor:
    def __init__(self, result):
        self.result = result
        self.calls: list = []

    def prepare(self, image):
        self.calls.append(image)
        return self.result


class FakeOcrEngine:
    """Stand-in for LiveOcrEngine. Records every ocr_image call so
    tests can verify which bytes/path the OCR pass actually saw."""

    def __init__(self, result, *, model_id="paddleocr-fake-1.0"):
        self.result = result
        self.model_id = model_id
        self.calls: list[dict] = []

    def ocr_image(self, image, *, lang_hint=None):
        self.calls.append({"image": image, "lang_hint": lang_hint})
        # Copy so a test mutating the result doesn't bleed into the
        # next call.
        return dict(self.result)


def _ok_pre(image_bytes=b"\x89PNG-cleaned", *, applied=None,
             card_bbox=None, rotated=False, elapsed=39):
    """Build a successful ImagePreprocessor.prepare() result."""
    if applied is None:
        applied = ["decode", "crop", "rotate", "clahe", "encode"]
    return {
        "ok":         True,
        "image":      image_bytes,
        "operations": [{"step": s, "ms": 1} for s in applied],
        "card_bbox":  card_bbox,
        "rotated":    rotated,
        "elapsed_ms": elapsed,
    }


def _fail_pre(error="NO_LIB", *, applied_before_failure=()):
    ops = [{"step": s, "ms": 1} for s in applied_before_failure]
    return {
        "ok":         False,
        "error":      error,
        "operations": ops,
        "elapsed_ms": 5,
    }


def _ok_ocr(*, lang="kr", text="hello", conf=0.9,
             low=False, tried=None, elapsed=200):
    return {
        "ok":             True,
        "lang_hint":      lang,
        "lines":          [{"text": text, "conf": conf, "bbox": []}],
        "full_text":      text,
        "avg_conf":       conf,
        "low_confidence": low,
        "tried":          tried or [lang],
        "image_path":     "/tmp/whatever",
        "model_id":       "paddleocr-fake-1.0",
        "elapsed_ms":     elapsed,
    }


def _fail_ocr(error="NO_TEXT", *, tried=("kr", "jp", "chs", "en"),
               elapsed=300):
    return {
        "ok":         False,
        "error":      error,
        "tried":      list(tried),
        "image_path": "",
        "model_id":   "paddleocr-fake-1.0",
        "elapsed_ms": elapsed,
    }


# ── Tests ─────────────────────────────────────────────────────────


class HappyPathTest(unittest.TestCase):
    def test_preprocess_then_ocr_returns_merged_result(self):
        pre = FakePreprocessor(_ok_pre())
        ocr = FakeOcrEngine(_ok_ocr(text="포켓몬", lang="kr",
                                      conf=0.91))
        pipe = OcrPipeline(preprocessor=pre, ocr_engine=ocr)
        res = pipe.ocr_snapshot(b"raw-snapshot-bytes",
                                  lang_hint="kr")
        self.assertTrue(res["ok"])
        self.assertEqual(res["full_text"], "포켓몬")
        self.assertEqual(res["lang_hint"], "kr")
        self.assertAlmostEqual(res["avg_conf"], 0.91, places=4)
        self.assertEqual(res["source"], "preprocessed")
        self.assertEqual(res["tried"], ["kr"])
        self.assertEqual(res["model_id"], "paddleocr-fake-1.0")
        self.assertGreaterEqual(res["elapsed_ms"], 0)

    def test_ocr_receives_preprocessed_bytes_not_original(self):
        cleaned = b"\x89PNG-cleaned-different"
        pre = FakePreprocessor(_ok_pre(image_bytes=cleaned))
        ocr = FakeOcrEngine(_ok_ocr())
        pipe = OcrPipeline(preprocessor=pre, ocr_engine=ocr)
        pipe.ocr_snapshot(b"original-bytes")
        self.assertEqual(ocr.calls[0]["image"], cleaned)
        self.assertNotEqual(ocr.calls[0]["image"], b"original-bytes")

    def test_lang_hint_forwarded_to_ocr_engine(self):
        pre = FakePreprocessor(_ok_pre())
        ocr = FakeOcrEngine(_ok_ocr())
        pipe = OcrPipeline(preprocessor=pre, ocr_engine=ocr)
        pipe.ocr_snapshot(b"x", lang_hint="jp")
        self.assertEqual(ocr.calls[0]["lang_hint"], "jp")

    def test_lang_hint_none_forwarded_as_none(self):
        pre = FakePreprocessor(_ok_pre())
        ocr = FakeOcrEngine(_ok_ocr())
        pipe = OcrPipeline(preprocessor=pre, ocr_engine=ocr)
        pipe.ocr_snapshot(b"x")  # default
        self.assertIsNone(ocr.calls[0]["lang_hint"])


class PreprocessFallbackTest(unittest.TestCase):
    def test_preprocess_failure_falls_back_to_original_bytes(self):
        pre = FakePreprocessor(_fail_pre("NO_LIB"))
        ocr = FakeOcrEngine(_ok_ocr())
        pipe = OcrPipeline(preprocessor=pre, ocr_engine=ocr)
        res = pipe.ocr_snapshot(b"original-bytes")
        self.assertTrue(res["ok"])
        self.assertEqual(res["source"], "original")
        # OCR was given the ORIGINAL, not anything from the
        # preprocessor (which failed before producing image bytes).
        self.assertEqual(ocr.calls[0]["image"], b"original-bytes")

    def test_preprocess_failure_records_error_in_summary(self):
        pre = FakePreprocessor(_fail_pre("DECODE_FAILED"))
        ocr = FakeOcrEngine(_ok_ocr())
        pipe = OcrPipeline(preprocessor=pre, ocr_engine=ocr)
        res = pipe.ocr_snapshot(b"x")
        self.assertEqual(res["preprocess"]["ok"], False)
        self.assertEqual(res["preprocess"]["error"], "DECODE_FAILED")

    def test_preprocess_failure_then_ocr_failure_returns_ocr_error(self):
        pre = FakePreprocessor(_fail_pre("NO_LIB"))
        ocr = FakeOcrEngine(_fail_ocr("NO_TEXT"))
        pipe = OcrPipeline(preprocessor=pre, ocr_engine=ocr)
        res = pipe.ocr_snapshot(b"x")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "NO_TEXT")
        self.assertEqual(res["source"], "original")


class OcrFailureTest(unittest.TestCase):
    def test_ocr_failure_propagates_error_with_preprocess_intact(self):
        pre = FakePreprocessor(_ok_pre(rotated=True,
                                         card_bbox=[10, 20, 700, 900]))
        ocr = FakeOcrEngine(_fail_ocr("NO_TEXT"))
        pipe = OcrPipeline(preprocessor=pre, ocr_engine=ocr)
        res = pipe.ocr_snapshot(b"x")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "NO_TEXT")
        self.assertEqual(res["source"], "preprocessed")
        # Preprocess details still surfaced even on OCR failure —
        # the operator may want to retry without preprocessing.
        self.assertTrue(res["preprocess"]["ok"])
        self.assertTrue(res["preprocess"]["rotated"])
        self.assertEqual(res["preprocess"]["card_bbox"],
                         [10, 20, 700, 900])

    def test_no_lib_from_ocr_propagates(self):
        pre = FakePreprocessor(_ok_pre())
        ocr = FakeOcrEngine(_fail_ocr("NO_LIB", tried=["kr"]))
        pipe = OcrPipeline(preprocessor=pre, ocr_engine=ocr)
        res = pipe.ocr_snapshot(b"x", lang_hint="kr")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "NO_LIB")
        self.assertEqual(res["tried"], ["kr"])


class PreprocessSummaryTest(unittest.TestCase):
    def test_applied_lists_only_succeeded_steps(self):
        pre_result = _ok_pre(applied=["decode", "rotate", "encode"])
        # Inject a step with an error — it should NOT show up in
        # applied (it was best-effort and failed).
        pre_result["operations"].insert(2, {
            "step": "clahe", "ms": 4, "error": "RuntimeError:boom"
        })
        pre = FakePreprocessor(pre_result)
        ocr = FakeOcrEngine(_ok_ocr())
        pipe = OcrPipeline(preprocessor=pre, ocr_engine=ocr)
        res = pipe.ocr_snapshot(b"x")
        self.assertEqual(res["preprocess"]["applied"],
                         ["decode", "rotate", "encode"])

    def test_card_bbox_and_rotated_surfaced(self):
        pre = FakePreprocessor(_ok_pre(card_bbox=[1, 2, 3, 4],
                                         rotated=True))
        ocr = FakeOcrEngine(_ok_ocr())
        pipe = OcrPipeline(preprocessor=pre, ocr_engine=ocr)
        res = pipe.ocr_snapshot(b"x")
        self.assertEqual(res["preprocess"]["card_bbox"], [1, 2, 3, 4])
        self.assertTrue(res["preprocess"]["rotated"])

    def test_low_confidence_flag_carried_through(self):
        pre = FakePreprocessor(_ok_pre())
        ocr = FakeOcrEngine(_ok_ocr(low=True, conf=0.3))
        pipe = OcrPipeline(preprocessor=pre, ocr_engine=ocr)
        res = pipe.ocr_snapshot(b"x")
        self.assertTrue(res["ok"])
        self.assertTrue(res["low_confidence"])


class TimingTest(unittest.TestCase):
    def test_total_elapsed_ms_at_least_zero(self):
        pre = FakePreprocessor(_ok_pre(elapsed=40))
        ocr = FakeOcrEngine(_ok_ocr(elapsed=300))
        pipe = OcrPipeline(preprocessor=pre, ocr_engine=ocr)
        res = pipe.ocr_snapshot(b"x")
        # Wall-clock total is independent of the fakes' reported
        # elapsed_ms — both fakes return instantly. Just assert
        # the field exists and is sane.
        self.assertIn("elapsed_ms", res)
        self.assertGreaterEqual(res["elapsed_ms"], 0)

    def test_preprocess_elapsed_ms_in_summary(self):
        pre = FakePreprocessor(_ok_pre(elapsed=42))
        ocr = FakeOcrEngine(_ok_ocr())
        pipe = OcrPipeline(preprocessor=pre, ocr_engine=ocr)
        res = pipe.ocr_snapshot(b"x")
        self.assertEqual(res["preprocess"]["elapsed_ms"], 42)


class IntrospectionTest(unittest.TestCase):
    def test_model_id_surfaced_from_ocr_engine(self):
        pre = FakePreprocessor(_ok_pre())
        ocr = FakeOcrEngine(_ok_ocr(),
                             model_id="paddleocr-ppocrv4-7.7")
        pipe = OcrPipeline(preprocessor=pre, ocr_engine=ocr)
        self.assertEqual(pipe.model_id, "paddleocr-ppocrv4-7.7")
        res = pipe.ocr_snapshot(b"x")
        self.assertEqual(res["model_id"], "paddleocr-ppocrv4-7.7")

    def test_default_construction_uses_real_engines(self):
        # Smoke test: with no injected dependencies, the pipeline
        # constructs ImagePreprocessor + LiveOcrEngine without
        # blowing up. cv2/paddleocr lazy-load so this works on a
        # CI box without either installed.
        pipe = OcrPipeline()
        self.assertIsNotNone(pipe.preprocessor)
        self.assertIsNotNone(pipe.ocr_engine)
        # model_id reads through to the live engine's worker.
        self.assertIsInstance(pipe.model_id, str)
        self.assertGreater(len(pipe.model_id), 0)


class InputForwardingTest(unittest.TestCase):
    def test_path_input_forwarded_to_preprocessor(self):
        pre = FakePreprocessor(_ok_pre())
        ocr = FakeOcrEngine(_ok_ocr())
        pipe = OcrPipeline(preprocessor=pre, ocr_engine=ocr)
        pipe.ocr_snapshot("/tmp/snapshot.jpg")
        self.assertEqual(pre.calls[0], "/tmp/snapshot.jpg")

    def test_bytes_input_forwarded_to_preprocessor(self):
        pre = FakePreprocessor(_ok_pre())
        ocr = FakeOcrEngine(_ok_ocr())
        pipe = OcrPipeline(preprocessor=pre, ocr_engine=ocr)
        pipe.ocr_snapshot(b"raw-bytes")
        self.assertEqual(pre.calls[0], b"raw-bytes")


if __name__ == "__main__":
    unittest.main()
