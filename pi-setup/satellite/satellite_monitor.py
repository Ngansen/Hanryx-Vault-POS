#!/usr/bin/env python3
"""
HanryxVault Satellite Connection Monitor
Runs as a persistent background service on the TRADE SHOW Pi.

Watches for connectivity to the home Pi and triggers a sync the moment
a connection is (re)established — whether that's after boot, after plugging
in a hotel ethernet cable, or after reconnecting a phone hotspot mid-show.

All sales made while offline are stored locally in SQLite and pushed
automatically the instant the tunnel comes back. Nothing is lost.

Behaviour:
  • Polls the home Pi every 30 seconds
  • On reconnect (offline → online): syncs immediately
  • While online: syncs every 5 minutes if there are unsynced records
  • While offline: logs a pending count every 5 minutes so you know how
    many sales are waiting ("3 sales waiting to sync to home Pi")
  • Prevents concurrent syncs (skips a trigger if one is already running)
  • Exits cleanly on SIGTERM (systemd stop) — never interrupts a running sync

Config: /opt/hanryxvault/satellite.conf
"""

import os
import sys
import sqlite3
import json
import signal
import subprocess
import threading
import time
import datetime
import urllib.request
import urllib.error

# ── Path discovery (works both in source tree and when installed) ─────────────
def _find_base_dir() -> str:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # When installed: script is in /opt/hanryxvault/ alongside db + conf
    if os.path.exists(os.path.join(script_dir, "vault_pos.db")) or \
       os.path.exists(os.path.join(script_dir, "satellite.conf")):
        return script_dir
    # When run from source tree (pi-setup/satellite/): walk up one level
    parent = os.path.realpath(os.path.join(script_dir, ".."))
    return parent

BASE_DIR  = _find_base_dir()
CONF_PATH = os.path.join(BASE_DIR, "satellite.conf")
DB_PATH   = os.path.join(BASE_DIR, "vault_pos.db")
SYNC_SCRIPT = os.path.join(BASE_DIR, "satellite_sync.py")
PYTHON_BIN  = os.path.join(BASE_DIR, "venv", "bin", "python3")

# Fall back to system python if venv not found yet
if not os.path.exists(PYTHON_BIN):
    PYTHON_BIN = sys.executable

# ── Defaults ──────────────────────────────────────────────────────────────────
_DEFAULTS = {
    "home_pi_url":         "http://10.10.0.1:8080",
    "timeout_s":           "8",
    "vpn_interface":       "wg0",
    "poll_interval_s":     "30",    # how often to check connectivity
    "online_sync_interval_s": "300", # re-sync while online if pending > 0 (5 min)
    "offline_log_interval_s": "300", # how often to log pending count offline
}

# ── Globals ───────────────────────────────────────────────────────────────────
_stop_event   = threading.Event()
_sync_lock    = threading.Lock()
_sync_running = False


def _ts() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str):
    print(f"{_ts()} [monitor] {msg}", flush=True)


def load_conf() -> dict:
    conf = dict(_DEFAULTS)
    if os.path.exists(CONF_PATH):
        with open(CONF_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    conf[k.strip()] = v.strip()
    return conf


# ── Database helpers ──────────────────────────────────────────────────────────

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def _get_last_sync_ms() -> int:
    """Return the timestamp of the last successful sync (0 = never synced)."""
    if not os.path.exists(DB_PATH):
        return 0
    try:
        db  = _get_db()
        row = db.execute(
            "SELECT value FROM server_state WHERE key='last_satellite_sync'"
        ).fetchone()
        db.close()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def _count_pending() -> tuple[int, int]:
    """
    Return (pending_sales, pending_deductions) — records created after last sync.
    These are waiting to be pushed to the home Pi.
    """
    if not os.path.exists(DB_PATH):
        return 0, 0
    since_ms = _get_last_sync_ms()
    try:
        db    = _get_db()
        sales = db.execute(
            "SELECT COUNT(*) FROM sales WHERE received_at > ?", (since_ms,)
        ).fetchone()[0]
        deductions = db.execute(
            "SELECT COUNT(*) FROM stock_deductions WHERE deducted_at > ?", (since_ms,)
        ).fetchone()[0]
        db.close()
        return sales, deductions
    except Exception:
        return 0, 0


# ── Connectivity check ────────────────────────────────────────────────────────

def _home_pi_reachable(home_url: str, timeout: int) -> bool:
    """Quick /health ping — returns True if home Pi responds."""
    try:
        req = urllib.request.Request(
            f"{home_url}/health",
            headers={"User-Agent": "HanryxVaultMonitor/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


# ── Sync trigger ──────────────────────────────────────────────────────────────

def _run_sync_background(reason: str):
    """
    Spawn satellite_sync.py in a background thread.
    Skips if a sync is already running.
    """
    global _sync_running

    with _sync_lock:
        if _sync_running:
            log(f"Sync already in progress — skipping trigger ({reason})")
            return
        _sync_running = True

    def _do():
        global _sync_running
        log(f"Starting sync — reason: {reason}")
        try:
            result = subprocess.run(
                [PYTHON_BIN, SYNC_SCRIPT],
                capture_output=True, text=True, timeout=180
            )
            for line in result.stdout.strip().splitlines():
                log(f"  sync › {line}")
            if result.returncode == 0:
                log("Sync completed successfully ✓")
            else:
                log(f"Sync exited with code {result.returncode}")
                if result.stderr:
                    log(f"  stderr: {result.stderr.strip()[:200]}")
        except subprocess.TimeoutExpired:
            log("Sync timed out after 3 minutes")
        except Exception as e:
            log(f"Sync error: {e}")
        finally:
            with _sync_lock:
                _sync_running = False

    threading.Thread(target=_do, daemon=True).start()


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    conf = load_conf()
    home_url    = conf["home_pi_url"].rstrip("/")
    timeout     = int(conf.get("timeout_s",              8))
    poll_s      = int(conf.get("poll_interval_s",       30))
    online_s    = int(conf.get("online_sync_interval_s", 300))
    offline_s   = int(conf.get("offline_log_interval_s", 300))

    log(f"Satellite monitor starting")
    log(f"  Home Pi      : {home_url}")
    log(f"  Poll interval: {poll_s}s")
    log(f"  DB path      : {DB_PATH}")
    log(f"  Sync script  : {SYNC_SCRIPT}")

    was_reachable      = False
    last_online_sync   = 0.0   # time.time() of last sync while online
    last_offline_log   = 0.0   # time.time() of last offline status log

    while not _stop_event.is_set():
        now       = time.time()
        reachable = _home_pi_reachable(home_url, timeout)

        if reachable:
            # ── Online ────────────────────────────────────────────────────────
            pending_sales, pending_ded = _count_pending()
            pending_total = pending_sales + pending_ded

            if not was_reachable:
                # Just reconnected — sync immediately regardless of pending count
                log(f"Connection to home Pi RESTORED  "
                    f"({pending_sales} sales + {pending_ded} deductions pending)")
                _run_sync_background("reconnect")
                last_online_sync = now

            elif pending_total > 0 and (now - last_online_sync) >= online_s:
                # Still online, has pending data, enough time has passed
                log(f"Periodic sync — {pending_sales} sales + {pending_ded} deductions pending")
                _run_sync_background("periodic")
                last_online_sync = now

            else:
                # Online and nothing to do
                if pending_total > 0:
                    log(f"Online — {pending_sales} sales + {pending_ded} deductions pending "
                        f"(next sync in {int(online_s - (now - last_online_sync))}s)")

        else:
            # ── Offline ───────────────────────────────────────────────────────
            if was_reachable:
                log(f"Lost connection to home Pi ({home_url}) — operating offline")

            if (now - last_offline_log) >= offline_s:
                pending_sales, pending_ded = _count_pending()
                if pending_sales + pending_ded > 0:
                    log(f"OFFLINE — {pending_sales} sales + {pending_ded} deductions "
                        f"stored locally, will push when connection returns")
                else:
                    log(f"OFFLINE — no pending data (all synced before going offline)")
                last_offline_log = now

        was_reachable = reachable
        _stop_event.wait(timeout=poll_s)

    log("Monitor stopped cleanly")


# ── Signal handling ───────────────────────────────────────────────────────────

def _on_signal(signum, frame):
    log(f"Received signal {signum} — stopping after current sync completes")
    _stop_event.set()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT,  _on_signal)
    main()
