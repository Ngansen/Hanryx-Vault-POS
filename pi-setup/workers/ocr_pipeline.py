"""
workers/ocr_pipeline.py — snapshot → preprocess → live OCR, in one call.

This is the public entry point the tablet UI calls when the
operator points the camera at a card. It wires together the two
slices that came before:

  * workers/image_preprocess.ImagePreprocessor — crops to the card,
    rotates landscape to portrait, normalises contrast.
  * workers/live_ocr.LiveOcrEngine — synchronous PaddleOCR pass(es)
    with KR-first auto-detect and early-exit on confident matches.

Why a separate module
---------------------
Either piece is independently useful — the cleanup module also
gets used by the catalogue ingest path, the OCR engine also gets
called by the batch worker — so neither owns the other. This
pipeline is the third party that knows how to USE both for the
specific tablet-snapshot scenario.

Failure handling
----------------
If the preprocessor fails (NO_LIB on a Pi without cv2 installed,
BAD_INPUT on a missing path, decode failure on a corrupt JPEG)
we DON'T abort — we fall back to OCR-ing the original image
unmodified. PaddleOCR can usually still read uncropped snapshots,
just less reliably; a low-confidence read beats no read at all
in front of a customer.

If the OCR pass itself fails (NO_LIB, NO_TEXT, FACTORY_ERROR)
that's a hard failure — we have nothing to give the caller.
The result dict's `source` field tells the UI which image variant
actually got OCR'd ("preprocessed" or "original") so the operator
can know whether to retry without preprocessing.

Result shape
------------
::

    {
      "ok":             True,
      "full_text":      "포켓몬 카드",
      "lang_hint":      "kr",
      "lines":          [{"text":..., "conf":..., "bbox":[...]}],
      "avg_conf":       0.87,
      "low_confidence": False,
      "tried":          ["kr"],
      "source":         "preprocessed",     # or "original"
      "preprocess":     {                   # mirrors ImagePreprocessor
        "ok":         True,
        "applied":    ["decode","crop","rotate","clahe","encode"],
        "card_bbox":  [x,y,w,h] | None,
        "rotated":    True,
        "elapsed_ms": 39,
        "error":      ""                    # only set when ok=False
      },
      "model_id":       "paddleocr-ppocrv4-1.0",
      "elapsed_ms":     451,                # preprocess + OCR
    }

On OCR failure::

    {
      "ok":         False,
      "error":      "NO_TEXT" | "NO_LIB" | "NO_IMAGE" | ...,
      "source":     "preprocessed" | "original",
      "preprocess": {...},
      "tried":      [...],
      "model_id":   "...",
      "elapsed_ms": ...,
    }
"""
from __future__ import annotations

import logging
import time
from typing import Any

from .image_preprocess import ImagePreprocessor
from .live_ocr import LiveOcrEngine

log = logging.getLogger("workers.ocr_pipeline")


class OcrPipeline:
    """Tablet-snapshot OCR pipeline.

    Owns one ImagePreprocessor + one LiveOcrEngine and runs them
    in series for each snapshot. Both stages can be supplied via
    the constructor for tests; defaults build fresh instances with
    standard configuration.
    """

    def __init__(
        self,
        *,
        preprocessor: ImagePreprocessor | None = None,
        ocr_engine: LiveOcrEngine | None = None,
    ):
        self.preprocessor = preprocessor or ImagePreprocessor()
        self.ocr_engine = ocr_engine or LiveOcrEngine()

    @property
    def model_id(self) -> str:
        return self.ocr_engine.model_id

    # ── Public API ─────────────────────────────────────────────────

    def ocr_snapshot(
        self,
        image: Any,
        *,
        lang_hint: str | None = None,
    ) -> dict[str, Any]:
        """Run preprocess → OCR on a single tablet snapshot.

        See module docstring for the result shape. `lang_hint` is
        forwarded to LiveOcrEngine; pass None to auto-detect.
        """
        t0 = time.monotonic()

        # ── Preprocess (best-effort) ─────────────────────────────
        pre = self.preprocessor.prepare(image)
        pre_summary = self._summarise_preprocess(pre)

        if pre.get("ok"):
            ocr_input: Any = pre["image"]
            source = "preprocessed"
        else:
            log.info(
                "[ocr_pipeline] preprocess failed (%s); "
                "falling back to original image for OCR",
                pre.get("error"),
            )
            ocr_input = image
            source = "original"

        # ── OCR ──────────────────────────────────────────────────
        ocr = self.ocr_engine.ocr_image(ocr_input, lang_hint=lang_hint)
        return self._merge(ocr, pre_summary, source, t0)

    # ── Internals ──────────────────────────────────────────────────

    @staticmethod
    def _summarise_preprocess(pre: dict[str, Any]) -> dict[str, Any]:
        """Extract the fields the caller actually wants — full
        operations log is too noisy for the UI, just give them the
        list of step names that ran successfully."""
        applied = [
            op["step"] for op in pre.get("operations") or []
            if "error" not in op
        ]
        out: dict[str, Any] = {
            "ok":         bool(pre.get("ok")),
            "applied":    applied,
            "card_bbox":  pre.get("card_bbox"),
            "rotated":    bool(pre.get("rotated")),
            "elapsed_ms": int(pre.get("elapsed_ms") or 0),
        }
        if not out["ok"]:
            out["error"] = pre.get("error") or ""
        return out

    def _merge(self, ocr: dict[str, Any], pre_summary: dict[str, Any],
                source: str, t0: float) -> dict[str, Any]:
        total_ms = int((time.monotonic() - t0) * 1000)
        merged: dict[str, Any] = {
            "ok":         bool(ocr.get("ok")),
            "source":     source,
            "preprocess": pre_summary,
            "tried":      ocr.get("tried") or [],
            "model_id":   self.model_id,
            "elapsed_ms": total_ms,
        }
        if ocr.get("ok"):
            merged.update({
                "full_text":      ocr.get("full_text", ""),
                "lang_hint":      ocr.get("lang_hint"),
                "lines":          ocr.get("lines") or [],
                "avg_conf":       ocr.get("avg_conf", 0.0),
                "low_confidence": bool(ocr.get("low_confidence")),
            })
        else:
            merged["error"] = ocr.get("error") or "UNKNOWN"
        return merged
