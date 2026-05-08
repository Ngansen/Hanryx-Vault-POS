"""
sync_orchestrator.py — schedules the offline-DB sync work.

Runs as its own long-lived container (`sync` service in docker-compose).
Sleeps between ticks; on each tick decides which jobs are due and runs
them in series. We intentionally avoid threading: each job touches the
SQLite mirror, and SQLite + WAL handles serial writers but not parallel
ones gracefully.

Schedule
--------
Every 6 minutes:
    - usb_mirror.run_mirror()  — Postgres → SQLite snapshot. Cheap (~1s).
                                  Most-frequent because the mirror is what
                                  the POS reads when WiFi is down; staler
                                  than 10 min is noticeable.

Every 1 hour (when WiFi up):
    - tcgplayer_proxy           — refresh raw English market prices for the
                                  inventory's top 1000 most-searched cards.
    - sync_tcg_db.py            — fetch new English card releases from
                                  pokemontcg.io.

Every 6 hours (when WiFi up):
    - ebay_sold.py              — sweep eBay for sold listings of the
                                  inventory's most valuable cards (so we
                                  capture grade-specific medians without
                                  burning the daily 5000-call quota).

Every 24 hours (3 AM local):
    - import_kr_cards.py force=False
    - import_jpn_cards.py
    - import_jpn_pocket_cards.py
    - import_chs_cards.py
                                  Korean / Japanese / Chinese full refresh
                                  from the source repos (idempotent — they
                                  no-op if the data hasn't changed).
    - import_artwork_hashes.py  — refresh card_hashes for the recognizer
                                  container's Hamming-distance index.

Why a separate container instead of cron on the host
----------------------------------------------------
- Self-contained: image is the same as `pos`, so it has every Python dep
  the importers need without polluting the host with Pi-OS package state.
- Restartable on its own without bouncing the POS.
- Logs go through `docker logs sync`, queryable the same way as every
  other service.
- Keeps the host minimal (nothing in /etc/cron.* to surprise a future
  admin, no host-side pip install).

Network awareness
-----------------
Each external-API job is wrapped in `_with_network_check()` that does a
lightweight HEAD against the target host before calling the importer. If
the network is down (trade-show WiFi just dropped), the job is skipped
WITHOUT marking it as failed — the orchestrator silently waits for the
next tick. When connectivity comes back, the next due job runs normally.
This prevents a flapping connection from spamming the status with red.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from cards_db_path import is_usb_configured, sync_log_dir
import usb_mirror


# ── Single-instance lock ──────────────────────────────────────────────────────
# Two orchestrator processes writing the same SQLite mirror at the same
# time would race on the WAL file and could corrupt it. The pidfile lock
# ensures only one orchestrator runs per host: on startup, write our pid;
# if a previous pid is alive (kill -0 succeeds), exit. Stale pidfiles
# (process gone) are silently overwritten.
def _acquire_singleton_lock() -> None:
    pid_file = sync_log_dir() / "sync.pid"
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            old_pid = 0
        if old_pid > 0:
            try:
                os.kill(old_pid, 0)  # signal 0 = liveness check, no signal sent
            except ProcessLookupError:
                pass  # stale — fall through and overwrite
            except PermissionError:
                # PID exists but owned by a different uid — that's also a
                # live process from our perspective; refuse to start.
                raise RuntimeError(
                    f"sync orchestrator pid {old_pid} from {pid_file} is alive (owned by another user). "
                    "If you're sure no other orchestrator is running, delete the pidfile."
                )
            except OSError as e:
                if e.errno == errno.ESRCH:
                    pass  # stale
                else:
                    raise
            else:
                raise RuntimeError(
                    f"sync orchestrator pid {old_pid} is already running (from {pid_file}). "
                    "Refusing to start a second instance — would corrupt the SQLite mirror."
                )
    pid_file.write_text(str(os.getpid()))

logging.basicConfig(
    level=os.environ.get("SYNC_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("sync_orchestrator")

TICK_SECONDS = int(os.environ.get("SYNC_TICK_SECONDS", "60"))


@dataclass
class Job:
    """A scheduled job. Tracked between ticks so we know when it's due next."""
    name: str
    interval_sec: int
    fn: Callable[[], None]
    needs_network: bool = False
    last_run: float = 0.0
    last_status: str = "never"
    last_error: str = ""
    last_duration_sec: float = 0.0
    runs: int = 0
    failures: int = 0


def _has_network(host: str = "1.1.1.1", port: int = 443, timeout: float = 2.0) -> bool:
    """Cheap reachability check — does NOT consume API quota."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _run_module_subprocess(module_path: str, args: list[str] | None = None) -> None:
    """Run an existing pi-setup script as a subprocess, raising on non-zero exit.

    Subprocess (rather than direct import) keeps blast radius small — if
    one of the importers leaks file handles or sets a global, it doesn't
    affect the orchestrator process or other jobs.
    """
    cmd = [sys.executable, module_path] + (args or [])
    log.info("[run] %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if result.returncode != 0:
        raise RuntimeError(f"{module_path} exited {result.returncode}: {result.stderr[-2000:]}")


def _job_mirror() -> None:
    summary = usb_mirror.run_mirror()
    log.info("[mirror] complete: %s", summary)


def _job_tcg_db() -> None:
    """Refresh English card pool from pokemontcg.io."""
    _run_module_subprocess("/app/sync_tcg_db.py", ["--once"])


def _job_kr_cards() -> None:
    _run_module_subprocess("/app/import_kr_cards.py")


def _job_jpn_cards() -> None:
    _run_module_subprocess("/app/import_jpn_cards.py")


def _job_jpn_pocket_cards() -> None:
    _run_module_subprocess("/app/import_jpn_pocket_cards.py")


def _job_chs_cards() -> None:
    _run_module_subprocess("/app/import_chs_cards.py")


def _job_card_hashes() -> None:
    _run_module_subprocess("/app/import_artwork_hashes.py")


# ── Unified card DB jobs (U1-U9 importers) ────────────────────────────────
# These are scheduled less aggressively than the per-language importers
# because the source data (Excel files in your Card-Database repo,
# TCGdex API, PokéAPI CSVs) changes WEEKS apart, not hours. The
# trade-off: a brand-new set release is in your per-language tables
# within hours but won't be in cards_master until the next consolidator
# tick (12h). The cashier still finds the card via the legacy table
# entries in fuzzy_search — they're listed AFTER cards_master, so a
# stale master row is shadowed by a fresh per-language row.

def _job_ref_mappings() -> None:
    _run_module_subprocess("/app/import_ref_mappings.py")


def _job_eng_xlsx() -> None:
    _run_module_subprocess("/app/import_eng_xlsx.py")


def _job_ex_codes() -> None:
    _run_module_subprocess("/app/import_ex_codes.py")


def _job_jp_xlsx() -> None:
    _run_module_subprocess("/app/import_jp_xlsx.py")


def _job_kr_promos() -> None:
    _run_module_subprocess("/app/import_kr_promos.py")


def _job_tcgdex() -> None:
    _run_module_subprocess("/app/import_tcgdex.py")


def _job_jp_pcc() -> None:
    _run_module_subprocess("/app/import_jp_pokemoncardcom.py")


def _job_pocket_lt() -> None:
    _run_module_subprocess("/app/import_pocket_limitless.py")


def _job_pokeapi_species() -> None:
    _run_module_subprocess("/app/import_pokeapi_species.py")


def _job_build_master() -> None:
    """Rebuild cards_master from every Layer-1/Layer-2 source.
    Runs LAST in the daily cycle so it sees fresh data from every
    importer that ran earlier in the day."""
    _run_module_subprocess("/app/build_cards_master.py")


def _job_ebay_sweep() -> None:
    """eBay sold-listings sweep for the highest-value inventory cards."""
    _run_module_subprocess("/app/ebay_sold.py", ["--sweep", "--top", "200"])


def _job_tcgplayer_refresh() -> None:
    _run_module_subprocess("/app/tcgplayer_proxy.py", ["--refresh", "--top", "1000"])


# ── Continuous Discovery v1 (D4) ──────────────────────────────────────────
# Two-stage worker that keeps cards_master growing without operator action:
#   1. discover_sets (daily): hits TCGdex per-language set endpoints
#      (en/ja/ko/zh-tw/zh-cn) and enqueues every unknown set_id into
#      discovery_queue. Cheap (one HTTP call per language).
#   2. discovery_dispatch (every 30 min): drains pending queue rows,
#      runs the right importers, builds cards_master, mirrors to USB.
def _job_discover_sets() -> None:
    _run_module_subprocess("/app/discover_new_sets.py")


def _job_discovery_dispatch() -> None:
    _run_module_subprocess("/app/discovery_dispatch.py")


def _job_market_refresh() -> None:
    """C11: backfill multi-source market prices for top-N inventory.

    Runs in-process (not subprocess) because it shares the same psycopg2
    connection style as usb_mirror and benefits from the orchestrator's
    own logging context. Top-N controlled by MARKET_REFRESH_TOP_N env
    (default 200). Cheap thanks to scrape_cache's 10-min Redis TTL.
    """
    import refresh_market_prices  # local import — module pulls in psycopg2 + scrapers
    summary = refresh_market_prices.refresh_once()
    log.info("[market_refresh] complete: %s", summary)


JOBS: list[Job] = [
    Job(name="mirror",          interval_sec=6 * 60,         fn=_job_mirror,            needs_network=False),
    Job(name="tcg_db",          interval_sec=60 * 60,        fn=_job_tcg_db,            needs_network=True),
    Job(name="tcgplayer",       interval_sec=60 * 60,        fn=_job_tcgplayer_refresh, needs_network=True),
    Job(name="ebay_sweep",      interval_sec=6 * 60 * 60,    fn=_job_ebay_sweep,        needs_network=True),
    Job(name="kr_cards",        interval_sec=24 * 60 * 60,   fn=_job_kr_cards,          needs_network=True),
    Job(name="jpn_cards",       interval_sec=24 * 60 * 60,   fn=_job_jpn_cards,         needs_network=True),
    Job(name="jpn_pocket",      interval_sec=24 * 60 * 60,   fn=_job_jpn_pocket_cards,  needs_network=True),
    Job(name="chs_cards",       interval_sec=24 * 60 * 60,   fn=_job_chs_cards,         needs_network=True),
    Job(name="card_hashes",     interval_sec=24 * 60 * 60,   fn=_job_card_hashes,       needs_network=False),

    # ── Unified card DB pipeline ──────────────────────────────────────
    # Order in this list IS the order they run when multiple are due
    # in the same tick (see _run_due_jobs). The consolidator goes LAST
    # so the importers above have a chance to refresh first. Intervals
    # are intentionally staggered (different prime-ish hour offsets) so
    # the Pi doesn't try to run all of them at once on a fresh boot.
    Job(name="ref_mappings",    interval_sec=7 * 24 * 60 * 60, fn=_job_ref_mappings,     needs_network=True),
    Job(name="eng_xlsx",        interval_sec=7 * 24 * 60 * 60, fn=_job_eng_xlsx,         needs_network=True),
    Job(name="ex_codes",        interval_sec=7 * 24 * 60 * 60, fn=_job_ex_codes,         needs_network=True),
    Job(name="jp_xlsx",         interval_sec=7 * 24 * 60 * 60, fn=_job_jp_xlsx,          needs_network=True),
    Job(name="kr_promos",       interval_sec=7 * 24 * 60 * 60, fn=_job_kr_promos,        needs_network=True),
    Job(name="tcgdex",          interval_sec=24 * 60 * 60,     fn=_job_tcgdex,           needs_network=True),
    Job(name="jp_pcc",          interval_sec=24 * 60 * 60,     fn=_job_jp_pcc,           needs_network=True),
    Job(name="pocket_lt",       interval_sec=24 * 60 * 60,     fn=_job_pocket_lt,        needs_network=True),
    Job(name="pokeapi_species", interval_sec=7 * 24 * 60 * 60, fn=_job_pokeapi_species,  needs_network=True),
    Job(name="build_master",    interval_sec=12 * 60 * 60,     fn=_job_build_master,     needs_network=False),

    # ── Continuous Discovery v1 ───────────────────────────────────────
    # Probe runs daily (cheap — 5 HTTP calls); dispatcher runs every
    # 30 min so a freshly-enqueued set lands in cards_master within
    # an hour rather than waiting for the next daily cycle.
    Job(name="discover_sets",     interval_sec=24 * 60 * 60, fn=_job_discover_sets,     needs_network=True),
    Job(name="discovery_dispatch", interval_sec=30 * 60,     fn=_job_discovery_dispatch, needs_network=True),

    # ── C11: Market intel backfill for the AI cashier ─────────────────
    # Pulls naver/bunjang/hareruya2/cardmarket prices for the top-N
    # in-stock cards every hour and writes them into price_history with
    # USD conversion. usb_mirror replicates to price_history_recent on
    # its next 6-min tick, so the AI assistant's local lookup picks
    # them up automatically without needing scraper access at inference
    # time. Skipped automatically when the network is down.
    Job(name="market_refresh",    interval_sec=60 * 60,      fn=_job_market_refresh,    needs_network=True),
]


def _write_status() -> None:
    """Write a JSON snapshot of all job state to /mnt/cards/logs/sync_status.json."""
    snapshot = {
        "ts": int(time.time()),
        "ts_iso": datetime.now(timezone.utc).isoformat(),
        "usb_configured": is_usb_configured(),
        "network": _has_network(),
        "jobs": [
            {
                "name": j.name,
                "interval_sec": j.interval_sec,
                "needs_network": j.needs_network,
                "last_run": int(j.last_run) if j.last_run else None,
                "last_status": j.last_status,
                "last_error": j.last_error,
                "last_duration_sec": j.last_duration_sec,
                "next_due_in_sec": max(0, int(j.interval_sec - (time.time() - j.last_run))) if j.last_run else 0,
                "runs": j.runs,
                "failures": j.failures,
            }
            for j in JOBS
        ],
    }
    try:
        (sync_log_dir() / "sync_status.json").write_text(json.dumps(snapshot, indent=2))
    except Exception as e:
        log.warning("[status] could not write sync_status.json: %s", e)


def _run_due_jobs(have_network: bool) -> None:
    now = time.time()
    for j in JOBS:
        due = (now - j.last_run) >= j.interval_sec
        if not due:
            continue
        if j.needs_network and not have_network:
            log.debug("[skip] %s — no network", j.name)
            continue
        started = time.time()
        try:
            j.fn()
            j.last_status = "ok"
            j.last_error = ""
        except Exception as e:
            j.last_status = "error"
            j.last_error = repr(e)[:500]
            j.failures += 1
            log.exception("[error] %s failed", j.name)
        j.last_run = time.time()
        j.last_duration_sec = round(j.last_run - started, 2)
        j.runs += 1
        _write_status()


def main() -> None:
    log.info("sync_orchestrator starting; tick=%ds; usb_configured=%s",
             TICK_SECONDS, is_usb_configured())
    if not is_usb_configured():
        log.warning("HANRYX_LOCAL_DB_DIR not set — orchestrator will run but mirror will fail loudly")
    else:
        # Pidfile lock — refuses to start if another orchestrator is alive.
        # Only enforced when USB is configured, because the sync_log_dir()
        # helper would write the pidfile to the in-package dev path
        # otherwise, which is a less interesting place to enforce the lock.
        _acquire_singleton_lock()

    # On boot, fire mirror once immediately so /admin/usb-sync/status has
    # something to show without waiting 6 minutes.
    if is_usb_configured():
        try:
            _job_mirror()
            for j in JOBS:
                if j.name == "mirror":
                    j.last_run = time.time()
                    j.last_status = "ok"
                    j.runs += 1
                    j.last_duration_sec = 0.0
        except Exception as e:
            log.exception("[boot-mirror] failed: %s", e)

    while True:
        _run_due_jobs(_has_network())
        _write_status()
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
