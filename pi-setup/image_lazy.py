"""
Lazy card-image fetch + persistent pHash cache.

Two wins
--------
1. **Bandwidth/disk**: importers were eagerly downloading every set's
   full-resolution artwork at import time, even for cards nobody will
   ever scan.  This module exposes ``fetch_image_bytes(url)`` that:

     • returns the cached PNG bytes if we've fetched the URL before,
     • otherwise downloads it through the shared rate-limited HTTP
       client and stores it under ``$HV/data/img_cache/<sha1>.png``.

   Importers are migrated by replacing eager ``urlretrieve`` calls
   with ``image_lazy.fetch_image_bytes`` — or by storing only the URL
   and calling ``ensure_phash`` on first scan.

2. **Recognizer dedupe**: the recognizer's pHash code path was
   computing the same perceptual hash on every scan.  We add a
   ``image_phash_cache`` table keyed by ``image_url`` so the second
   call is a single primary-key lookup.

Both paths are best-effort — failures return None and the recognizer
falls back to its existing OCR-only path.
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
from typing import Any

log = logging.getLogger("image_lazy")


_DDL = [
    """
    CREATE TABLE IF NOT EXISTS image_phash_cache (
        image_url   TEXT PRIMARY KEY,
        phash_hex   TEXT NOT NULL,
        width       INTEGER,
        height      INTEGER,
        bytes_len   INTEGER,
        fetched_at  BIGINT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_image_phash_hex ON image_phash_cache (phash_hex)",
]


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        for stmt in _DDL:
            cur.execute(stmt)
    conn.commit()


def _cache_dir() -> str:
    base = os.environ.get("HV") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    d = os.path.join(base, "data", "img_cache")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_path(url: str) -> str:
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return os.path.join(_cache_dir(), f"{h}.bin")


def fetch_image_bytes(url: str, *, priority: str = "background") -> bytes | None:
    """
    Return the bytes of ``url``, downloading once and caching on disk.

    Reads/writes the cache directory and uses the shared rate-limited
    HTTP client.  Returns ``None`` on any failure.
    """
    if not url:
        return None
    p = _cache_path(url)
    if os.path.exists(p):
        try:
            with open(p, "rb") as f:
                return f.read()
        except OSError as exc:
            log.info("[image_lazy] cache read failed for %s: %s", url, exc)
    try:
        import http_client
        res = http_client.request(url, priority=priority, timeout=20)
        if res is None:
            return None
        status, body, _ = res
        if status != 200 or not body:
            return None
        try:
            tmp = p + ".tmp"
            with open(tmp, "wb") as f:
                f.write(body)
            os.replace(tmp, p)
        except OSError as exc:
            log.info("[image_lazy] cache write failed for %s: %s", url, exc)
        return body
    except Exception as exc:
        log.info("[image_lazy] fetch failed for %s: %s", url, exc)
        return None


# ── pHash cache (DB-backed) ────────────────────────────────────────────────
def _compute_phash(image_bytes: bytes) -> tuple[str, int, int] | None:
    """
    Compute a 64-bit perceptual hash from PNG/JPEG bytes.
    Uses imagehash if available (already in recognizer deps); otherwise
    falls back to a simple average-hash so this module can be imported
    even before recognizer deps exist.
    """
    try:
        from PIL import Image
        import imagehash
        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
        ph = imagehash.phash(img)
        return str(ph), w, h
    except ImportError:
        pass
    except Exception as exc:
        log.info("[image_lazy] phash error: %s", exc)
        return None
    # Fallback: simple 8x8 average hash via PIL only.
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes)).convert("L").resize((8, 8))
        pixels = list(img.getdata())
        avg = sum(pixels) / len(pixels)
        bits = "".join("1" if p >= avg else "0" for p in pixels)
        return f"{int(bits, 2):016x}", 8, 8
    except Exception as exc:
        log.info("[image_lazy] fallback phash failed: %s", exc)
        return None


def ensure_phash(conn, image_url: str, *,
                 priority: str = "background") -> dict | None:
    """
    Idempotently produce a pHash for ``image_url``.  Returns
    ``{"phash_hex": str, "width": int, "height": int}`` or ``None``.

    Subsequent calls are a single PK lookup; first call fetches the
    image (through the cache) and computes.
    """
    if not image_url:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT phash_hex, width, height FROM image_phash_cache "
            "WHERE image_url=%s",
            (image_url,),
        )
        row = cur.fetchone()
    if row:
        return {"phash_hex": row[0], "width": row[1], "height": row[2]}

    body = fetch_image_bytes(image_url, priority=priority)
    if not body:
        return None
    res = _compute_phash(body)
    if res is None:
        return None
    ph, w, h = res
    import time as _t
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO image_phash_cache
                (image_url, phash_hex, width, height, bytes_len, fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (image_url) DO UPDATE
              SET phash_hex=EXCLUDED.phash_hex,
                  width=EXCLUDED.width, height=EXCLUDED.height,
                  bytes_len=EXCLUDED.bytes_len, fetched_at=EXCLUDED.fetched_at
            """,
            (image_url, ph, w, h, len(body), int(_t.time())),
        )
    conn.commit()
    return {"phash_hex": ph, "width": w, "height": h}
