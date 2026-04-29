"""
Tests for workers/image_preprocess.py.

Uses fake cv2 / numpy modules injected via the constructor so the
real ML stack (~250 MB) doesn't have to be installed for tests.
The fakes record the call sequence and let tests assert on which
pipeline stages ran.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
)

from workers.image_preprocess import (  # noqa: E402
    DEFAULT_TARGET_HEIGHT_PX,
    ImagePreprocessError,
    ImagePreprocessor,
    _elapsed_ms,
)


# ── Fakes ─────────────────────────────────────────────────────────


class FakeImage:
    """Stand-in for the cv2 ndarray. Carries a shape and supports
    cropping (`img[y:y+h, x:x+w]`) by returning a new FakeImage with
    the cropped shape so the rotation pass can read a sensible h/w.
    """
    def __init__(self, shape=(1000, 800, 3)):
        self.shape = tuple(shape)

    def __getitem__(self, key):
        if isinstance(key, tuple) and len(key) == 2:
            ys, xs = key
            new_h = ((ys.stop - ys.start)
                     if isinstance(ys, slice) else self.shape[0])
            new_w = ((xs.stop - xs.start)
                     if isinstance(xs, slice) else self.shape[1])
            channels = self.shape[2] if len(self.shape) > 2 else None
            new_shape = (new_h, new_w) + ((channels,) if channels else ())
            return FakeImage(shape=new_shape)
        return self


class FakeNp:
    uint8 = "uint8-sentinel"

    @staticmethod
    def frombuffer(data, dtype=None):
        # We don't actually decode — the fake cv2.imdecode below
        # returns a FakeImage regardless of input.
        return data


class FakeCv2:
    """Records every call so tests can assert on the pipeline order
    without needing real OpenCV behaviour."""

    # cv2 constants used by the module — actual values don't matter
    # since the fake methods ignore them, but we expose them so the
    # module's lookups succeed.
    IMREAD_COLOR = 1
    COLOR_BGR2GRAY = 6
    COLOR_BGR2LAB = 44
    COLOR_LAB2BGR = 56
    THRESH_BINARY = 0
    THRESH_OTSU = 8
    RETR_EXTERNAL = 0
    CHAIN_APPROX_SIMPLE = 2
    ROTATE_90_COUNTERCLOCKWISE = 2

    def __init__(self, *,
                 decode_result=None,
                 encode_ok=True,
                 encode_bytes=b"\x89PNG\r\n\x1a\nFAKE",
                 contour_bbox=(50, 60, 700, 900),
                 contour_count=1,
                 raise_on=None):
        # decode_result=None (the default) means imdecode returns
        # None → DECODE_FAILED. To test the success path, pass an
        # explicit FakeImage.
        self.decode_result = decode_result
        self.encode_ok = encode_ok
        self.encode_bytes = encode_bytes
        self.contour_bbox = contour_bbox
        self.contour_count = contour_count
        # raise_on={'crop': Exc, 'clahe': Exc} → raise from the
        # corresponding helper to test best-effort behaviour.
        self.raise_on = raise_on or {}
        self.calls: list[tuple] = []

    def imdecode(self, arr, flag):
        self.calls.append(("imdecode", flag))
        return self.decode_result

    def cvtColor(self, img, code):
        if "cvt" in self.raise_on:
            raise self.raise_on["cvt"]
        self.calls.append(("cvtColor", code))
        return img

    def GaussianBlur(self, img, ksize, sigma):
        if "blur" in self.raise_on:
            raise self.raise_on["blur"]
        self.calls.append(("GaussianBlur",))
        return img

    def threshold(self, img, t, maxv, flags):
        self.calls.append(("threshold",))
        return 128.0, img

    def findContours(self, img, mode, method):
        self.calls.append(("findContours",))
        return (["contour"] * self.contour_count, None)

    def contourArea(self, c):
        return 100000

    def boundingRect(self, c):
        return self.contour_bbox

    def rotate(self, img, code):
        self.calls.append(("rotate", code))
        # Swap h/w to mimic real rotation so any subsequent
        # processing sees the new orientation.
        h, w = img.shape[:2]
        rest = img.shape[2:]
        return FakeImage(shape=(w, h) + rest)

    def createCLAHE(self, *, clipLimit, tileGridSize):
        if "clahe" in self.raise_on:
            raise self.raise_on["clahe"]
        self.calls.append(("createCLAHE", clipLimit, tileGridSize))

        class _C:
            def apply(self_inner, img):
                return img
        return _C()

    def split(self, img):
        return (img, img, img)

    def merge(self, parts):
        return parts[0]

    def imencode(self, ext, img):
        self.calls.append(("imencode", ext))
        if not self.encode_ok:
            return False, None
        # Real cv2 returns a numpy array — bytes(ndarray) gives the
        # raw buffer. We return something `bytes()`-able.
        return True, self.encode_bytes


def _make_pp(**kw):
    """Build a preprocessor with a successful FakeCv2 by default."""
    fake_cv2 = kw.pop("fake_cv2", None) or FakeCv2(
        decode_result=FakeImage(shape=(1000, 800, 3))
    )
    fake_np = kw.pop("fake_np", None) or FakeNp()
    pp = ImagePreprocessor(cv2_module=fake_cv2,
                           np_module=fake_np, **kw)
    return pp, fake_cv2


# ── Tests ─────────────────────────────────────────────────────────


class HappyPathTest(unittest.TestCase):
    def test_full_pipeline_runs_all_five_steps_in_order(self):
        pp, cv2 = _make_pp(
            fake_cv2=FakeCv2(decode_result=FakeImage(shape=(1000, 800, 3))),
        )
        res = pp.prepare(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        self.assertTrue(res["ok"], res)
        steps = [op["step"] for op in res["operations"]]
        self.assertEqual(steps, ["decode", "crop", "rotate",
                                  "clahe", "encode"])
        # cv2 was driven through the expected order.
        ordered = [c[0] for c in cv2.calls]
        self.assertEqual(ordered[0], "imdecode")
        self.assertIn("findContours", ordered)
        self.assertEqual(ordered[-1], "imencode")

    def test_returns_png_bytes(self):
        pp, _ = _make_pp()
        res = pp.prepare(b"image-bytes-anything")
        self.assertTrue(res["ok"])
        self.assertIsInstance(res["image"], bytes)
        self.assertTrue(res["image"].startswith(b"\x89PNG"),
                        f"expected PNG magic, got {res['image'][:8]!r}")

    def test_elapsed_ms_set_on_success(self):
        pp, _ = _make_pp()
        res = pp.prepare(b"x")
        self.assertIn("elapsed_ms", res)
        self.assertIsInstance(res["elapsed_ms"], int)
        self.assertGreaterEqual(res["elapsed_ms"], 0)

    def test_card_bbox_returned_when_crop_succeeds(self):
        cv2 = FakeCv2(decode_result=FakeImage(shape=(1000, 800, 3)),
                      contour_bbox=(20, 30, 600, 800))
        pp, _ = _make_pp(fake_cv2=cv2)
        res = pp.prepare(b"x")
        self.assertEqual(res["card_bbox"], [20, 30, 600, 800])

    def test_card_bbox_in_operations_log(self):
        cv2 = FakeCv2(decode_result=FakeImage(shape=(1000, 800, 3)),
                      contour_bbox=(11, 22, 333, 444))
        pp, _ = _make_pp(fake_cv2=cv2)
        res = pp.prepare(b"x")
        crop_op = next(op for op in res["operations"]
                       if op["step"] == "crop")
        self.assertEqual(crop_op["bbox"], [11, 22, 333, 444])


class RotateTest(unittest.TestCase):
    def test_rotates_landscape_to_portrait(self):
        # Decoded shape is landscape (w=1200 > h=800) and the crop
        # bbox preserves the landscape ratio → rotate triggers.
        cv2 = FakeCv2(decode_result=FakeImage(shape=(800, 1200, 3)),
                      contour_bbox=(0, 0, 1200, 800))
        pp, _ = _make_pp(fake_cv2=cv2)
        res = pp.prepare(b"x")
        self.assertTrue(res["rotated"])
        rotate_op = next(op for op in res["operations"]
                         if op["step"] == "rotate")
        self.assertTrue(rotate_op["applied"])

    def test_does_not_rotate_portrait(self):
        cv2 = FakeCv2(decode_result=FakeImage(shape=(1200, 800, 3)),
                      contour_bbox=(0, 0, 800, 1200))
        pp, _ = _make_pp(fake_cv2=cv2)
        res = pp.prepare(b"x")
        self.assertFalse(res["rotated"])
        rotate_op = next(op for op in res["operations"]
                         if op["step"] == "rotate")
        self.assertFalse(rotate_op["applied"])


class SkipFlagsTest(unittest.TestCase):
    def test_skip_crop_omits_step_and_bbox(self):
        pp, _ = _make_pp(skip_crop=True)
        res = pp.prepare(b"x")
        self.assertTrue(res["ok"])
        steps = [op["step"] for op in res["operations"]]
        self.assertNotIn("crop", steps)
        self.assertIsNone(res["card_bbox"])

    def test_skip_rotate_omits_step(self):
        pp, _ = _make_pp(skip_rotate=True)
        res = pp.prepare(b"x")
        self.assertTrue(res["ok"])
        steps = [op["step"] for op in res["operations"]]
        self.assertNotIn("rotate", steps)
        self.assertFalse(res["rotated"])

    def test_skip_clahe_omits_step(self):
        pp, _ = _make_pp(skip_clahe=True)
        res = pp.prepare(b"x")
        self.assertTrue(res["ok"])
        steps = [op["step"] for op in res["operations"]]
        self.assertNotIn("clahe", steps)

    def test_skip_all_three_runs_only_decode_and_encode(self):
        pp, _ = _make_pp(skip_crop=True, skip_rotate=True,
                          skip_clahe=True)
        res = pp.prepare(b"x")
        steps = [op["step"] for op in res["operations"]]
        self.assertEqual(steps, ["decode", "encode"])


class FailureTest(unittest.TestCase):
    def test_no_lib_when_libs_unavailable(self):
        # Don't inject — let the lazy loader try to import. Stub
        # cv2 in sys.modules to None so the import fails.
        from unittest import mock
        with mock.patch.dict(sys.modules, {"cv2": None}):
            pp = ImagePreprocessor()
            res = pp.prepare(b"x")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "NO_LIB")
        self.assertIn("elapsed_ms", res)

    def test_decode_failed_when_imdecode_returns_none(self):
        cv2 = FakeCv2(decode_result=None)  # imdecode returns None
        pp, _ = _make_pp(fake_cv2=cv2)
        res = pp.prepare(b"not-a-real-image")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "DECODE_FAILED")

    def test_encode_failed_when_imencode_returns_false(self):
        cv2 = FakeCv2(decode_result=FakeImage(shape=(1000, 800, 3)),
                      encode_ok=False)
        pp, _ = _make_pp(fake_cv2=cv2)
        res = pp.prepare(b"x")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "ENCODE_FAILED")

    def test_none_image_raises(self):
        pp, _ = _make_pp()
        with self.assertRaises(ImagePreprocessError):
            pp.prepare(None)

    def test_unsupported_image_type_raises(self):
        pp, _ = _make_pp()
        with self.assertRaises(ImagePreprocessError):
            pp.prepare(12345)

    def test_path_not_found_returns_bad_input(self):
        pp, _ = _make_pp()
        res = pp.prepare("/no/such/file/abcdef.png")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "BAD_INPUT")


class BestEffortTest(unittest.TestCase):
    """Crop and CLAHE failures should NOT abort the pipeline —
    they're cosmetic; we'd rather have a half-cleaned image than
    no image at all."""

    def test_crop_exception_logged_but_pipeline_continues(self):
        cv2 = FakeCv2(decode_result=FakeImage(shape=(1000, 800, 3)),
                      raise_on={"blur": RuntimeError("crop boom")})
        pp, _ = _make_pp(fake_cv2=cv2)
        res = pp.prepare(b"x")
        self.assertTrue(res["ok"])
        crop_op = next(op for op in res["operations"]
                       if op["step"] == "crop")
        self.assertIn("error", crop_op)
        self.assertIn("crop boom", crop_op["error"])

    def test_clahe_exception_logged_but_pipeline_continues(self):
        cv2 = FakeCv2(decode_result=FakeImage(shape=(1000, 800, 3)),
                      raise_on={"clahe": RuntimeError("clahe boom")})
        pp, _ = _make_pp(fake_cv2=cv2)
        res = pp.prepare(b"x")
        self.assertTrue(res["ok"])
        clahe_op = next(op for op in res["operations"]
                        if op["step"] == "clahe")
        self.assertIn("error", clahe_op)


class CropFallbackTest(unittest.TestCase):
    def test_no_contours_skips_crop(self):
        cv2 = FakeCv2(decode_result=FakeImage(shape=(1000, 800, 3)),
                      contour_count=0)
        pp, _ = _make_pp(fake_cv2=cv2)
        res = pp.prepare(b"x")
        self.assertTrue(res["ok"])
        self.assertIsNone(res["card_bbox"])

    def test_tiny_contour_skipped_as_noise(self):
        # bbox area = 10*10 = 100, well under 10% of 1000*800.
        cv2 = FakeCv2(decode_result=FakeImage(shape=(1000, 800, 3)),
                      contour_bbox=(0, 0, 10, 10))
        pp, _ = _make_pp(fake_cv2=cv2)
        res = pp.prepare(b"x")
        self.assertTrue(res["ok"])
        self.assertIsNone(res["card_bbox"])


class InputTypeTest(unittest.TestCase):
    def test_bytes_input(self):
        pp, _ = _make_pp()
        res = pp.prepare(b"\x89PNG-bytes")
        self.assertTrue(res["ok"])

    def test_bytearray_input(self):
        pp, _ = _make_pp()
        res = pp.prepare(bytearray(b"hello"))
        self.assertTrue(res["ok"])

    def test_path_str_input(self):
        fd, path = tempfile.mkstemp(prefix="ipt_", suffix=".png")
        try:
            os.write(fd, b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
            os.close(fd)
            pp, _ = _make_pp()
            res = pp.prepare(path)
            self.assertTrue(res["ok"])
        finally:
            os.unlink(path)

    def test_pathlib_input(self):
        import pathlib
        fd, path = tempfile.mkstemp(prefix="ipt_", suffix=".png")
        try:
            os.write(fd, b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
            os.close(fd)
            pp, _ = _make_pp()
            res = pp.prepare(pathlib.Path(path))
            self.assertTrue(res["ok"])
        finally:
            os.unlink(path)


class ConfigTest(unittest.TestCase):
    def test_default_target_height_constant_is_sane(self):
        # If somebody bumps it absurdly low/high, OCR quality dies.
        # 256-4096 is a generous sanity range.
        self.assertGreaterEqual(DEFAULT_TARGET_HEIGHT_PX, 256)
        self.assertLessEqual(DEFAULT_TARGET_HEIGHT_PX, 4096)

    def test_clahe_params_propagate_to_cv2_call(self):
        cv2 = FakeCv2(decode_result=FakeImage(shape=(1000, 800, 3)))
        pp = ImagePreprocessor(cv2_module=cv2, np_module=FakeNp(),
                                clahe_clip=3.5, clahe_tile_size=12)
        pp.prepare(b"x")
        clahe_call = next(c for c in cv2.calls
                          if c[0] == "createCLAHE")
        self.assertAlmostEqual(clahe_call[1], 3.5)
        self.assertEqual(clahe_call[2], (12, 12))


class HelperTest(unittest.TestCase):
    def test_elapsed_ms_returns_nonnegative_int(self):
        import time
        t0 = time.monotonic()
        ms = _elapsed_ms(t0)
        self.assertIsInstance(ms, int)
        self.assertGreaterEqual(ms, 0)


if __name__ == "__main__":
    unittest.main()
