#!/usr/bin/env python3
"""
discovery_dispatch.py — pulls pending rows from `discovery_queue`,
dispatches them to the right importer(s), and writes audit rows to
`discovery_log` (D3).

This is the second half of Continuous Discovery v1. The probe
(`discover_new_sets.py`) detects what's new upstream; this module
actually fetches it and rolls it into `cards_master`. Splitting the
two means probes are cheap (one HTTP call per language, ~1 s) while
the heavy lifting (TCGdex full re-import is ~2 min) only happens when
there's actually something to import.

Routing by `kind` (the `payload->>'set_id'` and `languages` fields
control which importers fire):

    kind='set'    → import_tcgdex.py (always, multilingual)
                  + import_jp_pokemoncardcom.py (if 'ja' in languages
                    AND looks JP-exclusive)
                  + import_kr_cards.py            (if 'ko' in languages)
                  + import_chs_cards.py           (if 'zh-cn' in languages)
                  + build_cards_master.py        (always, after the above)

    kind='report' → search src_* tables for the query string in EN/JP/
                    KR/CHS/CHT name columns. If found, mark resolved
                    with master_id. If not, mark 'noop' so a future
                    set discovery has a chance to surface it.

Backoff: 1h / 6h / 24h between retries; row marked `failed` after the
third attempt (surfaces in the admin UI for manual triage).

Safe to run concurrently — uses `SELECT ... FOR UPDATE SKIP LOCKED`
so two dispatcher instances will divide the queue between themselves
rather than fight over rows.

CLI:
    python3 discovery_dispatch.py                # process up to BATCH_LIMIT rows
    python3 discovery_dispatch.py --batch 5      # cap at 5 rows this tick
    python3 discovery_dispatch.py --kind set     # only process set discoveries
    python3 discovery_dispatch.py --dry-run      # log decisions, do nothing
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from typing import Any

import psycopg2

log = logging.getLogger("discovery_dispatch")

# Cap how many queue rows we drain per tick. Keeps any single run bounded
# (an import_tcgdex pass is ~2 min; six rows = ~12 min worst case, well
# under the orchestrator's 1 h job timeout).
BATCH_LIMIT = 6

# Per-importer subprocess timeout. Generous because TCGdex full re-import
# can run long over flaky trade-show WiFi.
IMPORT_TIMEOUT_SEC = 30 * 60

# Which directory to look for importer modules in. The Docker container
# mounts pi-setup at /app; on the Pi outside Docker we fall back to the
# script's own directory.
APP_DIR = os.environ.get("DISCOVERY_APP_DIR") or (
    "/app" if os.path.isdir("/app") and os.path.exists("/app/import_tcgdex.py")
    else os.path.dirname(os.path.abspath(__file__))
)

# Backoff schedule (in milliseconds). Index = attempts already made.
# Beyond the third attempt the row is marked `failed` for manual triage.
BACKOFF_MS = [
    1 * 60 * 60 * 1000,    # 1 h after attempt 1
    6 * 60 * 60 * 1000,    # 6 h after attempt 2
    24 * 60 * 60 * 1000,   # 24 h after attempt 3 (then `failed`)
]
MAX_ATTEMPTS = len(BACKOFF_MS)


# ─── Subprocess plumbing ───────────────────────────────────────────────────

def _run_importer(module_name: str, args: list[str] | None = None) -> tuple[bool, str]:
    """Run an importer module as a subprocess. Returns (ok, stderr_tail)."""
    script = os.path.join(APP_DIR, module_name)
    if not os.path.exists(script):
        return False, f"importer not found at {script}"
    cmd = [sys.executable, script] + (args or [])
    log.info("[run] %s", " ".join(cmd))
    started = time.time()
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=IMPORT_TIMEOUT_SEC,
        )
        dur = time.time() - started
        if result.returncode != 0:
            tail = (result.stderr or "")[-1500:]
            log.error("[run] %s exited %d after %.1fs: %s",
                      module_name, result.returncode, dur, tail[-300:])
            return False, tail
        log.info("[run] %s OK in %.1fs", module_name, dur)
        return True, ""
    except subprocess.TimeoutExpired:
        return False, f"timeout after {IMPORT_TIMEOUT_SEC}s"
    except Exception as e:
        return False, f"subprocess crashed: {e}"


# ─── Queue helpers ─────────────────────────────────────────────────────────

def _claim_pending(cur, batch: int, kind_filter: str | None) -> list[dict]:
    """Atomically claim up to `batch` pending rows whose backoff has elapsed.

    Uses `FOR UPDATE SKIP LOCKED` so concurrent dispatchers cooperate
    rather than collide. Returns the claimed rows (status set to 'running').
    """
    where_kind = ""
    params: list[Any] = [int(time.time() * 1000), batch]
    if kind_filter:
        where_kind = "AND kind = %s "
        params = [int(time.time() * 1000), kind_filter, batch]

    cur.execute(
        f"""
        WITH claimed AS (
            SELECT id
              FROM discovery_queue
             WHERE status = 'pending'
               AND next_attempt_at <= %s
               {where_kind}
             ORDER BY discovered_at
             LIMIT %s
             FOR UPDATE SKIP LOCKED
        )
        UPDATE discovery_queue q
           SET status   = 'running',
               attempts = q.attempts + 1
          FROM claimed c
         WHERE q.id = c.id
        RETURNING q.id, q.kind, q.payload, q.source, q.attempts, q.reporter
        """,
        params,
    )
    rows = []
    for rid, kind, payload, source, attempts, reporter in cur.fetchall():
        rows.append({
            "id": rid, "kind": kind,
            "payload": payload if isinstance(payload, dict) else (json.loads(payload) if payload else {}),
            "source": source, "attempts": attempts, "reporter": reporter,
        })
    return rows


def _write_log(cur, queue_id: int, source_tried: str, outcome: str,
               cards_added: int, duration_ms: int, note: str = "") -> None:
    cur.execute(
        """
        INSERT INTO discovery_log
            (queue_id, attempted_at, duration_ms, source_tried,
             outcome, cards_added, note)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (queue_id, int(time.time() * 1000), duration_ms,
         source_tried, outcome, cards_added, note[:2000]),
    )


def _mark_resolved(cur, queue_id: int, master_id: int | None, cards_added: int) -> None:
    cur.execute(
        """
        UPDATE discovery_queue
           SET status = 'resolved',
               resolved_at = %s,
               resolved_master_id = %s,
               last_error = ''
         WHERE id = %s
        """,
        (int(time.time() * 1000), master_id, queue_id),
    )
    log.info("[resolve] queue_id=%s cards_added=%s", queue_id, cards_added)


def _mark_noop(cur, queue_id: int, note: str) -> None:
    cur.execute(
        """
        UPDATE discovery_queue
           SET status = 'noop',
               resolved_at = %s,
               last_error = %s
         WHERE id = %s
        """,
        (int(time.time() * 1000), note[:500], queue_id),
    )


def _mark_backoff_or_fail(cur, queue_id: int, attempts: int, err: str) -> None:
    """If we have retries left, push next_attempt_at out and re-pend the row.
    Otherwise mark `failed` so the admin UI can surface it for triage.
    """
    if attempts >= MAX_ATTEMPTS:
        cur.execute(
            """
            UPDATE discovery_queue
               SET status = 'failed',
                   last_error = %s
             WHERE id = %s
            """,
            (err[:500], queue_id),
        )
        log.error("[fail] queue_id=%s after %d attempts: %s",
                  queue_id, attempts, err[:200])
    else:
        # attempts is 1-based after the claim incremented it; index into
        # BACKOFF_MS is attempts-1 (next backoff is for the next attempt).
        delay_ms = BACKOFF_MS[min(attempts - 1, MAX_ATTEMPTS - 1)]
        cur.execute(
            """
            UPDATE discovery_queue
               SET status = 'pending',
                   next_attempt_at = %s,
                   last_error = %s
             WHERE id = %s
            """,
            (int(time.time() * 1000) + delay_ms, err[:500], queue_id),
        )
        log.warning("[backoff] queue_id=%s attempt %d/%d, retry in %d min: %s",
                    queue_id, attempts, MAX_ATTEMPTS,
                    delay_ms // 60_000, err[:200])


# ─── Per-kind handlers ─────────────────────────────────────────────────────

def _count_master_rows(cur, set_id: str) -> int:
    cur.execute("SELECT COUNT(*) FROM cards_master WHERE set_id = %s", (set_id,))
    row = cur.fetchone()
    return int(row[0]) if row else 0


def _looks_jp_exclusive(payload: dict) -> bool:
    """Heuristic: only Japanese name populated, no English release."""
    return bool(payload.get("name_ja")) and not payload.get("name_en")


def _handle_set(cur, db_conn, row: dict, dry_run: bool) -> tuple[str, int, str]:
    """Run the right importers for a kind='set' discovery.

    Returns (outcome, cards_added, note).
    Outcomes: 'resolved' | 'error'.
    """
    payload = row["payload"]
    set_id = (payload.get("set_id") or "").strip()
    langs  = payload.get("languages") or []
    if not set_id:
        return "error", 0, "payload.set_id missing"

    # Snapshot existing master row count for this set so we can report
    # how many cards the dispatcher actually added.
    before = _count_master_rows(cur, set_id)

    plan: list[tuple[str, list[str]]] = []
    plan.append(("import_tcgdex.py", []))   # always — multilingual

    if "ja" in langs and _looks_jp_exclusive(payload):
        plan.append(("import_jp_pokemoncardcom.py", []))
    if "ko" in langs:
        plan.append(("import_kr_cards.py", []))
    if "zh-cn" in langs:
        plan.append(("import_chs_cards.py", []))

    plan.append(("build_cards_master.py", []))

    if dry_run:
        steps = " → ".join(p[0] for p in plan)
        return "resolved", 0, f"DRY-RUN would run: {steps}"

    sources_tried = []
    for module, args in plan:
        ok, err = _run_importer(module, args)
        sources_tried.append(module)
        if not ok:
            return "error", 0, f"{module} failed: {err[-300:]}"

    # Re-count after the rebuild — must reconnect to see committed changes
    # from the subprocess (the cur is on its own transaction).
    db_conn.commit()
    after = _count_master_rows(cur, set_id)
    cards_added = max(0, after - before)
    note = f"sources: {', '.join(sources_tried)}; rows {before}→{after}"
    return "resolved", cards_added, note


def _handle_report(cur, row: dict, dry_run: bool) -> tuple[str, int, str]:
    """Operator-driven 'I searched for X and got nothing' report.

    For v1 we just check whether the query string now matches any name in
    the unified cards_master (a recent set discovery may have added it).
    If yes → resolved with master_id; if no → noop and try again next tick.
    """
    payload = row["payload"]
    q = (payload.get("query") or "").strip()
    if not q:
        return "noop", 0, "payload.query missing"

    if dry_run:
        return "noop", 0, "DRY-RUN report check skipped"

    cur.execute(
        """
        SELECT master_id
          FROM cards_master
         WHERE name_en  ILIKE %s
            OR name_kr  ILIKE %s
            OR name_jp  ILIKE %s
            OR name_chs ILIKE %s
            OR name_cht ILIKE %s
         LIMIT 1
        """,
        tuple([f"%{q}%"] * 5),
    )
    found = cur.fetchone()
    if found:
        return "resolved", 1, f"matched master_id={found[0]} for '{q}'"
    return "noop", 0, f"no match for '{q}' in cards_master yet"


# ─── Main loop ─────────────────────────────────────────────────────────────

def dispatch(db_conn, *, batch: int = BATCH_LIMIT,
             kind_filter: str | None = None,
             dry_run: bool = False) -> dict:
    cur = db_conn.cursor()
    claimed = _claim_pending(cur, batch=batch, kind_filter=kind_filter)
    db_conn.commit()  # release the FOR UPDATE locks ASAP

    if not claimed:
        log.info("[dispatch] no pending rows due for processing")
        return {"claimed": 0, "resolved": 0, "noop": 0,
                "errored": 0, "failed_terminal": 0}

    log.info("[dispatch] claimed %d row(s): %s",
             len(claimed),
             ", ".join(f"#{r['id']}({r['kind']})" for r in claimed))

    counts = {"resolved": 0, "noop": 0, "errored": 0, "failed_terminal": 0}

    for row in claimed:
        rid = row["id"]
        started = time.time()
        try:
            if row["kind"] == "set":
                outcome, cards_added, note = _handle_set(cur, db_conn, row, dry_run)
            elif row["kind"] == "report":
                outcome, cards_added, note = _handle_report(cur, row, dry_run)
            else:
                outcome, cards_added, note = "error", 0, f"unknown kind '{row['kind']}'"
        except Exception as e:
            outcome, cards_added, note = "error", 0, f"handler crashed: {e}"

        dur_ms = int((time.time() - started) * 1000)
        _write_log(cur, rid, row["source"] or "tcgdex",
                   outcome, cards_added, dur_ms, note)

        if outcome == "resolved":
            _mark_resolved(cur, rid, master_id=None, cards_added=cards_added)
            counts["resolved"] += 1
        elif outcome == "noop":
            _mark_noop(cur, rid, note)
            counts["noop"] += 1
        else:
            _mark_backoff_or_fail(cur, rid, row["attempts"], note)
            if row["attempts"] >= MAX_ATTEMPTS:
                counts["failed_terminal"] += 1
            else:
                counts["errored"] += 1

        db_conn.commit()

    summary = {"claimed": len(claimed), **counts}
    log.info("[dispatch] done: %s", summary)
    return summary


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--batch", type=int, default=BATCH_LIMIT,
                    help=f"Max rows to process this tick (default {BATCH_LIMIT})")
    ap.add_argument("--kind", choices=["set", "report"],
                    help="Only process this kind")
    ap.add_argument("--dry-run", action="store_true",
                    help="Log what would happen, don't run any importer")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    with psycopg2.connect(url) as conn:
        result = dispatch(conn, batch=args.batch,
                          kind_filter=args.kind, dry_run=args.dry_run)

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
