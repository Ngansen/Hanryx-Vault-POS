"""
HanRyx-Vault Card Recognizer — hybrid OCR + perceptual-hash pipeline.

This service replaces the heavy `pokemon-card-recognizer` (CNN) build with a
faster, lighter, more accurate two-stage pipeline:

    1. OCR pass     — Tesseract reads the printed card-number ("025/198",
                      "S1a-001", etc.) from the corner of the card. If we get
                      a clean read and a hit in the card DB, we're done.
    2. pHash pass   — A 64-bit perceptual hash of the artwork crop is
                      compared (Hamming distance) against an in-memory index
                      of every card's artwork hash. Top-K candidates are
                      returned with a confidence score.

Both passes can run independently; results from each are ranked together so
the caller sees `method = "ocr_number" | "phash" | "fallback"`.

The original endpoint surface is preserved so the existing `server.py` POS
proxy at `/card/scan/recognizer/*` keeps working without changes:

    POST /recognize/image   multipart `image`              → list of guesses
    POST /recognize/video   multipart `video`              → per-frame list
    POST /recognize/scan    multipart `image` (rich JSON: method + confidence)
    GET  /healthz                                          → liveness + stats

The card-hash index lives in Postgres table `card_hashes`, populated by
`pi-setup/import_artwork_hashes.py`. The recognizer reloads the index from
the DB every `HASH_REFRESH_SEC` seconds (default 600) so newly imported
cards become searchable without a restart.
"""
from __future__ import annotations

import io
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import cv2
import imagehash
import numpy as np
import psycopg2
import psycopg2.extras
import pytesseract
from flask import Flask, jsonify, request
from PIL import Image

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("recognizer")

app = Flask(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
DB_URL = os.environ.get(
    "RECOGNIZER_DB_URL",
    os.environ.get("DATABASE_URL",
                   "postgresql://vaultpos:vaultpos@db:5432/vaultpos"),
)
HASH_REFRESH_SEC = int(os.environ.get("HASH_REFRESH_SEC", "600"))
TUNING_REFRESH_SEC = int(os.environ.get("TUNING_REFRESH_SEC", "300"))
PORT = int(os.environ.get("PORT", "8081"))

# Hamming-distance threshold for a "good" pHash match. 64-bit hash:
#   ≤  8 bits = essentially identical artwork
#   ≤ 16 bits = same card, minor lighting/blur differences
#   > 22 bits = different card (or very poor camera)
PHASH_THRESHOLD = int(os.environ.get("PHASH_THRESHOLD", "18"))

# OCR languages bundled into the image; pytesseract uses '+' to combine.
OCR_LANGS = os.environ.get("OCR_LANGS", "eng+kor+jpn+chi_sim")

# Card-number regex: matches "25/198", "025/198", "S1a-001", "SV01-001", etc.
RE_NUM_FRAC = re.compile(r"\b(\d{1,4})\s*/\s*(\d{1,4})\b")
RE_NUM_DASH = re.compile(r"\b([A-Z]{1,4}\d{0,3}-\d{1,4})\b")


# ── In-memory pHash index ────────────────────────────────────────────────────
@dataclass
class HashRow:
    source: str
    card_id: str
    set_code: str
    card_number: str
    name: str
    language: str
    image_url: str
    phash: int  # 64-bit unsigned, stored as Python int


_hash_lock = threading.Lock()
_hash_index: list[HashRow] = []
_hash_loaded_at: float = 0.0
_hash_load_error: str | None = None


def _connect_db():
    return psycopg2.connect(DB_URL, connect_timeout=5)


def _ensure_schema() -> None:
    """Create the card_hashes table if it doesn't exist (safe to re-run)."""
    with _connect_db() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS card_hashes (
                source       TEXT NOT NULL,
                card_id      TEXT NOT NULL,
                set_code     TEXT NOT NULL DEFAULT '',
                card_number  TEXT NOT NULL DEFAULT '',
                name         TEXT NOT NULL DEFAULT '',
                language     TEXT NOT NULL DEFAULT '',
                image_url    TEXT NOT NULL DEFAULT '',
                phash        BIGINT NOT NULL,
                image_sha    TEXT NOT NULL DEFAULT '',
                created_at   BIGINT NOT NULL DEFAULT 0,
                PRIMARY KEY (source, card_id)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_card_hashes_setnum "
                    "ON card_hashes (set_code, card_number)")
        conn.commit()


def _load_hash_index() -> None:
    """Pull every (card_id, phash) row into memory for O(N) Hamming search."""
    global _hash_index, _hash_loaded_at, _hash_load_error
    try:
        _ensure_schema()
        with _connect_db() as conn:
            cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
            cur.execute("""
                SELECT source, card_id, set_code, card_number, name,
                       language, image_url, phash
                FROM card_hashes
            """)
            rows = [
                HashRow(
                    source=r["source"], card_id=r["card_id"],
                    set_code=r["set_code"], card_number=r["card_number"],
                    name=r["name"], language=r["language"],
                    image_url=r["image_url"],
                    # Postgres BIGINT is signed; convert back to unsigned 64-bit.
                    phash=int(r["phash"]) & 0xFFFFFFFFFFFFFFFF,
                )
                for r in cur.fetchall()
            ]
        with _hash_lock:
            _hash_index = rows
            _hash_loaded_at = time.time()
            _hash_load_error = None
        log.info("[recognizer] hash index loaded: %d rows", len(rows))
    except Exception as exc:
        _hash_load_error = f"{type(exc).__name__}: {exc}"
        log.exception("[recognizer] hash load failed")


def _hash_refresh_loop() -> None:
    """Background thread: refresh the in-memory hash index periodically."""
    while True:
        time.sleep(HASH_REFRESH_SEC)
        try:
            _load_hash_index()
        except Exception:
            log.exception("[recognizer] periodic hash refresh failed")


# ── Auto-tuning from operator picks ─────────────────────────────────────────
# Polled from `recognizer_tuning` table populated by the POS server's
# /admin/recognizer/retune route. Lets the recognizer adjust its candidate
# scores based on which (method, source) combinations actually produce
# accepted picks in the field — no model retraining required.
_tuning: dict = {}
_tuning_lock = threading.Lock()
_tuning_loaded_at: float = 0.0


def _load_tuning() -> None:
    """Pull the most recent tuning blob from Postgres into memory."""
    global _tuning, _tuning_loaded_at
    try:
        with _connect_db() as conn, conn.cursor(
            cursor_factory=psycopg2.extras.DictCursor
        ) as cur:
            # Table may not exist on a fresh install; guard with to_regclass.
            cur.execute("SELECT to_regclass('public.recognizer_tuning')")
            row = cur.fetchone()
            if not row or row[0] is None:
                return
            cur.execute(
                "SELECT tuning FROM recognizer_tuning "
                "ORDER BY computed_at DESC LIMIT 1"
            )
            row = cur.fetchone()
            if not row:
                return
            blob = row["tuning"]
            if isinstance(blob, str):
                import json as _json
                blob = _json.loads(blob)
        with _tuning_lock:
            _tuning = blob or {}
            _tuning_loaded_at = time.time()
        cells = len(_tuning.get("method_x_source") or {})
        confused = len(_tuning.get("confused_pairs") or [])
        log.info("[recognizer] tuning loaded: %d cells, %d confused-pairs, "
                 "baseline=%.2f%%",
                 cells, confused,
                 100 * (_tuning.get("baseline_acceptance") or 0))
    except Exception:
        log.exception("[recognizer] tuning load failed")


def _tuning_refresh_loop() -> None:
    """Background thread: refresh the tuning blob periodically."""
    while True:
        time.sleep(TUNING_REFRESH_SEC)
        try:
            _load_tuning()
        except Exception:
            log.exception("[recognizer] periodic tuning refresh failed")


def _apply_tuning(c: dict) -> float:
    """
    Return the candidate's score multiplied by its tuning weight.
    Most-specific cell wins: method_x_source > method_weights > 1.0.
    """
    with _tuning_lock:
        t = _tuning
    if not t:
        return float(c.get("score", 0))
    method = c.get("method") or ""
    source = c.get("source") or ""
    cell = (t.get("method_x_source") or {}).get(f"{method}|{source}")
    if cell is None:
        cell = (t.get("method_weights") or {}).get(method, 1.0)
    return float(c.get("score", 0)) * float(cell)


# ── Image preprocessing ──────────────────────────────────────────────────────
def _decode_image(buf: bytes) -> np.ndarray:
    arr = np.frombuffer(buf, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("could not decode image bytes")
    return img


def _detect_card_warp(img: np.ndarray) -> np.ndarray:
    """
    Find the largest 4-sided contour with a card-like aspect ratio and
    perspective-warp it to a canonical 700×980. If no card is found, return
    the original image so downstream OCR / pHash can still try.
    """
    h, w = img.shape[:2]
    target_w, target_h = 700, 980

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blur, 50, 150)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)[:5]

    for c in contours:
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(approx) != 4:
            continue
        if cv2.contourArea(c) < (w * h) * 0.15:  # too small, ignore
            continue
        # Order corners: top-left, top-right, bottom-right, bottom-left.
        pts = approx.reshape(4, 2).astype("float32")
        s = pts.sum(axis=1)
        d = np.diff(pts, axis=1).flatten()
        rect = np.array([
            pts[np.argmin(s)], pts[np.argmin(d)],
            pts[np.argmax(s)], pts[np.argmax(d)],
        ], dtype="float32")
        dst = np.array([[0, 0], [target_w, 0],
                        [target_w, target_h], [0, target_h]], dtype="float32")
        M = cv2.getPerspectiveTransform(rect, dst)
        return cv2.warpPerspective(img, M, (target_w, target_h))

    return img  # fallback: caller works on the raw frame


# ── OCR-pass: read the printed card number ──────────────────────────────────
def _ocr_card_number(card_img: np.ndarray) -> list[str]:
    """Return all plausible card-number tokens found in the bottom strip."""
    h, w = card_img.shape[:2]
    bottom = card_img[int(h * 0.85): h, :]
    gray = cv2.cvtColor(bottom, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 0, 255,
                              cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    cfg = ("--oem 3 --psm 7 "
           "-c tessedit_char_whitelist=0123456789/-ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    try:
        text = pytesseract.image_to_string(thresh, lang="eng", config=cfg)
    except Exception as exc:
        log.warning("OCR failed: %s", exc)
        return []

    found: list[str] = []
    for m in RE_NUM_FRAC.finditer(text):
        found.append(f"{int(m.group(1))}/{int(m.group(2))}")
        # Common-zero-padded variant for set DBs that store '025/198':
        found.append(f"{m.group(1).zfill(3)}/{m.group(2).zfill(3)}")
    for m in RE_NUM_DASH.finditer(text):
        found.append(m.group(1))
    # Dedupe while preserving order.
    seen = set()
    return [x for x in found if not (x in seen or seen.add(x))]


def _lookup_by_card_number(numbers: Iterable[str]) -> list[dict]:
    """Query every card_* table for a matching card_number / number field."""
    if not numbers:
        return []
    nums = list(numbers)
    out: list[dict] = []
    queries = [
        ("kr", "SELECT 'kr' AS source, card_id, set_name, card_number, "
               "name_kr AS name, image_url, 'kr' AS language "
               "FROM cards_kr WHERE card_number = ANY(%s) LIMIT 5"),
        ("chs", "SELECT 'chs' AS source, card_id, set_name, card_number, "
                "name_chs AS name, image_url, 'chs' AS language "
                "FROM cards_chs WHERE card_number = ANY(%s) LIMIT 5"),
        ("jpn", "SELECT 'jpn' AS source, card_id, set_name, card_number, "
                "name_jp AS name, image_url, 'jpn' AS language "
                "FROM cards_jpn WHERE card_number = ANY(%s) LIMIT 5"),
        ("multi", "SELECT 'multi:' || game AS source, card_id, set_name, "
                  "card_number, name, image_url, language "
                  "FROM cards_multi WHERE card_number = ANY(%s) LIMIT 5"),
    ]
    try:
        with _connect_db() as conn:
            cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
            for label, sql in queries:
                try:
                    cur.execute(sql, (nums,))
                    for r in cur.fetchall():
                        out.append(dict(r))
                except psycopg2.Error:
                    # Table may not exist (e.g. cards_chs hasn't been imported
                    # yet) — skip and continue.
                    conn.rollback()
    except Exception as exc:
        log.warning("card-number lookup failed: %s", exc)
    return out


# ── pHash-pass: KNN over the in-memory index ────────────────────────────────
def _phash_image(card_img: np.ndarray) -> int:
    """64-bit perceptual hash of the card artwork region."""
    # Use roughly the artwork area: top 65% × middle 90%.
    h, w = card_img.shape[:2]
    art = card_img[int(h * 0.10): int(h * 0.65),
                   int(w * 0.05): int(w * 0.95)]
    pil = Image.fromarray(cv2.cvtColor(art, cv2.COLOR_BGR2RGB))
    return int(str(imagehash.phash(pil)), 16)


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _phash_topk(query_phash: int, k: int = 5) -> list[tuple[int, HashRow]]:
    with _hash_lock:
        idx = list(_hash_index)
    scored = [(_hamming(query_phash, r.phash), r) for r in idx]
    scored.sort(key=lambda x: x[0])
    return scored[:k]


# ── Result formatting ───────────────────────────────────────────────────────
def _candidate(source: str, card_id: str, set_code: str, number: str,
               name: str, language: str, image_url: str,
               method: str, score: float) -> dict:
    """Shape candidates for both the legacy and v2 endpoints."""
    return {
        "source":       source,
        "card_id":      card_id,
        "set_code":     set_code,
        "set":          set_code,           # legacy alias
        "card_number":  number,
        "number":       number,             # legacy alias
        "name":         name,
        "language":     language,
        "image_url":    image_url,
        "method":       method,
        "score":        round(float(score), 4),
        "confidence":   round(float(score), 4),  # legacy alias
    }


def _scan_image(img: np.ndarray) -> dict:
    """Run the full hybrid pipeline on one frame; return ranked candidates."""
    card = _detect_card_warp(img)
    candidates: list[dict] = []
    method_used = "fallback"

    # Pass 1 — OCR card number → exact-match DB lookup.
    numbers = _ocr_card_number(card)
    if numbers:
        for row in _lookup_by_card_number(numbers):
            candidates.append(_candidate(
                source=row.get("source", ""),
                card_id=row.get("card_id", ""),
                set_code=row.get("set_name", ""),
                number=row.get("card_number", ""),
                name=row.get("name", ""),
                language=row.get("language", ""),
                image_url=row.get("image_url", ""),
                method="ocr_number",
                score=0.99,
            ))
        if candidates:
            method_used = "ocr_number"

    # Pass 2 — perceptual hash KNN. Always run; either to confirm OCR or as
    # the primary signal if OCR didn't yield anything.
    try:
        qhash = _phash_image(card)
        topk = _phash_topk(qhash, k=5)
        for dist, row in topk:
            if dist > PHASH_THRESHOLD:
                continue
            # Confidence: 1.0 at distance 0, 0.5 at threshold, 0 at 32 bits.
            conf = max(0.0, 1.0 - (dist / 32.0))
            candidates.append(_candidate(
                source=row.source, card_id=row.card_id,
                set_code=row.set_code, number=row.card_number,
                name=row.name, language=row.language, image_url=row.image_url,
                method="phash", score=conf,
            ))
        if not candidates and topk:
            method_used = "fallback"
        elif not candidates:
            method_used = "fallback"
        else:
            method_used = method_used if method_used != "fallback" else "phash"
    except Exception as exc:
        log.exception("phash pass failed: %s", exc)

    # Sort: OCR exact matches first, then by tuning-adjusted score desc.
    # _apply_tuning() multiplies the raw score by the operator-pick-derived
    # weight for this (method, source) cell — so methods that consistently
    # produce accepted picks float to the top automatically.
    for c in candidates:
        c["tuned_score"] = _apply_tuning(c)
    candidates.sort(key=lambda c: (c["method"] != "ocr_number",
                                   -c["tuned_score"]))
    # Dedupe by (source, card_id) keeping the best tuned score.
    seen: dict[tuple[str, str], dict] = {}
    for c in candidates:
        key = (c["source"], c["card_id"])
        if key not in seen or seen[key]["tuned_score"] < c["tuned_score"]:
            seen[key] = c
    final = sorted(seen.values(),
                   key=lambda c: (c["method"] != "ocr_number",
                                  -c["tuned_score"]))[:5]

    return {
        "method":     method_used,
        "ocr_tokens": numbers,
        "count":      len(final),
        "results":    final,
    }


# ── Routes ───────────────────────────────────────────────────────────────────
@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({
        "ok":              True,
        "hash_index_size": len(_hash_index),
        "hash_loaded_at":  _hash_loaded_at,
        "hash_load_error": _hash_load_error,
        "phash_threshold": PHASH_THRESHOLD,
        "ocr_langs":       OCR_LANGS,
    })


@app.route("/recognize/scan", methods=["POST"])
def recognize_scan():
    upload = request.files.get("image")
    if not upload or not upload.filename:
        return jsonify({"error": "POST multipart 'image' field"}), 400
    try:
        img = _decode_image(upload.read())
    except Exception as exc:
        return jsonify({"error": f"bad image: {exc}"}), 400
    return jsonify(_scan_image(img))


@app.route("/recognize/image", methods=["POST"])
def recognize_image():
    """Legacy-compatible endpoint — returns same shape as the old service."""
    upload = request.files.get("image")
    if not upload or not upload.filename:
        return jsonify({"error": "POST multipart 'image' field"}), 400
    try:
        img = _decode_image(upload.read())
    except Exception as exc:
        return jsonify({"error": f"bad image: {exc}"}), 400
    res = _scan_image(img)
    return jsonify({"count": res["count"], "results": res["results"]})


@app.route("/recognize/video", methods=["POST"])
def recognize_video():
    """Sample 1 frame per second up to 10 s, run image recog on each."""
    upload = request.files.get("video")
    if not upload or not upload.filename:
        return jsonify({"error": "POST multipart 'video' field"}), 400
    suffix = Path(upload.filename).suffix or ".mp4"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        upload.save(tmp.name)
        tmp_path = tmp.name
    try:
        cap = cv2.VideoCapture(tmp_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        sample_every = max(1, int(round(fps)))
        frames = []
        i = 0
        while len(frames) < 10:
            ok, frame = cap.read()
            if not ok:
                break
            if i % sample_every == 0:
                frames.append(frame)
            i += 1
        cap.release()

        all_results: list[dict] = []
        for f in frames:
            r = _scan_image(f)
            all_results.extend(r["results"])
        # Dedupe by card_id, keep best score across frames.
        seen: dict[tuple[str, str], dict] = {}
        for c in all_results:
            key = (c["source"], c["card_id"])
            if key not in seen or seen[key]["score"] < c["score"]:
                seen[key] = c
        final = sorted(seen.values(), key=lambda c: -c["score"])[:20]
        return jsonify({"count": len(final), "results": final,
                        "frames_scanned": len(frames)})
    except Exception as exc:
        log.exception("video recognize failed")
        return jsonify({"error": str(exc)}), 500
    finally:
        try: os.unlink(tmp_path)
        except OSError: pass


# ── Boot ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("[recognizer] hybrid OCR+pHash service starting on :%d", PORT)
    log.info("[recognizer] DB: %s", DB_URL.split("@")[-1])
    threading.Thread(target=_load_hash_index, name="hash-init",
                     daemon=True).start()
    threading.Thread(target=_hash_refresh_loop, name="hash-refresh",
                     daemon=True).start()
    threading.Thread(target=_load_tuning, name="tuning-init",
                     daemon=True).start()
    threading.Thread(target=_tuning_refresh_loop, name="tuning-refresh",
                     daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
