"""
price_filter.py — market listing quality filters for the POS price scraper.

IQR outlier removal
-------------------
Scraped results often contain:
  • Bulk lots (10 cards listed together → appears dirt-cheap per card)
  • Wrong variants (Pikachu V when you searched plain Pikachu)
  • Sealed product mixed into singles results

Inter-quartile-range (IQR) filtering on price_usd catches these without
silently discarding them — outliers are tagged ``_outlier: true`` so the
UI can render them greyed-out rather than hiding them entirely.

CLIP visual verification
------------------------
When listing thumbnails are available (Naver, Bunjang) and the caller
supplies a reference embedding, each thumbnail is CLIP-scored against
the reference card. Listings that score below ``clip_threshold`` are
tagged ``_clip_mismatch: true``.

This step is optional and gracefully skipped when:
  • no reference embedding is supplied / available in the DB
  • onnxruntime / numpy / Pillow are not installed
  • the listing has no ``image`` URL
  • thumbnail download fails or times out

Design note: the embed_fn and reference_embedding are caller-supplied so
this module stays import-free (no heavy ML deps at module load time) and
fully unit-testable with fakes.
"""
from __future__ import annotations

import io
import logging
import statistics
import threading
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ── IQR outlier filter ────────────────────────────────────────────────────────

DEFAULT_IQR_K = 1.5          # standard Tukey fence multiplier
MIN_SAMPLE    = 4            # below this count, don't filter (too few to be meaningful)


def _quartiles(vals: list[float]) -> tuple[float, float]:
    """Return (Q1, Q3) using the 25th and 75th percentile (linear interpolation)."""
    s = sorted(vals)
    n = len(s)

    def _interp(p: float) -> float:
        idx = p * (n - 1)
        lo, hi = int(idx), min(int(idx) + 1, n - 1)
        return s[lo] + (s[hi] - s[lo]) * (idx - lo)

    return _interp(0.25), _interp(0.75)


def iqr_filter(
    listings: list[dict],
    price_key: str = "price_usd",
    k: float = DEFAULT_IQR_K,
) -> tuple[list[dict], dict]:
    """
    Tag listings as outliers using the Tukey IQR fence.

    Each listing gains two new keys:
      ``_outlier``   — True if the price is outside [Q1 - k*IQR, Q3 + k*IQR]
      ``_iqr_fence`` — (lo, hi) tuple added to each row for transparency

    Parameters
    ----------
    listings : list[dict]
        Flat list of normalised price rows (must already have ``price_usd``).
    price_key : str
        Key to filter on (default ``"price_usd"``).
    k : float
        Tukey fence multiplier (default 1.5; use 3.0 for "far outliers" only).

    Returns
    -------
    (tagged_listings, stats)
        ``tagged_listings`` — same order, every row has ``_outlier`` bool.
        ``stats`` — {n_total, n_outliers, q1, q3, iqr, lo, hi,
                     raw_median, filtered_median}
    """
    vals = [r[price_key] for r in listings if isinstance(r.get(price_key), (int, float))]

    if len(vals) < MIN_SAMPLE:
        for r in listings:
            r["_outlier"] = False
        return listings, {"n_total": len(listings), "n_outliers": 0,
                          "filtered_median": statistics.median(vals) if vals else None}

    q1, q3  = _quartiles(vals)
    iqr_val = q3 - q1
    lo      = q1 - k * iqr_val
    hi      = q3 + k * iqr_val

    raw_vals      = vals
    filtered_vals = [v for v in vals if lo <= v <= hi]

    raw_median      = round(statistics.median(raw_vals),      4) if raw_vals else None
    filtered_median = round(statistics.median(filtered_vals), 4) if filtered_vals else None

    n_out = 0
    for r in listings:
        v = r.get(price_key)
        if isinstance(v, (int, float)):
            is_out = not (lo <= v <= hi)
        else:
            is_out = False          # no price_usd → don't flag
        r["_outlier"]    = is_out
        r["_iqr_fence"]  = (round(lo, 4), round(hi, 4))
        if is_out:
            n_out += 1

    return listings, {
        "n_total":          len(listings),
        "n_outliers":       n_out,
        "q1":               round(q1, 4),
        "q3":               round(q3, 4),
        "iqr":              round(iqr_val, 4),
        "lo":               round(lo, 4),
        "hi":               round(hi, 4),
        "raw_median_usd":   raw_median,
        "filtered_median_usd": filtered_median,
    }


# ── CLIP visual verification ──────────────────────────────────────────────────

_CLIP_TIMEOUT_S   = 3.0    # per-thumbnail download + encode budget
_CLIP_MIN_SCORE   = 0.20   # cosine similarity below this → likely wrong card
_CLIP_WARN_SCORE  = 0.35   # below this → uncertain (flag but don't hide)


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two pre-normalised vectors (dot product)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def _clip_score_one(
    image_url: str,
    reference_embedding: list[float],
    embed_fn: Callable[[bytes], Optional[list[float]]],
) -> Optional[float]:
    """
    Download ``image_url`` and compute cosine similarity against
    ``reference_embedding``.  Returns None on any failure.
    """
    try:
        import urllib.request
        req = urllib.request.Request(
            image_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; HanryxVault/1.0; price-verify)"
                )
            },
        )
        with urllib.request.urlopen(req, timeout=_CLIP_TIMEOUT_S) as resp:
            img_bytes = resp.read(4 * 1024 * 1024)   # cap at 4MB
    except Exception as exc:
        log.debug("[price_filter] thumbnail fetch failed %s: %s", image_url, exc)
        return None

    try:
        emb = embed_fn(img_bytes)
    except Exception as exc:
        log.debug("[price_filter] embed_fn failed: %s", exc)
        return None

    if emb is None:
        return None

    return _cosine(reference_embedding, emb)


def clip_score_listings(
    listings: list[dict],
    reference_embedding: list[float],
    embed_fn: Callable[[bytes], Optional[list[float]]],
    timeout_s: float = 8.0,
    min_score: float = _CLIP_MIN_SCORE,
    warn_score: float = _CLIP_WARN_SCORE,
) -> list[dict]:
    """
    Add ``clip_score`` and ``_clip_mismatch`` to each listing that has
    an ``image`` URL.  Runs all downloads concurrently in daemon threads
    so the total wall time is bounded by ``timeout_s`` regardless of
    how many listings have images.

    Listings without an ``image`` field are left unchanged.

    Parameters
    ----------
    listings : list[dict]
        Flat normalised listing rows (may or may not have ``image`` key).
    reference_embedding : list[float]
        L2-normalised 512-dim CLIP embedding of the reference NM card.
    embed_fn : callable(bytes) → list[float] | None
        Function that takes raw image bytes and returns a normalised
        embedding.  The clip_embedder worker provides this; pass a lambda
        wrapping ``ClipEmbedderWorker._embed_bytes()`` or similar.
    timeout_s : float
        Wall-clock budget for ALL concurrent downloads+encodes.
    min_score : float
        Below this cosine similarity the listing is flagged
        ``_clip_mismatch: True`` (likely wrong card or sealed product).
    warn_score : float
        Below this (but above min_score) the listing gets
        ``_clip_uncertain: True``.

    Returns
    -------
    Same list with extra keys populated in-place.
    """
    results: dict[int, Optional[float]] = {}
    threads: list[threading.Thread] = []
    lock = threading.Lock()

    to_check = [
        (i, r) for i, r in enumerate(listings)
        if (r.get("image") or "").startswith("http")
    ]

    def _worker(idx: int, url: str) -> None:
        score = _clip_score_one(url, reference_embedding, embed_fn)
        with lock:
            results[idx] = score

    for idx, row in to_check:
        t = threading.Thread(
            target=_worker, args=(idx, row["image"]), daemon=True
        )
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=timeout_s)

    for idx, row in to_check:
        score = results.get(idx)          # None = timeout / failed
        if score is None:
            row["clip_score"]     = None
            row["_clip_mismatch"] = False  # benefit of doubt on failure
            row["_clip_uncertain"] = False
        else:
            row["clip_score"]      = round(score, 4)
            row["_clip_mismatch"]  = score < min_score
            row["_clip_uncertain"] = (min_score <= score < warn_score)

    return listings


# ── Combined pipeline ─────────────────────────────────────────────────────────

def apply_filters(
    listings: list[dict],
    *,
    iqr_k: float = DEFAULT_IQR_K,
    reference_embedding: Optional[list[float]] = None,
    embed_fn: Optional[Callable[[bytes], Optional[list[float]]]] = None,
    clip_timeout_s: float = 8.0,
) -> tuple[list[dict], dict]:
    """
    Run IQR filter (always) then CLIP verification (when reference is available).

    Returns (listings_with_tags, filter_stats).
    ``filter_stats`` includes IQR stats plus ``clip_checked: bool``.
    """
    listings, stats = iqr_filter(listings, k=iqr_k)

    clip_checked = False
    if reference_embedding and embed_fn:
        try:
            listings = clip_score_listings(
                listings, reference_embedding, embed_fn,
                timeout_s=clip_timeout_s,
            )
            clip_checked = True
        except Exception:
            log.exception("[price_filter] CLIP scoring failed, continuing without it")

    stats["clip_checked"] = clip_checked
    return listings, stats
