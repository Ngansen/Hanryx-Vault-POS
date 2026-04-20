"""
Auto-targeted backfill when a new alias cluster is discovered.

Hook
----
``set_alias_sync.sync_now()`` calls ``schedule_for_new(...)`` after
each successful pull.  We diff the just-written synced clusters
against the previously-recorded set of cluster names; for each new
cluster, we enqueue a *targeted* import for every language importer
that knows how to take a list of set codes.

This avoids two pain points:

1. **Don't wait for the next full import** — when pokemontcg.io
   publishes a new SV set, our KR/JPN tables stay empty for that set
   until a full re-import runs, which is wasteful and slow.  Instead
   we trigger a tiny import for just that set's codes.

2. **Don't re-import everything** — a "full import" pulls thousands
   of cards we already have; targeted backfill pulls only what's
   actually new (single-digit-set scope).

Importer contract
-----------------
Each importer module is consulted for an optional callable named
``backfill_codes(conn, set_codes: list[str]) -> dict``.  Importers
that don't expose this are simply skipped (the cluster gets queued
for the next full import).  The callable returns a small status dict
the queue records.

Queue
-----
Jobs run sequentially in a single background worker thread to avoid
hammering Postgres or any one upstream source.  Status is exposed
via ``status()`` for the admin UI.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable

log = logging.getLogger("cluster_backfill")


_LOCK = threading.RLock()
_QUEUE: deque[dict] = deque()
_HISTORY: deque[dict] = deque(maxlen=50)
_KNOWN_NAMES_KEY = "cluster_backfill.known_cluster_names"
_WORKER_STARTED = False
_RUNNING_JOB: dict | None = None

# Map language label → ("module name", "callable attr")
_IMPORTERS: dict[str, tuple[str, str]] = {
    "jpn":         ("import_jpn_cards", "backfill_codes"),
    "jpn_pocket":  ("import_jpn_pocket_cards", "backfill_codes"),
    "kr":          ("import_kr_cards", "backfill_codes"),
    "chs":         ("import_chs_cards", "backfill_codes"),
    "multi":       ("import_multi_tcg", "backfill_codes"),
}


def _get_conn():
    """Lazy server import to avoid a circular dependency."""
    from server import _PgConn  # type: ignore
    return _PgConn()


def _load_known(conn) -> set[str]:
    try:
        import source_state
        names = source_state.get_state(conn, "cluster_backfill", "known_names")
        if isinstance(names, list):
            return {str(n) for n in names}
    except Exception as exc:
        log.info("[cluster_backfill] could not load known names: %s", exc)
    return set()


def _save_known(conn, names: set[str]) -> None:
    try:
        import source_state
        source_state.set_state(conn, "cluster_backfill", "known_names",
                               sorted(names))
    except Exception as exc:
        log.info("[cluster_backfill] could not persist known names: %s", exc)


def schedule_for_new(synced_clusters: list[dict]) -> dict:
    """
    Diff the freshly-synced cluster list against what we'd seen before
    and enqueue targeted imports for any new clusters.

    Called by ``set_alias_sync`` after every successful sync.
    """
    if not synced_clusters:
        return {"enqueued": 0, "new_clusters": []}

    new_names: list[str] = []
    try:
        with _get_conn() as conn:
            known = _load_known(conn)
            current = {(c.get("name") or "").strip()
                       for c in synced_clusters
                       if (c.get("name") or "").strip()}
            new_names = sorted(current - known)
            if new_names:
                _save_known(conn, current | known)
    except Exception as exc:
        log.info("[cluster_backfill] schedule diff failed: %s", exc)
        return {"enqueued": 0, "new_clusters": [], "error": str(exc)}

    enqueued = 0
    name_to_cluster = {c.get("name"): c for c in synced_clusters}
    for nm in new_names:
        cl = name_to_cluster.get(nm)
        if not cl:
            continue
        # Only enqueue codes that look like set ids (short alphanumeric),
        # not free-text names — importers want codes.
        codes = sorted({t for t in cl.get("tokens", [])
                        if t and 2 <= len(t) <= 12 and not (" " in t)})
        if not codes:
            continue
        for lang in _IMPORTERS:
            with _LOCK:
                _QUEUE.append({
                    "lang":      lang,
                    "cluster":   nm,
                    "set_codes": codes,
                    "queued_at": int(time.time()),
                    "status":    "pending",
                })
                enqueued += 1

    if enqueued:
        _ensure_worker()
    return {"enqueued": enqueued, "new_clusters": new_names}


def schedule_jobs(jobs: list[dict]) -> int:
    """
    Operator-driven enqueue (e.g. from /admin/imports/backfill).
    `jobs` shape matches import_gaps.suggest_backfill_jobs() output.
    """
    n = 0
    with _LOCK:
        for j in jobs:
            lang = j.get("language")
            if lang not in _IMPORTERS:
                continue
            _QUEUE.append({
                "lang":      lang,
                "cluster":   j.get("cluster") or "?",
                "set_codes": j.get("set_codes") or [],
                "missing":   j.get("missing_numbers") or [],
                "queued_at": int(time.time()),
                "status":    "pending",
            })
            n += 1
    if n:
        _ensure_worker()
    return n


# ── worker ─────────────────────────────────────────────────────────────────
def _ensure_worker() -> None:
    global _WORKER_STARTED
    with _LOCK:
        if _WORKER_STARTED:
            return
        _WORKER_STARTED = True
    t = threading.Thread(target=_worker, daemon=True, name="cluster-backfill")
    t.start()


def _worker() -> None:
    global _RUNNING_JOB, _WORKER_STARTED
    while True:
        with _LOCK:
            if not _QUEUE:
                _WORKER_STARTED = False
                _RUNNING_JOB = None
                return
            job = _QUEUE.popleft()
            _RUNNING_JOB = job
        job["status"] = "running"
        job["started_at"] = int(time.time())
        try:
            result = _run_job(job)
            job["status"] = "ok"
            job["result"] = result
        except Exception as exc:
            log.warning("[cluster_backfill] job failed: %s", exc)
            job["status"] = "error"
            job["error"] = str(exc)
        job["finished_at"] = int(time.time())
        with _LOCK:
            _HISTORY.appendleft(job)
            _RUNNING_JOB = None


def _run_job(job: dict) -> dict:
    lang = job["lang"]
    mod_name, attr = _IMPORTERS[lang]
    try:
        mod = __import__(mod_name)
    except Exception as exc:
        return {"skipped": True, "reason": f"importer {mod_name} unavailable: {exc}"}
    fn: Callable | None = getattr(mod, attr, None)
    if fn is None:
        return {"skipped": True, "reason": f"{mod_name}.{attr} not implemented"}
    with _get_conn() as conn:
        return dict(fn(conn, job["set_codes"]) or {"ok": True})


def status() -> dict:
    with _LOCK:
        return {
            "queue_depth": len(_QUEUE),
            "worker_running": _WORKER_STARTED,
            "running_job": dict(_RUNNING_JOB) if _RUNNING_JOB else None,
            "queued":   [dict(j) for j in list(_QUEUE)[:20]],
            "history":  list(_HISTORY)[:20],
        }
