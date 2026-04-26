"""
cards_db_path.py — Single source of truth for the local SQLite + FAISS paths.

Resolves all on-disk locations used by the offline card database from one
environment variable so the whole stack can be redirected onto the
USB-mounted ext4 drive at `/mnt/cards` without touching application code.

Why this exists
---------------
Before this module, four files independently hard-coded
`os.path.dirname(__file__) + "/pokedex_local.db"` — meaning the SQLite file
lived inside the `pos` container's image layer and was wiped on every
`docker compose build`. Same story for the FAISS index, which lived in
`/tmp/` (also ephemeral). The new behaviour is:

    HANRYX_LOCAL_DB_DIR=/mnt/cards   ← set in docker-compose.yml
        → SQLite at /mnt/cards/pokedex_local.db
        → FAISS  at /mnt/cards/faiss/hanryx_cards.index
        → Logs   at /mnt/cards/logs/

If the env var is unset, every helper falls back to the previous in-package
path so existing test fixtures, dev shells, and the migrate-db-to-usb.sh
helper script keep working unchanged.

The fallback is intentional, not a bug: an unprivileged developer running
`python3 import_tcg_db.py --stats` on their laptop should not need
`/mnt/cards` to exist. The cost of the fallback is that someone could
accidentally write to the in-container path on the Pi if the env var is
missing — `assert_usb_configured()` is provided for the cron-driven
sync orchestrator to fail loudly in that case.
"""
from __future__ import annotations

import os
from pathlib import Path

# Module-level constant resolved once at import time. Recomputing it on
# every call would let a stray os.environ mutation in one request bleed
# into another — the value is intentionally process-wide.
_ENV_VAR = "HANRYX_LOCAL_DB_DIR"

# Fallback when the env var is unset: the directory containing THIS file,
# i.e. `pi-setup/`. Matches the historical hard-coded path so a dev box
# without /mnt/cards still gets a working SQLite at pi-setup/pokedex_local.db.
_PACKAGE_DIR = Path(__file__).resolve().parent


def local_db_dir() -> Path:
    """Directory that holds pokedex_local.db, FAISS index, and sync logs.

    Reads HANRYX_LOCAL_DB_DIR every call so a test can monkey-patch the env.
    The directory is created if missing — but only if the env var is set
    (we don't want to accidentally mkdir inside a read-only container layer
    when the var is unset).
    """
    raw = os.environ.get(_ENV_VAR, "").strip()
    if raw:
        d = Path(raw).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        return d
    return _PACKAGE_DIR


def local_db_path() -> str:
    """Absolute path to pokedex_local.db (the master SQLite file)."""
    return str(local_db_dir() / "pokedex_local.db")


def faiss_index_path() -> str:
    """Absolute path to the FAISS index file built from card image embeddings."""
    d = local_db_dir() / "faiss"
    d.mkdir(parents=True, exist_ok=True)
    return str(d / "hanryx_cards.index")


def faiss_ids_path() -> str:
    """Absolute path to the FAISS row-id → QR-code JSON sidecar."""
    d = local_db_dir() / "faiss"
    d.mkdir(parents=True, exist_ok=True)
    return str(d / "hanryx_cards_ids.json")


def sync_log_dir() -> Path:
    """Directory where the sync orchestrator writes per-source run logs."""
    d = local_db_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_usb_configured() -> bool:
    """True if HANRYX_LOCAL_DB_DIR is set AND the directory is writable.

    Used by /admin/usb-sync/status and the sync orchestrator's startup check
    to distinguish 'running without USB' (degraded fallback) from 'running on
    USB' (the production-on-Pi configuration).
    """
    raw = os.environ.get(_ENV_VAR, "").strip()
    if not raw:
        return False
    d = Path(raw).expanduser()
    if not d.exists():
        return False
    return os.access(str(d), os.W_OK)


def assert_usb_configured() -> None:
    """Raise RuntimeError if HANRYX_LOCAL_DB_DIR is missing or unwritable.

    Called at orchestrator startup so a misconfigured cron container fails
    loudly instead of silently writing the SQLite mirror to an ephemeral
    in-container path that nobody will ever see.
    """
    if not is_usb_configured():
        raise RuntimeError(
            f"{_ENV_VAR} is not set or directory is not writable. "
            "Set it in docker-compose.yml and bind-mount /mnt/cards "
            "into the container before starting the sync orchestrator."
        )
