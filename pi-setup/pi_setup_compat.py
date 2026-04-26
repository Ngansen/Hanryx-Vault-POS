"""
pi_setup_compat — tiny compatibility shim used by the new `cards/` package.

Centralises the one or two helper functions that the new modules need but
that don't justify importing the full server.py (which has ~26k lines and
~150 module-level side effects).

Today: just `sqlite_connect()` with row factory pre-set, matching what the
existing tcg_lookup.py does. Lifting it here means cards/fuzzy_search.py
and cards/ai_assistant.py share identical connection semantics with the
older module.
"""
from __future__ import annotations

import sqlite3


def sqlite_connect(path: str) -> sqlite3.Connection:
    """Open a SQLite connection with sqlite3.Row factory + read-only PRAGMAs."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # busy_timeout: handle the case where the sync orchestrator is mid-write
    # (the orchestrator runs WAL mode but SQLite still has occasional locks
    # during checkpoints; 5s is plenty for read traffic).
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn
