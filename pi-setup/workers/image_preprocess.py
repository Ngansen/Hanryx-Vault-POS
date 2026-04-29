"""
workers/image_preprocess.py — image cleanup for OCR-grade snapshots.

The PaddleOCR engine in workers/ocr_indexer.py and workers/live_ocr.py
performs much better when fed images that are:

  * cropped to JUST the card (no background, no operator's fingers),
  * roughly upright (portrait orientation, not landscape),
  * contrast-normalised (matters for matte JP holos under harsh
    overhead show lighting).

This module provides an `ImagePreprocessor` that runs those cleanup
passes on a tablet snapshot and returns the cleaned PNG bytes ready
to hand to LiveOcrEngine.ocr_image().

Design conventions (mirrors live_ocr / ocr_indexer / price_refresh):

  * Lazy import of cv2 + numpy with NO_LIB fallback. A fresh Pi
    without the ML stack still imports this module cleanly; calling
    .prepare() returns {"ok": False, "error": "NO_LIB"}.
  * cv2_module / np_module injectable for tests so we can verify
    pipeline dispatch without pulling the real libs (which are
    ~250 MB).
  * Dict result with an `operations` log so the live OCR endpoint
    can surface "we cropped, we rotated, here's the new image".
  * Per-step skip flags (skip_crop, skip_rotate, skip_clahe) so an
    operator can disable a misbehaving stage without redeploying.

Pipeline
--------
  1. decode      — bytes → array (cv2.imdecode, IMREAD_COLOR)
  2. crop        — find largest contour, take its bounding rect.
                   This is the simplest cropper that works in
                   trade-show conditions; perspective-warp is a
                   future enhancement (needs robust edge detection
                   on glossy cards which is its own project).
  3. rotate      — if the cropped result is landscape (w > h),
                   rotate 90° CCW so PaddleOCR sees portrait.
                   Doesn't try to detect upside-down — that needs
                   OCR-based scoring, deferred to a later slice.
  4. clahe       — Contrast Limited Adaptive Histogram Equalisation
                   on the luminance channel of LAB. Brings out matte
                   text on dim foil cards without blowing out
                   already-bright shots.
  5. encode      — array → PNG bytes (lossless; JPEG would risk
                   eating the small text PaddleOCR needs).

Each step can be skipped individually via the constructor.

What this module deliberately does NOT do
-----------------------------------------
  * Perspective-warp the card to a fixed rectangle. That needs
    reliable 4-corner detection which is unsolved on glossy holos
    under glare. The simple bounding-rect crop above gets us 80%
    of the win at 5% of the engineering cost.
  * Detect / fix upside-down. Needs an OCR-confidence-feedback
    loop (try, rotate 180°, retry, pick higher conf) — belongs
    in LiveOcrEngine, not here.
  * Auto-white-balance. Most cards have known white borders we
    could anchor to, but trade-show lighting is so variable that
    a naive WB hurts as often as it helps. Deferred until we have
    real-world data to tune against.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

log = logging.getLogger("workers.image_preprocess")


# Standard Pokémon TCG card aspect: 63 × 88 mm = 1:1.397. Picked the
# encoder target so a portrait card lands at a height that's a
# decent OCR resolution without being huge (PaddleOCR processes
# faster on smaller inputs and we already have plenty of detail).
DEFAULT_TARGET_HEIGHT_PX = 1024


class ImagePreprocessError(Exception):
    """Raised on programmer errors (bad input type). Recoverable
    per-image errors come back in the result dict's `error` field."""


class ImagePreprocessor:
    """Clean up a tablet snapshot before handing it to PaddleOCR.

    See module docstring for the pipeline and design rationale.
    """

    def __init__(
        self,
        *,
        target_height_px: int | None = None,
        clahe_clip: float = 2.0,
        clahe_tile_size: int = 8,
        skip_crop: bool = False,
        skip_rotate: bool = False,
        skip_clahe: bool = False,
        cv2_module: Any = None,
        np_module: Any = None,
    ):
        self.target_height_px = (target_height_px
                                 or DEFAULT_TARGET_HEIGHT_PX)
        self.clahe_clip = float(clahe_clip)
        self.clahe_tile_size = int(clahe_tile_size)
        self.skip_crop = bool(skip_crop)
        self.skip_rotate = bool(skip_rotate)
        self.skip_clahe = bool(skip_clahe)

        self._injected_cv2 = cv2_module
        self._injected_np = np_module
        self._libs_loaded = False
        self._load_failure = ""  # "" | "NO_LIB" | "ERROR:..."
        self._cv2: Any = None
        self._np: Any = None

    # ── Lazy lib loader ────────────────────────────────────────────

    def _ensure_libs(self) -> tuple[Any, Any] | None:
        """Returns (cv2, np) or None on lib failure. Tri-state load
        cache mirrors ocr_indexer._ensure_paddle so a missing lib is
        only logged once per process."""
        if self._injected_cv2 is not None and self._injected_np is not None:
            return self._injected_cv2, self._injected_np
        if self._libs_loaded:
            return self._cv2, self._np
        if self._load_failure:
            return None
        try:
            import cv2  # type: ignore
            import numpy as np  # type: ignore
        except ImportError as e:
            self._load_failure = "NO_LIB"
            log.warning(
                "[image_preprocess] cv2 / numpy missing: %s — install "
                "with `pip install opencv-python-headless numpy`", e,
            )
            return None
        self._cv2 = cv2
        self._np = np
        self._libs_loaded = True
        return cv2, np

    # ── Public API ─────────────────────────────────────────────────

    def prepare(self, image: Any) -> dict[str, Any]:
        """Clean up `image` and return PNG bytes.

        Parameters
        ----------
        image : str | os.PathLike | bytes | bytearray | memoryview
            Filesystem path or raw image bytes.

        Returns
        -------
        On success::

            {
              "ok":          True,
              "image":       <PNG bytes>,
              "operations":  [{"step": "decode",  "ms": 4,
                               "shape": (1080, 1920, 3)},
                              {"step": "crop",    "ms": 12,
                               "bbox": [50, 100, 950, 1500]},
                              {"step": "rotate",  "ms": 1,
                               "applied": True},
                              {"step": "clahe",   "ms": 8},
                              {"step": "encode",  "ms": 14,
                               "bytes": 154321}],
              "card_bbox":   [50, 100, 950, 1500],   # crop result
              "rotated":     True,
              "elapsed_ms":  39,
            }

        On failure::

            {"ok": False, "error": "NO_LIB" | "BAD_INPUT"
                                   | "DECODE_FAILED" | "ENCODE_FAILED"
                                   | "ERROR:...",
             "operations": [...partial...],
             "elapsed_ms": ...}
        """
        t0 = time.monotonic()
        operations: list[dict[str, Any]] = []

        if image is None:
            raise ImagePreprocessError(
                "prepare() requires an image (path or bytes)")

        libs = self._ensure_libs()
        if libs is None:
            return self._fail(self._load_failure or "NO_LIB",
                              t0, operations)
        cv2, np = libs

        # Step 1: decode -------------------------------------------------
        raw_bytes = self._materialise_bytes(image)
        if raw_bytes is None:
            return self._fail("BAD_INPUT", t0, operations)
        try:
            ts = time.monotonic()
            arr = np.frombuffer(raw_bytes, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                return self._fail("DECODE_FAILED", t0, operations)
            operations.append({
                "step": "decode",
                "ms": _elapsed_ms(ts),
                "shape": tuple(getattr(img, "shape", ())),
            })
        except Exception as e:  # noqa: BLE001
            return self._fail(f"ERROR:decode:{type(e).__name__}:{e}",
                              t0, operations)

        # Step 2: crop ---------------------------------------------------
        card_bbox = None
        if not self.skip_crop:
            try:
                ts = time.monotonic()
                cropped, card_bbox = self._crop_to_card(img, cv2, np)
                operations.append({
                    "step": "crop",
                    "ms": _elapsed_ms(ts),
                    "bbox": card_bbox,
                })
                img = cropped
            except Exception as e:  # noqa: BLE001
                # Crop is best-effort — fall through to whole image
                # rather than aborting the pipeline.
                operations.append({
                    "step": "crop",
                    "ms": _elapsed_ms(ts),
                    "error": f"{type(e).__name__}:{e}",
                })

        # Step 3: rotate -------------------------------------------------
        rotated = False
        if not self.skip_rotate:
            ts = time.monotonic()
            h, w = img.shape[:2]
            if w > h:
                img = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
                rotated = True
            operations.append({
                "step": "rotate",
                "ms": _elapsed_ms(ts),
                "applied": rotated,
            })

        # Step 4: CLAHE on luminance ------------------------------------
        if not self.skip_clahe:
            try:
                ts = time.monotonic()
                lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
                l, a, b = cv2.split(lab)
                clahe = cv2.createCLAHE(
                    clipLimit=self.clahe_clip,
                    tileGridSize=(self.clahe_tile_size,
                                  self.clahe_tile_size),
                )
                l2 = clahe.apply(l)
                lab2 = cv2.merge((l2, a, b))
                img = cv2.cvtColor(lab2, cv2.COLOR_LAB2BGR)
                operations.append({
                    "step": "clahe",
                    "ms": _elapsed_ms(ts),
                })
            except Exception as e:  # noqa: BLE001
                # CLAHE is also best-effort.
                operations.append({
                    "step": "clahe",
                    "ms": _elapsed_ms(ts),
                    "error": f"{type(e).__name__}:{e}",
                })

        # Step 5: encode -------------------------------------------------
        try:
            ts = time.monotonic()
            ok, png = cv2.imencode(".png", img)
            if not ok:
                return self._fail("ENCODE_FAILED", t0, operations)
            png_bytes = bytes(png)
            operations.append({
                "step": "encode",
                "ms": _elapsed_ms(ts),
                "bytes": len(png_bytes),
            })
        except Exception as e:  # noqa: BLE001
            return self._fail(f"ERROR:encode:{type(e).__name__}:{e}",
                              t0, operations)

        return {
            "ok": True,
            "image":      png_bytes,
            "operations": operations,
            "card_bbox":  card_bbox,
            "rotated":    rotated,
            "elapsed_ms": _elapsed_ms(t0),
        }

    # ── Internals ──────────────────────────────────────────────────

    def _materialise_bytes(self, image: Any) -> bytes | None:
        if isinstance(image, (bytes, bytearray, memoryview)):
            return bytes(image)
        if isinstance(image, (str, os.PathLike)):
            try:
                with open(os.fspath(image), "rb") as fh:
                    return fh.read()
            except OSError:
                return None
        raise ImagePreprocessError(
            f"image must be a path or bytes; got {type(image).__name__}"
        )

    def _crop_to_card(self, img: Any, cv2: Any, np: Any
                       ) -> tuple[Any, list[int] | None]:
        """Return (cropped_img, [x, y, w, h]) or (img, None) if no
        plausible card-sized contour was found.

        Strategy: greyscale → blur → adaptive threshold → find the
        largest external contour → take its axis-aligned bounding
        rect. If the rect is implausibly small (<10% of the frame
        area) we assume the card fills the frame and skip the crop.
        """
        h0, w0 = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        # Otsu picks the threshold automatically — robust against
        # show-floor lighting variation.
        _, thr = cv2.threshold(
            blur, 0, 255,
            cv2.THRESH_BINARY + cv2.THRESH_OTSU,
        )
        contours, _ = cv2.findContours(
            thr, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
        )
        if not contours:
            return img, None
        # Largest contour by area.
        c = max(contours, key=cv2.contourArea)
        x, y, w, h = cv2.boundingRect(c)
        if w * h < (w0 * h0) * 0.10:
            # Tiny blob — probably noise, not a card.
            return img, None
        return img[y:y + h, x:x + w], [int(x), int(y), int(w), int(h)]

    def _fail(self, error: str, t0: float,
               operations: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "ok": False,
            "error":      error,
            "operations": operations,
            "elapsed_ms": _elapsed_ms(t0),
        }


def _elapsed_ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)
