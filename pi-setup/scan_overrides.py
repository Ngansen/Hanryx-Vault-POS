"""
Scan override / training-data logger.

Every time the recognizer returns candidates and the operator either accepts
the top result, picks a different one from the list, or rejects all and types
the card manually, we record what happened. That gives us:

  • Honest accuracy metrics per source (kr/chs/jpn/multi) and per method
    (ocr_number / phash / fallback) — see /admin/scan-overrides/stats.
  • A clean labeled-data export for retraining or threshold tuning.

The schema is self-bootstrapping (CREATE TABLE IF NOT EXISTS) so this module
works the moment it's imported — no separate migration step.

Public API
----------
    bootstrap(conn)
    log_pick(conn, payload)
    export_csv(conn, since_ms=0, limit=10_000) → str
    stats(conn, since_ms=0)                    → dict
"""
from __future__ import annotations

import csv
import io
import json
import logging
import time
from typing import Any

log = logging.getLogger("scan_overrides")


_DDL = """
CREATE TABLE IF NOT EXISTS card_scan_overrides (
    id              BIGSERIAL PRIMARY KEY,
    ts              BIGINT       NOT NULL,
    operator        TEXT         NOT NULL DEFAULT '',
    device          TEXT         NOT NULL DEFAULT '',
    image_sha       TEXT         NOT NULL DEFAULT '',
    auto_method     TEXT         NOT NULL DEFAULT '',  -- ocr_number | phash | fallback
    auto_source     TEXT         NOT NULL DEFAULT '',  -- kr | chs | jpn | multi:<game>
    auto_card_id    TEXT         NOT NULL DEFAULT '',
    auto_score      REAL         NOT NULL DEFAULT 0,
    picked_source   TEXT         NOT NULL DEFAULT '',
    picked_card_id  TEXT         NOT NULL DEFAULT '',
    picked_index    INTEGER      NOT NULL DEFAULT -1,  -- -1 = manual / rejected
    action          TEXT         NOT NULL DEFAULT '',  -- accepted | overridden | rejected | manual
    ocr_tokens      JSONB        NOT NULL DEFAULT '[]'::jsonb,
    candidates      JSONB        NOT NULL DEFAULT '[]'::jsonb,
    notes           TEXT         NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_overrides_ts        ON card_scan_overrides (ts DESC);
CREATE INDEX IF NOT EXISTS idx_overrides_action    ON card_scan_overrides (action);
CREATE INDEX IF NOT EXISTS idx_overrides_method    ON card_scan_overrides (auto_method);
CREATE INDEX IF NOT EXISTS idx_overrides_image_sha ON card_scan_overrides (image_sha);
"""


def bootstrap(conn) -> None:
    """Create the table + indexes if missing. Safe to call repeatedly."""
    with conn.cursor() as cur:
        for stmt in [s.strip() for s in _DDL.split(";") if s.strip()]:
            cur.execute(stmt)
    conn.commit()


def _classify_action(payload: dict) -> str:
    """Normalise / validate the `action` field coming from the tablet."""
    raw = (payload.get("action") or "").strip().lower()
    if raw in ("accepted", "overridden", "rejected", "manual"):
        return raw
    # Infer from picked_index when the client forgot to send it.
    pi = payload.get("picked_index")
    if pi is None or pi < 0:
        return "rejected"
    if pi == 0:
        return "accepted"
    return "overridden"


def log_pick(conn, payload: dict) -> int:
    """
    Insert one override row. `payload` is the JSON body POSTed from the
    tablet — see TABLET_API.md for the exact shape. Returns the new row id.
    """
    candidates = payload.get("candidates") or []
    auto = candidates[0] if candidates else {}
    pi = payload.get("picked_index")
    pi = int(pi) if isinstance(pi, (int, float)) else -1
    picked = (candidates[pi] if (0 <= pi < len(candidates)) else {}) or {}

    row = (
        int(payload.get("ts") or time.time() * 1000),
        str(payload.get("operator") or ""),
        str(payload.get("device") or ""),
        str(payload.get("image_sha") or ""),
        str(auto.get("method") or ""),
        str(auto.get("source") or ""),
        str(auto.get("card_id") or ""),
        float(auto.get("score") or auto.get("confidence") or 0),
        str(picked.get("source") or payload.get("picked_source") or ""),
        str(picked.get("card_id") or payload.get("picked_card_id") or ""),
        pi,
        _classify_action(payload),
        json.dumps(payload.get("ocr_tokens") or []),
        json.dumps(candidates),
        str(payload.get("notes") or "")[:500],
    )

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO card_scan_overrides
              (ts, operator, device, image_sha,
               auto_method, auto_source, auto_card_id, auto_score,
               picked_source, picked_card_id, picked_index,
               action, ocr_tokens, candidates, notes)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, row)
        new_id = cur.fetchone()[0]
    conn.commit()
    return int(new_id)


def stats(conn, since_ms: int = 0) -> dict:
    """Aggregate accuracy by method/source since a given timestamp (ms)."""
    sql = """
        SELECT auto_method, auto_source, action, COUNT(*) AS n
        FROM card_scan_overrides
        WHERE ts >= %s
        GROUP BY auto_method, auto_source, action
        ORDER BY n DESC
    """
    out: dict[str, Any] = {"since_ms": since_ms,
                           "total": 0, "by_action": {}, "rows": []}
    with conn.cursor() as cur:
        cur.execute(sql, (since_ms,))
        for method, source, action, n in cur.fetchall():
            out["rows"].append({"method": method, "source": source,
                                "action": action, "count": int(n)})
            out["by_action"][action] = out["by_action"].get(action, 0) + int(n)
            out["total"] += int(n)
    accepted = out["by_action"].get("accepted", 0)
    out["accuracy_percent"] = (round(100 * accepted / out["total"], 2)
                               if out["total"] else None)
    return out


def export_csv(conn, since_ms: int = 0, limit: int = 10_000) -> str:
    """Return a CSV blob of overrides — useful for offline retraining."""
    cols = ("id", "ts", "operator", "device", "image_sha",
            "auto_method", "auto_source", "auto_card_id", "auto_score",
            "picked_source", "picked_card_id", "picked_index",
            "action", "notes")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT {",".join(cols)}
            FROM card_scan_overrides
            WHERE ts >= %s
            ORDER BY ts DESC
            LIMIT %s
        """, (since_ms, limit))
        for r in cur.fetchall():
            w.writerow(r)
    return buf.getvalue()
