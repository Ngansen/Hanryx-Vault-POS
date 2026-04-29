"""
workers/data_analyst.py — periodic data-quality analyst.

Runs a fixed catalogue of report queries against `cards_master` (and
related tables) and stores each result as a JSONB snapshot row in
`data_analysis_report`. The admin UI reads the latest snapshot of
each kind via the `data_analysis_latest` view.

Why a worker (vs. a one-shot script)?
  * Reports are cheap individually but useful daily — the task queue
    gives us scheduling, retry, and heartbeat for free.
  * Future report kinds drop in by adding one entry to REPORTS — no
    new schema, no new CLI plumbing.
  * Snapshot history means the admin can chart "completeness over
    time" without us building a separate metrics pipeline.

Each report = one task. seed() enqueues every kind in REPORTS that
either hasn't been generated yet or whose latest snapshot is older
than `recheck_after_s` (default 24h). Idempotent.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from .base import Worker, WorkerError

log = logging.getLogger("workers.data_analyst")


# ── Report SQL catalogue ─────────────────────────────────────────
#
# Each entry is (label, sql, row_shape).
#   row_shape='one'  → fetchone()  → store as a single dict
#   row_shape='many' → fetchall() → store as a list of dicts
#
# Adding a new report: append one entry. No code changes elsewhere.

REPORTS: dict[str, tuple[str, str]] = {
    # Field-level fill rates across all cards.
    "completeness": ("one", """
        SELECT
            COUNT(*)                                                AS total_cards,
            COUNT(*) FILTER (WHERE COALESCE(name_en,  '') <> '')    AS with_name_en,
            COUNT(*) FILTER (WHERE COALESCE(name_kr,  '') <> '')    AS with_name_kr,
            COUNT(*) FILTER (WHERE COALESCE(name_jp,  '') <> '')    AS with_name_jp,
            COUNT(*) FILTER (WHERE COALESCE(name_chs, '') <> '')    AS with_name_chs,
            COUNT(*) FILTER (WHERE COALESCE(rarity,   '') <> '')    AS with_rarity,
            COUNT(*) FILTER (WHERE COALESCE(artist,   '') <> '')    AS with_artist,
            COUNT(*) FILTER (WHERE hp IS NOT NULL)                  AS with_hp,
            COUNT(*) FILTER (WHERE pokedex_id IS NOT NULL)          AS with_pokedex,
            COUNT(*) FILTER (WHERE COALESCE(image_url, '') <> '')   AS with_primary_image,
            COUNT(*) FILTER (
                WHERE jsonb_typeof(image_url_alt) = 'array'
                  AND jsonb_array_length(image_url_alt) > 0
            )                                                       AS with_any_image
          FROM cards_master
    """),

    # How many cards have how many of the 4 supported language names.
    "language_coverage": ("one", """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (
                WHERE COALESCE(name_en,'')<>'' AND COALESCE(name_kr,'')<>''
                  AND COALESCE(name_jp,'')<>'' AND COALESCE(name_chs,'')<>''
            ) AS all_four,
            COUNT(*) FILTER (
                WHERE (CASE WHEN COALESCE(name_en,'')<>''  THEN 1 ELSE 0 END)
                    + (CASE WHEN COALESCE(name_kr,'')<>''  THEN 1 ELSE 0 END)
                    + (CASE WHEN COALESCE(name_jp,'')<>''  THEN 1 ELSE 0 END)
                    + (CASE WHEN COALESCE(name_chs,'')<>'' THEN 1 ELSE 0 END) = 3
            ) AS three_of_four,
            COUNT(*) FILTER (
                WHERE (CASE WHEN COALESCE(name_en,'')<>''  THEN 1 ELSE 0 END)
                    + (CASE WHEN COALESCE(name_kr,'')<>''  THEN 1 ELSE 0 END)
                    + (CASE WHEN COALESCE(name_jp,'')<>''  THEN 1 ELSE 0 END)
                    + (CASE WHEN COALESCE(name_chs,'')<>'' THEN 1 ELSE 0 END) = 2
            ) AS two_of_four,
            COUNT(*) FILTER (
                WHERE COALESCE(name_en,'')='' AND COALESCE(name_kr,'')=''
                  AND COALESCE(name_jp,'')='' AND COALESCE(name_chs,'')=''
            ) AS none_named
          FROM cards_master
    """),

    # Joins to image_health_check via the most recent check per card.
    "image_coverage": ("one", """
        WITH latest_health AS (
            SELECT DISTINCT ON (set_id, card_number)
                   set_id, card_number, status
              FROM image_health_check
             ORDER BY set_id, card_number, checked_at DESC
        )
        SELECT
            COUNT(*) AS total_cards,
            COUNT(lh.status) FILTER (WHERE lh.status='OK')          AS image_ok,
            COUNT(lh.status) FILTER (WHERE lh.status='PARTIAL')     AS image_partial,
            COUNT(lh.status) FILTER (WHERE lh.status='ALL_MISSING') AS image_all_missing,
            COUNT(lh.status) FILTER (WHERE lh.status='ALL_EMPTY')   AS image_all_empty,
            COUNT(lh.status) FILTER (WHERE lh.status='ALL_CORRUPT') AS image_all_corrupt,
            COUNT(lh.status) FILTER (WHERE lh.status='NO_PATHS')    AS image_no_paths,
            COUNT(*) FILTER (WHERE lh.status IS NULL)               AS not_yet_checked
          FROM cards_master c
     LEFT JOIN latest_health lh USING (set_id, card_number)
    """),

    # Top 20 sets where admin should focus completion effort first.
    "top_gap_sets": ("many", """
        SELECT set_id,
               COUNT(*) AS cards_in_set,
               COUNT(*) FILTER (WHERE COALESCE(name_en,'')='')  AS missing_en,
               COUNT(*) FILTER (WHERE COALESCE(name_kr,'')='')  AS missing_kr,
               COUNT(*) FILTER (WHERE COALESCE(name_jp,'')='')  AS missing_jp,
               COUNT(*) FILTER (WHERE COALESCE(name_chs,'')='') AS missing_chs,
               COUNT(*) FILTER (
                   WHERE jsonb_typeof(image_url_alt) <> 'array'
                      OR jsonb_array_length(image_url_alt) = 0
               ) AS missing_images
          FROM cards_master
         GROUP BY set_id
         ORDER BY (
                   COUNT(*) FILTER (WHERE COALESCE(name_en,'')='')  +
                   COUNT(*) FILTER (WHERE COALESCE(name_kr,'')='')  +
                   COUNT(*) FILTER (WHERE COALESCE(name_jp,'')='')  +
                   COUNT(*) FILTER (WHERE COALESCE(name_chs,'')='') +
                   COUNT(*) FILTER (
                       WHERE jsonb_typeof(image_url_alt) <> 'array'
                          OR jsonb_array_length(image_url_alt) = 0
                   )
                 ) DESC
         LIMIT 20
    """),

    # Inventory breakdown by rarity.
    "rarity_distribution": ("many", """
        SELECT COALESCE(NULLIF(rarity, ''), '<unknown>') AS rarity,
               COUNT(*) AS n
          FROM cards_master
         GROUP BY 1
         ORDER BY 2 DESC
         LIMIT 50
    """),

    # Suspected duplicates: same Pokémon + same printed number across
    # multiple sets that aren't already cross-aliased. Useful for
    # spotting accidental dual-imports.
    "duplicates": ("many", """
        SELECT pokedex_id,
               card_number,
               array_agg(DISTINCT set_id ORDER BY set_id) AS set_ids,
               COUNT(*) AS occurrences
          FROM cards_master
         WHERE pokedex_id IS NOT NULL
           AND COALESCE(card_number, '') <> ''
         GROUP BY pokedex_id, card_number
        HAVING COUNT(*) > 1
         ORDER BY occurrences DESC, pokedex_id
         LIMIT 50
    """),
}


def _row_to_dict(row, description) -> dict:
    """Normalise a fetchone/fetchall row to a column-name dict.
    Handles both tuple cursors (default) and dict cursors."""
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    cols = [d[0] for d in description]
    return dict(zip(cols, row))


# ── Worker ───────────────────────────────────────────────────────


class DataAnalystWorker(Worker):
    TASK_TYPE = "data_analysis"
    BATCH_SIZE = 10        # 6 reports total, plenty of headroom.
    IDLE_SLEEP_S = 300.0   # Nothing pending = sleep 5 min before re-poll.
    CLAIM_TIMEOUT_S = 600
    DEFAULT_RECHECK_AFTER_S = 24 * 3600   # Daily snapshot.

    # Allow tests to inject an alternative report catalogue.
    REPORTS = REPORTS

    def __init__(self, conn, *, recheck_after_s: int | None = None,
                 reports: dict | None = None, **kw):
        super().__init__(conn, **kw)
        self.recheck_after_s = recheck_after_s \
            if recheck_after_s is not None \
            else self.DEFAULT_RECHECK_AFTER_S
        if reports is not None:
            self.REPORTS = reports

    def seed(self) -> int:
        """Enqueue any report kind whose latest snapshot is missing
        or older than recheck_after_s. Each report kind = one task,
        keyed by report_kind so re-running while a snapshot is
        pending is a no-op."""
        # Build the (kind, payload) value list inline since we don't
        # want six round-trips for a 6-row insert.
        kinds = list(self.REPORTS.keys())
        if not kinds:
            return 0
        cutoff = int(time.time()) - self.recheck_after_s
        cur = self.conn.cursor()
        placeholders = ",".join(["(%s, %s, %s::jsonb, 'PENDING', %s)"] * len(kinds))
        params: list = []
        now = int(time.time())
        for kind in kinds:
            params.extend([
                "data_analysis",
                kind,
                json.dumps({"report_kind": kind}),
                now,
            ])
        # Skip kinds with a recent snapshot OR a pending task.
        cur.execute(f"""
            WITH proposed (task_type, task_key, payload, status, created_at) AS (
                VALUES {placeholders}
            )
            INSERT INTO bg_task_queue
                (task_type, task_key, payload, status, created_at)
            SELECT p.task_type, p.task_key, p.payload, p.status, p.created_at
              FROM proposed p
             WHERE NOT EXISTS (
                       SELECT 1 FROM data_analysis_report r
                        WHERE r.report_kind = p.task_key
                          AND r.generated_at > %s
                   )
            ON CONFLICT (task_type, task_key) DO NOTHING
        """, (*params, cutoff))
        n = cur.rowcount or 0
        self.conn.commit()
        log.info("[data_analyst] seed enqueued %d task(s)", n)
        return n

    def process(self, task: dict) -> dict:
        payload = task.get("payload") or {}
        kind = (payload.get("report_kind") or task.get("task_key") or "").strip()
        if kind not in self.REPORTS:
            raise WorkerError(
                f"data_analysis task {task['task_id']} has unknown "
                f"report_kind={kind!r} (known: {sorted(self.REPORTS)})"
            )
        shape, sql = self.REPORTS[kind]

        cur = self.conn.cursor()
        cur.execute(sql)

        if shape == "one":
            row = cur.fetchone()
            payload_out: Any = _row_to_dict(row, cur.description)
            rows_examined = int(payload_out.get("total_cards", 0)
                                or payload_out.get("total", 0)
                                or (1 if payload_out else 0))
        elif shape == "many":
            rows = cur.fetchall() or []
            payload_out = [_row_to_dict(r, cur.description) for r in rows]
            rows_examined = len(payload_out)
        else:
            raise WorkerError(
                f"data_analysis report {kind!r} has unknown shape {shape!r}"
            )

        cur.execute("""
            INSERT INTO data_analysis_report
                (report_kind, payload, rows_examined, generated_at)
            VALUES (%s, %s::jsonb, %s, %s)
        """, (
            kind,
            json.dumps(payload_out, ensure_ascii=False, default=_json_safe),
            rows_examined,
            int(time.time()),
        ))
        self.conn.commit()

        return {"report_kind": kind, "rows_examined": rows_examined}


def _json_safe(o):
    """Postgres returns Decimal / date / datetime / array — coerce
    them to JSON-friendly types when serialising the snapshot."""
    try:
        from decimal import Decimal
        if isinstance(o, Decimal):
            return float(o)
    except Exception:
        pass
    if isinstance(o, (set, tuple)):
        return list(o)
    if hasattr(o, "isoformat"):
        return o.isoformat()
    return str(o)
