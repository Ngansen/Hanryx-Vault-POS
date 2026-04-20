"""
Auto-tune the recognizer from operator-pick training data.

Every scan that an operator accepts, overrides, or rejects flows into
`card_scan_overrides` (see scan_overrides.py). Once you've collected a
few hundred picks, that table contains a quiet little signal:

    "OCR-on-CHS scans is wrong 30% of the time, but pHash-on-CHS is
     wrong only 12% — so for that source the recognizer should weight
     pHash above OCR."

This module mines that signal and emits a tuning blob the recognizer
service polls every 5 minutes and applies as a per-candidate score
multiplier. No retraining, no model rebuild — just data-driven weights.

Output shape
------------
    {
      "version":      1,
      "computed_at":  <epoch ms>,
      "sample_size":  <int>,
      "min_n_per_cell": 20,
      "baseline_acceptance": 0.834,
      "method_x_source": {
        "ocr_number|kr":  1.08,
        "phash|kr":       1.02,
        "ocr_number|chs": 0.79,
        "phash|chs":      1.06,
        ...
      },
      "method_weights": { "ocr_number": 1.04, "phash": 0.96, "fallback": 0.55 },
      "source_weights": { "kr": 1.00, "chs": 0.91, "jpn": 0.97, ... },
      "confused_pairs": [
        {"auto": "sv1-25", "picked": "sv1-026", "n": 14, "source": "multi:Pokemon"},
        ...
      ]
    }

Weights are clamped to [0.5, 1.5] so a noisy bucket can't completely
silence or runaway-promote a method. Cells with fewer than `min_n` rows
fall back to 1.0 (= no adjustment).

Public API
----------
    bootstrap(conn)
    compute(conn, *, since_ms=0, min_n=20) → dict
    persist(conn, tuning) → int    # row id
    latest(conn) → dict | None
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

log = logging.getLogger("recognizer_tuning")

_DDL = """
CREATE TABLE IF NOT EXISTS recognizer_tuning (
    id           BIGSERIAL PRIMARY KEY,
    computed_at  BIGINT       NOT NULL,
    sample_size  INTEGER      NOT NULL DEFAULT 0,
    tuning       JSONB        NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rectune_computed_at
  ON recognizer_tuning (computed_at DESC);
"""

# Bounds on every emitted multiplier. Stops a small bad-luck streak from
# nuking a whole method.
_W_MIN, _W_MAX = 0.5, 1.5

# Pull at most this many confused-pair rows into the tuning blob.
_MAX_CONFUSED_PAIRS = 50


# ── helpers ────────────────────────────────────────────────────────

def _clamp(x: float) -> float:
    return max(_W_MIN, min(_W_MAX, x))


def _weight_from_acceptance(rate: float, baseline: float) -> float:
    """
    Convert an acceptance rate to a multiplier relative to baseline.

    rate=0.95 / baseline=0.85 → 1.10  (above-average → boost 10%)
    rate=0.70 / baseline=0.85 → 0.85  (below-average → penalize 15%)
    """
    if baseline <= 0:
        return 1.0
    return _clamp(rate / baseline)


def bootstrap(conn) -> None:
    """Create the table if missing. Idempotent."""
    with conn.cursor() as cur:
        for stmt in [s.strip() for s in _DDL.split(";") if s.strip()]:
            cur.execute(stmt)
    conn.commit()


# ── main computation ──────────────────────────────────────────────

def compute(conn, *, since_ms: int = 0, min_n: int = 20) -> dict:
    """
    Mine `card_scan_overrides` and return a tuning blob.

    `since_ms`: only count rows with ts >= this (default 0 = all time)
    `min_n`:    minimum samples in a (method, source) cell to emit a
                non-1.0 weight. Cells below this stay at 1.0.
    """
    bootstrap(conn)

    # ── overall acceptance baseline ────────────────────────────────
    with conn.cursor() as cur:
        cur.execute(
            "SELECT action, COUNT(*) FROM card_scan_overrides "
            "WHERE ts >= %s GROUP BY action",
            (since_ms,),
        )
        action_counts = {a: int(n) for a, n in cur.fetchall()}

    total = sum(action_counts.values())
    accepted_total = action_counts.get("accepted", 0)
    baseline = accepted_total / total if total else 0.0

    # ── per (method, source) acceptance ────────────────────────────
    with conn.cursor() as cur:
        cur.execute("""
            SELECT auto_method, auto_source, action, COUNT(*) AS n
            FROM card_scan_overrides
            WHERE ts >= %s
            GROUP BY auto_method, auto_source, action
        """, (since_ms,))
        cells: dict[tuple[str, str], dict[str, int]] = {}
        for method, source, action, n in cur.fetchall():
            key = (method or "", source or "")
            cells.setdefault(key, {"accepted": 0, "total": 0})
            cells[key]["total"] += int(n)
            if action == "accepted":
                cells[key]["accepted"] += int(n)

    method_x_source: dict[str, float] = {}
    for (method, source), c in cells.items():
        if c["total"] < min_n or not method or not source:
            continue
        rate = c["accepted"] / c["total"]
        w = _weight_from_acceptance(rate, baseline)
        # Skip cells whose weight is essentially 1 to keep the blob small.
        if abs(w - 1.0) < 0.02:
            continue
        method_x_source[f"{method}|{source}"] = round(w, 3)

    # ── per-method aggregate (marginalize over source) ─────────────
    method_totals: dict[str, dict[str, int]] = {}
    for (method, _), c in cells.items():
        if not method:
            continue
        agg = method_totals.setdefault(method, {"accepted": 0, "total": 0})
        agg["accepted"] += c["accepted"]
        agg["total"]    += c["total"]

    method_weights: dict[str, float] = {}
    for method, c in method_totals.items():
        if c["total"] < min_n:
            continue
        rate = c["accepted"] / c["total"]
        method_weights[method] = round(
            _weight_from_acceptance(rate, baseline), 3
        )

    # ── per-source aggregate (marginalize over method) ─────────────
    source_totals: dict[str, dict[str, int]] = {}
    for (_, source), c in cells.items():
        if not source:
            continue
        agg = source_totals.setdefault(source, {"accepted": 0, "total": 0})
        agg["accepted"] += c["accepted"]
        agg["total"]    += c["total"]

    source_weights: dict[str, float] = {}
    for source, c in source_totals.items():
        if c["total"] < min_n:
            continue
        rate = c["accepted"] / c["total"]
        source_weights[source] = round(
            _weight_from_acceptance(rate, baseline), 3
        )

    # ── confused pairs (auto card vs picked card on overrides) ─────
    confused: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute("""
            SELECT auto_card_id, picked_card_id, auto_source, COUNT(*) AS n
            FROM card_scan_overrides
            WHERE action = 'overridden'
              AND ts >= %s
              AND auto_card_id <> ''
              AND picked_card_id <> ''
              AND auto_card_id <> picked_card_id
            GROUP BY auto_card_id, picked_card_id, auto_source
            HAVING COUNT(*) >= 3
            ORDER BY n DESC
            LIMIT %s
        """, (since_ms, _MAX_CONFUSED_PAIRS))
        for auto_id, picked_id, source, n in cur.fetchall():
            confused.append({
                "auto":   auto_id,
                "picked": picked_id,
                "source": source or "",
                "n":      int(n),
            })

    return {
        "version":              1,
        "computed_at":          int(time.time() * 1000),
        "sample_size":          total,
        "min_n_per_cell":       min_n,
        "baseline_acceptance":  round(baseline, 4),
        "method_x_source":      method_x_source,
        "method_weights":       method_weights,
        "source_weights":       source_weights,
        "confused_pairs":       confused,
    }


# ── persistence ────────────────────────────────────────────────────

def persist(conn, tuning: dict) -> int:
    """Write a tuning blob; return the new row id. Old rows are kept for audit."""
    bootstrap(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recognizer_tuning (computed_at, sample_size, tuning) "
            "VALUES (%s, %s, %s::jsonb) RETURNING id",
            (int(tuning.get("computed_at") or time.time() * 1000),
             int(tuning.get("sample_size") or 0),
             json.dumps(tuning)),
        )
        new_id = int(cur.fetchone()[0])
    conn.commit()
    return new_id


def latest(conn) -> dict | None:
    """Return the most recent tuning blob, or None if the table is empty."""
    bootstrap(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tuning FROM recognizer_tuning "
            "ORDER BY computed_at DESC LIMIT 1"
        )
        row = cur.fetchone()
    if not row:
        return None
    blob = row[0]
    return blob if isinstance(blob, dict) else json.loads(blob)
