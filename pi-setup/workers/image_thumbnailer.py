"""
workers/image_thumbnailer.py — tiered thumbnail generator.

The kiosk renders card art at three sizes:
  *  200 px — search results, inventory tables, sales-history rows
  *  800 px — card detail panel, comparison view
  * full   — the original mirrored image (only when zoomed)

Serving the full 2000-3000 px source image for a results grid is
~50× the bytes the screen actually needs. This worker pre-renders
two WEBP thumbnails per card image so the kiosk can switch to a
size-appropriate URL.

  /mnt/cards/thumbs/200/<set_id>/<card_number>.webp
  /mnt/cards/thumbs/800/<set_id>/<card_number>.webp

Idempotent via mtime: re-running is cheap. A thumb whose mtime is
newer than the source is left alone; a thumb older than the source
(or missing) is regenerated.

Designed to enqueue itself only after image_health has marked a
card OK — generating a thumbnail from a corrupt source just
materialises the corruption at a smaller size. Slice 17 already
keeps image_health → image_mirror in sync; this worker rides on
top of the same OK signal.

Lazy Pillow: when PIL isn't installed the worker exits cleanly with
NO_LIB so a barebones consolidator container that hasn't yet picked
up the image-processing extra still boots.
"""
from __future__ import annotations

import io
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from .base import Worker, WorkerError

log = logging.getLogger("workers.image_thumbnailer")

# Two tiers cover the kiosk's actual render sizes — a 200 px thumb
# for grid views and an 800 px thumb for detail panels. Anything
# larger than 800 px and the kiosk just uses the full source. WEBP
# quality 82 is the sweet spot for card art (no banding on holos,
# ~6× smaller than the source PNG/JPEG).
DEFAULT_SIZES: tuple[int, ...] = (200, 800)
DEFAULT_WEBP_QUALITY: int = 82

# Skip sources whose stat is below this — a 1-byte CDN error stub
# would otherwise materialise as a 1-byte WEBP and confuse the UI.
MIN_SOURCE_BYTES: int = 64

# /mnt/cards is the convention shared with sync_card_mirror.
DEFAULT_THUMB_ROOT = Path(os.environ.get(
    "MIRROR_ROOT", "/mnt/cards")) / "thumbs"


def _thumb_path(thumb_root: Path, size: int, sid: str, num: str) -> Path:
    """<thumb_root>/<size>/<set_id>/<card_number>.webp.

    The set_id and card_number are filesystem-safe in our domain
    (alnum + '-'), so no escaping is needed."""
    return thumb_root / str(size) / sid / f"{num}.webp"


def _is_thumb_fresh(thumb_path: Path, source_mtime: float) -> bool:
    """A thumb is fresh iff it exists AND its mtime is >= the
    source's mtime. Equality is treated as fresh because some
    filesystems (notably FAT32 on USB) only have 2-second mtime
    resolution and a same-second regenerate would be wasted I/O."""
    try:
        st = thumb_path.stat()
    except FileNotFoundError:
        return False
    except OSError:
        # Permission / weird FS error — be conservative and regenerate.
        return False
    return st.st_mtime >= source_mtime


class ImageThumbnailerWorker(Worker):
    TASK_TYPE = "image_thumbnail"
    BATCH_SIZE = 50
    IDLE_SLEEP_S = 60.0
    # Pillow on a Pi 5 chews through ~4-8 thumbs/second; 5-min
    # ceiling gives huge headroom even for the worst USB latency.
    CLAIM_TIMEOUT_S = 300

    def __init__(
        self,
        conn,
        *,
        thumb_root: Optional[Path] = None,
        sizes: tuple[int, ...] = DEFAULT_SIZES,
        webp_quality: int = DEFAULT_WEBP_QUALITY,
        min_source_bytes: int = MIN_SOURCE_BYTES,
        pil_module: Any = None,
        **kw,
    ):
        super().__init__(conn, **kw)
        self._thumb_root = Path(thumb_root) if thumb_root is not None \
            else DEFAULT_THUMB_ROOT
        self._sizes = tuple(sizes)
        self._webp_quality = webp_quality
        self._min_source_bytes = min_source_bytes
        # Same three-mode contract as image_mirror:
        #   None  → lazy auto-detect on first use
        #   False → explicitly disabled (NO_LIB short-circuit, useful
        #           for tests AND for environments where Pillow can't
        #           be installed yet)
        #   <obj> → use it (real PIL or a fake)
        self._pil_module = None if pil_module is False else pil_module
        self._pil_disabled = pil_module is False
        self._pil_tried = pil_module is not None

    def _ensure_pil(self) -> Any:
        if self._pil_disabled:
            return None
        if self._pil_tried:
            return self._pil_module
        self._pil_tried = True
        try:
            from PIL import Image  # type: ignore
            self._pil_module = Image
        except ImportError:
            log.warning("[thumbnailer] Pillow not installed — worker is "
                        "a no-op until you `pip install Pillow` in the "
                        "consolidator container.")
            self._pil_module = None
        return self._pil_module

    # ── Worker contract ──────────────────────────────────────────

    def seed(self) -> int:
        """Enqueue every card that has at least one local path AND a
        recent OK image_health_check verdict. ON CONFLICT collapses
        re-seed runs to a no-op while a previous task is still
        PENDING / RUNNING.

        We deliberately scope on the LATEST image_health_check row
        per card via DISTINCT ON — we don't want to enqueue a card
        whose latest verdict is CORRUPT just because an older row
        was OK."""
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO bg_task_queue
                (task_type, task_key, payload, status, created_at)
            SELECT 'image_thumbnail',
                   c.set_id || '/' || c.card_number,
                   jsonb_build_object('set_id',      c.set_id,
                                      'card_number', c.card_number),
                   'PENDING',
                   %s
              FROM cards_master c
              JOIN LATERAL (
                       SELECT status
                         FROM image_health_check h
                        WHERE h.set_id      = c.set_id
                          AND h.card_number = c.card_number
                        ORDER BY h.checked_at DESC
                        LIMIT 1
                   ) latest ON TRUE
             WHERE latest.status = 'OK'
               AND EXISTS (
                       SELECT 1
                         FROM jsonb_array_elements(c.image_url_alt) p
                        WHERE COALESCE(p->>'local','') <> ''
                   )
            ON CONFLICT (task_type, task_key) DO NOTHING
        """, (int(time.time()),))
        n = cur.rowcount or 0
        self.conn.commit()
        log.info("[thumbnailer] seed enqueued %d task(s)", n)
        return n

    def process(self, task: dict) -> dict:
        payload = task.get("payload") or {}
        sid = (payload.get("set_id")     or "").strip()
        num = (payload.get("card_number") or "").strip()
        if not sid or not num:
            raise WorkerError(
                f"image_thumbnail task {task.get('task_id')} missing "
                f"set_id/card_number in payload: {payload!r}"
            )

        pil = self._ensure_pil()
        if pil is None:
            # NO_LIB is not a failure — we just can't help. Returning
            # cleanly lets the queue mark the task COMPLETED so we
            # don't retry pointlessly until Pillow shows up.
            return {"status": "NO_LIB"}

        cur = self.conn.cursor()
        cur.execute("""
            SELECT image_url_alt
              FROM cards_master
             WHERE set_id = %s AND card_number = %s
        """, (sid, num))
        row = cur.fetchone()
        if row is None:
            return {"status": "MISSING_CARD"}

        raw = row[0] if not isinstance(row, dict) else row.get("image_url_alt")
        if isinstance(raw, str):
            try:
                paths_meta = json.loads(raw)
            except Exception:
                paths_meta = []
        else:
            paths_meta = raw or []

        # Pick the FIRST usable local source. We could thumb every
        # local copy, but the kiosk only ever serves one — picking
        # the first stable source matches what the resolver does.
        source: Optional[Path] = None
        for entry in paths_meta:
            if not isinstance(entry, dict):
                continue
            local = (entry.get("local") or "").strip()
            if not local:
                continue
            p = Path(local)
            if not p.exists():
                continue
            try:
                size_bytes = p.stat().st_size
            except OSError:
                continue
            if size_bytes < self._min_source_bytes:
                continue
            source = p
            break

        if source is None:
            return {"status": "NO_USABLE_SOURCE",
                    "paths_seen": len(paths_meta)}

        source_mtime = source.stat().st_mtime
        generated, skipped, failed = [], [], []
        for size in self._sizes:
            tp = _thumb_path(self._thumb_root, size, sid, num)
            if _is_thumb_fresh(tp, source_mtime):
                skipped.append(size)
                continue
            try:
                self._render(pil, source, tp, size)
                generated.append(size)
            except Exception as e:  # noqa: BLE001 — Pillow zoo
                log.warning("[thumbnailer] %s/%s @%dpx failed: %s",
                            sid, num, size, e)
                failed.append({"size": size, "error": type(e).__name__})

        status = "OK" if not failed else (
            "PARTIAL" if generated else "FAILED")
        return {
            "status": status,
            "source": str(source),
            "generated": generated,
            "skipped": skipped,
            "failed": failed,
        }

    # ── Rendering ────────────────────────────────────────────────

    def _render(self, pil: Any, source: Path, dest: Path,
                max_edge: int) -> None:
        """Resize source to fit max_edge on its longest side (no
        upscale) and write to dest atomically as WEBP."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(source, "rb") as f:
            img = pil.open(io.BytesIO(f.read()))
            # Some sources are CMYK or palette — convert to RGB so
            # WEBP encoding doesn't choke. RGBA is preserved when
            # present so card holofoils with transparency stay clean.
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA" if "A" in img.mode else "RGB")
            # No upscale: thumbnail() is no-op when both dims < max_edge.
            img.thumbnail((max_edge, max_edge))

            # Atomic write: tmp file in the SAME directory as dest
            # (so rename is atomic — different-filesystem renames
            # are not), then os.replace. fsync the file body before
            # rename so a crash mid-rename can't leave a 0-byte
            # thumb behind.
            fd, tmp_str = tempfile.mkstemp(
                prefix=".tmp.", suffix=".webp", dir=str(dest.parent))
            tmp = Path(tmp_str)
            try:
                with os.fdopen(fd, "wb") as out:
                    img.save(out, format="WEBP",
                             quality=self._webp_quality, method=4)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(str(tmp), str(dest))
            except Exception:
                # Clean up the tmp; never leave half-files behind.
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
