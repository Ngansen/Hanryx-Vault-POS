"""
filler_db.py — standalone Postgres connector for the offline filler scripts
(notion_master_import, pkmncards_image_filler, korean_names_filler,
japanese_names_filler, japanese_image_filler, etc.).

Why this exists
---------------
The fillers used to do `from server import _direct_db`, which works but pulls
in server.py at module-import time. server.py runs a *lot* of side effects on
import — it connects to Redis, monkey-patches json.dumps to orjson, and
crucially calls `_resolve_faiss_index_path()`, which does
`mkdir(/mnt/cards/faiss, parents=True, exist_ok=True)`.

If the USB card storage hiccups (`/mnt/cards` stale or Errno 5), every filler
crashes at import time before it can do a single DB write — even though the
work it needs to do is 100% Postgres-only.

This module provides the *minimum* server.py-equivalent surface the fillers
actually use:

    db = _direct_db()
    cur = db.execute("SELECT ... WHERE x = ?", (x,))   # '?' → '%s' rewrite
    for row in cur: row["col"] / row[0]                # DictCursor rows
    db.commit() / db.rollback() / db.close()

The DSN is read from $DATABASE_URL with the same default as server.py so the
container picks up the right database with no additional configuration.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Iterable, Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool


DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://vaultpos:vaultpos@localhost:5432/vaultpos",
)


_pg_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None
_pool_lock = threading.Lock()


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """Lazy-initialize a small connection pool sized for batch jobs.

    Defaults are intentionally tiny (1–4) — fillers are single-threaded
    crawlers and don't need server.py's 20-conn pool. TCP keepalives match
    server.py so dead connections surface in seconds, not minutes.
    Statement timeout is generous (60s) because some bulk-update queries on
    cards_master legitimately take a while.
    """
    global _pg_pool
    if _pg_pool is None:
        with _pool_lock:
            if _pg_pool is None:
                _pg_pool = psycopg2.pool.ThreadedConnectionPool(
                    int(os.environ.get("FILLER_PG_POOL_MIN", "1")),
                    int(os.environ.get("FILLER_PG_POOL_MAX", "4")),
                    dsn=DATABASE_URL,
                    keepalives=1,
                    keepalives_idle=30,
                    keepalives_interval=10,
                    keepalives_count=3,
                    options=(
                        f"-c statement_timeout="
                        f"{os.environ.get('FILLER_PG_STATEMENT_TIMEOUT_MS', '60000')}"
                    ),
                )
    return _pg_pool


class _PgConn:
    """Thin wrapper that mimics the sqlite3 connection API used across the
    fillers. Mirrors server._PgConn so the call-sites stay byte-identical."""

    def __init__(self, raw_conn):
        self._conn = raw_conn

    # ---- core ------------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        try:
            cur.execute(sql.replace("?", "%s"), params or ())
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
            raise
        return cur

    def executemany(self, sql: str, params_list):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        try:
            cur.executemany(sql.replace("?", "%s"), params_list)
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
            raise
        return cur

    def cursor(self, *args, **kwargs):
        kwargs.setdefault("cursor_factory", psycopg2.extras.DictCursor)
        return self._conn.cursor(*args, **kwargs)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        commit_err = None
        try:
            self._conn.commit()
        except Exception as e:
            commit_err = e
            try:
                self._conn.rollback()
            except Exception:
                pass
        try:
            _get_pool().putconn(self._conn)
        finally:
            if commit_err is not None:
                raise commit_err

    # ---- sqlite-compat no-ops -------------------------------------------
    @property
    def row_factory(self):
        return None

    @row_factory.setter
    def row_factory(self, _):
        pass


def _direct_db() -> _PgConn:
    """Return a pooled raw psycopg2 connection wrapped to look like sqlite3.

    Drop-in replacement for `server._direct_db()` for offline scripts that
    must NOT depend on /mnt/cards being healthy at import time.
    """
    return _PgConn(_get_pool().getconn())
