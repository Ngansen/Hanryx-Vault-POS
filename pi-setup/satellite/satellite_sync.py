#!/usr/bin/env python3
"""
HanryxVault Satellite Sync
Runs on the TRADE SHOW Pi as a systemd oneshot service on every boot.

What it does (only when powered on and home Pi is reachable):
  1. Pushes all sales made offline to the home Pi
  2. Pushes all stock deductions to the home Pi (keeps home inventory accurate)
  3. Pulls the latest full inventory from the home Pi (so you start each show
     with current stock levels from home)

If the home Pi is not reachable (no VPN, no internet), it exits silently
and the trade show Pi continues running fully offline with local data.

Config: /opt/hanryxvault/satellite.conf
"""

import sqlite3
import json
import os
import sys
import time
import datetime
import urllib.request
import urllib.error

CONF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "satellite.conf")
DB_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vault_pos.db")

# Resolve paths relative to the script location
CONF_PATH = os.path.realpath(CONF_PATH)
DB_PATH   = os.path.realpath(DB_PATH)

# Default config — overridden by satellite.conf
_DEFAULTS = {
    "home_pi_url": "http://10.10.0.1:8080",
    "timeout_s":   "15",
    "retry_count": "2",
}


def _ts():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(f"{_ts()} [satellite-sync] {msg}", flush=True)


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


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _api_post(url: str, data, timeout: int) -> dict:
    body = json.dumps(data).encode()
    req  = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type":  "application/json",
            "User-Agent":    "HanryxVaultSatellite/1.0",
            "X-Source":      "satellite",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _api_get(url: str, timeout: int) -> object:
    req = urllib.request.Request(
        url,
        headers={
            "Accept":     "application/json",
            "User-Agent": "HanryxVaultSatellite/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def get_last_sync(db: sqlite3.Connection) -> int:
    row = db.execute(
        "SELECT value FROM server_state WHERE key='last_satellite_sync'"
    ).fetchone()
    return int(row[0]) if row else 0


def set_last_sync(db: sqlite3.Connection, ts_ms: int):
    db.execute(
        "INSERT OR REPLACE INTO server_state (key, value) VALUES ('last_satellite_sync', ?)",
        (str(ts_ms),)
    )
    db.commit()


# ── 1. Push sales ─────────────────────────────────────────────────────────────

def push_sales(db: sqlite3.Connection, home_url: str, since_ms: int, timeout: int) -> int:
    rows = db.execute(
        "SELECT * FROM sales WHERE received_at > ? ORDER BY received_at ASC",
        (since_ms,)
    ).fetchall()

    if not rows:
        log("No new sales to push to home Pi")
        return 0

    payload = []
    for r in rows:
        payload.append({
            "transactionId": r["transaction_id"],
            "timestamp":     r["timestamp_ms"],
            "subtotal":      r["subtotal"],
            "taxAmount":     r["tax_amount"],
            "tipAmount":     r["tip_amount"],
            "totalAmount":   r["total_amount"],
            "paymentMethod": r["payment_method"],
            "employeeId":    r["employee_id"],
            "items":         json.loads(r["items_json"] or "[]"),
            "cashReceived":  r["cash_received"],
            "changeGiven":   r["change_given"],
            "isRefunded":    bool(r["is_refunded"]),
        })

    result = _api_post(f"{home_url}/sync/sales", payload, timeout)
    log(f"Pushed {len(payload)} sales → home  "
        f"(inserted={result.get('inserted','?')} skipped={result.get('skipped','?')})")
    return len(payload)


# ── 2. Push stock deductions ──────────────────────────────────────────────────

def push_deductions(db: sqlite3.Connection, home_url: str, since_ms: int, timeout: int) -> int:
    rows = db.execute(
        "SELECT * FROM stock_deductions WHERE deducted_at > ? ORDER BY deducted_at ASC",
        (since_ms,)
    ).fetchall()

    if not rows:
        log("No new stock deductions to push")
        return 0

    items = [
        {
            "qrCode":    r["qr_code"],
            "name":      r["name"],
            "quantity":  r["quantity"],
            "unitPrice": r["unit_price"],
            "lineTotal": r["line_total"],
        }
        for r in rows
    ]

    result = _api_post(f"{home_url}/inventory/deduct", items, timeout)
    log(f"Pushed {len(items)} deductions → home  ({result})")
    return len(items)


# ── 3. Pull inventory from home Pi ────────────────────────────────────────────

def pull_inventory(db: sqlite3.Connection, home_url: str, timeout: int) -> int:
    products = _api_get(f"{home_url}/inventory", timeout)

    if not isinstance(products, list):
        log(f"Unexpected inventory response type: {type(products)}")
        return 0

    upserted = 0
    for p in products:
        qr   = (p.get("qrCode") or p.get("qr_code") or "").strip()
        name = (p.get("name") or "").strip()
        if not qr or not name:
            continue
        db.execute("""
            INSERT INTO inventory
                (qr_code, name, price, category, rarity, set_code, description, stock, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(qr_code) DO UPDATE SET
                name=excluded.name, price=excluded.price,
                category=excluded.category, rarity=excluded.rarity,
                set_code=excluded.set_code, description=excluded.description,
                stock=excluded.stock, last_updated=excluded.last_updated
        """, (
            qr, name,
            float(p.get("price", 0)),
            p.get("category") or "General",
            p.get("rarity")   or "",
            p.get("setCode")  or p.get("set_code") or "",
            p.get("description") or "",
            int(p.get("stockQuantity") or p.get("stock") or 0),
            int(p.get("lastUpdated")   or (time.time() * 1000)),
        ))
        upserted += 1

    db.commit()
    log(f"Pulled {upserted} products from home Pi → local inventory updated")
    return upserted


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    conf     = load_conf()
    home_url = conf["home_pi_url"].rstrip("/")
    timeout  = int(conf.get("timeout_s", 15))

    log(f"Satellite sync starting — home Pi: {home_url}")

    # ── Reachability check ────────────────────────────────────────────────────
    try:
        health = _api_get(f"{home_url}/health", timeout=5)
        log(f"Home Pi reachable  inventory={health.get('inventory','?')}  "
            f"total_sales={health.get('total_sales','?')}")
    except Exception as e:
        log(f"Home Pi not reachable ({e}) — running offline, no sync performed")
        sys.exit(0)   # not an error — this is expected when offline at shows

    # ── Determine sync window ─────────────────────────────────────────────────
    if not os.path.exists(DB_PATH):
        log(f"Database not found at {DB_PATH} — has the server been installed?")
        sys.exit(1)

    db       = get_db()
    since_ms = get_last_sync(db)
    if since_ms:
        since_str = datetime.datetime.fromtimestamp(since_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    else:
        since_str = "never (first sync — sending everything)"
    log(f"Last sync: {since_str}")

    # ── Run sync steps ────────────────────────────────────────────────────────
    errors = 0

    try:
        push_sales(db, home_url, since_ms, timeout)
    except Exception as e:
        log(f"ERROR pushing sales: {e}")
        errors += 1

    try:
        push_deductions(db, home_url, since_ms, timeout)
    except Exception as e:
        log(f"ERROR pushing deductions: {e}")
        errors += 1

    try:
        pull_inventory(db, home_url, timeout)
    except Exception as e:
        log(f"ERROR pulling inventory: {e}")
        errors += 1

    # ── Update sync timestamp (only if all steps succeeded) ───────────────────
    if errors == 0:
        now_ms = int(time.time() * 1000)
        set_last_sync(db, now_ms)
        log("Sync complete ✓ — timestamp saved, home Pi inventory is now local")
    else:
        log(f"Sync finished with {errors} error(s) — timestamp NOT updated "
            "(will retry everything on next boot)")

    db.close()
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
