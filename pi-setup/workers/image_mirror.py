"""
workers/image_mirror.py — re-fetch worker for rotted card images.

Closes the loop that `image_health` opens. image_health detects
images that are MISSING / EMPTY / CORRUPT on /mnt/cards and writes
those findings to image_health_check, but historically nothing
acted on them — the rot was observability-only and a corrupt
image stayed corrupt until somebody noticed at the booth.

This worker is the missing piece. Given a (set_id, card_number)
task, it:

  1. Reads cards_master.image_url_alt — a JSONB list of
     {src, url, local} entries.
  2. For every entry that has a `local` path, checks the current
     state of that file (missing / empty / too-small / corrupt /
     ok).
  3. For every broken entry that ALSO has a canonical `url`,
     attempts to re-download from the URL into the same local
     path, atomically (tmp + fsync + rename) so a crash mid-download
     never replaces a good file with a partial one.
  4. Verifies the new file by magic-byte sniff; Pillow is used for
     stricter decode-verify when available.
  5. Returns an aggregated outcome
     {status, paths_attempted, paths_recovered, paths_still_broken}.

Operational notes:
  * urllib (stdlib) is used — no `requests` dependency. The same
    pattern sync_card_mirror.py already uses successfully on the
    bare Pi host.
  * Atomic write with min_size guard — CDNs occasionally return
    1-byte error stubs as 200 OK; we treat anything < 256 bytes
    as a failure.
  * Pillow is lazy-imported and optional; magic-byte sniff is the
    fallback so a fresh Pi without Pillow still recovers files,
    just with slightly weaker post-fetch verification.
  * Per-task retries are bounded by bg_task_queue.max_attempts
    (default 3) — a permanently-dead URL stops being retried after
    three passes, surfacing in `idx_bg_task_failed` for operator
    triage.
  * urlopen_fn is injectable for tests so the suite never hits the
    network and stays deterministic.

Why not just rerun sync_card_mirror?
  sync_card_mirror is a bulk catch-up tool that walks the entire
  catalogue. This worker is targeted: it only touches the cards
  that image_health has actually flagged as broken, which on a
  stable Pi is single-digit cards/day rather than 100k+ URLs.
  Both tools share the same atomic-download primitive, but they
  serve different cadences (bulk-mirror vs. live-recovery).
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import tempfile
from typing import Any, Callable

# urllib is stdlib — always available, no NO_LIB path needed.
import urllib.error
import urllib.request

from .base import Worker, WorkerError

log = logging.getLogger("workers.image_mirror")


# Reuse the same magic-byte set as image_health so OK/CORRUPT
# verdicts agree across the two workers (an image image_mirror
# successfully re-fetches must also pass image_health on the next
# nightly run).
_MAGIC_PREFIXES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n",   "png"),
    (b"\xff\xd8\xff",        "jpeg"),
    (b"GIF87a",              "gif"),
    (b"GIF89a",              "gif"),
    (b"RIFF",                "webp"),
    (b"BM",                  "bmp"),
    (b"\x00\x00\x01\x00",    "ico"),
)

USER_AGENT = "HanryxVault-mirror/1.0 (+https://github.com/Ngansen)"

# Files smaller than this are treated as failed downloads even on
# HTTP 200 — see sync_card_mirror.py for prior art.
MIN_FILE_BYTES = 256

# Per-fetch network timeout. Pi booth uplink can be flaky; 30s is
# the same value sync_card_mirror uses.
DEFAULT_FETCH_TIMEOUT_S = 30


def _sniff_magic(head: bytes) -> str | None:
    for prefix, name in _MAGIC_PREFIXES:
        if head.startswith(prefix):
            if name == "webp" and b"WEBP" not in head[:16]:
                continue
            return name
    return None


def _check_path(path: str, *, pil_module: Any = None) -> str:
    """Return one of: OK | MISSING | EMPTY | TOO_SMALL | CORRUPT | UNREADABLE.

    Lighter than image_health.check_one_path because we only need a
    boolean "should we re-fetch this file?" verdict, not a structured
    diagnostic — image_health already records the diagnostic.
    """
    if not path:
        return "MISSING"
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return "MISSING"
    except OSError:
        return "UNREADABLE"

    if st.st_size == 0:
        return "EMPTY"
    if st.st_size < 64:
        return "TOO_SMALL"

    try:
        with open(path, "rb") as f:
            head = f.read(32)
    except OSError:
        return "UNREADABLE"

    if not _sniff_magic(head):
        return "CORRUPT"

    if pil_module is None:
        return "OK"

    try:
        with open(path, "rb") as f:
            pil_module.open(io.BytesIO(f.read())).verify()
    except Exception:  # noqa: BLE001 — Pillow raises a wide variety
        return "CORRUPT"
    return "OK"


def _atomic_download(
    url: str,
    dest: str,
    *,
    urlopen_fn: Callable[..., Any],
    timeout: int = DEFAULT_FETCH_TIMEOUT_S,
    min_size: int = MIN_FILE_BYTES,
) -> tuple[bool, str]:
    """Download `url` to `dest` atomically. Returns (ok, status).

    Writes to <dest>.tmp first, fsyncs, renames. The tmp file is
    deleted on any failure path so a crashed download never leaves
    half-files lying around. Mirrors sync_card_mirror._download.
    """
    parent = os.path.dirname(dest) or "."
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as e:
        return False, f"mkdir-{type(e).__name__}"

    # Use a real tempfile in the same directory so the rename is
    # atomic on the same filesystem.
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".im_mirror.", suffix=".tmp", dir=parent,
        )
        os.close(tmp_fd)
    except OSError as e:
        return False, f"mktemp-{type(e).__name__}"

    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT})
        with urlopen_fn(req, timeout=timeout) as r:
            status = getattr(r, "status", 200)
            if status >= 400:
                return False, f"http-{status}"
            with open(tmp_path, "wb") as f:
                shutil.copyfileobj(r, f, length=64 * 1024)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    # fsync can fail on some FUSE / network mounts —
                    # the rename below is still atomic enough for our
                    # crash-during-download protection.
                    pass
        size = os.path.getsize(tmp_path)
        if size < min_size:
            return False, "too-small"
        os.replace(tmp_path, dest)
        tmp_path = ""  # transferred, don't unlink
        return True, "ok"
    except urllib.error.HTTPError as e:
        return False, f"http-{e.code}"
    except Exception as e:  # noqa: BLE001 — many transient failures
        return False, f"err-{type(e).__name__}"
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ── Worker ──────────────────────────────────────────────────────────


class ImageMirrorWorker(Worker):
    TASK_TYPE = "image_mirror"

    # Each task may attempt up to N downloads (one per broken alt
    # path), each ~1–5s. 10 keeps a single batch under a minute on
    # a typical Pi booth uplink while still draining a backlog at
    # ~600 cards/hour.
    BATCH_SIZE = 10

    # Re-poll the queue once a minute when idle — image_health is
    # the producer and it runs hourly, so a higher sleep would just
    # add latency between detection and recovery for no benefit.
    IDLE_SLEEP_S = 60.0

    # 10 minutes — generous headroom for slow CDNs without the
    # reaper killing in-flight downloads.
    CLAIM_TIMEOUT_S = 600

    def __init__(
        self, conn, *,
        urlopen_fn: Callable[..., Any] | None = None,
        pil_module: Any = None,
        fetch_timeout_s: int = DEFAULT_FETCH_TIMEOUT_S,
        min_size: int = MIN_FILE_BYTES,
        **kw,
    ):
        super().__init__(conn, **kw)
        # Default to urllib's real urlopen when not injected; tests
        # always inject a fake to keep the suite hermetic.
        self._urlopen_fn = urlopen_fn or urllib.request.urlopen
        self._fetch_timeout_s = fetch_timeout_s
        self._min_size = min_size
        # Pillow is optional; constructor injection has three modes:
        #   pil_module=None   → lazy auto-detect on first use
        #   pil_module=False  → explicitly disable PIL, magic-only
        #   pil_module=<obj>  → use the supplied object (real PIL or
        #                       a test fake)
        # Tests that only care about fetch/atomicity should pass
        # False so the result is deterministic regardless of whether
        # Pillow happens to be installed in the test environment.
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
            log.info("[image_mirror] Pillow not installed — magic-byte "
                     "verification only. `pip install Pillow` for "
                     "stricter post-fetch decode checks.")
            self._pil_module = None
        return self._pil_module

    # ── Worker contract ──────────────────────────────────────────

    def process(self, task: dict) -> dict:
        payload = task.get("payload") or {}
        sid = (payload.get("set_id") or "").strip()
        num = (payload.get("card_number") or "").strip()
        if not sid or not num:
            raise WorkerError(
                f"image_mirror task {task.get('task_id')} missing "
                f"set_id/card_number in payload: {payload!r}"
            )

        cur = self.conn.cursor()
        cur.execute(
            "SELECT image_url_alt FROM cards_master "
            "WHERE set_id = %s AND card_number = %s",
            (sid, num),
        )
        row = cur.fetchone()
        if row is None:
            return {"status": "MISSING_CARD",
                    "paths_attempted": 0,
                    "paths_recovered": 0,
                    "paths_still_broken": 0}

        raw = row[0] if not isinstance(row, dict) else row.get("image_url_alt")
        if isinstance(raw, str):
            try:
                paths_meta = json.loads(raw)
            except Exception:
                paths_meta = []
        else:
            paths_meta = raw or []

        # Filter to entries that name a local path. Entries with no
        # local path were never mirrored to begin with — that's a
        # job for sync_card_mirror, not for the rot-recovery worker.
        candidates: list[dict] = [
            e for e in paths_meta
            if isinstance(e, dict) and (e.get("local") or "").strip()
        ]
        if not candidates:
            return {"status": "NO_PATHS",
                    "paths_attempted": 0,
                    "paths_recovered": 0,
                    "paths_still_broken": 0}

        pil = self._ensure_pil()

        attempted = recovered = still_broken = 0
        already_ok = 0
        details: list[dict] = []

        for entry in candidates:
            local = (entry.get("local") or "").strip()
            url = (entry.get("url") or "").strip()
            src = (entry.get("src") or "?").strip()

            current = _check_path(local, pil_module=pil)
            if current == "OK":
                already_ok += 1
                details.append({"src": src, "local": local,
                                "before": "OK", "action": "skip"})
                continue

            if not url.startswith(("http://", "https://")):
                # Broken file but no canonical URL to refetch from.
                still_broken += 1
                details.append({"src": src, "local": local,
                                "before": current, "action": "no-url"})
                continue

            attempted += 1
            ok, status = _atomic_download(
                url, local,
                urlopen_fn=self._urlopen_fn,
                timeout=self._fetch_timeout_s,
                min_size=self._min_size,
            )
            if not ok:
                still_broken += 1
                details.append({"src": src, "local": local,
                                "before": current, "action": "fetch",
                                "result": status})
                continue

            after = _check_path(local, pil_module=pil)
            if after == "OK":
                recovered += 1
                details.append({"src": src, "local": local,
                                "before": current, "action": "fetch",
                                "result": "ok"})
            else:
                # Downloaded bytes that don't decode — server probably
                # served an HTML error page with image content-type.
                # Leave the (now overwritten) file in place so the
                # next image_health run sees the failure too, but
                # count it as still broken for this pass.
                still_broken += 1
                details.append({"src": src, "local": local,
                                "before": current, "action": "fetch",
                                "result": f"verify-{after}"})

        # Aggregate verdict. OK = nothing was broken to begin with
        # (the queue producer was stale). RECOVERED = at least one
        # path successfully restored. PARTIAL = some recovered, some
        # still broken. FAILED = nothing recovered.
        if still_broken == 0 and recovered > 0:
            status_word = "RECOVERED"
        elif still_broken == 0 and recovered == 0:
            status_word = "OK"
        elif recovered > 0:
            status_word = "PARTIAL"
        else:
            status_word = "FAILED"

        return {
            "status": status_word,
            "paths_attempted": attempted,
            "paths_recovered": recovered,
            "paths_still_broken": still_broken,
            "paths_already_ok": already_ok,
            "details": details,
        }
