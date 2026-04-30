"""
workers/cards_master_audit.py — cards_master integrity audit.

Sibling of en_set_audit / kr_set_audit / zh_set_audit, but instead of
checking SET-level completeness (which sets are missing how many cards)
this worker checks ROW-level invariant violations across the unified
cards_master table itself:

  * `all_names_blank`     — every one of the 5 user-facing name_*
                            columns (en/kr/jp/chs/cht) is empty. Such
                            a row is invisible to the resolver and to
                            search; it's dead weight.
  * `no_set_id`           — set_id='' (NOT NULL but the column allows
                            ''). Resolver can't pin tier-1 hits, and
                            cross-region aliasing breaks.
  * `no_card_number`      — card_number=''. Same problem as above.
  * `duplicate_identity`  — more than one row sharing
                            (set_id, card_number, variant_code).
                            cards_master enforces this with a UNIQUE
                            constraint, so this should ALWAYS be 0;
                            a non-zero result means the constraint
                            was dropped during a migration or the
                            schema is corrupted. Cheap canary.

Each violation type gets one row in `cards_master_gap` with a count
and a JSONB sample (up to 20 example master_ids or composite keys),
so the operator can jump straight to the offending row in the admin
UI without re-running the audit. UPSERT-on-violation_type means re-
running mid-day overwrites stale numbers and writing 0 with an empty
sample list is meaningful — it tells the dashboard "this category is
currently clean".

Idempotent. EMPTY_SOURCE short-circuit: if cards_master itself is
empty (fresh Pi, no imports yet) we refuse to write — better the
dashboard show "no audit yet" than "0 violations of every kind!"
which would be misleading.
"""
from __future__ import annotations

import datetime
import json
import logging
import time
from typing import Any, Callable

from .base import Worker

log = logging.getLogger("workers.cards_master_audit")

SAMPLE_LIMIT = 20  # how many example keys to record per violation type


# Each entry: (violation_type, count_sql, sample_sql, key_fn).
# key_fn maps a sample row tuple to the stringified key we store in
# JSONB. master_id for single-row violations; pipe-joined coordinates
# for multi-row group violations like duplicate_identity.
VIOLATIONS: list[tuple[str, str, str, Callable[[Any], Any]]] = [
    (
        "all_names_blank",
        """
        SELECT COUNT(*) FROM cards_master
         WHERE name_en  = ''
           AND name_kr  = ''
           AND name_jp  = ''
           AND name_chs = ''
           AND name_cht = ''
        """,
        """
        SELECT master_id FROM cards_master
         WHERE name_en  = ''
           AND name_kr  = ''
           AND name_jp  = ''
           AND name_chs = ''
           AND name_cht = ''
         ORDER BY master_id
         LIMIT %s
        """,
        lambda row: int(row[0] if not isinstance(row, dict)
                        else row.get("master_id")),
    ),
    (
        "no_set_id",
        "SELECT COUNT(*) FROM cards_master WHERE set_id = ''",
        """
        SELECT master_id FROM cards_master
         WHERE set_id = ''
         ORDER BY master_id
         LIMIT %s
        """,
        lambda row: int(row[0] if not isinstance(row, dict)
                        else row.get("master_id")),
    ),
    (
        "no_card_number",
        "SELECT COUNT(*) FROM cards_master WHERE card_number = ''",
        """
        SELECT master_id FROM cards_master
         WHERE card_number = ''
         ORDER BY master_id
         LIMIT %s
        """,
        lambda row: int(row[0] if not isinstance(row, dict)
                        else row.get("master_id")),
    ),
    (
        "duplicate_identity",
        """
        SELECT COUNT(*) FROM (
            SELECT set_id, card_number, variant_code
              FROM cards_master
             GROUP BY set_id, card_number, variant_code
             HAVING COUNT(*) > 1
        ) sub
        """,
        """
        SELECT set_id, card_number, variant_code
          FROM cards_master
         GROUP BY set_id, card_number, variant_code
         HAVING COUNT(*) > 1
         ORDER BY set_id, card_number, variant_code
         LIMIT %s
        """,
        lambda row: (
            f"{row[0]}|{row[1]}|{row[2]}" if not isinstance(row, dict)
            else f"{row.get('set_id')}|{row.get('card_number')}|{row.get('variant_code')}"
        ),
    ),
]


def _has_any_rows(cur) -> bool:
    """Cheap LIMIT-1 probe so we can short-circuit when cards_master
    is empty (fresh Pi, no importer has run). Avoids stamping every
    violation row with a misleading 0 count."""
    cur.execute("SELECT 1 FROM cards_master LIMIT 1")
    row = cur.fetchone()
    return row is not None


class CardsMasterAuditWorker(Worker):
    TASK_TYPE = "cards_master_audit"
    BATCH_SIZE = 1            # one full-run task per pass
    IDLE_SLEEP_S = 300.0      # 5 min idle — daily-ish cadence is fine
    CLAIM_TIMEOUT_S = 900     # 15 min ceiling; 4 COUNT(*) + 4 LIMIT 20 is fast

    def __init__(self, conn, *, today_fn=None, **kw):
        super().__init__(conn, **kw)
        # Injectable for deterministic seed tests.
        self._today_fn = today_fn or (
            lambda: datetime.datetime.utcnow().strftime("%Y-%m-%d"))

    # ── Worker contract ──────────────────────────────────────────

    def seed(self) -> int:
        """Enqueue one audit task per UTC day. ON CONFLICT collapses
        re-seed runs to a no-op so a 5-minute orchestrator tick
        doesn't pile up duplicate audits."""
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO bg_task_queue
                (task_type, task_key, payload, status, created_at)
            VALUES ('cards_master_audit', %s, '{}'::jsonb, 'PENDING', %s)
            ON CONFLICT (task_type, task_key) DO NOTHING
        """, (self._today_fn(), int(time.time())))
        n = cur.rowcount or 0
        self.conn.commit()
        log.info("[cm-audit] seed enqueued %d task(s)", n)
        return n

    def process(self, task: dict) -> dict:
        cur = self.conn.cursor()
        if not _has_any_rows(cur):
            # Fresh Pi or post-truncate. Refuse to audit: writing
            # zeros across every violation type would be technically
            # "0 violations" but factually misleading.
            log.warning("[cm-audit] cards_master is empty — skip")
            return {"status": "EMPTY_SOURCE", "violations_audited": 0}

        now = int(time.time())
        results: dict[str, int] = {}
        for vtype, count_sql, sample_sql, key_fn in VIOLATIONS:
            cur.execute(count_sql)
            row = cur.fetchone() or (0,)
            count = int(row[0] if not isinstance(row, dict)
                        else next(iter(row.values())))

            samples: list[Any] = []
            if count > 0:
                cur.execute(sample_sql, (SAMPLE_LIMIT,))
                samples = [key_fn(r) for r in (cur.fetchall() or [])]

            cur.execute("""
                INSERT INTO cards_master_gap
                    (violation_type, violation_count, sample_keys, audited_at)
                VALUES (%s, %s, %s::jsonb, %s)
                ON CONFLICT (violation_type) DO UPDATE SET
                    violation_count = EXCLUDED.violation_count,
                    sample_keys     = EXCLUDED.sample_keys,
                    audited_at      = EXCLUDED.audited_at
            """, (vtype, count, json.dumps(samples), now))
            results[vtype] = count

        self.conn.commit()

        total = sum(results.values())
        log.info("[cm-audit] audited %d violation type(s), total %d issue(s)",
                 len(VIOLATIONS), total)
        return {
            "status": "OK",
            "violations_audited": len(VIOLATIONS),
            "total_violations": total,
            "by_type": results,
        }
