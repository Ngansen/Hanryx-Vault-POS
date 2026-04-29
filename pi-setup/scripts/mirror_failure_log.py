"""
scripts/mirror_failure_log.py — persistent failure log for the
sync_card_mirror Phase B/C downloader.

Before this module existed the mirror downloader logged failures to
debug only — operators had no historical record of WHICH URLs were
failing or HOW OFTEN. A single 1-byte CDN error stub on a marquee
KR card could go unnoticed until a customer asked for it at the
booth.

This module wraps a single function — `record_mirror_outcome()` —
that the downloader calls after every _download() return value.
The semantics are intentionally tiny:

  * Success after a previous failure → flip resolved_at to NOW,
    keep first_seen_at and attempt_count for historical curiosity.
  * Success on a never-failed URL → no-op (no insert; we only
    track URLs that have been broken at least once, otherwise the
    table would explode to ~120k rows on a clean Phase C run).
  * Failure → upsert: insert with attempt_count=1 on first sight,
    increment attempt_count and refresh last_status / last_attempt_at
    on every subsequent failure. resolved_at is forcibly cleared
    in the conflict branch so a re-broken URL drops out of the
    "resolved" filter immediately.

Operator queries:

  -- What's broken right now?
  SELECT url, src, last_status, attempt_count, last_attempt_at
    FROM mirror_fetch_failure
   WHERE resolved_at IS NULL
   ORDER BY attempt_count DESC, last_attempt_at DESC;

  -- What URLs have ever failed (for SLA dashboards)?
  SELECT url, src, attempt_count, resolved_at
    FROM mirror_fetch_failure
   ORDER BY attempt_count DESC;

The table DDL lives in unified/schema.py:DDL_MIRROR_FETCH_FAILURE
and is registered in _ALL_DDL so init_unified_schema() ensures it
on every startup.

Designed for testability: every database call goes through the
caller-supplied connection, and the wall-clock is injectable via
the `now` parameter.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

log = logging.getLogger("scripts.mirror_failure_log")


# Status strings that mean the file is on disk and usable. Anything
# else is treated as a failure outcome. Kept in sync with
# sync_card_mirror._download return contract.
SUCCESS_STATUSES: frozenset[str] = frozenset({"ok", "skip-exists",
                                              "not-modified"})


def record_mirror_outcome(
    conn,
    *,
    url: str,
    src: str,
    dest_path: str,
    ok: bool,
    status: str,
    now: Optional[int] = None,
) -> str:
    """Record a single download outcome in mirror_fetch_failure.

    Returns one of:
      'inserted'   — first-ever failure for this URL
      'incremented' — repeat failure, attempt_count bumped
      'resolved'   — previously-failed URL just succeeded
      'noop'       — success for a URL that has never failed
                     (we don't insert success-only rows; the table
                     only tracks URLs that have failed at least once)

    Behaviour intentionally biased toward "small table, fast triage":
    a clean Phase C inserts ZERO rows; only broken URLs land here.
    """
    now_ts = now if now is not None else int(time.time())
    cur = conn.cursor()

    if ok:
        # Flip resolved_at on any pre-existing unresolved row. We
        # use UPDATE rather than INSERT because we don't want to
        # create rows for URLs that never failed.
        cur.execute("""
            UPDATE mirror_fetch_failure
               SET resolved_at     = %s,
                   last_status     = %s,
                   last_attempt_at = %s
             WHERE url = %s
               AND resolved_at IS NULL
        """, (now_ts, status, now_ts, url))
        rc = cur.rowcount or 0
        conn.commit()
        if rc > 0:
            log.info("[mirror_failure_log] resolved %s (status=%s)",
                     url, status)
            return "resolved"
        return "noop"

    # Failure path — upsert. ON CONFLICT DO UPDATE so a URL that
    # was previously resolved-then-rebreaks gets a fresh resolved_at
    # = NULL and an incremented attempt_count.
    cur.execute("""
        INSERT INTO mirror_fetch_failure
            (url, src, dest_path, last_status, attempt_count,
             first_seen_at, last_attempt_at, resolved_at)
        VALUES (%s, %s, %s, %s, 1, %s, %s, NULL)
        ON CONFLICT (url) DO UPDATE SET
            -- Refresh src/dest in case the call site has more
            -- accurate data than the original insert (e.g. a URL
            -- migrated between phases).
            src             = EXCLUDED.src,
            dest_path       = EXCLUDED.dest_path,
            last_status     = EXCLUDED.last_status,
            attempt_count   = mirror_fetch_failure.attempt_count + 1,
            last_attempt_at = EXCLUDED.last_attempt_at,
            -- Force-clear resolved_at: a previously-resolved URL
            -- that just rebroke must drop out of any "resolved"
            -- query immediately.
            resolved_at     = NULL
        RETURNING (xmax = 0) AS inserted
    """, (url, src, dest_path, status, now_ts, now_ts))
    row = cur.fetchone()
    conn.commit()
    # PostgreSQL trick: xmax=0 on RETURNING distinguishes a true
    # INSERT (xmax==0) from a conflict-driven UPDATE (xmax!=0).
    inserted = bool(row[0]) if row else False
    if inserted:
        log.debug("[mirror_failure_log] new failure %s (%s)", url, status)
        return "inserted"
    log.debug("[mirror_failure_log] repeat failure %s (%s)", url, status)
    return "incremented"


def is_success_status(status: str) -> bool:
    """Convenience for callers that want to derive `ok` from the
    string status alone (mirrors what _download returns)."""
    return status in SUCCESS_STATUSES
