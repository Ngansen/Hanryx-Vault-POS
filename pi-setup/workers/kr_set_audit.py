"""
workers/kr_set_audit.py — KR set completeness audit.

Walks the cloned ptcg-kr-db repo and compares the canonical (set_id,
card_number) inventory upstream against what landed in cards_master
for the same set_ids. Per-set diff lands in `kr_set_gap` so the
operator can see, at a glance, which sets are short and which numbers
are gone — the difference between "Iono SAR is missing" and "32
numbers are missing" matters when triaging a botched import.

The worker reads ONLY the locally cloned repo — no network, no API
calls. ptcg-kr-db has no top-level sets.json; the canonical set list
is implicit, derived by walking pokemon/, trainers/, energy/ JSON
files and collecting the (prodCode, number) pairs from each card's
version_infos[].

Why this lives as a worker instead of a CLI script:
  * It must run on a schedule (nightly during the off-hours).
  * It needs to coexist with image_health and image_mirror in the
    bg_task_queue retry / heartbeat machinery.
  * The admin dashboard is the natural consumer, and it already
    surfaces bg_worker_run notes.

Idempotent. UPSERT on (set_id) so re-running mid-day overwrites the
previous gap row with fresh numbers; no duplicate accumulation.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from .base import Worker, WorkerError

log = logging.getLogger("workers.kr_set_audit")

# Per-card files live under these three subdirs of the cloned repo.
# The repo's directory layout is part of the upstream contract — when
# pokemon-card.com adds a new supertype directory we'll need to adjust
# here, but it's been stable for several years.
CARD_SUBDIRS: tuple[str, ...] = ("pokemon", "trainers", "energy")

# Same convention as unified/local_images: USB_CARDS_ROOT (default
# /mnt/cards) holds every cloned dataset under <root>/<repo_name>/.
DEFAULT_KR_DB_ROOT = Path(
    os.environ.get("USB_CARDS_ROOT", "/mnt/cards")
) / "ptcg-kr-db"


def _normalise_number(raw: str) -> str:
    """Match import_kr_cards's normalisation: strip leading zeros so
    '001' and '1' compare equal. Empty becomes '0' to mirror the
    importer (which writes '0' for unnumbered promos)."""
    s = (raw or "").strip().lstrip("0")
    return s or "0"


def _walk_canonical(root: Path) -> dict[str, set[str]]:
    """Walk the cloned repo and return {set_id: {card_number, ...}}.

    Returns {} if the root doesn't exist or none of the subdirs do.
    Malformed JSON files are logged and skipped — one bad file must
    not abort an audit of 47 healthy sets."""
    out: dict[str, set[str]] = {}
    if not root.exists() or not root.is_dir():
        return out
    for sub in CARD_SUBDIRS:
        d = root / sub
        if not d.is_dir():
            continue
        for jp in d.glob("*.json"):
            try:
                raw = json.loads(jp.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                log.warning("[kr-audit] skipping %s: %s", jp.name, exc)
                continue
            cards = raw if isinstance(raw, list) else [raw]
            for card in cards:
                if not isinstance(card, dict):
                    continue
                versions = card.get("version_infos") or []
                if not isinstance(versions, list):
                    continue
                for v in versions:
                    if not isinstance(v, dict):
                        continue
                    sid = (v.get("prodCode") or "").strip().lower()
                    num = _normalise_number(v.get("number") or "")
                    if not sid:
                        # No prodCode → no comparable cards_master row.
                        # We could fall back to a synthetic 'unknown'
                        # bucket but that would just create permanent
                        # phantom gaps; better to skip silently.
                        continue
                    out.setdefault(sid, set()).add(num)
    return out


class KrSetAuditWorker(Worker):
    TASK_TYPE = "kr_set_audit"
    BATCH_SIZE = 1            # one full-run task per pass
    IDLE_SLEEP_S = 300.0      # 5 min idle — this isn't a hot loop
    CLAIM_TIMEOUT_S = 900     # 15 min ceiling; walk + UPSERT is fast

    def __init__(
        self,
        conn,
        *,
        kr_db_root: Optional[Path] = None,
        today_fn=None,
        **kw,
    ):
        super().__init__(conn, **kw)
        self._kr_db_root = Path(kr_db_root) if kr_db_root is not None \
            else DEFAULT_KR_DB_ROOT
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
            VALUES ('kr_set_audit', %s, '{}'::jsonb, 'PENDING', %s)
            ON CONFLICT (task_type, task_key) DO NOTHING
        """, (self._today_fn(), int(time.time())))
        n = cur.rowcount or 0
        self.conn.commit()
        log.info("[kr-audit] seed enqueued %d task(s)", n)
        return n

    def process(self, task: dict) -> dict:
        canonical = _walk_canonical(self._kr_db_root)
        if not self._kr_db_root.exists():
            return {"status": "NO_REPO",
                    "kr_db_root": str(self._kr_db_root),
                    "sets_audited": 0}
        if not canonical:
            # Repo is there but contains no parseable cards. Either a
            # half-cloned checkout or every JSON file is malformed.
            # Either way, refusing to audit beats writing zeros over
            # the previous gap report.
            return {"status": "EMPTY_REPO",
                    "kr_db_root": str(self._kr_db_root),
                    "sets_audited": 0}

        # Pull cards_master numbers ONLY for the sets we have a
        # canonical for. We could audit every set_id in cards_master,
        # but reporting "set bw1 has 0 expected, 102 extra" is noise
        # — those rows came from a different importer (English
        # spine, JP backfill) and don't belong in a KR audit.
        set_ids = sorted(canonical.keys())
        cur = self.conn.cursor()
        cur.execute("""
            SELECT set_id, ARRAY_AGG(card_number)
              FROM cards_master
             WHERE set_id = ANY(%s)
             GROUP BY set_id
        """, (set_ids,))
        actual: dict[str, set[str]] = {}
        for row in (cur.fetchall() or []):
            sid = row[0] if not isinstance(row, dict) else row.get("set_id")
            nums = row[1] if not isinstance(row, dict) else row.get("array_agg")
            actual[sid] = {(n or "").strip() for n in (nums or []) if n}

        now = int(time.time())
        total_missing = 0
        total_extra = 0
        for sid in set_ids:
            expected_set = canonical[sid]
            actual_set = actual.get(sid, set())
            missing = sorted(expected_set - actual_set,
                             key=_num_sort_key)
            extra = sorted(actual_set - expected_set,
                           key=_num_sort_key)
            total_missing += len(missing)
            total_extra += len(extra)
            cur.execute("""
                INSERT INTO kr_set_gap
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

        log.info("[kr-audit] sets=%d missing=%d extra=%d",
                 len(set_ids), total_missing, total_extra)
        return {
            "status": "OK",
            "sets_audited": len(set_ids),
            "total_missing": total_missing,
            "total_extra": total_extra,
        }


def _num_sort_key(n: str):
    """Numeric-aware sort: '1', '2', '10' instead of '1', '10', '2'.
    Falls back to string ordering for promo strings like 'PRE-001'."""
    s = (n or "").strip()
    try:
        return (0, int(s))
    except ValueError:
        return (1, s)
