"""
Tests for workers/live_ocr.py — synchronous OCR for tablet snapshots.

The engine composes an OcrIndexerWorker(conn=None) and reuses its
PaddleOCR cache and result parser. Tests inject a fake
paddle_factory so PaddleOCR itself is never imported and we can
script per-language results.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
)

from workers.live_ocr import (  # noqa: E402
    DEFAULT_MIN_AUTO_CONF,
    LiveOcrEngine,
    LiveOcrError,
    _sniff_image_ext,
)
from workers.ocr_indexer import LANG_PRIORITY, PADDLE_LANG_MAP  # noqa: E402


# ── Test fixtures ─────────────────────────────────────────────────


def _box():
    return [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]


def _raw(*lines_with_conf):
    """Build a PaddleOCR-shaped raw result from (text, conf) pairs.

    PaddleOCR returns
        [ [ [box, (text, conf)], [box, (text, conf)], ... ] ]
    for a single-image call. Wrap our test data the same way so
    OcrIndexerWorker._parse_ocr_result is exercised end-to-end.
    """
    return [[[_box(), (text, conf)] for text, conf in lines_with_conf]]


def _make_factory(per_lang_results=None, per_lang_raises=None):
    """Return a factory(lang) -> fake-OCR-instance.

    per_lang_results: {paddle_lang: raw}  (e.g. {'korean': _raw(...)})
    per_lang_raises:  {paddle_lang: Exception}

    factory.construction_count tracks how often it was called per
    lang (so tests can assert on the worker's per-lang cache).
    """
    per_lang_results = per_lang_results or {}
    per_lang_raises = per_lang_raises or {}
    construction_count: dict[str, int] = {}

    def _factory(lang):
        construction_count[lang] = construction_count.get(lang, 0) + 1

        class _Inst:
            def ocr(self_inner, path, cls=False, _l=lang):
                if _l in per_lang_raises:
                    raise per_lang_raises[_l]
                return per_lang_results.get(_l, [])

        return _Inst()

    _factory.construction_count = construction_count
    return _factory


def _write_real_image(suffix=".png"):
    """Create a small on-disk file (just a PNG header — content is
    irrelevant since the fake factory ignores the path) so the
    `os.path.exists` and `getsize > 0` guards pass."""
    fd, path = tempfile.mkstemp(prefix="liveocr_test_", suffix=suffix)
    try:
        os.write(fd, b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    finally:
        os.close(fd)
    return path


# ── Tests ─────────────────────────────────────────────────────────


class OcrImagePathTest(unittest.TestCase):
    def test_path_input_explicit_lang(self):
        path = _write_real_image()
        try:
            fac = _make_factory({"korean": _raw(("포켓몬", 0.9))})
            eng = LiveOcrEngine(paddle_factory=fac)
            res = eng.ocr_image(path, lang_hint="kr")
        finally:
            os.unlink(path)
        self.assertTrue(res["ok"])
        self.assertEqual(res["lang_hint"], "kr")
        self.assertEqual(res["full_text"], "포켓몬")
        self.assertEqual(res["tried"], ["kr"])
        self.assertAlmostEqual(res["avg_conf"], 0.9, places=4)
        self.assertFalse(res["low_confidence"])
        self.assertEqual(res["image_path"], path)
        self.assertEqual(res["model_id"], eng.model_id)
        self.assertGreaterEqual(res["elapsed_ms"], 0)
        self.assertEqual(len(res["lines"]), 1)
        self.assertEqual(res["lines"][0]["text"], "포켓몬")

    def test_pathlib_path_accepted(self):
        path = _write_real_image()
        try:
            fac = _make_factory({"japan": _raw(("ピカチュウ", 0.92))})
            eng = LiveOcrEngine(paddle_factory=fac)
            res = eng.ocr_image(pathlib.Path(path), lang_hint="jp")
        finally:
            os.unlink(path)
        self.assertTrue(res["ok"])
        self.assertEqual(res["full_text"], "ピカチュウ")


class OcrImageBytesTest(unittest.TestCase):
    def test_bytes_input_writes_tempfile_and_cleans_up(self):
        # Capture the path PaddleOCR was called with by stashing it
        # on the factory closure.
        seen_paths: list[str] = []

        def fac(lang):
            class _Inst:
                def ocr(self_inner, path, cls=False):
                    seen_paths.append(path)
                    return _raw(("hello", 0.88))
            return _Inst()

        eng = LiveOcrEngine(paddle_factory=fac)
        res = eng.ocr_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16,
                             lang_hint="en")
        self.assertTrue(res["ok"])
        self.assertEqual(len(seen_paths), 1)
        # Tempfile got the .png suffix from the magic bytes.
        self.assertTrue(seen_paths[0].endswith(".png"),
                        f"expected .png suffix, got {seen_paths[0]}")
        # And it was cleaned up after the call returned.
        self.assertFalse(os.path.exists(seen_paths[0]),
                         "temp file leaked")

    def test_bytes_tempfile_cleaned_up_on_paddle_exception(self):
        seen_paths: list[str] = []

        def fac(lang):
            class _Inst:
                def ocr(self_inner, path, cls=False):
                    seen_paths.append(path)
                    raise RuntimeError("paddle blew up")
            return _Inst()

        eng = LiveOcrEngine(paddle_factory=fac, auto_priority=("en",))
        res = eng.ocr_image(b"\xff\xd8\xff\xe0\x00\x10JFIF",  # JPEG
                             lang_hint=None)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "NO_TEXT")
        # JPEG magic → .jpg suffix.
        self.assertTrue(seen_paths[0].endswith(".jpg"))
        self.assertFalse(os.path.exists(seen_paths[0]),
                         "temp file leaked after exception")

    def test_unknown_bytes_get_bin_suffix(self):
        # The sniffer falls back to .bin for unknown formats so
        # PaddleOCR fails loudly instead of silently OCRing garbage.
        self.assertEqual(_sniff_image_ext(b"NOTANIMAGE..."), ".bin")
        self.assertEqual(_sniff_image_ext(b"\x89PNG\r\n\x1a\n"), ".png")
        self.assertEqual(_sniff_image_ext(b"\xff\xd8\xff\xe0"), ".jpg")
        self.assertEqual(_sniff_image_ext(b"GIF89a"), ".gif")
        self.assertEqual(_sniff_image_ext(b"RIFFxxxxWEBP"), ".webp")
        self.assertEqual(_sniff_image_ext(b"BM..."), ".bmp")


class LangHintTest(unittest.TestCase):
    def test_explicit_lang_does_not_try_other_langs(self):
        fac = _make_factory({"korean": _raw(("X", 0.9))})
        eng = LiveOcrEngine(paddle_factory=fac)
        path = _write_real_image()
        try:
            res = eng.ocr_image(path, lang_hint="kr")
        finally:
            os.unlink(path)
        self.assertEqual(res["tried"], ["kr"])
        self.assertEqual(set(fac.construction_count), {"korean"})

    def test_unknown_lang_hint_raises(self):
        eng = LiveOcrEngine(paddle_factory=_make_factory())
        with self.assertRaises(LiveOcrError):
            eng.ocr_image("/tmp/whatever", lang_hint="zz")

    def test_unknown_auto_priority_code_raises(self):
        with self.assertRaises(LiveOcrError):
            LiveOcrEngine(paddle_factory=_make_factory(),
                          auto_priority=("kr", "klingon"))

    def test_none_image_raises(self):
        eng = LiveOcrEngine(paddle_factory=_make_factory())
        with self.assertRaises(LiveOcrError):
            eng.ocr_image(None)

    def test_unsupported_image_type_raises(self):
        eng = LiveOcrEngine(paddle_factory=_make_factory())
        with self.assertRaises(LiveOcrError):
            eng.ocr_image(12345)


class AutoDetectTest(unittest.TestCase):
    def test_early_exit_on_first_high_conf_lang(self):
        # KR is first in priority and returns conf > threshold →
        # JP / CHS / EN should never be constructed.
        fac = _make_factory({
            "korean": _raw(("Korean", 0.9)),
            "japan":  _raw(("Japanese", 0.95)),  # would be better
        })
        eng = LiveOcrEngine(paddle_factory=fac)
        path = _write_real_image()
        try:
            res = eng.ocr_image(path)  # auto
        finally:
            os.unlink(path)
        self.assertTrue(res["ok"])
        self.assertEqual(res["lang_hint"], "kr")
        self.assertEqual(res["tried"], ["kr"])
        self.assertEqual(set(fac.construction_count), {"korean"})
        self.assertFalse(res["low_confidence"])

    def test_falls_through_to_next_lang_on_low_conf(self):
        # KR has text but conf is below threshold → tries JP →
        # JP exceeds threshold, wins.
        fac = _make_factory({
            "korean": _raw(("garbage", 0.2)),
            "japan":  _raw(("good", 0.9)),
        })
        eng = LiveOcrEngine(paddle_factory=fac, min_auto_conf=0.5)
        path = _write_real_image()
        try:
            res = eng.ocr_image(path)
        finally:
            os.unlink(path)
        self.assertTrue(res["ok"])
        self.assertEqual(res["lang_hint"], "jp")
        self.assertEqual(res["tried"], ["kr", "jp"])
        self.assertFalse(res["low_confidence"])

    def test_picks_best_when_no_lang_meets_threshold(self):
        # All langs return text but none above threshold → picks
        # the highest conf and flags low_confidence.
        fac = _make_factory({
            "korean": _raw(("a", 0.30)),
            "japan":  _raw(("b", 0.55)),  # winner
            "ch":     _raw(("c", 0.40)),
            "en":     _raw(("d", 0.20)),
        })
        eng = LiveOcrEngine(paddle_factory=fac, min_auto_conf=0.9)
        path = _write_real_image()
        try:
            res = eng.ocr_image(path)
        finally:
            os.unlink(path)
        self.assertTrue(res["ok"])
        self.assertEqual(res["lang_hint"], "jp")
        self.assertTrue(res["low_confidence"])
        self.assertEqual(res["tried"], ["kr", "jp", "chs", "en"])

    def test_no_text_when_every_lang_returns_empty(self):
        fac = _make_factory({})  # every lang returns []
        eng = LiveOcrEngine(paddle_factory=fac)
        path = _write_real_image()
        try:
            res = eng.ocr_image(path)
        finally:
            os.unlink(path)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "NO_TEXT")
        self.assertEqual(res["tried"], list(LANG_PRIORITY))
        self.assertEqual(res["image_path"], path)

    def test_paddle_exception_on_one_lang_falls_through(self):
        fac = _make_factory(
            per_lang_results={"japan": _raw(("ok", 0.9))},
            per_lang_raises={"korean": RuntimeError("boom")},
        )
        eng = LiveOcrEngine(paddle_factory=fac)
        path = _write_real_image()
        try:
            res = eng.ocr_image(path)
        finally:
            os.unlink(path)
        self.assertTrue(res["ok"])
        self.assertEqual(res["lang_hint"], "jp")
        self.assertEqual(res["tried"], ["kr", "jp"])

    def test_custom_auto_priority_order_honored(self):
        # Operator overrides default KR-first with EN-first
        # (e.g. for an English-language event).
        fac = _make_factory({"en": _raw(("hi", 0.99))})
        eng = LiveOcrEngine(paddle_factory=fac,
                             auto_priority=("en", "kr"))
        path = _write_real_image()
        try:
            res = eng.ocr_image(path)
        finally:
            os.unlink(path)
        self.assertEqual(res["tried"], ["en"])
        self.assertEqual(res["lang_hint"], "en")


class FailureModeTest(unittest.TestCase):
    def test_no_lib_when_factory_unavailable(self):
        # No paddle_factory injected AND paddleocr import fails →
        # _ensure_paddle returns None → engine bails out with NO_LIB.
        # We force the import to fail by stubbing paddleocr to a
        # module without PaddleOCR (will get caught before raising).
        from unittest import mock

        with mock.patch.dict(sys.modules, {"paddleocr": None}):
            eng = LiveOcrEngine()  # no factory → real lazy import
            path = _write_real_image()
            try:
                res = eng.ocr_image(path, lang_hint="kr")
            finally:
                os.unlink(path)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "NO_LIB")
        self.assertEqual(res["tried"], ["kr"])

    def test_no_image_when_path_missing(self):
        eng = LiveOcrEngine(paddle_factory=_make_factory())
        res = eng.ocr_image("/nonexistent/path/abcdef.png",
                             lang_hint="kr")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "NO_IMAGE")
        # No language was tried — bail before any pass.
        self.assertEqual(res["tried"], [])

    def test_no_image_when_path_is_empty_file(self):
        fd, path = tempfile.mkstemp(prefix="empty_")
        os.close(fd)
        try:
            eng = LiveOcrEngine(paddle_factory=_make_factory())
            res = eng.ocr_image(path, lang_hint="kr")
        finally:
            os.unlink(path)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "NO_IMAGE")

    def test_factory_construction_failure_returns_factory_error(self):
        # If the factory itself raises (e.g. model file corrupt),
        # the worker records FACTORY_ERROR:... and the engine
        # surfaces that to the caller.
        def boom_factory(lang):
            raise OSError("model file missing")
        eng = LiveOcrEngine(paddle_factory=boom_factory)
        path = _write_real_image()
        try:
            res = eng.ocr_image(path, lang_hint="kr")
        finally:
            os.unlink(path)
        self.assertFalse(res["ok"])
        self.assertTrue(res["error"].startswith("FACTORY_ERROR:"),
                        f"got {res['error']!r}")


class CacheTest(unittest.TestCase):
    def test_paddleocr_constructed_once_per_lang_across_calls(self):
        # The wrapped worker has _ocr_cache so even if we OCR 50
        # snapshots in a row, PaddleOCR is built once per language.
        fac = _make_factory({"korean": _raw(("X", 0.9))})
        eng = LiveOcrEngine(paddle_factory=fac)
        path = _write_real_image()
        try:
            for _ in range(10):
                res = eng.ocr_image(path, lang_hint="kr")
                self.assertTrue(res["ok"])
        finally:
            os.unlink(path)
        # Factory called exactly once for 'korean'.
        self.assertEqual(fac.construction_count.get("korean"), 1)


class IntrospectionTest(unittest.TestCase):
    def test_models_dir_propagates_from_kwarg(self):
        eng = LiveOcrEngine(paddle_factory=_make_factory(),
                             models_dir="/tmp/custom-paddle")
        self.assertEqual(eng.models_dir, "/tmp/custom-paddle")

    def test_model_id_propagates_from_kwarg(self):
        eng = LiveOcrEngine(paddle_factory=_make_factory(),
                             model_id="paddleocr-test-9.9")
        self.assertEqual(eng.model_id, "paddleocr-test-9.9")

    def test_default_min_auto_conf_in_sensible_range(self):
        # Sanity check on the constant — if someone bumps it out
        # of [0, 1], the auto-detect logic silently breaks.
        self.assertGreaterEqual(DEFAULT_MIN_AUTO_CONF, 0.0)
        self.assertLessEqual(DEFAULT_MIN_AUTO_CONF, 1.0)

    def test_default_priority_matches_lang_priority(self):
        # Live engine MUST default to the same KR-first priority
        # as the batch worker; otherwise auto-detect would behave
        # differently between live and batch passes on the same
        # card image.
        eng = LiveOcrEngine(paddle_factory=_make_factory())
        self.assertEqual(eng.auto_priority, LANG_PRIORITY)
        self.assertEqual(eng.auto_priority[0], "kr")


class ElapsedMsTest(unittest.TestCase):
    def test_elapsed_ms_set_on_success(self):
        fac = _make_factory({"korean": _raw(("X", 0.9))})
        eng = LiveOcrEngine(paddle_factory=fac)
        path = _write_real_image()
        try:
            res = eng.ocr_image(path, lang_hint="kr")
        finally:
            os.unlink(path)
        self.assertIn("elapsed_ms", res)
        self.assertIsInstance(res["elapsed_ms"], int)
        self.assertGreaterEqual(res["elapsed_ms"], 0)

    def test_elapsed_ms_set_on_failure(self):
        eng = LiveOcrEngine(paddle_factory=_make_factory())
        res = eng.ocr_image("/nope.png", lang_hint="kr")
        self.assertIn("elapsed_ms", res)
        self.assertGreaterEqual(res["elapsed_ms"], 0)


class ContractTest(unittest.TestCase):
    """Catches drift between the live engine and the batch worker
    that would silently break things if PADDLE_LANG_MAP grows."""

    def test_every_priority_lang_has_a_paddle_mapping(self):
        eng = LiveOcrEngine(paddle_factory=_make_factory())
        for code in eng.auto_priority:
            self.assertIn(code, PADDLE_LANG_MAP,
                          f"auto_priority code {code!r} has no "
                          f"PADDLE_LANG_MAP entry")


if __name__ == "__main__":
    unittest.main()
