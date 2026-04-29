"""
workers/clip_embedder.py — visual-fingerprint helper.

For every card with a usable on-disk image, computes a CLIP
ViT-B/32 image embedding and stores it as a 512-dim REAL[] in
`card_image_embedding`. The recognizer service then loads all rows
for the active model_id at startup, builds an in-memory NumPy
matrix, and answers "what card is this photo?" with a cosine
similarity scan.

Why CLIP and why ViT-B/32?
  * Off-the-shelf, freely-redistributable ONNX export exists.
  * Trained on web-scale image+text pairs → robust to non-card
    backgrounds and casual phone photos (the trade-show booth
    scenario).
  * 512-dim is small enough that an in-memory float32 matrix for
    100k cards is ~200MB — fits on the Pi 5 with room to spare.
  * Inference cost on Pi 5 CPU is ~50ms/image — fast enough for
    the offline indexer; the live recognizer uses the same model
    on the operator's phone-photo at lookup time.

Storage shape (see unified.schema.DDL_CARD_IMAGE_EMBEDDING):
  PK = (set_id, card_number, model_id)
  embedding REAL[]   — L2-normalised float32, ready for cosine
  norm_before REAL   — original L2 norm (for debug / drift tracking)
  failure TEXT       — '' on success; on failure we still INSERT a
                        row so the same task isn't re-tried forever
                        without admin intervention. Re-trying after
                        installing the model / lib is one DELETE
                        + one re-seed.

Lazy imports
------------
`onnxruntime`, `numpy`, and `Pillow` are heavy and ML-stack-y, and
the rest of the worker framework must keep running on a fresh Pi
that hasn't installed them yet. We therefore:
  * never import them at module load time
  * try once per worker process; cache the outcome
  * if any of them is missing OR the model file is missing, every
    process() call records `NO_LIB` / `NO_MODEL` and exits cleanly
    so the queue drains instead of crashing in a loop

Test-friendliness
-----------------
The constructor accepts `ort_session_factory`, `np_module`, and
`image_module` injection so unit tests can supply fakes — no real
ONNX runtime / Pillow needed. See test_clip_embedder.py.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable

from .base import Worker, WorkerError

log = logging.getLogger("workers.clip_embedder")

# OpenAI CLIP ViT-B/32 preprocessing constants. These are the exact
# RGB-channel mean/std the official model was trained against; do
# not change without re-exporting the ONNX file.
CLIP_INPUT_SIZE = 224
CLIP_MEAN = (0.48145466, 0.4578275, 0.40821073)
CLIP_STD  = (0.26862954, 0.26130258, 0.27577711)
CLIP_EMBEDDING_DIM = 512


class ClipEmbedderWorker(Worker):
    TASK_TYPE = "clip_embed"
    # Inference is the bottleneck — keep batches small so a stalled
    # worker doesn't tie up too many cards. The per-task commit means
    # progress is durable even if the box loses power mid-batch.
    BATCH_SIZE = 20
    IDLE_SLEEP_S = 60.0
    # Model load + 20 inferences on Pi 5 CPU is ~3 min worst-case.
    # 30 min gives huge headroom for slow USB image reads.
    CLAIM_TIMEOUT_S = 1800

    # CLIP embeddings are stable for a given model file; re-embed
    # quarterly to catch image-file replacements (e.g. mirror
    # downloaded a sharper copy after the original was a thumbnail).
    DEFAULT_RECHECK_AFTER_S = 90 * 86400

    DEFAULT_MODEL_ID   = "clip-vit-b32-onnx-1.0"
    DEFAULT_MODEL_PATH = "/mnt/cards/models/clip-vit-b32.onnx"

    def __init__(self, conn, *,
                 model_path: str | None = None,
                 model_id: str | None = None,
                 recheck_after_s: int | None = None,
                 ort_session_factory: Callable[[str], Any] | None = None,
                 np_module: Any = None,
                 image_module: Any = None,
                 **kw):
        super().__init__(conn, **kw)
        # Env override > constructor arg > class default. Env is the
        # production knob (set in docker-compose.yml).
        self.model_path = (model_path
                           or os.environ.get("CLIP_MODEL_PATH")
                           or self.DEFAULT_MODEL_PATH)
        self.model_id = (model_id
                         or os.environ.get("CLIP_MODEL_ID")
                         or self.DEFAULT_MODEL_ID)
        self.recheck_after_s = (recheck_after_s
                                if recheck_after_s is not None
                                else self.DEFAULT_RECHECK_AFTER_S)

        # Injection hooks for tests; left None in production.
        self._injected_session_factory = ort_session_factory
        self._injected_np = np_module
        self._injected_image = image_module

        # Resolved-once caches. Tri-state:
        #   None  = not tried yet
        #   False = tried & failed (reason recorded in self._load_failure)
        #   <obj> = ready to use
        self._session: Any = None
        self._np: Any = None
        self._image: Any = None
        self._load_failure: str = ""   # 'NO_LIB' | 'NO_MODEL' | 'ORT_INIT_ERROR:...'

    # ── Lazy-load helpers ──────────────────────────────────────────

    def _ensure_libs(self) -> bool:
        """Resolve numpy + Pillow. Returns True if both available.
        Sets self._load_failure on failure."""
        if self._np is not None and self._image is not None:
            return True
        if self._load_failure:
            return False
        try:
            np_mod = self._injected_np
            if np_mod is None:
                import numpy as np_mod  # type: ignore
            img_mod = self._injected_image
            if img_mod is None:
                from PIL import Image as img_mod  # type: ignore
        except ImportError as e:
            self._load_failure = "NO_LIB"
            log.warning("[clip_embedder] numpy / Pillow missing: %s — "
                        "install with `pip install numpy Pillow`", e)
            return False
        self._np = np_mod
        self._image = img_mod
        return True

    def _ensure_session(self) -> Any:
        """Resolve the ONNX session. Returns the session or None.
        On None, self._load_failure is set."""
        if self._session is not None and self._session is not False:
            return self._session
        if self._load_failure:
            return None
        if not self._ensure_libs():
            return None

        # Model file must exist BEFORE we touch onnxruntime — this is
        # the most common operator error and the cheapest to detect.
        if not os.path.exists(self.model_path):
            self._load_failure = "NO_MODEL"
            log.warning("[clip_embedder] model file not found at %s — "
                        "set CLIP_MODEL_PATH or place clip-vit-b32.onnx there",
                        self.model_path)
            self._session = False
            return None

        try:
            if self._injected_session_factory is not None:
                self._session = self._injected_session_factory(self.model_path)
            else:
                import onnxruntime as ort  # type: ignore
                # CPUExecutionProvider only — no CUDA on Pi, and we
                # don't want silent GPU/CPU drift between dev & Pi.
                self._session = ort.InferenceSession(
                    self.model_path,
                    providers=["CPUExecutionProvider"],
                )
        except ImportError as e:
            self._load_failure = "NO_LIB"
            log.warning("[clip_embedder] onnxruntime not installed: %s", e)
            self._session = False
            return None
        except Exception as e:  # noqa: BLE001 — ORT raises many types
            self._load_failure = f"ORT_INIT_ERROR:{e}"
            log.error("[clip_embedder] failed to init session: %s", e)
            self._session = False
            return None
        return self._session

    # ── Pre-/post-processing ───────────────────────────────────────

    def _preprocess(self, img: Any) -> Any:
        """PIL Image → numpy float32 array (1, 3, 224, 224).

        Implements the OpenAI CLIP preprocessing pipeline:
          RGB convert → resize-shortest-edge → center crop →
          /255 → channel-wise mean/std normalise → HWC→CHW → batch.

        Kept as a separate method so unit tests can verify shape /
        dtype / value ranges without touching ONNX.
        """
        np = self._np
        img = img.convert("RGB")

        # Resize shortest edge to 224 (bicubic). This is the official
        # CLIP transform — preserves aspect, then center-crops.
        w, h = img.size
        if w < h:
            new_w = CLIP_INPUT_SIZE
            new_h = int(round(h * CLIP_INPUT_SIZE / w))
        else:
            new_h = CLIP_INPUT_SIZE
            new_w = int(round(w * CLIP_INPUT_SIZE / h))
        # PIL.Image.BICUBIC = 3; use the integer to avoid attr lookup
        # on stub modules in tests.
        try:
            resample = self._image.BICUBIC  # type: ignore[attr-defined]
        except AttributeError:
            resample = 3
        img = img.resize((new_w, new_h), resample)

        # Center crop to CLIP_INPUT_SIZE × CLIP_INPUT_SIZE.
        left = (new_w - CLIP_INPUT_SIZE) // 2
        top  = (new_h - CLIP_INPUT_SIZE) // 2
        img = img.crop((left, top,
                        left + CLIP_INPUT_SIZE,
                        top  + CLIP_INPUT_SIZE))

        arr = np.asarray(img, dtype=np.float32) / 255.0   # (H, W, 3)
        mean = np.array(CLIP_MEAN, dtype=np.float32).reshape(1, 1, 3)
        std  = np.array(CLIP_STD,  dtype=np.float32).reshape(1, 1, 3)
        arr = (arr - mean) / std
        # HWC → CHW, then add batch dim.
        arr = np.transpose(arr, (2, 0, 1))
        arr = np.expand_dims(arr, axis=0)
        return arr.astype(np.float32)

    def _embed(self, arr: Any) -> tuple[list, float]:
        """Run inference and return (l2-normalised vector as Python
        list of floats, original L2 norm).

        Returning a Python list (not a numpy array) means the caller
        can pass it straight to psycopg2 — REAL[] adapter handles
        plain lists. Avoids a numpy dependency at the SQL layer.
        """
        session = self._ensure_session()
        if session is None:
            raise WorkerError(self._load_failure or "NO_MODEL")
        np = self._np

        input_name = session.get_inputs()[0].name
        outputs = session.run(None, {input_name: arr})
        # CLIP image encoder output: (batch, 512). We always batch=1.
        vec = outputs[0]
        if hasattr(vec, "shape") and len(vec.shape) == 2:
            vec = vec[0]
        vec = np.asarray(vec, dtype=np.float32).reshape(-1)

        norm = float(np.linalg.norm(vec))
        if norm == 0.0:
            # Degenerate — treat as failure rather than store a
            # divide-by-zero. Real CLIP outputs never zero out.
            raise WorkerError("ZERO_EMBEDDING")
        normalised = (vec / norm).astype(np.float32)
        return normalised.tolist(), norm

    # ── Image-file selection ───────────────────────────────────────

    @staticmethod
    def _pick_image_path(paths_meta: list) -> tuple[str, str]:
        """Walk image_url_alt entries and return (path, src_tag) of
        the first entry whose `local` exists on disk. ('', '') if
        none. Same selection logic as image_health.OK detection so
        the embedder picks the same image the health-check verified.
        """
        for entry in paths_meta or []:
            if not isinstance(entry, dict):
                continue
            local = (entry.get("local") or "").strip()
            if not local:
                continue
            if os.path.exists(local) and os.path.getsize(local) > 0:
                src = (entry.get("src") or "").strip()
                lang = (entry.get("lang") or "").strip()
                tag = f"{src}|{lang}" if (src or lang) else ""
                return local, tag
        return "", ""

    # ── Worker contract ────────────────────────────────────────────

    def seed(self) -> int:
        """Enqueue every card whose latest image_health check is OK
        or PARTIAL, AND that doesn't already have an embedding for
        this model_id newer than `recheck_after_s`.

        We deliberately filter on image_health rather than re-doing
        the disk walk here — image_health is the single source of
        truth for "this card has a usable image", and it gets
        re-run on its own cadence.
        """
        cutoff = int(time.time()) - self.recheck_after_s
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO bg_task_queue
                (task_type, task_key, payload, status, created_at)
            SELECT 'clip_embed',
                   %s || ':' || h.set_id || '/' || h.card_number,
                   jsonb_build_object('set_id',      h.set_id,
                                      'card_number', h.card_number,
                                      'model_id',    %s),
                   'PENDING',
                   %s
              FROM image_health_check h
             WHERE h.status IN ('OK','PARTIAL')
               -- One row per (set,num) — keep the most recent check
               -- only. Otherwise we'd enqueue every historical row.
               AND h.checked_at = (
                   SELECT MAX(h2.checked_at)
                     FROM image_health_check h2
                    WHERE h2.set_id      = h.set_id
                      AND h2.card_number = h.card_number
               )
               AND NOT EXISTS (
                   SELECT 1 FROM card_image_embedding e
                    WHERE e.set_id      = h.set_id
                      AND e.card_number = h.card_number
                      AND e.model_id    = %s
                      AND e.failure     = ''
                      AND e.created_at  > %s
               )
            ON CONFLICT (task_type, task_key) DO NOTHING
        """, (self.model_id, self.model_id,
              int(time.time()),
              self.model_id, cutoff))
        n = cur.rowcount or 0
        self.conn.commit()
        log.info("[clip_embedder] seed enqueued %d task(s) for model_id=%s",
                 n, self.model_id)
        return n

    def process(self, task: dict) -> dict:
        payload = task.get("payload") or {}
        sid = (payload.get("set_id")     or "").strip()
        num = (payload.get("card_number") or "").strip()
        # payload model_id wins over instance model_id — lets a future
        # admin enqueue a one-off re-embed under a different model.
        model_id = (payload.get("model_id") or self.model_id).strip()
        if not sid or not num:
            raise WorkerError(
                f"clip_embed task {task['task_id']} missing "
                f"set_id/card_number in payload: {payload!r}"
            )

        cur = self.conn.cursor()
        cur.execute("""
            SELECT image_url_alt
              FROM cards_master
             WHERE set_id = %s AND card_number = %s
        """, (sid, num))
        row = cur.fetchone()
        if row is None:
            self._record_failure(sid, num, model_id, "", "", "MISSING_CARD")
            return {"status": "MISSING_CARD"}

        raw = row[0] if not isinstance(row, dict) else row.get("image_url_alt")
        if isinstance(raw, str):
            try:
                paths_meta = json.loads(raw)
            except Exception:
                paths_meta = []
        else:
            paths_meta = raw or []

        image_path, image_src = self._pick_image_path(paths_meta)
        if not image_path:
            self._record_failure(sid, num, model_id, "", "", "NO_IMAGE")
            return {"status": "NO_IMAGE"}

        # Library / model availability is recorded as a stored failure
        # rather than a raised error so we don't churn the queue.
        if self._ensure_session() is None:
            self._record_failure(sid, num, model_id,
                                 image_path, image_src,
                                 self._load_failure or "NO_MODEL")
            return {"status": self._load_failure or "NO_MODEL"}

        try:
            with open(image_path, "rb") as f:
                img_bytes = f.read()
            img = self._image.open(_BytesIOWrap(img_bytes))
            arr = self._preprocess(img)
        except Exception as e:  # noqa: BLE001 — many decode failure modes
            self._record_failure(sid, num, model_id,
                                 image_path, image_src,
                                 f"BAD_IMAGE:{type(e).__name__}")
            return {"status": "BAD_IMAGE"}

        try:
            embedding, norm_before = self._embed(arr)
        except WorkerError:
            raise   # ZERO_EMBEDDING / NO_MODEL — surface to fail()
        except Exception as e:  # noqa: BLE001 — ORT raises many types
            self._record_failure(sid, num, model_id,
                                 image_path, image_src,
                                 f"ORT_ERROR:{type(e).__name__}:{e}")
            return {"status": "ORT_ERROR"}

        self._record_success(sid, num, model_id,
                             image_path, image_src,
                             embedding, norm_before)
        return {"status": "OK", "dim": len(embedding),
                "norm_before": norm_before}

    # ── DB helpers ─────────────────────────────────────────────────

    def _record_success(self, sid: str, num: str, model_id: str,
                        image_path: str, image_src: str,
                        embedding: list, norm_before: float) -> None:
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO card_image_embedding
                (set_id, card_number, image_path, image_src,
                 model_id, embedding, embedding_dim,
                 norm_before, failure, created_at)
            VALUES (%s, %s, %s, %s,
                    %s, %s::REAL[], %s,
                    %s, '', %s)
            ON CONFLICT (set_id, card_number, model_id)
            DO UPDATE SET image_path    = EXCLUDED.image_path,
                          image_src     = EXCLUDED.image_src,
                          embedding     = EXCLUDED.embedding,
                          embedding_dim = EXCLUDED.embedding_dim,
                          norm_before   = EXCLUDED.norm_before,
                          failure       = '',
                          created_at    = EXCLUDED.created_at
        """, (
            sid, num, image_path, image_src,
            model_id, embedding, len(embedding),
            float(norm_before), int(time.time()),
        ))
        self.conn.commit()

    def _record_failure(self, sid: str, num: str, model_id: str,
                        image_path: str, image_src: str,
                        failure: str) -> None:
        """Record a failure row so the same task isn't re-tried
        forever. Operator can re-trigger by:
            DELETE FROM card_image_embedding
             WHERE failure <> '' AND model_id = '...';
        then re-running --seed.
        """
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO card_image_embedding
                (set_id, card_number, image_path, image_src,
                 model_id, embedding, embedding_dim,
                 norm_before, failure, created_at)
            VALUES (%s, %s, %s, %s,
                    %s, '{}'::REAL[], 0,
                    0, %s, %s)
            ON CONFLICT (set_id, card_number, model_id)
            DO UPDATE SET image_path  = EXCLUDED.image_path,
                          image_src   = EXCLUDED.image_src,
                          failure     = EXCLUDED.failure,
                          created_at  = EXCLUDED.created_at
        """, (
            sid, num, image_path, image_src,
            model_id, failure, int(time.time()),
        ))
        self.conn.commit()


# Tiny shim: PIL.Image.open accepts file-like objects, and unit
# tests that stub `self._image.open` need the same call shape.
# Importing `io` lazily here keeps the module import-time cheap.
def _BytesIOWrap(b: bytes):
    import io
    return io.BytesIO(b)
