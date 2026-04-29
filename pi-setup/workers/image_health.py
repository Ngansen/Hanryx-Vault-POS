"""
workers/image_health.py — first concrete background helper.

Walks every cards_master row that has at least one `image_url_alt`
local path and verifies the file is:
  * present on disk
  * non-empty
  * decodable as an image (via Pillow when available, else just a
    minimal magic-byte sniff so the worker still runs without PIL)

Per-card outcome is recorded in `image_health_check`. Aggregated
status:
  OK             — at least one path opens cleanly
  PARTIAL        — some paths OK, some broken (worth investigating)
  ALL_MISSING    — every path is missing on disk (image not mirrored
                    yet; image_mirror worker should be queued)
  ALL_EMPTY      — every path exists but is 0 bytes (interrupted
                    download — re-fetch with image_mirror)
  ALL_CORRUPT    — every path exists with bytes but won't decode
  NO_PATHS       — image_url_alt is empty (no candidates known)
  MISSING_CARD   — cards_master row was deleted between seed & process

Designed to be safe to re-run nightly: history rows in
image_health_check let admin chart "image rot" over time.

Cheap on the Pi (~10k cards/min on a stat-only run, ~1-2k cards/min
with full Pillow decode).
"""
from __future__ import annotations

import io
import json
import logging
import os
import time
from typing import Any

from .base import Worker, WorkerError

log = logging.getLogger("workers.image_health")

# Lazy-imported in _decode() so the worker still runs on a Pi without
# Pillow installed (image_mirror has the same lazy pattern). When PIL
# isn't available we fall back to magic-byte sniffing.
_PIL_TRIED = False
_PIL_IMAGE: Any = None


def _try_load_pil() -> Any:
    global _PIL_TRIED, _PIL_IMAGE
    if _PIL_TRIED:
        return _PIL_IMAGE
    _PIL_TRIED = True
    try:
        from PIL import Image  # type: ignore
        _PIL_IMAGE = Image
    except ImportError:
        log.info("[image_health] Pillow not installed — falling back to "
                 "magic-byte sniffing only. `pip install Pillow` for "
                 "stricter validation.")
        _PIL_IMAGE = None
    return _PIL_IMAGE


# Common image magic-byte prefixes.
_MAGIC_PREFIXES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n",   "png"),
    (b"\xff\xd8\xff",        "jpeg"),
    (b"GIF87a",              "gif"),
    (b"GIF89a",              "gif"),
    (b"RIFF",                "webp"),  # RIFF....WEBP — confirm in body
    (b"BM",                  "bmp"),
    (b"\x00\x00\x01\x00",    "ico"),
)


def _sniff_magic(head: bytes) -> str | None:
    for prefix, name in _MAGIC_PREFIXES:
        if head.startswith(prefix):
            if name == "webp" and b"WEBP" not in head[:16]:
                continue
            return name
    return None


def check_one_path(path: str) -> dict:
    """Returns {'path': path, 'status': str, 'size_bytes': int,
                'fmt': str | ''}.

    status one of: OK | MISSING | EMPTY | UNREADABLE | TOO_SMALL | CORRUPT
    """
    if not path:
        return {"path": "", "status": "MISSING", "size_bytes": 0, "fmt": ""}
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return {"path": path, "status": "MISSING", "size_bytes": 0, "fmt": ""}
    except PermissionError:
        return {"path": path, "status": "UNREADABLE",
                "size_bytes": 0, "fmt": ""}
    except OSError as e:
        return {"path": path, "status": f"UNREADABLE:{e.errno}",
                "size_bytes": 0, "fmt": ""}

    if st.st_size == 0:
        return {"path": path, "status": "EMPTY", "size_bytes": 0, "fmt": ""}
    if st.st_size < 64:
        # 64 bytes is generous — even the tiniest valid image header
        # (1×1 PNG) is ~67 bytes; anything smaller is truncation.
        return {"path": path, "status": "TOO_SMALL",
                "size_bytes": st.st_size, "fmt": ""}

    try:
        with open(path, "rb") as f:
            head = f.read(32)
    except OSError as e:
        return {"path": path, "status": f"UNREADABLE:{e.errno}",
                "size_bytes": st.st_size, "fmt": ""}

    fmt = _sniff_magic(head) or ""
    if not fmt:
        return {"path": path, "status": "CORRUPT",
                "size_bytes": st.st_size, "fmt": ""}

    pil = _try_load_pil()
    if pil is None:
        # No Pillow — magic byte match is enough for OK
        return {"path": path, "status": "OK",
                "size_bytes": st.st_size, "fmt": fmt}

    try:
        with open(path, "rb") as f:
            img = pil.open(io.BytesIO(f.read()))
            img.verify()  # checks decode integrity without full decompress
    except Exception:  # noqa: BLE001 — PIL raises a wide variety
        return {"path": path, "status": "CORRUPT",
                "size_bytes": st.st_size, "fmt": fmt}

    return {"path": path, "status": "OK",
            "size_bytes": st.st_size, "fmt": fmt}


def aggregate_status(results: list[dict]) -> str:
    if not results:
        return "NO_PATHS"
    n = len(results)
    ok = sum(1 for r in results if r["status"] == "OK")
    if ok == n:
        return "OK"
    if ok > 0:
        return "PARTIAL"
    # All broken. Diagnose the failure mode:
    statuses = {r["status"] for r in results}
    if statuses == {"MISSING"}:
        return "ALL_MISSING"
    if statuses <= {"EMPTY", "TOO_SMALL"}:
        return "ALL_EMPTY"
    if statuses <= {"CORRUPT"}:
        return "ALL_CORRUPT"
    return "PARTIAL"  # mixed failure modes — admin should investigate


# ── Worker ───────────────────────────────────────────────────────


class ImageHealthWorker(Worker):
    TASK_TYPE = "image_health"
    BATCH_SIZE = 100
    IDLE_SLEEP_S = 30.0
    # Stat + Pillow.verify on ~100 paths/card is fast; 5 min ceiling
    # is more than enough headroom for slow USB.
    CLAIM_TIMEOUT_S = 300

    # How recent must an existing health-check be to skip re-seeding
    # this card? Default: 7 days. Override with --recheck-after-days
    # on the CLI.
    DEFAULT_RECHECK_AFTER_S = 7 * 86400

    def __init__(self, conn, *, recheck_after_s: int | None = None,
                 **kw):
        super().__init__(conn, **kw)
        self.recheck_after_s = recheck_after_s \
            if recheck_after_s is not None \
            else self.DEFAULT_RECHECK_AFTER_S

    def seed(self) -> int:
        """Enqueue every cards_master row whose image_url_alt has at
        least one local path AND hasn't been checked in the last
        `recheck_after_s` seconds. Idempotent via UNIQUE
        (task_type, task_key) — re-running while items are already
        PENDING is a no-op."""
        cutoff = int(time.time()) - self.recheck_after_s
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO bg_task_queue
                (task_type, task_key, payload, status, created_at)
            SELECT 'image_health',
                   c.set_id || '/' || c.card_number,
                   jsonb_build_object('set_id',      c.set_id,
                                      'card_number', c.card_number),
                   'PENDING',
                   %s
              FROM cards_master c
             WHERE EXISTS (
                       SELECT 1
                         FROM jsonb_array_elements(c.image_url_alt) p
                        WHERE COALESCE(p->>'local','') <> ''
                   )
               AND NOT EXISTS (
                       SELECT 1 FROM image_health_check h
                        WHERE h.set_id      = c.set_id
                          AND h.card_number = c.card_number
                          AND h.checked_at  > %s
                   )
            ON CONFLICT (task_type, task_key) DO NOTHING
        """, (int(time.time()), cutoff))
        n = cur.rowcount or 0
        self.conn.commit()
        log.info("[image_health] seed enqueued %d task(s)", n)
        return n

    def process(self, task: dict) -> dict:
        payload = task.get("payload") or {}
        sid = (payload.get("set_id")     or "").strip()
        num = (payload.get("card_number") or "").strip()
        if not sid or not num:
            raise WorkerError(
                f"image_health task {task['task_id']} missing "
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
            self._record(sid, num, "MISSING_CARD", [])
            return {"status": "MISSING_CARD"}

        # row[0] may be JSONB-decoded list or JSON string depending on
        # how the cursor adapter is configured; handle both.
        raw = row[0] if not isinstance(row, dict) else row.get("image_url_alt")
        if isinstance(raw, str):
            try:
                paths_meta = json.loads(raw)
            except Exception:
                paths_meta = []
        else:
            paths_meta = raw or []

        results = []
        for entry in paths_meta:
            local = (entry.get("local") or "").strip() if isinstance(entry, dict) else ""
            if not local:
                continue
            results.append(check_one_path(local))

        status = aggregate_status(results)
        self._record(sid, num, status, results)
        return {"status": status, "paths_checked": len(results),
                "paths_ok": sum(1 for r in results if r["status"] == "OK")}

    def _record(self, sid: str, num: str, status: str,
                results: list[dict]) -> None:
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO image_health_check
              (set_id, card_number, status,
               paths_checked, paths_ok, details, checked_at)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (set_id, card_number, checked_at) DO NOTHING
        """, (
            sid, num, status,
            len(results),
            sum(1 for r in results if r["status"] == "OK"),
            json.dumps(results, ensure_ascii=False),
            int(time.time()),
        ))
        self.conn.commit()
