"""
workers/ocr_indexer.py — text-on-card extraction helper.

For every card with a usable on-disk image, runs PaddleOCR in the
language hint that best matches the card and stores the recognised
text in `card_ocr` (one row per (set, num, lang_hint, model_id)).

Why per-language rows instead of merging?
  Pokémon TCG cards routinely have text in MORE than one language
  on the same physical card — a Korean print still has the
  Pokémon's English name in small print on the bottom border, and
  many Japanese promos have an English overlay sticker. By keeping
  one row per (card, lang_hint), the recognizer can choose which
  pass to query against (operator scans a Korean card → match
  against lang_hint='kr' first; falls back to 'en' if nothing).

Why PaddleOCR?
  * PP-OCRv4 has the best CJK accuracy in the open-source space
    (Tesseract is fine for English but weak on Korean and JP).
  * Models are downloadable per-language (the Pi only needs to
    cache the languages it processes).
  * Pure-Python wrapper — no proprietary native lib like
    PaddlePaddle's full server install.

Lang-hint priority (KR-specialist tuning)
-----------------------------------------
The seed step picks ONE primary lang per card based on which
name_* column is populated, ranked KR > JP > CHS > EN. This
matches the inventory profile (mostly Korean cards) so the OCR
budget is spent where it has the most search-value.

Operator can re-run with `--lang jp` etc. to back-fill a second
language pass for cards that need it (foil JPs with English
overlays, dual-language promos).

Lazy imports
------------
PaddleOCR has a heavy dependency chain (numpy, opencv, shapely,
pyclipper, …). Lazy-load and fail soft (`NO_LIB` recorded as a
failure row) so a fresh Pi without the ML stack still drains the
queue cleanly. Tests inject a fake `paddle_factory(lang) → ocr`
to avoid pulling the real lib.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable

from .base import Worker, WorkerError

log = logging.getLogger("workers.ocr_indexer")

# cards_master language suffix / CLI --ocr-lang-hint → PaddleOCR
# `lang=` argument.
#
# Why both `chs` and `zh-sim`?
#   `chs` was the original key, picked up by `pick_primary_lang` from
#   the existing `cards_master.name_chs` column. After ZH-2 introduced
#   a TC vs SC split (`/mnt/cards/zh/zh-tc/` vs `/zh-sc/`) the operator
#   needs an explicit way to OCR each variant differently — Traditional
#   needs the `chinese_cht` PP-OCRv4 pack; Simplified is fine on `ch`.
#   `zh-sim` is therefore a deliberate alias of `chs` (same Paddle
#   model, same `~/.paddleocr` cache dir under PaddleOCR's hood) so an
#   operator can run `--ocr-lang-hint zh-sim` against the SC mirror
#   without touching name_chs-tagged legacy KR-pack rows. `chs` stays
#   as the auto-pick for backward compat.
PADDLE_LANG_MAP: dict[str, str] = {
    "kr":     "korean",
    "jp":     "japan",
    "chs":    "ch",            # legacy alias; auto-picked from name_chs
    "zh-sim": "ch",             # explicit Simplified, same model as chs
    "zh-cht": "chinese_cht",    # Traditional — separate Paddle pack
                                #   (rec model is PP-OCRv3, not v4 —
                                #   Paddle never shipped v4 for chinese_cht;
                                #   see scripts/setup-ocr-models.sh comment)
    "en":     "en",
}

# Tie-break order for picking a card's primary OCR language.
# Korean first because the inventory is KR-specialist.
LANG_PRIORITY: tuple[str, ...] = ("kr", "jp", "chs", "en")


class OcrIndexerWorker(Worker):
    TASK_TYPE = "ocr_index"
    # OCR is heavier than CLIP — 200-500ms/card on Pi 5 CPU. Smaller
    # batches keep the per-pass commit cadence sensible.
    BATCH_SIZE = 10
    IDLE_SLEEP_S = 60.0
    # Model load is slow on first language; 30 min is generous.
    CLAIM_TIMEOUT_S = 1800

    DEFAULT_RECHECK_AFTER_S = 90 * 86400

    DEFAULT_MODEL_ID = "paddleocr-ppocrv4-1.0"

    # Default location for PaddleOCR per-language model files.
    # Overridable via the `models_dir` ctor arg or the OCR_MODELS_DIR
    # env var. Lives on the USB drive next to the CLIP model so a
    # fresh Pi can be brought up by plugging in the drive — no need
    # to re-download 50-100MB per language.
    DEFAULT_MODELS_DIR = "/mnt/cards/models/paddleocr"

    def __init__(self, conn, *,
                 model_id: str | None = None,
                 lang_hint: str | None = None,
                 recheck_after_s: int | None = None,
                 models_dir: str | None = None,
                 paddle_factory: Callable[[str], Any] | None = None,
                 **kw):
        super().__init__(conn, **kw)
        self.model_id = (model_id
                         or os.environ.get("OCR_MODEL_ID")
                         or self.DEFAULT_MODEL_ID)
        # If lang_hint is set on the worker, every seeded task and
        # every processed task uses it (instead of per-card pick).
        # Useful for "do a JP-OCR pass on the whole catalogue".
        self.lang_hint = lang_hint.strip() if lang_hint else None
        if self.lang_hint and self.lang_hint not in PADDLE_LANG_MAP:
            raise ValueError(
                f"Unknown lang_hint {self.lang_hint!r}; "
                f"expected one of {sorted(PADDLE_LANG_MAP)}"
            )
        self.recheck_after_s = (recheck_after_s
                                if recheck_after_s is not None
                                else self.DEFAULT_RECHECK_AFTER_S)

        # models_dir resolution order: explicit ctor arg → env var →
        # hard-coded /mnt/cards default. Empty string is treated as
        # "use PaddleOCR's own ~/.paddleocr default" — gives the
        # operator an escape hatch if the drive is unavailable and
        # they want to fall back to in-container caching.
        if models_dir is not None:
            self.models_dir = models_dir
        else:
            self.models_dir = (os.environ.get("OCR_MODELS_DIR")
                               or self.DEFAULT_MODELS_DIR)

        self._injected_paddle_factory = paddle_factory
        self._ocr_cache: dict[str, Any] = {}
        # Tri-state similar to clip_embedder: '' = not tried, 'NO_LIB'
        # = failed, anything else = an error string.
        self._load_failure: str = ""
        self._paddle_loaded: bool = False

    # ── Lazy-load helpers ──────────────────────────────────────────

    def _ensure_paddle(self) -> Callable[[str], Any] | None:
        """Returns a callable `factory(lang) -> ocr_instance` or
        None if PaddleOCR can't be loaded."""
        if self._injected_paddle_factory is not None:
            return self._injected_paddle_factory
        if self._paddle_loaded:
            return self._real_paddle_factory  # type: ignore[attr-defined]
        if self._load_failure:
            return None
        try:
            from paddleocr import PaddleOCR  # type: ignore
        except ImportError as e:
            self._load_failure = "NO_LIB"
            log.warning("[ocr_indexer] paddleocr not installed: %s — "
                        "install with `pip install paddleocr paddlepaddle`",
                        e)
            return None
        # Keep a bound reference so we can build per-lang sessions on
        # demand. The PaddleOCR constructor downloads the model the
        # first time it's called per (lang, model_version).
        models_dir = self.models_dir
        def _factory(lang: str) -> Any:
            # show_log=False keeps the Pi console clean; cls=False is
            # set later at .ocr() call time (no angle classifier).
            kwargs: dict[str, Any] = {
                "use_angle_cls": False,
                "lang": lang,
                "show_log": False,
            }
            # Point PaddleOCR at /mnt/cards/models/paddleocr/<lang>/
            # so the per-language detection (det) and recognition
            # (rec) model files land on the USB drive instead of in
            # the container's ~/.paddleocr (which gets wiped on every
            # `docker compose build` and lives on the SD card). If
            # the dirs are empty PaddleOCR will download into them
            # on first use; subsequent boots load instantly from the
            # drive.
            if models_dir:
                lang_dir = os.path.join(models_dir, lang)
                kwargs["det_model_dir"] = os.path.join(lang_dir, "det")
                kwargs["rec_model_dir"] = os.path.join(lang_dir, "rec")
            return PaddleOCR(**kwargs)
        self._real_paddle_factory = _factory  # type: ignore[attr-defined]
        self._paddle_loaded = True
        return _factory

    def _get_ocr(self, paddle_lang: str) -> Any:
        """Return a cached PaddleOCR instance for the given language,
        constructing it on first use. Returns None on lib failure."""
        if paddle_lang in self._ocr_cache:
            return self._ocr_cache[paddle_lang]
        factory = self._ensure_paddle()
        if factory is None:
            return None
        try:
            ocr = factory(paddle_lang)
        except Exception as e:  # noqa: BLE001 — factory may raise many
            self._load_failure = f"FACTORY_ERROR:{type(e).__name__}:{e}"
            log.error("[ocr_indexer] PaddleOCR(lang=%s) construction "
                      "failed: %s", paddle_lang, e)
            return None
        self._ocr_cache[paddle_lang] = ocr
        return ocr

    # ── Card → primary lang resolution ─────────────────────────────

    @staticmethod
    def pick_primary_lang(name_kr: str, name_jp: str,
                          name_chs: str, name_en: str) -> str:
        """Pick the lang_hint to OCR a card under, KR-first.

        Returns one of the keys in PADDLE_LANG_MAP. Falls back to
        'en' if every name field is empty (cards with no localised
        names are extremely rare — usually data-import bugs — but
        we still want them OCR'd).
        """
        names = {"kr": name_kr or "",
                 "jp": name_jp or "",
                 "chs": name_chs or "",
                 "en": name_en or ""}
        for lang in LANG_PRIORITY:
            if names[lang].strip():
                return lang
        return "en"

    # ── Image-file selection ───────────────────────────────────────

    @staticmethod
    def _pick_image_path(paths_meta: list) -> str:
        """Same selection rule as clip_embedder: first existing,
        non-empty `local` path. We don't care about src_tag here
        because OCR results are about the card text, not which
        scan was used — though we do persist image_path for debug."""
        for entry in paths_meta or []:
            if not isinstance(entry, dict):
                continue
            local = (entry.get("local") or "").strip()
            if not local:
                continue
            if os.path.exists(local) and os.path.getsize(local) > 0:
                return local
        return ""

    # ── Result parsing ─────────────────────────────────────────────

    @staticmethod
    def _parse_ocr_result(raw: Any) -> tuple[list[dict], str, float]:
        """PaddleOCR returns
            [[ [box, (text, conf)], [box, (text, conf)], ... ]]
        for a single image. `box` is 4×(x,y).

        We flatten to:
            lines = [{text, conf, bbox:[x1,y1,x2,y2,x3,y3,x4,y4]}, ...]
            full_text = "\\n".join(line texts)
            avg_conf = mean of confidences (0 if no lines)

        Defensive against PaddleOCR version drift — different point
        releases have returned slightly different shapes (sometimes
        outer list omitted, sometimes (text, conf) is a list).
        """
        if not raw:
            return [], "", 0.0

        # Handle the "outer list of pages" wrapper that PaddleOCR
        # adds for multi-page input. We always pass single images.
        page = raw[0] if (isinstance(raw, list)
                          and raw
                          and isinstance(raw[0], list)
                          and (not raw[0]
                               or isinstance(raw[0][0], (list, tuple)))) \
                       else raw
        if page is None:
            return [], "", 0.0

        lines: list[dict] = []
        confs: list[float] = []
        for entry in page:
            if not entry or len(entry) < 2:
                continue
            box = entry[0]
            text_conf = entry[1]
            if isinstance(text_conf, (list, tuple)) and len(text_conf) >= 2:
                text = str(text_conf[0])
                try:
                    conf = float(text_conf[1])
                except (TypeError, ValueError):
                    conf = 0.0
            else:
                text = str(text_conf)
                conf = 0.0
            # Flatten the box: [[x1,y1],[x2,y2],...] → [x1,y1,...]
            flat_box: list[float] = []
            try:
                for pt in box:
                    flat_box.append(float(pt[0]))
                    flat_box.append(float(pt[1]))
            except (TypeError, ValueError, IndexError):
                flat_box = []
            lines.append({"text": text, "conf": conf, "bbox": flat_box})
            confs.append(conf)

        full_text = "\n".join(ln["text"] for ln in lines)
        avg_conf = (sum(confs) / len(confs)) if confs else 0.0
        return lines, full_text, avg_conf

    # ── Worker contract ────────────────────────────────────────────

    def seed(self) -> int:
        """Enqueue every card whose latest image_health is OK/PARTIAL
        AND that lacks a recent OCR pass for the resolved lang_hint.

        If the worker has a fixed self.lang_hint, that lang is used
        for every card. Otherwise we compute the primary lang from
        cards_master.name_* in SQL using the same KR>JP>CHS>EN rule
        as pick_primary_lang() — keeps Python and SQL aligned.
        """
        cutoff = int(time.time()) - self.recheck_after_s
        cur = self.conn.cursor()

        # SQL CASE mirrors pick_primary_lang(); kept as one expression
        # so the seed remains a single INSERT...SELECT (one round-trip,
        # no per-card chatter).
        if self.lang_hint:
            lang_expr = "%s"
            lang_params: list[Any] = [self.lang_hint]
        else:
            lang_expr = """CASE
                WHEN COALESCE(c.name_kr,'')  <> '' THEN 'kr'
                WHEN COALESCE(c.name_jp,'')  <> '' THEN 'jp'
                WHEN COALESCE(c.name_chs,'') <> '' THEN 'chs'
                ELSE 'en'
            END"""
            lang_params = []

        sql = f"""
            INSERT INTO bg_task_queue
                (task_type, task_key, payload, status, created_at)
            SELECT 'ocr_index',
                   %s || ':' || {lang_expr} || ':'
                       || h.set_id || '/' || h.card_number,
                   jsonb_build_object('set_id',      h.set_id,
                                      'card_number', h.card_number,
                                      'lang_hint',   {lang_expr},
                                      'model_id',    %s),
                   'PENDING',
                   %s
              FROM image_health_check h
              JOIN cards_master c
                ON c.set_id = h.set_id AND c.card_number = h.card_number
             WHERE h.status IN ('OK','PARTIAL')
               AND h.checked_at = (
                   SELECT MAX(h2.checked_at)
                     FROM image_health_check h2
                    WHERE h2.set_id      = h.set_id
                      AND h2.card_number = h.card_number
               )
               AND NOT EXISTS (
                   SELECT 1 FROM card_ocr o
                    WHERE o.set_id      = h.set_id
                      AND o.card_number = h.card_number
                      AND o.lang_hint   = {lang_expr}
                      AND o.model_id    = %s
                      AND o.failure     = ''
                      AND o.created_at  > %s
               )
            ON CONFLICT (task_type, task_key) DO NOTHING
        """
        # Param order: model_id (for task_key prefix), [lang_expr1],
        # [lang_expr2 in payload], model_id (payload),
        # created_at (now), [lang_expr3 in NOT EXISTS], model_id (filter),
        # cutoff.
        params: list[Any] = [self.model_id]
        params.extend(lang_params)            # task_key lang
        params.extend(lang_params)            # payload lang
        params.append(self.model_id)          # payload model_id
        params.append(int(time.time()))       # created_at
        params.extend(lang_params)            # NOT EXISTS lang
        params.append(self.model_id)          # NOT EXISTS model_id
        params.append(cutoff)                 # NOT EXISTS cutoff

        cur.execute(sql, params)
        n = cur.rowcount or 0
        self.conn.commit()
        log.info("[ocr_indexer] seed enqueued %d task(s) for "
                 "model_id=%s lang_hint=%s",
                 n, self.model_id, self.lang_hint or "<auto>")
        return n

    def process(self, task: dict) -> dict:
        payload = task.get("payload") or {}
        sid = (payload.get("set_id")     or "").strip()
        num = (payload.get("card_number") or "").strip()
        # Worker-instance lang_hint (e.g. CLI --lang jp) takes
        # precedence over the payload — lets an operator rerun an
        # already-seeded queue under a different lang.
        lang_hint = (self.lang_hint
                     or (payload.get("lang_hint") or "").strip()
                     or "")
        model_id = (payload.get("model_id") or self.model_id).strip()
        if not sid or not num:
            raise WorkerError(
                f"ocr_index task {task['task_id']} missing "
                f"set_id/card_number in payload: {payload!r}"
            )

        cur = self.conn.cursor()
        cur.execute("""
            SELECT image_url_alt, name_kr, name_jp, name_chs, name_en
              FROM cards_master
             WHERE set_id = %s AND card_number = %s
        """, (sid, num))
        row = cur.fetchone()
        if row is None:
            self._record_failure(sid, num, lang_hint or "en", model_id,
                                 "", "MISSING_CARD")
            return {"status": "MISSING_CARD"}

        raw_img = row[0]
        name_kr, name_jp, name_chs, name_en = row[1], row[2], row[3], row[4]

        # Resolve lang_hint if it wasn't already set.
        if not lang_hint:
            lang_hint = self.pick_primary_lang(name_kr or "", name_jp or "",
                                               name_chs or "", name_en or "")
        paddle_lang = PADDLE_LANG_MAP.get(lang_hint)
        if paddle_lang is None:
            raise WorkerError(f"unknown lang_hint in task: {lang_hint!r}")

        if isinstance(raw_img, str):
            try:
                paths_meta = json.loads(raw_img)
            except Exception:
                paths_meta = []
        else:
            paths_meta = raw_img or []

        image_path = self._pick_image_path(paths_meta)
        if not image_path:
            self._record_failure(sid, num, lang_hint, model_id,
                                 "", "NO_IMAGE")
            return {"status": "NO_IMAGE"}

        ocr = self._get_ocr(paddle_lang)
        if ocr is None:
            self._record_failure(sid, num, lang_hint, model_id,
                                 image_path,
                                 self._load_failure or "NO_LIB")
            return {"status": self._load_failure or "NO_LIB"}

        try:
            # cls=False: skip the angle classifier (cards are upright).
            raw_result = ocr.ocr(image_path, cls=False)
        except Exception as e:  # noqa: BLE001 — paddle raises many
            self._record_failure(sid, num, lang_hint, model_id,
                                 image_path,
                                 f"OCR_ERROR:{type(e).__name__}:{e}")
            return {"status": "OCR_ERROR"}

        lines, full_text, avg_conf = self._parse_ocr_result(raw_result)
        self._record_success(sid, num, lang_hint, model_id,
                             image_path, lines, full_text, avg_conf)
        return {"status": "OK", "line_count": len(lines),
                "avg_conf": avg_conf, "chars": len(full_text)}

    # ── DB helpers ─────────────────────────────────────────────────

    def _record_success(self, sid: str, num: str, lang_hint: str,
                        model_id: str, image_path: str,
                        lines: list[dict], full_text: str,
                        avg_conf: float) -> None:
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO card_ocr
                (set_id, card_number, lang_hint, model_id,
                 image_path, full_text, lines, line_count,
                 avg_conf, failure, created_at)
            VALUES (%s, %s, %s, %s,
                    %s, %s, %s::jsonb, %s,
                    %s, '', %s)
            ON CONFLICT (set_id, card_number, lang_hint, model_id)
            DO UPDATE SET image_path = EXCLUDED.image_path,
                          full_text  = EXCLUDED.full_text,
                          lines      = EXCLUDED.lines,
                          line_count = EXCLUDED.line_count,
                          avg_conf   = EXCLUDED.avg_conf,
                          failure    = '',
                          created_at = EXCLUDED.created_at
        """, (
            sid, num, lang_hint, model_id,
            image_path, full_text,
            json.dumps(lines, ensure_ascii=False),
            len(lines), float(avg_conf), int(time.time()),
        ))
        self.conn.commit()

    def _record_failure(self, sid: str, num: str, lang_hint: str,
                        model_id: str, image_path: str,
                        failure: str) -> None:
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO card_ocr
                (set_id, card_number, lang_hint, model_id,
                 image_path, full_text, lines, line_count,
                 avg_conf, failure, created_at)
            VALUES (%s, %s, %s, %s,
                    %s, '', '[]'::jsonb, 0,
                    0, %s, %s)
            ON CONFLICT (set_id, card_number, lang_hint, model_id)
            DO UPDATE SET image_path = EXCLUDED.image_path,
                          failure    = EXCLUDED.failure,
                          created_at = EXCLUDED.created_at
        """, (
            sid, num, lang_hint, model_id,
            image_path, failure, int(time.time()),
        ))
        self.conn.commit()
