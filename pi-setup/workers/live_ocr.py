"""
workers/live_ocr.py — synchronous OCR for tablet snapshot scans.

Counterpart to workers/ocr_indexer.py:

  * ocr_indexer is the BATCH path — async, queue-driven, writes one
    row per (card, lang) into the card_ocr table; processes the
    master catalogue over days.
  * live_ocr is the FOREGROUND path — sync, returns immediately to
    the caller, never touches the database. Used by the tablet UI
    when the operator points the camera at a card a customer is
    buying or selling and we need text out RIGHT NOW.

Reuses ocr_indexer's PaddleOCR engine (same models_dir resolution,
same per-language cache, same result parser):

  * The first call warms the same in-process cache the batch worker
    uses, so the batch worker doesn't pay the 3-5 s per-language
    model-load cost again on its next pass.
  * Bug fixes in _parse_ocr_result automatically apply to both paths.
  * Tests for one path's parsing cover the other.

Why not just call OcrIndexerWorker.process()?
  process() is shaped for queue rows ({task_id, payload:{set_id,
  card_number, lang_hint, model_id}}) and writes the result to
  card_ocr. The live caller has none of that — it has bytes-in-hand
  (or a snapshot path), wants bytes-of-text-out, and never touches
  the catalogue.

Composition over inheritance, conn=None
---------------------------------------
LiveOcrEngine holds an OcrIndexerWorker constructed with conn=None.
Worker.__init__ stores conn but never DEREFERENCES it — only
seed/process/_record_* do that, and we never call those. Keeping
this seam composition-only (instead of refactoring the worker into
a base engine class) preserves the 379-test cushion the worker has.

Auto-detect language
--------------------
When lang_hint=None the engine tries each language in
auto_priority order (KR > JP > CHS > EN by default — KR-specialist
inventory). It EARLY-EXITS as soon as a pass returns text with
avg_conf >= min_auto_conf, saving up to 3 redundant PaddleOCR
runs (~1.5 s on Pi 5). If no language meets the threshold but
some produced text, the highest-conf attempt is returned with
low_confidence=True so the UI can prompt the operator to pick a
language manually.
"""
from __future__ import annotations

import logging
import os
import tempfile
import time
from typing import Any, Callable, Iterable

from .ocr_indexer import (
    LANG_PRIORITY,
    PADDLE_LANG_MAP,
    OcrIndexerWorker,
)

log = logging.getLogger("workers.live_ocr")


# How confident do we have to be in a per-language pass before we
# stop trying other languages? 0.65 picked from PaddleOCR avg_conf
# distributions on KR cards: real Korean text on a clean image
# averages 0.85+; garbage on a wrong-language pass averages <0.4.
# Cards in mixed lighting hover around 0.7, so 0.65 lets us
# early-exit on most clean shots while still falling through to
# the next language on a smudgy first guess.
DEFAULT_MIN_AUTO_CONF = 0.65


class LiveOcrError(Exception):
    """Raised by LiveOcrEngine on programmer errors (bad lang_hint,
    None image, etc.). Recoverable per-image errors are returned in
    the result dict's `error` field instead of raised — the caller
    is a UI that needs to keep going."""


class LiveOcrEngine:
    """Synchronous OCR engine for one-off tablet snapshots.

    See the module docstring for the design rationale (composition
    over inheritance, conn=None on the wrapped worker).
    """

    DEFAULT_AUTO_PRIORITY = LANG_PRIORITY

    def __init__(
        self,
        *,
        models_dir: str | None = None,
        model_id: str | None = None,
        paddle_factory: Callable[[str], Any] | None = None,
        auto_priority: Iterable[str] | None = None,
        min_auto_conf: float = DEFAULT_MIN_AUTO_CONF,
    ):
        # conn=None is intentional — see module docstring. Worker
        # only touches conn from seed/process/_record_*, none of
        # which we call.
        self._worker = OcrIndexerWorker(
            None,
            models_dir=models_dir,
            model_id=model_id,
            paddle_factory=paddle_factory,
        )
        self.auto_priority = tuple(
            auto_priority if auto_priority is not None
            else self.DEFAULT_AUTO_PRIORITY
        )
        for code in self.auto_priority:
            if code not in PADDLE_LANG_MAP:
                raise LiveOcrError(
                    f"unknown auto_priority code {code!r}; "
                    f"expected from {sorted(PADDLE_LANG_MAP)}"
                )
        self.min_auto_conf = float(min_auto_conf)

    # ── Read-through properties so callers can introspect ──────────

    @property
    def model_id(self) -> str:
        return self._worker.model_id

    @property
    def models_dir(self) -> str:
        return self._worker.models_dir

    # ── Public API ─────────────────────────────────────────────────

    def ocr_image(
        self,
        image: Any,
        *,
        lang_hint: str | None = None,
    ) -> dict[str, Any]:
        """Run OCR on a single image and return a result dict.

        Parameters
        ----------
        image : str | os.PathLike | bytes | bytearray
            Filesystem path, or raw image bytes (PNG / JPEG /
            WebP / GIF / BMP).
        lang_hint : 'kr' | 'jp' | 'chs' | 'en' | None
            Force a single-language pass, or None to auto-detect
            in self.auto_priority order with early-exit on a
            confident match.

        Returns
        -------
        On success::

            {
              "ok": True,
              "lang_hint":   "kr",
              "lines":       [{"text":..., "conf":..., "bbox":[...]}],
              "full_text":   "...",
              "avg_conf":    0.87,
              "low_confidence": False,
              "tried":       ["kr"],
              "image_path":  "/path/or/tmp",
              "model_id":    "paddleocr-ppocrv4-1.0",
              "elapsed_ms":  412,
            }

        On failure::

            {
              "ok": False,
              "error":      "NO_LIB" | "NO_IMAGE" | "NO_TEXT"
                            | "FACTORY_ERROR:..." ,
              "tried":      [...],
              "image_path": "...",
              "model_id":   "...",
              "elapsed_ms": ...,
            }
        """
        t0 = time.monotonic()

        if image is None:
            raise LiveOcrError(
                "ocr_image() requires an image (path or bytes)"
            )
        if lang_hint is not None and lang_hint not in PADDLE_LANG_MAP:
            raise LiveOcrError(
                f"unknown lang_hint {lang_hint!r}; "
                f"expected one of {sorted(PADDLE_LANG_MAP)} or None"
            )

        path, owned_tmp = self._materialise_image(image)
        try:
            if (not path
                    or not os.path.exists(path)
                    or os.path.getsize(path) == 0):
                return self._failure("NO_IMAGE", t0, [],
                                     image_path=path or "")
            try_order = ([lang_hint] if lang_hint
                         else list(self.auto_priority))
            return self._run_passes(path, try_order, t0)
        finally:
            if owned_tmp:
                try:
                    os.unlink(owned_tmp)
                except OSError:
                    pass

    # ── Internals ──────────────────────────────────────────────────

    def _materialise_image(
        self, image: Any,
    ) -> tuple[str, str | None]:
        """Return (path, owned_tmp). owned_tmp is the path the
        caller must unlink, or None if we didn't create one."""
        if isinstance(image, (str, os.PathLike)):
            return os.fspath(image), None
        if isinstance(image, (bytes, bytearray, memoryview)):
            data = bytes(image)
            # Sniff the format so the tmpfile gets the right
            # suffix — PaddleOCR delegates to cv2.imread which
            # cares about the extension on some platforms.
            ext = _sniff_image_ext(data)
            fd, tmp = tempfile.mkstemp(prefix="live_ocr_",
                                        suffix=ext)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            return tmp, tmp
        raise LiveOcrError(
            f"image must be a path or bytes; "
            f"got {type(image).__name__}"
        )

    def _run_passes(self, path: str, langs: list[str],
                     t0: float) -> dict[str, Any]:
        tried: list[str] = []
        best: dict[str, Any] | None = None
        for code in langs:
            tried.append(code)
            paddle_lang = PADDLE_LANG_MAP[code]
            ocr = self._worker._get_ocr(paddle_lang)  # noqa: SLF001
            if ocr is None:
                # NO_LIB / FACTORY_ERROR — same code path for every
                # language, so bail out immediately instead of
                # looping through the same failure 4 times.
                return self._failure(
                    self._worker._load_failure or "NO_LIB",  # noqa: SLF001
                    t0, tried, image_path=path,
                )
            try:
                raw = ocr.ocr(path, cls=False)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "[live_ocr] PaddleOCR raised on lang=%s "
                    "for %s: %s", code, path, e,
                )
                # Treat as "this language didn't work" but try the
                # next one — could be a CJK-only model choking on a
                # pure-EN card or a similar mismatch.
                continue
            lines, full_text, avg_conf = (
                self._worker._parse_ocr_result(raw)  # noqa: SLF001
            )
            attempt = {
                "lang_hint": code,
                "lines": lines,
                "full_text": full_text,
                "avg_conf": avg_conf,
            }
            if lines and avg_conf >= self.min_auto_conf:
                return self._success(path, attempt, tried, t0)
            if lines and (best is None
                          or avg_conf > best["avg_conf"]):
                best = attempt

        if best is not None:
            # No language met the threshold but we got SOME text.
            # Flag low_confidence so the UI can prompt the operator
            # to pick a language manually instead of trusting it.
            return self._success(path, best, tried, t0,
                                  low_confidence=True)
        return self._failure("NO_TEXT", t0, tried, image_path=path)

    def _success(self, image_path: str, attempt: dict[str, Any],
                  tried: list[str], t0: float, *,
                  low_confidence: bool = False) -> dict[str, Any]:
        return {
            "ok": True,
            "lang_hint":      attempt["lang_hint"],
            "lines":          attempt["lines"],
            "full_text":      attempt["full_text"],
            "avg_conf":       attempt["avg_conf"],
            "low_confidence": low_confidence,
            "tried":          tried,
            "image_path":     image_path,
            "model_id":       self.model_id,
            "elapsed_ms":     int((time.monotonic() - t0) * 1000),
        }

    def _failure(self, error: str, t0: float, tried: list[str], *,
                  image_path: str = "") -> dict[str, Any]:
        return {
            "ok": False,
            "error":      error,
            "tried":      tried,
            "image_path": image_path,
            "model_id":   self.model_id,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
        }


# ── Module-level helpers ────────────────────────────────────────────

# Magic-byte → suffix table. Order matters for false-positive
# avoidance (RIFF could be WAV but in our tablet-snapshot context
# every RIFF will be a WebP).
_MAGIC_TO_EXT: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff",       ".jpg"),
    (b"GIF87a",             ".gif"),
    (b"GIF89a",             ".gif"),
    (b"RIFF",               ".webp"),
    (b"BM",                 ".bmp"),
)


def _sniff_image_ext(data: bytes) -> str:
    """Return the file extension implied by the image's magic bytes,
    or '.bin' for unknown formats. Falling through to '.bin' makes
    PaddleOCR fail loudly on non-image input rather than silently
    succeed with garbage, which is what we want."""
    for magic, ext in _MAGIC_TO_EXT:
        if data.startswith(magic):
            return ext
    return ".bin"
