"""
workers/en_set_audit.py — EN set completeness audit.

Mirrors the kr_set_audit / zh_set_audit pattern, but EN's source-of-truth
isn't a filesystem checkout. EN is structurally different from KR and ZH:

  * KR has a Ngansen fork of `ptcg-kr-db` cloned to /mnt/cards/ptcg-kr-db
    that the KR audit walks file-by-file.
  * SC has Ngansen/PTCG-CHS-Datasets cloned to /mnt/cards/PTCG-CHS-Datasets
    that the ZH audit walks similarly.
  * EN has no equivalent local repo — the canonical EN catalogue lives in
    the `src_tcgdex_multi` table (populated by import_tcgdex.py against the
    public TCGdex API). For every card in every set there is one row whose
    `names` JSONB has an "en" key.

So this worker reads canonical (set_id, card_local_id) pairs from
`src_tcgdex_multi` and compares them against the (set_id, card_number)
pairs that landed in `cards_master` with a non-empty `name_en`. The diff
goes into `en_set_gap` so the operator can see at a glance which sets
have English data and which are short — same JSON shape as kr_set_gap so
existing dashboards work unchanged.

Why this lives as a worker instead of a CLI script:
  * It must run on a schedule (nightly, off-hours).
  * It needs to coexist with image_health and image_mirror in the
    bg_task_queue retry / heartbeat machinery.
  * The admin status endpoint is the natural consumer.

Idempotent. UPSERT on (set_id) so re-running mid-day overwrites the
previous gap row with fresh numbers; no duplicate accumulation.
"""
from __future__ import annotations

import datetime
import json
import logging
import time
from typing import Any, Optional

from .base import Worker, WorkerError

log = logging.getLogger("workers.en_set_audit")


def _normalise_number(raw: str) -> str:
    """Match cards_master's storage convention: strip leading zeros so
    '001' and '1' compare equal. Empty becomes '0' to mirror the
    KR audit's normalisation. Strings like 'TG01' or 'SV-P-001' are
    returned verbatim — only the leading zeros of plain ints are stripped.
    """
    s = (raw or "").strip()
    if not s:
        return "0"
    # Plain digits → strip leading zeros; otherwise verbatim.
    if s.isdigit():
        return s.lstrip("0") or "0"
    return s


def _read_canonical(cur) -> dict[str, set[str]]:
    """Read TCGdex's EN catalogue: {set_id: {card_local_id, ...}}.

    Only includes rows where the `names` JSONB has an "en" key — TCGdex
    publishes some cards in JP-only or KR-only at first, and we don't
    want to flag those as 'EN missing' in cards_master.
    """
    cur.execute("""
        SELECT set_id, ARRAY_AGG(card_local_id)
          FROM src_tcgdex_multi
         WHERE names ? 'en'
           AND set_id <> ''
         GROUP BY set_id
    """)
    out: dict[str, set[str]] = {}
    for row in (cur.fetchall() or []):
        sid = row[0] if not isinstance(row, dict) else row.get("set_id")
        nums = row[1] if not isinstance(row, dict) else row.get("array_agg")
        if not sid:
            continue
        out[sid] = {_normalise_number(n) for n in (nums or []) if n}
    return out


def _read_actual(cur, set_ids: list[str]) -> dict[str, set[str]]:
    """Read cards_master EN coverage for the given canonical set_ids only.

    A row counts as 'has EN' iff `name_en` is non-empty. Rows where
    name_en is NULL or '' came from a non-EN spine (e.g. JP-only backfill)
    and shouldn't count toward EN coverage.
    """
    if not set_ids:
        return {}
    cur.execute("""
        SELECT set_id, ARRAY_AGG(card_number)
          FROM cards_master
         WHERE set_id = ANY(%s)
           AND name_en IS NOT NULL
           AND name_en <> ''
         GROUP BY set_id
    """, (set_ids,))
    actual: dict[str, set[str]] = {}
    for row in (cur.fetchall() or []):
        sid = row[0] if not isinstance(row, dict) else row.get("set_id")
        nums = row[1] if not isinstance(row, dict) else row.get("array_agg")
        actual[sid] = {_normalise_number(n) for n in (nums or []) if n}
    return actual


class EnSetAuditWorker(Worker):
    TASK_TYPE = "en_set_audit"
    BATCH_SIZE = 1            # one full-run task per pass
    IDLE_SLEEP_S = 300.0      # 5 min idle — this isn't a hot loop
    CLAIM_TIMEOUT_S = 900     # 15 min ceiling; SELECT + UPSERT is fast

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
            VALUES ('en_set_audit', %s, '{}'::jsonb, 'PENDING', %s)
            ON CONFLICT (task_type, task_key) DO NOTHING
        """, (self._today_fn(), int(time.time())))
        n = cur.rowcount or 0
        self.conn.commit()
        log.info("[en-audit] seed enqueued %d task(s)", n)
        return n

    def process(self, task: dict) -> dict:
        cur = self.conn.cursor()
        canonical = _read_canonical(cur)
        if not canonical:
            # src_tcgdex_multi is empty (or has no EN rows). Either
            # import_tcgdex hasn't run yet or the API was down on the
            # last refresh. Either way, refusing to audit beats writing
            # zeros over the previous gap report.
            log.warning("[en-audit] src_tcgdex_multi has no EN rows — skip")
            return {"status": "EMPTY_SOURCE", "sets_audited": 0}

        # Pull cards_master numbers ONLY for the sets we have a
        # canonical for. Auditing every cards_master set_id would
        # surface noise: 'set bw1: 0 expected, 102 extra' just means
        # we have a row from a non-EN spine that TCGdex doesn't list.
        set_ids = sorted(canonical.keys())
        actual = _read_actual(cur, set_ids)

        now = int(time.time())
        total_missing = 0
        total_extra = 0
        for sid in set_ids:
            expected_set = canonical[sid]
            actual_set = actual.get(sid, set())
            missing = sorted(expected_set - actual_set, key=_num_sort_key)
            extra = sorted(actual_set - expected_set, key=_num_sort_key)
            total_missing += len(missing)
            total_extra += len(extra)
            cur.execute("""
                INSERT INTO en_set_gap
                    (set_id, expected_count, actual_count,
                     missing_numbers, extra_numbers, audited_at)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s)
                ON CONFLICT (set_id) DO UPDATE SET
                    expected_count  = EXCLUDED.expected_count,
                    actual_count    = EXCLUDED.actual_count,
                    missing_numbers = EXCLUDED.missing_numbers,
                    extra_numbers   = EXCLUDED.extra_numbers,
                    audited_at      = EXCLUDED.audited_at
            """, (sid, len(expected_set), len(actual_set),
                  json.dumps(missing), json.dumps(extra), now))
        self.conn.commit()

        log.info("[en-audit] sets=%d missing=%d extra=%d",
                 len(set_ids), total_missing, total_extra)
        return {
            "status": "OK",
            "sets_audited": len(set_ids),
            "total_missing": total_missing,
            "total_extra": total_extra,
        }


def _num_sort_key(n: str):
    """Numeric-aware sort: '1', '2', '10' instead of '1', '10', '2'.
    Falls back to string ordering for promo strings like 'SV-P-001'."""
    s = (n or "").strip()
    try:
        return (0, int(s))
    except ValueError:
        return (1, s)
