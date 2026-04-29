"""
workers/base.py — shared framework for HanryxVault background helpers.

The Worker base class provides the boring-but-tricky bits so each
concrete helper can focus on its specific work:

  * SAFE CLAIM  — uses Postgres SELECT … FOR UPDATE SKIP LOCKED so
    multiple worker processes can run side-by-side without ever
    handing the same task to two workers.
  * HEARTBEAT   — tasks claimed but never completed (worker died,
    OOM, kill -9) are released back to PENDING after CLAIM_TIMEOUT_S
    by the reaper so they get retried instead of stuck forever.
  * RETRY       — failures bump `attempts`; once attempts >=
    max_attempts the task moves to FAILED and stops being claimed.
  * RUN LOG     — every batch records start/end/counts in
    `bg_worker_run` for the admin dashboard.

Concrete subclasses override:
  TASK_TYPE   (str)        — required, e.g. 'image_health'
  BATCH_SIZE  (int)        — how many tasks to claim per pass
  IDLE_SLEEP_S (float)     — how long to sleep when the queue is empty
                              before re-polling (loop mode only)
  CLAIM_TIMEOUT_S (int)    — after this many seconds, an unfinished
                              CLAIMED task is reaped back to PENDING
  seed(self) -> int        — optional: enqueue work based on DB state.
                              Returns count enqueued. Default: no-op.
  process(self, task)      — required: do the work. Return any dict;
                              raise on failure (gets recorded in
                              `last_error`, attempts incremented).

The framework is deliberately psycopg2-flavoured but every SQL
interaction goes through methods (claim_batch, complete, fail,
record_run) so tests can inject a FakeConn that records SQL and
canned-result fetchall() / fetchone() calls.
"""
from __future__ import annotations

import abc
import json
import logging
import os
import socket
import time
import traceback
from typing import Any

log = logging.getLogger("workers.base")

# How long (seconds) a CLAIMED task may stay in-flight before the reaper
# considers it abandoned and releases it back to PENDING. Conservative
# default — image_health is fast (<1 s/card) so 10 minutes leaves huge
# headroom; CLIP and OCR workers should override to ~30 minutes.
DEFAULT_CLAIM_TIMEOUT_S = 600


class WorkerError(Exception):
    """Raised by Worker.process() to signal a permanent failure that
    should NOT be retried (e.g. malformed payload). Other exceptions
    are treated as transient and counted toward max_attempts."""


class Worker(abc.ABC):
    TASK_TYPE: str = ""
    BATCH_SIZE: int = 50
    IDLE_SLEEP_S: float = 5.0
    CLAIM_TIMEOUT_S: int = DEFAULT_CLAIM_TIMEOUT_S
    DEFAULT_MAX_ATTEMPTS: int = 3

    def __init__(self, conn, *, worker_id: str | None = None,
                 batch_size: int | None = None):
        if not self.TASK_TYPE:
            raise ValueError(f"{type(self).__name__}.TASK_TYPE must be set")
        self.conn = conn
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        if batch_size is not None:
            self.BATCH_SIZE = batch_size

    # ── Public lifecycle ──────────────────────────────────────────

    def seed(self) -> int:
        """Enqueue tasks from current DB state. Default is a no-op so
        a worker that doesn't need seeding (e.g. one that's filled
        externally by another module) still works. Subclasses that
        do seed should INSERT … ON CONFLICT DO NOTHING for idempotency.
        Returns the number of new tasks enqueued."""
        return 0

    @abc.abstractmethod
    def process(self, task: dict) -> Any:
        """Do the work for ONE task. Receives a dict with keys:
            task_id, task_type, task_key, payload, attempts
        Returns anything truthy on success; raises on failure.
        Raise WorkerError for permanent failures (no retry)."""
        raise NotImplementedError

    def run_once(self) -> dict:
        """Reap any stale claims, claim a batch of pending tasks,
        process each, persist results. Returns a stats dict."""
        self.reap_stale()
        started_at = int(time.time())
        run_id = self._record_run_start(started_at)

        tasks = self.claim_batch(self.BATCH_SIZE)
        ok = fail = 0
        for t in tasks:
            try:
                self.process(t)
                self.complete(t["task_id"])
                ok += 1
            except WorkerError as e:
                # Permanent failure — burn all remaining attempts.
                self.fail(t["task_id"], str(e), permanent=True)
                fail += 1
                log.warning("[worker:%s] permanent failure on task %d: %s",
                            self.TASK_TYPE, t["task_id"], e)
            except Exception as e:  # noqa: BLE001 — transient by default
                tb = traceback.format_exc(limit=3)
                self.fail(t["task_id"], f"{e}\n{tb}", permanent=False)
                fail += 1
                log.warning("[worker:%s] transient failure on task %d "
                            "(attempt %d): %s",
                            self.TASK_TYPE, t["task_id"],
                            t.get("attempts", 0), e)

        ended_at = int(time.time())
        self._record_run_end(run_id, ended_at, len(tasks), ok, fail)
        return {
            "claimed":  len(tasks),
            "ok":       ok,
            "failed":   fail,
            "duration": ended_at - started_at,
        }

    def run_forever(self, *, max_idle_passes: int | None = None) -> dict:
        """Loop run_once forever. If `max_idle_passes` is set, exit
        after that many consecutive empty claims (useful for tests
        and one-shot 'drain the queue' invocations)."""
        idle = 0
        totals = {"claimed": 0, "ok": 0, "failed": 0, "passes": 0}
        while True:
            stats = self.run_once()
            totals["claimed"] += stats["claimed"]
            totals["ok"]      += stats["ok"]
            totals["failed"]  += stats["failed"]
            totals["passes"]  += 1
            if stats["claimed"] == 0:
                idle += 1
                if max_idle_passes is not None and idle >= max_idle_passes:
                    return totals
                time.sleep(self.IDLE_SLEEP_S)
            else:
                idle = 0

    # ── Queue operations (overridable for tests) ─────────────────

    def enqueue(self, task_key: str, payload: dict | None = None,
                priority: int = 100,
                max_attempts: int | None = None) -> bool:
        """Insert a single task. Returns True if newly inserted,
        False if an identical (task_type, task_key) already existed."""
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO bg_task_queue
                (task_type, task_key, payload, priority,
                 max_attempts, status, created_at)
            VALUES (%s, %s, %s::jsonb, %s, %s, 'PENDING', %s)
            ON CONFLICT (task_type, task_key) DO NOTHING
            RETURNING task_id
        """, (
            self.TASK_TYPE,
            task_key,
            json.dumps(payload or {}),
            priority,
            max_attempts if max_attempts is not None
                else self.DEFAULT_MAX_ATTEMPTS,
            int(time.time()),
        ))
        return cur.fetchone() is not None

    def claim_batch(self, n: int) -> list[dict]:
        """Atomically move up to n PENDING tasks to CLAIMED and return
        them. SKIP LOCKED prevents two workers from grabbing the same
        row even mid-transaction."""
        cur = self.conn.cursor()
        cur.execute("""
            WITH claimable AS (
                SELECT task_id FROM bg_task_queue
                 WHERE task_type = %s
                   AND status    = 'PENDING'
                   AND attempts  < max_attempts
                 ORDER BY priority, created_at
                 LIMIT %s
                 FOR UPDATE SKIP LOCKED
            )
            UPDATE bg_task_queue q
               SET status     = 'CLAIMED',
                   claimed_at = %s,
                   claimed_by = %s,
                   attempts   = q.attempts + 1
              FROM claimable c
             WHERE q.task_id = c.task_id
            RETURNING q.task_id, q.task_type, q.task_key,
                      q.payload, q.attempts
        """, (self.TASK_TYPE, n, int(time.time()), self.worker_id))
        rows = cur.fetchall() or []
        self.conn.commit()
        # rows may be tuples or dict-rows depending on cursor type;
        # normalise to dicts so process() callers don't care.
        out = []
        for r in rows:
            if isinstance(r, dict):
                out.append(dict(r))
            else:
                tid, ttype, tkey, payload, attempts = r
                out.append({
                    "task_id":   tid,
                    "task_type": ttype,
                    "task_key":  tkey,
                    "payload":   payload if isinstance(payload, dict)
                                  else json.loads(payload or "{}"),
                    "attempts":  attempts,
                })
        return out

    def complete(self, task_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute("""
            UPDATE bg_task_queue
               SET status       = 'DONE',
                   completed_at = %s,
                   last_error   = ''
             WHERE task_id = %s
        """, (int(time.time()), task_id))
        self.conn.commit()

    def fail(self, task_id: int, err: str, *, permanent: bool) -> None:
        """Mark a task failed. If permanent, force attempts up to
        max_attempts so it never gets re-claimed. Otherwise leave it
        PENDING (with bumped attempts) for natural retry until
        max_attempts is hit, then mark FAILED."""
        cur = self.conn.cursor()
        if permanent:
            cur.execute("""
                UPDATE bg_task_queue
                   SET status       = 'FAILED',
                       attempts     = max_attempts,
                       completed_at = %s,
                       last_error   = %s
                 WHERE task_id = %s
            """, (int(time.time()), err[:4000], task_id))
        else:
            cur.execute("""
                UPDATE bg_task_queue
                   SET status     = CASE
                                      WHEN attempts >= max_attempts
                                        THEN 'FAILED'
                                      ELSE 'PENDING'
                                    END,
                       completed_at = CASE
                                        WHEN attempts >= max_attempts
                                          THEN %s
                                        ELSE NULL
                                      END,
                       last_error = %s
                 WHERE task_id = %s
            """, (int(time.time()), err[:4000], task_id))
        self.conn.commit()

    def reap_stale(self) -> int:
        """Release CLAIMED tasks that have been in-flight longer than
        CLAIM_TIMEOUT_S. Returns count released."""
        cutoff = int(time.time()) - self.CLAIM_TIMEOUT_S
        cur = self.conn.cursor()
        cur.execute("""
            UPDATE bg_task_queue
               SET status     = 'PENDING',
                   claimed_at = NULL,
                   claimed_by = '',
                   last_error = COALESCE(NULLIF(last_error, ''), '') ||
                                CASE WHEN last_error <> '' THEN ' | ' ELSE '' END ||
                                'reaped after timeout'
             WHERE task_type = %s
               AND status    = 'CLAIMED'
               AND claimed_at < %s
        """, (self.TASK_TYPE, cutoff))
        n = cur.rowcount or 0
        self.conn.commit()
        if n:
            log.info("[worker:%s] reaped %d stale CLAIMED task(s)",
                     self.TASK_TYPE, n)
        return n

    # ── Run-log helpers ──────────────────────────────────────────

    def _record_run_start(self, started_at: int) -> int:
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO bg_worker_run (worker_type, worker_id, started_at)
            VALUES (%s, %s, %s)
            RETURNING run_id
        """, (self.TASK_TYPE, self.worker_id, started_at))
        row = cur.fetchone()
        self.conn.commit()
        # row may be tuple or dict
        if isinstance(row, dict):
            return int(row["run_id"])
        return int(row[0]) if row else 0

    def _record_run_end(self, run_id: int, ended_at: int,
                        claimed: int, ok: int, failed: int,
                        notes: str = "") -> None:
        if not run_id:
            return
        cur = self.conn.cursor()
        cur.execute("""
            UPDATE bg_worker_run
               SET ended_at      = %s,
                   items_claimed = %s,
                   items_ok      = %s,
                   items_failed  = %s,
                   notes         = %s
             WHERE run_id = %s
        """, (ended_at, claimed, ok, failed, notes[:4000], run_id))
        self.conn.commit()
