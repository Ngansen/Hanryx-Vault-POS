"""
HanRyx-Vault Card Recognizer service.

Thin Flask wrapper around prateekt/pokemon-card-recognizer.  Imports the heavy
recogniser lazily so the container can boot and report `/healthz` while the
ML model warms up in the background — useful because torch + easyocr load
takes 20-40 seconds on a Pi 5.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("recognizer")

app = Flask(__name__)

# ── Lazy / background model load ─────────────────────────────────────────────
_recognizer_lock = threading.Lock()
_image_recognizer = None
_video_recognizer = None
_load_error: str | None = None
_load_started = False
_load_done = False


def _load_models() -> None:
    """Import and instantiate both recogniser modes (slow — runs once)."""
    global _image_recognizer, _video_recognizer, _load_error, _load_done
    try:
        log.info("[recognizer] importing pokemon_card_recognizer …")
        from pokemon_card_recognizer.api.card_recognizer import (
            CardRecognizer, OperatingMode,
        )
        log.info("[recognizer] building image recogniser")
        _image_recognizer = CardRecognizer(mode=OperatingMode.IMAGE)
        log.info("[recognizer] building video recogniser")
        _video_recognizer = CardRecognizer(mode=OperatingMode.PULLS_VIDEO)
        log.info("[recognizer] models ready")
    except Exception as exc:
        _load_error = f"{type(exc).__name__}: {exc}"
        log.exception("[recognizer] model load failed")
    finally:
        _load_done = True


def _ensure_loading() -> None:
    global _load_started
    with _recognizer_lock:
        if not _load_started:
            _load_started = True
            threading.Thread(target=_load_models, name="model-load",
                             daemon=True).start()


def _wait_for_model(timeout: float = 5.0) -> bool:
    """Block briefly so a quick request can succeed if loading is almost done."""
    deadline = time.time() + timeout
    while time.time() < deadline and not _load_done:
        time.sleep(0.2)
    return _load_done


def _format_pull(p) -> dict:
    """Convert a recogniser result object into a JSON-friendly dict."""
    out = {}
    for attr in ("name", "card_id", "set", "set_code", "number",
                 "card_number", "rarity", "score", "confidence"):
        if hasattr(p, attr):
            try:
                out[attr] = getattr(p, attr)
            except Exception:
                pass
    if not out:
        out["raw"] = repr(p)
    return out


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({
        "ok": True,
        "model_loaded": _image_recognizer is not None,
        "load_started": _load_started,
        "load_done":    _load_done,
        "load_error":   _load_error,
    })


@app.route("/recognize/image", methods=["POST"])
def recognize_image():
    _ensure_loading()
    if not _wait_for_model(timeout=2.0):
        return jsonify({"error": "model still loading", "ready": False}), 503
    if _load_error:
        return jsonify({"error": _load_error}), 500

    upload = request.files.get("image")
    if not upload or not upload.filename:
        return jsonify({"error": "POST multipart 'image' field"}), 400

    suffix = Path(upload.filename).suffix or ".jpg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        upload.save(tmp.name)
        tmp_path = tmp.name

    try:
        results = _image_recognizer.exec(tmp_path)
        items = [_format_pull(r) for r in (results or [])]
        return jsonify({"count": len(items), "results": items})
    except Exception as exc:
        log.exception("[recognizer] image recognise failed")
        return jsonify({"error": str(exc)}), 500
    finally:
        try: os.unlink(tmp_path)
        except OSError: pass


@app.route("/recognize/video", methods=["POST"])
def recognize_video():
    _ensure_loading()
    if not _wait_for_model(timeout=2.0):
        return jsonify({"error": "model still loading", "ready": False}), 503
    if _load_error:
        return jsonify({"error": _load_error}), 500

    upload = request.files.get("video")
    if not upload or not upload.filename:
        return jsonify({"error": "POST multipart 'video' field"}), 400

    suffix = Path(upload.filename).suffix or ".mp4"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        upload.save(tmp.name)
        tmp_path = tmp.name

    try:
        pulls = _video_recognizer.exec(tmp_path)
        items = [_format_pull(p) for p in (pulls or [])]
        return jsonify({"count": len(items), "results": items})
    except Exception as exc:
        log.exception("[recognizer] video recognise failed")
        return jsonify({"error": str(exc)}), 500
    finally:
        try: os.unlink(tmp_path)
        except OSError: pass


if __name__ == "__main__":
    # Kick off the slow model load immediately on boot so the first real
    # request doesn't have to wait.
    _ensure_loading()
    port = int(os.environ.get("PORT", "8081"))
    log.info("[recognizer] starting on :%d", port)
    app.run(host="0.0.0.0", port=port, threaded=True)
