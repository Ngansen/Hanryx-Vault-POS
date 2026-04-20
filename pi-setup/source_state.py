"""
Per-source persistence for delta sync, gap detection, and provenance.

Three responsibilities, one tiny table
--------------------------------------
1. **Delta-sync watermarks** — record the most-recent ``updated_at`` /
   ``last_seen_id`` we successfully imported from each source so the
   next run can ask the upstream for "everything since X" instead of
   pulling the whole world.

2. **Source-of-truth versioning** — record which source supplied each
   card row and when, so corrections from upstream can targeted-replace
   our cached value (see ``mark_card_source``).

3. **Run history** — every importer logs (started_at, finished_at,
   rows_seen, rows_inserted, rows_updated, errors) so the admin
   ``/admin/imports`` page can show "Korean import: 14 min ago, 47
   rows added, 0 errors" without grepping logs.

Schema
------
``source_state``        one row per (source, key) — generic kv with json value
``source_runs``         one row per importer execution — small audit log
``card_provenance``     one row per (table, pk, source) — N:N to cards

Everything is created idempotently via ``ensure_schema(conn)``.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

log = logging.getLogger("source_state")


_DDL = [
    """
    CREATE TABLE IF NOT EXISTS source_state (
        source      TEXT NOT NULL,
        key         TEXT NOT NULL,
        value       JSONB,
        updated_at  BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (source, key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_source_state_updated ON source_state (updated_at)",

    """
    CREATE TABLE IF NOT EXISTS source_runs (
        run_id        BIGSERIAL PRIMARY KEY,
        source        TEXT NOT NULL,
        started_at    BIGINT NOT NULL,
        finished_at   BIGINT,
        ok            BOOLEAN,
        rows_seen     INTEGER NOT NULL DEFAULT 0,
        rows_inserted INTEGER NOT NULL DEFAULT 0,
        rows_updated  INTEGER NOT NULL DEFAULT 0,
        rows_skipped  INTEGER NOT NULL DEFAULT 0,
        errors        INTEGER NOT NULL DEFAULT 0,
        notes         TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_source_runs_src   ON source_runs (source, started_at DESC)",

    """
    CREATE TABLE IF NOT EXISTS card_provenance (
        table_name    TEXT NOT NULL,
        pk_text       TEXT NOT NULL,
        source        TEXT NOT NULL,
        source_row_id TEXT NOT NULL DEFAULT '',
        fetched_at    BIGINT NOT NULL,
        version       TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (table_name, pk_text, source)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_provenance_src ON card_provenance (source, fetched_at DESC)",
]


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        for stmt in _DDL:
            cur.execute(stmt)
    conn.commit()


# ── delta-sync watermarks ───────────────────────────────────────────────────
def get_state(conn, source: str, key: str, default: Any = None) -> Any:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM source_state WHERE source=%s AND key=%s",
            (source, key),
        )
        row = cur.fetchone()
    if not row:
        return default
    val = row[0]
    return val if val is not None else default


def set_state(conn, source: str, key: str, value: Any) -> None:
    payload = json.dumps(value)
    now = int(time.time())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO source_state (source, key, value, updated_at)
            VALUES (%s, %s, %s::jsonb, %s)
            ON CONFLICT (source, key) DO UPDATE
              SET value      = EXCLUDED.value,
                  updated_at = EXCLUDED.updated_at
            """,
            (source, key, payload, now),
        )
    conn.commit()


def watermark(conn, source: str) -> int | None:
    """Convenience: returns the int ``last_updated_at`` watermark, or None."""
    val = get_state(conn, source, "last_updated_at")
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def set_watermark(conn, source: str, ts: int) -> None:
    set_state(conn, source, "last_updated_at", int(ts))


# ── run history ─────────────────────────────────────────────────────────────
def begin_run(conn, source: str, notes: str = "") -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO source_runs (source, started_at, notes)
            VALUES (%s, %s, %s)
            RETURNING run_id
            """,
            (source, int(time.time()), notes[:500]),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return int(run_id)


def end_run(conn, run_id: int, *, ok: bool,
            rows_seen: int = 0, rows_inserted: int = 0,
            rows_updated: int = 0, rows_skipped: int = 0,
            errors: int = 0, notes: str | None = None) -> None:
    with conn.cursor() as cur:
        if notes is None:
            cur.execute(
                """
                UPDATE source_runs SET finished_at=%s, ok=%s,
                    rows_seen=%s, rows_inserted=%s, rows_updated=%s,
                    rows_skipped=%s, errors=%s
                WHERE run_id=%s
                """,
                (int(time.time()), ok, rows_seen, rows_inserted, rows_updated,
                 rows_skipped, errors, run_id),
            )
        else:
            cur.execute(
                """
                UPDATE source_runs SET finished_at=%s, ok=%s,
                    rows_seen=%s, rows_inserted=%s, rows_updated=%s,
                    rows_skipped=%s, errors=%s, notes=%s
                WHERE run_id=%s
                """,
                (int(time.time()), ok, rows_seen, rows_inserted, rows_updated,
                 rows_skipped, errors, notes[:500], run_id),
            )
    conn.commit()


def recent_runs(conn, source: str | None = None, limit: int = 50) -> list[dict]:
    with conn.cursor() as cur:
        if source:
            cur.execute(
                """SELECT run_id, source, started_at, finished_at, ok,
                          rows_seen, rows_inserted, rows_updated, rows_skipped,
                          errors, notes
                   FROM source_runs WHERE source=%s
                   ORDER BY started_at DESC LIMIT %s""",
                (source, limit),
            )
        else:
            cur.execute(
                """SELECT run_id, source, started_at, finished_at, ok,
                          rows_seen, rows_inserted, rows_updated, rows_skipped,
                          errors, notes
                   FROM source_runs
                   ORDER BY started_at DESC LIMIT %s""",
                (limit,),
            )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ── card provenance ─────────────────────────────────────────────────────────
def mark_card_source(conn, *, table_name: str, pk_text: str,
                     source: str, source_row_id: str = "",
                     version: str = "") -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO card_provenance
                (table_name, pk_text, source, source_row_id, fetched_at, version)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (table_name, pk_text, source) DO UPDATE
              SET source_row_id = EXCLUDED.source_row_id,
                  fetched_at    = EXCLUDED.fetched_at,
                  version       = EXCLUDED.version
            """,
            (table_name, pk_text, source, source_row_id,
             int(time.time()), version),
        )
    conn.commit()
