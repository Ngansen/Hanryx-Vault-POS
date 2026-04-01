"""
HanryxVault POS — Raspberry Pi Backend Server
Runs on port 8080 on your local network (e.g. 10.0.0.1:8080)

Endpoints consumed by the tablet app:
  GET  /health              — connectivity check
  POST /sync/sales          — receives completed SaleEntity JSON from tablet
  POST /inventory/deduct    — receives SoldItemEntry list to decrement stock

Admin dashboard:
  GET  /admin               — web UI showing today's sales + inventory levels
  GET  /admin/sales         — JSON dump of all sales
  GET  /admin/inventory     — JSON dump of full inventory
  POST /admin/inventory     — add/update a product in local inventory
  DELETE /admin/inventory/<qr_code> — remove product
  GET  /download/apk        — download the latest debug APK

Install (on your Pi):
  pip3 install flask

Run:
  python3 server.py
"""

import sqlite3
import json
import datetime
import os
import subprocess
import threading
import time as _time
import urllib.parse
import urllib.request
import urllib.error
import base64
from flask import Flask, request, jsonify, redirect, g

# ---------------------------------------------------------------------------
# Zettle OAuth + Payment configuration
# ---------------------------------------------------------------------------

ZETTLE_CLIENT_ID     = os.environ.get("ZETTLE_CLIENT_ID", "")
ZETTLE_CLIENT_SECRET = os.environ.get("ZETTLE_CLIENT_SECRET", "")
ZETTLE_REDIRECT_URI  = "https://hanryxvault.tailcfc0a3.ts.net/zettle/callback"
ZETTLE_OAUTH_BASE    = "https://oauth.zettle.com"
ZETTLE_POS_BASE      = "https://pos.api.zettle.com"
ZETTLE_APP_SCHEME    = "hanryxvaultdone://zettle-done"

_token_lock   = threading.Lock()
_zettle_state = {"access_token": None, "refresh_token": None, "expires_at": 0.0}


def _basic_auth():
    return base64.b64encode(f"{ZETTLE_CLIENT_ID}:{ZETTLE_CLIENT_SECRET}".encode()).decode()


def _token_post(form_data):
    data = urllib.parse.urlencode(form_data).encode()
    req  = urllib.request.Request(
        f"{ZETTLE_OAUTH_BASE}/token",
        data=data,
        headers={
            "Authorization": f"Basic {_basic_auth()}",
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _store_tokens(result):
    with _token_lock:
        _zettle_state["access_token"]  = result.get("access_token")
        _zettle_state["refresh_token"] = result.get("refresh_token")
        _zettle_state["expires_at"]    = _time.time() + result.get("expires_in", 7200) - 60


def _refresh_token_if_needed():
    with _token_lock:
        rt  = _zettle_state.get("refresh_token")
        exp = _zettle_state.get("expires_at", 0.0)
        at  = _zettle_state.get("access_token")
    if not rt:
        return None
    if at and _time.time() < exp:
        return at
    try:
        result = _token_post({"grant_type": "refresh_token", "refresh_token": rt})
        _store_tokens(result)
        return result.get("access_token")
    except Exception as e:
        print(f"[zettle] Token refresh failed: {e}")
        return None

app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "vault_pos.db")

# ---------------------------------------------------------------------------
# Cloud inventory sources — Pi auto-syncs from these on startup (if empty)
# and whenever /admin/sync-from-cloud is called.
# ---------------------------------------------------------------------------
CLOUD_INVENTORY_SOURCES = [
    "https://inventory-scanner-ngansen84.replit.app/api/inventory",
    "https://hanryxvault.app/api/products",
]


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA cache_size=4000")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sales (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id  TEXT UNIQUE NOT NULL,
            timestamp_ms    INTEGER NOT NULL,
            subtotal        REAL NOT NULL DEFAULT 0,
            tax_amount      REAL NOT NULL DEFAULT 0,
            tip_amount      REAL NOT NULL DEFAULT 0,
            total_amount    REAL NOT NULL DEFAULT 0,
            payment_method  TEXT NOT NULL DEFAULT 'UNKNOWN',
            employee_id     TEXT NOT NULL DEFAULT 'UNKNOWN',
            items_json      TEXT NOT NULL DEFAULT '[]',
            cash_received   REAL NOT NULL DEFAULT 0,
            change_given    REAL NOT NULL DEFAULT 0,
            is_refunded     INTEGER NOT NULL DEFAULT 0,
            received_at     INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)
        );

        CREATE TABLE IF NOT EXISTS inventory (
            qr_code         TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            price           REAL NOT NULL DEFAULT 0,
            category        TEXT NOT NULL DEFAULT 'General',
            rarity          TEXT NOT NULL DEFAULT '',
            set_code        TEXT NOT NULL DEFAULT '',
            description     TEXT NOT NULL DEFAULT '',
            stock           INTEGER NOT NULL DEFAULT 0,
            last_updated    INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)
        );

        CREATE TABLE IF NOT EXISTS stock_deductions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id  TEXT,
            qr_code         TEXT NOT NULL,
            name            TEXT NOT NULL,
            quantity        INTEGER NOT NULL,
            unit_price      REAL NOT NULL,
            line_total      REAL NOT NULL,
            deducted_at     INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)
        );

        CREATE TABLE IF NOT EXISTS scan_queue (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            qr_code     TEXT NOT NULL,
            scanned_at  INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000),
            processed   INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_scan_pending
            ON scan_queue(processed, id);

        CREATE INDEX IF NOT EXISTS idx_sales_timestamp
            ON sales(timestamp_ms);

        CREATE INDEX IF NOT EXISTS idx_stock_qr
            ON stock_deductions(qr_code);

        CREATE TABLE IF NOT EXISTS sale_history (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            name     TEXT NOT NULL,
            price    REAL NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            sold_at  INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)
        );

        CREATE INDEX IF NOT EXISTS idx_sale_history_name
            ON sale_history(name, sold_at);
    """)
    db.commit()
    db.close()
    print("[DB] Initialized vault_pos.db")


def sync_inventory_from_cloud(force: bool = False) -> dict:
    """
    Pull inventory from both Replit cloud sources and upsert into the local DB.
    Called automatically on startup when inventory is empty, and on demand via
    /admin/sync-from-cloud.  Set force=True to sync even if DB already has products.
    """
    import urllib.request, json as _json, time

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    if not force:
        count = db.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
        if count > 0:
            db.close()
            print(f"[cloud-sync] Inventory already has {count} products — skipping auto-sync (use force=True to override)")
            return {"skipped": True, "existing": count}

    total_upserted = 0
    total_skipped = 0
    results = {}

    for url in CLOUD_INVENTORY_SOURCES:
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "HanryxVaultPi/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                items = _json.loads(resp.read().decode())

            if not isinstance(items, list):
                items = items.get("items") or items.get("products") or items.get("inventory") or []

            upserted = 0
            for item in items:
                qr = (item.get("qrCode") or item.get("qr_code") or item.get("barcode") or item.get("id") or "").strip()
                name = (item.get("name") or item.get("title") or "").strip()
                if not qr or not name:
                    total_skipped += 1
                    continue
                try:
                    db.execute("""
                        INSERT INTO inventory (qr_code, name, price, category, rarity, set_code, description, stock, last_updated)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(qr_code) DO UPDATE SET
                            name=excluded.name, price=excluded.price, category=excluded.category,
                            rarity=excluded.rarity, set_code=excluded.set_code,
                            description=excluded.description, stock=excluded.stock,
                            last_updated=excluded.last_updated
                    """, (
                        qr, name,
                        float(item.get("price", 0) or 0),
                        item.get("category") or "General",
                        item.get("rarity") or "",
                        item.get("setCode") or item.get("set_code") or item.get("setName") or item.get("set_name") or "",
                        item.get("description") or "",
                        int(item.get("stock") or item.get("stockQuantity") or item.get("quantity") or 0),
                        int(time.time() * 1000),
                    ))
                    upserted += 1
                except Exception as row_err:
                    print(f"[cloud-sync] Row error ({url}): {row_err}")
                    total_skipped += 1

            db.commit()
            total_upserted += upserted
            results[url] = {"ok": True, "upserted": upserted}
            print(f"[cloud-sync] {url} → {upserted} products upserted")

        except Exception as e:
            results[url] = {"ok": False, "error": str(e)}
            print(f"[cloud-sync] Failed to fetch {url}: {e}")

    db.close()
    print(f"[cloud-sync] Done — total upserted={total_upserted} skipped={total_skipped}")
    return {"upserted": total_upserted, "skipped": total_skipped, "sources": results}


def _cleanup_scan_queue():
    """Delete processed scan entries older than 1 hour to keep the table small."""
    import threading
    try:
        cutoff = int((__import__('time').time() - 3600) * 1000)
        db = sqlite3.connect(DB_PATH)
        db.execute("DELETE FROM scan_queue WHERE processed = 1 AND scanned_at < ?", (cutoff,))
        db.commit()
        db.close()
    except Exception:
        pass
    threading.Timer(3600, _cleanup_scan_queue).start()


def get_connection():
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA cache_size=4000")
    return db


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _now_ms():
    return int(datetime.datetime.now().timestamp() * 1000)


def _cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return response


@app.after_request
def after_request(response):
    return _cors(response)


@app.before_request
def handle_options():
    if request.method == "OPTIONS":
        return _cors(jsonify({}))


# ---------------------------------------------------------------------------
# App endpoints (called by the tablet)
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    """Simple ping — tablet calls this to verify Pi is reachable."""
    return jsonify({"status": "ok", "server": "HanryxVault Pi", "time_ms": int(_time.time() * 1000)})


# ---------------------------------------------------------------------------
# Expo Scanner Relay — Expo app POSTs scans here, tablet polls and picks them up
# ---------------------------------------------------------------------------

@app.route("/scan", methods=["POST"])
def scan_post():
    """
    Called by the Expo scanner app whenever it scans a QR/barcode.
    Body: { "qrCode": "PRODUCT-CODE-HERE" }
    The tablet polls /scan/pending and will add the product to cart automatically.
    """
    data = request.get_json(force=True, silent=True) or {}
    qr_code = (data.get("qrCode") or data.get("qr_code") or data.get("code") or "").strip()
    if not qr_code:
        return jsonify({"error": "qrCode is required"}), 400
    db = get_db()
    db.execute(
        "INSERT INTO scan_queue (qr_code) VALUES (?)", (qr_code,)
    )
    db.commit()
    print(f"[scan] Queued: {qr_code}")
    return jsonify({"ok": True, "queued": qr_code}), 201


@app.route("/scan/pending", methods=["GET"])
def scan_pending():
    """
    Called by the tablet every 1.5 s.
    Returns the oldest unprocessed scan, or {id:0, qrCode:""} if the queue is empty.
    """
    db = get_db()
    row = db.execute(
        "SELECT id, qr_code FROM scan_queue WHERE processed = 0 ORDER BY id ASC LIMIT 1"
    ).fetchone()
    if row:
        return jsonify({"id": row["id"], "qrCode": row["qr_code"]})
    return jsonify({"id": 0, "qrCode": ""})


@app.route("/scan/ack/<int:scan_id>", methods=["POST"])
def scan_ack(scan_id):
    """
    Called by the tablet after it has handled a scan — marks it processed.
    """
    db = get_db()
    db.execute("UPDATE scan_queue SET processed = 1 WHERE id = ?", (scan_id,))
    db.commit()
    return jsonify({"ok": True, "acked": scan_id})


@app.route("/sync/sales", methods=["POST"])
def sync_sales():
    """
    Receives a JSON array of SaleEntity objects from the tablet.
    Inserts each one (ignores duplicates by transactionId).
    """
    data = request.get_json(force=True, silent=True)
    if not data or not isinstance(data, list):
        return jsonify({"error": "Expected a JSON array of sales"}), 400

    db = get_db()
    inserted = 0
    skipped = 0

    for sale in data:
        transaction_id = sale.get("transactionId") or sale.get("transaction_id")
        if not transaction_id:
            skipped += 1
            continue
        try:
            db.execute("""
                INSERT OR IGNORE INTO sales
                    (transaction_id, timestamp_ms, subtotal, tax_amount, tip_amount,
                     total_amount, payment_method, employee_id, items_json,
                     cash_received, change_given, is_refunded)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                transaction_id,
                sale.get("timestamp", _now_ms()),
                sale.get("subtotal", 0.0),
                sale.get("taxAmount", 0.0),
                sale.get("tipAmount", 0.0),
                sale.get("totalAmount", 0.0),
                sale.get("paymentMethod", "UNKNOWN"),
                sale.get("employeeId", "UNKNOWN"),
                json.dumps(sale.get("items", [])),
                sale.get("cashReceived", 0.0),
                sale.get("changeGiven", 0.0),
                1 if sale.get("isRefunded", False) else 0,
            ))
            if db.execute("SELECT changes()").fetchone()[0] > 0:
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"[sync/sales] Error on {transaction_id}: {e}")
            skipped += 1

    db.commit()
    print(f"[sync/sales] inserted={inserted} skipped={skipped}")
    return jsonify({"inserted": inserted, "skipped": skipped}), 200


@app.route("/inventory/deduct", methods=["POST"])
def inventory_deduct():
    """
    Receives a JSON array of SoldItemEntry objects:
      [{ qrCode, name, quantity, unitPrice, lineTotal }]
    Decrements stock for each QR code in local inventory.
    Logs every deduction regardless of whether the product is tracked.
    """
    data = request.get_json(force=True, silent=True)
    if not data or not isinstance(data, list):
        return jsonify({"error": "Expected a JSON array of sold items"}), 400

    db = get_db()
    deducted = 0
    unknown = 0

    for item in data:
        qr_code = item.get("qrCode", "")
        name = item.get("name", "Unknown")
        quantity = int(item.get("quantity", 1))
        unit_price = float(item.get("unitPrice", 0.0))
        line_total = float(item.get("lineTotal", unit_price * quantity))

        # Log the deduction event
        db.execute("""
            INSERT INTO stock_deductions (qr_code, name, quantity, unit_price, line_total)
            VALUES (?, ?, ?, ?, ?)
        """, (qr_code, name, quantity, unit_price, line_total))

        # Decrement if product exists in local inventory
        result = db.execute("""
            UPDATE inventory
            SET stock = MAX(0, stock - ?),
                last_updated = ?
            WHERE qr_code = ?
        """, (quantity, _now_ms(), qr_code))

        if result.rowcount > 0:
            deducted += 1
        else:
            unknown += 1

    db.commit()
    print(f"[inventory/deduct] deducted={deducted} unknown_sku={unknown}")
    return jsonify({"deducted": deducted, "unknown_skus": unknown}), 200


# ---------------------------------------------------------------------------
# Tablet inventory endpoint — returns merged product list, no images
# ---------------------------------------------------------------------------

@app.route("/inventory", methods=["GET"])
def get_inventory():
    """
    Called by the tablet to pull the product catalogue.
    Optional ?since=<epoch_ms> — returns only products updated after that timestamp (delta sync).
    Optional ?q=<text>        — text search filter.
    Matches the ProductEntity shape the tablet expects:
      { qrCode, name, price, category, rarity, setCode, description, stockQuantity, lastUpdated }
    """
    db     = get_db()
    search = request.args.get("q", "").strip().lower()
    since  = request.args.get("since", "")

    since_clause = ""
    since_args   = []
    if since:
        try:
            since_ms     = int(since)
            since_clause = "AND last_updated > ?" if search else "WHERE last_updated > ?"
            since_args   = [since_ms]
        except (ValueError, TypeError):
            pass

    if search:
        rows = db.execute(f"""
            SELECT qr_code, name, price, category, rarity, set_code, description, stock, last_updated
            FROM inventory
            WHERE (LOWER(name) LIKE ? OR LOWER(qr_code) LIKE ? OR LOWER(category) LIKE ?)
            {since_clause}
            ORDER BY name ASC
        """, [f"%{search}%", f"%{search}%", f"%{search}%"] + since_args).fetchall()
    else:
        rows = db.execute(f"""
            SELECT qr_code, name, price, category, rarity, set_code, description, stock, last_updated
            FROM inventory {since_clause} ORDER BY name ASC
        """, since_args).fetchall()

    products = [
        {
            "qrCode":        r["qr_code"],
            "name":          r["name"],
            "price":         r["price"],
            "category":      r["category"] or "General",
            "rarity":        r["rarity"] or "",
            "setCode":       r["set_code"] or "",
            "description":   r["description"] or "",
            "stockQuantity": r["stock"],
            "lastUpdated":   r["last_updated"],
        }
        for r in rows
    ]
    return jsonify(products)


@app.route("/push/inventory", methods=["POST"])
def push_inventory():
    """
    Receives product data pushed from your websites or scanner app.
    Accepts a single product OR an array. No images stored — text fields only.
    Body (single or array):
      { qrCode, name, price, category, rarity, setCode, description, stock }
    """
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    items = data if isinstance(data, list) else [data]
    db = get_db()
    upserted = 0
    errors = 0

    for item in items:
        qr_code = item.get("qrCode") or item.get("qr_code") or item.get("barcode") or item.get("id")
        name = item.get("name") or item.get("title") or item.get("productName")
        if not qr_code or not name:
            errors += 1
            continue
        try:
            db.execute("""
                INSERT INTO inventory (qr_code, name, price, category, rarity, set_code, description, stock, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(qr_code) DO UPDATE SET
                    name        = excluded.name,
                    price       = excluded.price,
                    category    = excluded.category,
                    rarity      = excluded.rarity,
                    set_code    = excluded.set_code,
                    description = excluded.description,
                    stock       = excluded.stock,
                    last_updated= excluded.last_updated
            """, (
                str(qr_code),
                str(name),
                float(item.get("price", 0.0)),
                str(item.get("category", "General")),
                str(item.get("rarity", "")),
                str(item.get("setCode") or item.get("set_code", "")),
                str(item.get("description", "")),
                int(item.get("stock") or item.get("stockQuantity") or item.get("quantity", 0)),
                _now_ms(),
            ))
            upserted += 1
        except Exception as e:
            print(f"[push/inventory] Error on {qr_code}: {e}")
            errors += 1

    db.commit()
    print(f"[push/inventory] upserted={upserted} errors={errors}")
    return jsonify({"upserted": upserted, "errors": errors}), 200


@app.route("/push/inventory/csv", methods=["POST"])
def push_inventory_csv():
    """
    Bulk-import products from a CSV file upload.
    Column header must include: qrCode (or barcode), name, price.
    Optional: category, rarity, setCode, description, stock.

    Usage: curl -X POST http://10.0.0.1:8080/push/inventory/csv -F file=@products.csv
    """
    import csv, io
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "Upload a CSV with field 'file'"}), 400

    content = f.read().decode("utf-8-sig")  # strip BOM if present
    reader = csv.DictReader(io.StringIO(content))

    db = get_db()
    upserted = 0
    skipped = 0

    for row in reader:
        qr_code = (row.get("qrCode") or row.get("barcode") or row.get("qr_code") or "").strip()
        name = (row.get("name") or row.get("title") or "").strip()
        if not qr_code or not name:
            skipped += 1
            continue
        try:
            price = float(row.get("price", 0) or 0)
            stock = int(row.get("stock") or row.get("stockQuantity") or row.get("quantity") or 0)
            db.execute("""
                INSERT INTO inventory (qr_code, name, price, category, rarity, set_code, description, stock, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(qr_code) DO UPDATE SET
                    name=excluded.name, price=excluded.price, category=excluded.category,
                    rarity=excluded.rarity, set_code=excluded.set_code,
                    description=excluded.description, stock=excluded.stock,
                    last_updated=excluded.last_updated
            """, (
                qr_code, name, price,
                row.get("category", "General"),
                row.get("rarity", ""),
                row.get("setCode") or row.get("set_code", ""),
                row.get("description", ""),
                stock, _now_ms(),
            ))
            upserted += 1
        except Exception as e:
            print(f"[csv] Row error: {e} — {row}")
            skipped += 1

    db.commit()
    return jsonify({"upserted": upserted, "skipped": skipped}), 200


# ---------------------------------------------------------------------------
# Admin endpoints (browser dashboard)
# ---------------------------------------------------------------------------

@app.route("/admin", methods=["GET"])
def admin_dashboard():
    """Simple HTML dashboard — open in any browser on your network."""
    db = get_db()

    # Today's sales
    midnight_ms = int(datetime.datetime.combine(
        datetime.date.today(), datetime.time.min
    ).timestamp() * 1000)

    today_sales = db.execute("""
        SELECT COUNT(*) as count, COALESCE(SUM(total_amount), 0) as revenue,
               COALESCE(SUM(tax_amount), 0) as tax, COALESCE(SUM(tip_amount), 0) as tips
        FROM sales WHERE timestamp_ms >= ?
    """, (midnight_ms,)).fetchone()

    recent_sales = db.execute("""
        SELECT transaction_id, timestamp_ms, total_amount, payment_method, employee_id
        FROM sales ORDER BY timestamp_ms DESC LIMIT 25
    """).fetchall()

    low_stock = db.execute("""
        SELECT qr_code, name, stock, price, category
        FROM inventory WHERE stock <= 5 ORDER BY stock ASC
    """).fetchall()

    inventory = db.execute("""
        SELECT qr_code, name, stock, price, category
        FROM inventory ORDER BY name ASC
    """).fetchall()

    def fmt_time(ms):
        if not ms:
            return "—"
        return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")

    rows_recent = "".join(
        f"<tr><td>{r['transaction_id'][:12]}…</td><td>${r['total_amount']:.2f}</td>"
        f"<td>{r['payment_method']}</td><td>{r['employee_id']}</td>"
        f"<td>{fmt_time(r['timestamp_ms'])}</td></tr>"
        for r in recent_sales
    )

    rows_low = "".join(
        f"<tr style='color:{'#f44336' if r['stock']==0 else '#ff9800'}'>"
        f"<td>{r['name']}</td><td>{r['qr_code']}</td>"
        f"<td><b>{r['stock']}</b></td><td>${r['price']:.2f}</td><td>{r['category']}</td></tr>"
        for r in low_stock
    ) or "<tr><td colspan='5' style='color:#4caf50'>All stock levels healthy ✓</td></tr>"

    rows_inv = "".join(
        f"<tr data-qr=\"{r['qr_code']}\" data-name=\"{r['name']}\" data-price=\"{r['price']}\" "
        f"data-cat=\"{r['category']}\" data-stock=\"{r['stock']}\" style=\"cursor:pointer\">"
        f"<td>{r['name']}</td><td><code style='color:#aaa'>{r['qr_code']}</code></td>"
        f"<td>{r['stock']}</td><td>${r['price']:.2f}</td><td>{r['category']}</td>"
        f"<td><button class='btn-del' onclick=\"event.stopPropagation();deleteProduct('{r['qr_code']}')\">DEL</button></td></tr>"
        for r in inventory
    ) or "<tr><td colspan='6' style='color:#555;text-align:center;padding:20px'>No products yet — use the form above to add your first product</td></tr>"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HanryxVault POS — Pi Dashboard</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: #0d0d0d; color: #e0e0e0; padding: 24px; }}
  h1 {{ color: #FFD700; font-size: 22px; margin-bottom: 4px; }}
  .subtitle {{ color: #666; font-size: 13px; margin-bottom: 24px; }}
  .cards {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 28px; }}
  .card {{ background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 10px;
           padding: 18px 24px; min-width: 160px; flex: 1; }}
  .card label {{ color: #888; font-size: 11px; letter-spacing: 1px; text-transform: uppercase; }}
  .card .value {{ color: #FFD700; font-size: 28px; font-weight: 900; margin-top: 4px; }}
  .card .value.green {{ color: #4caf50; }}
  h2 {{ color: #aaa; font-size: 13px; letter-spacing: 1.5px; text-transform: uppercase;
        margin-bottom: 10px; margin-top: 28px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; color: #555; padding: 8px 10px; border-bottom: 1px solid #222; }}
  td {{ padding: 8px 10px; border-bottom: 1px solid #1a1a1a; }}
  tr:hover td {{ background: #1a1a1a; }}
  .badge {{ background: #222; border-radius: 4px; padding: 2px 7px; font-size: 11px; color: #aaa; }}
  a {{ color: #FFD700; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .form-panel {{ background: #111; border: 1px solid #2a2a2a; border-radius: 10px; padding: 20px; margin-top: 28px; }}
  .form-panel h2 {{ margin-top: 0; margin-bottom: 16px; }}
  .form-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 12px; }}
  .form-grid input, .form-grid select {{ width: 100%; background: #1a1a1a; border: 1px solid #333;
    border-radius: 6px; color: #e0e0e0; padding: 8px 10px; font-size: 13px; }}
  .form-grid input:focus, .form-grid select:focus {{ outline: none; border-color: #FFD700; }}
  .form-grid label {{ display: block; color: #666; font-size: 10px; letter-spacing: 1px;
    text-transform: uppercase; margin-bottom: 4px; }}
  .btn-gold {{ background: #FFD700; color: #000; border: none; border-radius: 6px;
    padding: 10px 24px; font-weight: 900; font-size: 13px; cursor: pointer; letter-spacing: 1px; }}
  .btn-gold:hover {{ background: #ffe033; }}
  .btn-del {{ background: none; border: 1px solid #c62828; color: #c62828; border-radius: 4px;
    padding: 3px 8px; font-size: 11px; cursor: pointer; }}
  .btn-del:hover {{ background: #c62828; color: #fff; }}
  #toast {{ position: fixed; bottom: 24px; right: 24px; background: #4caf50; color: #fff;
    padding: 12px 20px; border-radius: 8px; font-weight: bold; display: none; z-index: 99; }}
  #toast.err {{ background: #c62828; }}
</style>
</head>
<body>
<h1>HanryxVault POS</h1>
<div class="subtitle">Raspberry Pi Dashboard &nbsp;·&nbsp; <span id="clock"></span>
  &nbsp;·&nbsp; <a href="/admin/sales">Sales JSON</a>
  &nbsp;·&nbsp; <a href="/admin/inventory">Inventory JSON</a>
  &nbsp;·&nbsp; <a href="/download/apk" style="background:#FFD700;color:#000;padding:3px 10px;border-radius:4px;font-weight:bold;">⬇ Download APK</a>
</div>

<div class="cards">
  <div class="card"><label>Today's Sales</label><div class="value green">{today_sales['count']}</div></div>
  <div class="card"><label>Revenue Today</label><div class="value">${today_sales['revenue']:.2f}</div></div>
  <div class="card"><label>Tax Collected</label><div class="value">${today_sales['tax']:.2f}</div></div>
  <div class="card"><label>Tips Today</label><div class="value">${today_sales['tips']:.2f}</div></div>
</div>

<!-- SYNC FROM CLOUD -->
<div class="form-panel" style="margin-top:16px;border-color:#4caf50;background:#001a05">
  <h2 style="color:#4caf50">☁️ SYNC PRODUCTS FROM YOUR REPLIT SITES</h2>
  <p style="color:#aaa;font-size:13px;margin-bottom:12px">
    Pulls your full product catalogue from both Replit inventory websites into the Pi database.
    The tablet will pick up the products on its next sync.
    Use <b>Force Re-Sync</b> to refresh products that already exist.
  </p>
  <div style="display:flex;gap:12px;flex-wrap:wrap">
    <button onclick="syncFromCloud(false)" style="background:#4caf50;color:#000;border:none;padding:10px 22px;border-radius:6px;font-weight:bold;cursor:pointer;font-size:14px">
      ☁️ Sync Now (new products only)
    </button>
    <button onclick="syncFromCloud(true)" style="background:#1a3a20;color:#4caf50;border:1px solid #4caf50;padding:10px 22px;border-radius:6px;font-weight:bold;cursor:pointer;font-size:14px">
      🔄 Force Re-Sync All
    </button>
  </div>
  <div id="sync-result" style="margin-top:12px;font-size:13px;color:#aaa;display:none"></div>
  <p style="color:#555;font-size:11px;margin-top:10px">
    Sources: inventory-scanner-ngansen84.replit.app &nbsp;|&nbsp; hanryxvault.cards
  </p>
</div>

<!-- EXPO SCANNER SETUP -->
<div class="form-panel" style="margin-top:16px;border-color:#FFD700;background:#0d0f00">
  <h2 style="color:#FFD700">📱 EXPO SCANNER → CART SETUP</h2>
  <p style="color:#aaa;font-size:13px;margin-bottom:16px">
    Configure your Expo scanner app to POST each scanned code to <b>one</b> of the endpoints below.
    The tablet picks it up within 1–2 seconds and adds the matching product to the cart automatically.
  </p>

  <p style="color:#FFD700;font-size:12px;margin-bottom:6px;font-weight:bold">☁️ OPTION 1 — Cloud Hub (works from anywhere, no LAN needed)</p>
  <div style="background:#1a1a00;border:1px solid #555;border-radius:6px;padding:12px 16px;font-family:monospace;font-size:13px;margin-bottom:14px">
    <span style="color:#888">POST</span> &nbsp;
    <span style="color:#FFD700">https://updated-hanryx-vault-pos-system.replit.app/scan</span>
    <br><br>
    <span style="color:#888">Body (JSON):</span>&nbsp;
    <span style="color:#4caf50">{{ "qrCode": "PRODUCT-QR-CODE" }}</span>
  </div>

  <p style="color:#aaa;font-size:12px;margin-bottom:6px;font-weight:bold">🏠 OPTION 2 — Pi Direct (same Wi-Fi LAN only)</p>
  <div style="background:#0d0d0d;border:1px solid #444;border-radius:6px;padding:12px 16px;font-family:monospace;font-size:13px;margin-bottom:12px">
    <span style="color:#888">POST</span> &nbsp;
    <span style="color:#aaa" id="scan-url">http://&lt;pi-ip&gt;:8080/scan</span>
    <br><br>
    <span style="color:#888">Body (JSON):</span>&nbsp;
    <span style="color:#4caf50">{{ "qrCode": "PRODUCT-QR-CODE" }}</span>
  </div>

  <p style="color:#666;font-size:11px;margin-top:4px">
    ⚡ The tablet checks the cloud every 2 s and the Pi every 1.5 s.
    The QR code must match a product already synced to the tablet's inventory.
  </p>
</div>

<!-- ADD / EDIT PRODUCT FORM -->
<div class="form-panel">
  <h2>➕ ADD / UPDATE PRODUCT</h2>
  <p style="color:#666;font-size:12px;margin-bottom:16px;">
    Fill in at minimum Name, QR Code and Price. If a product with the same QR code already exists it will be updated.
    After saving, the tablet will receive the new product the next time it syncs.
  </p>
  <div class="form-grid">
    <div><label>Product Name *</label><input id="f-name" placeholder="e.g. Pikachu VMAX" /></div>
    <div><label>QR / Barcode *</label><input id="f-qr" placeholder="e.g. SM-123-PIKA" /></div>
    <div><label>Price ($) *</label><input id="f-price" type="number" step="0.01" min="0" placeholder="9.99" /></div>
    <div><label>Category</label><input id="f-cat" placeholder="Pokemon" value="General" /></div>
    <div><label>Rarity</label><input id="f-rarity" placeholder="Rare Holo" /></div>
    <div><label>Set Code</label><input id="f-set" placeholder="BRS" /></div>
    <div><label>Stock Qty</label><input id="f-stock" type="number" min="0" placeholder="1" value="1" /></div>
    <div><label>Description</label><input id="f-desc" placeholder="Optional notes" /></div>
  </div>
  <div style="margin-top:16px;display:flex;gap:10px;align-items:center;">
    <button class="btn-gold" onclick="addProduct()">SAVE TO INVENTORY</button>
    <button onclick="clearForm()" style="background:none;border:1px solid #444;color:#aaa;border-radius:6px;padding:9px 18px;cursor:pointer;font-size:13px;">CLEAR</button>
  </div>
</div>

<h2>⚠ Low Stock (≤ 5 units)</h2>
<table>
  <tr><th>Product</th><th>QR Code</th><th>Stock</th><th>Price</th><th>Category</th></tr>
  {rows_low}
</table>

<h2>Recent Transactions</h2>
<table>
  <tr><th>Transaction</th><th>Total</th><th>Method</th><th>Employee</th><th>Time</th></tr>
  {rows_recent}
</table>

<h2>Full Inventory <span style="color:#555;font-size:11px;font-weight:normal;letter-spacing:0">— click a row to edit</span></h2>
<table id="inv-table">
  <tr><th>Product</th><th>QR Code</th><th>Stock</th><th>Price</th><th>Category</th><th></th></tr>
  {rows_inv}
</table>

<div id="toast"></div>

<script>
  function tick() {{
    document.getElementById('clock').textContent = new Date().toLocaleTimeString();
  }}
  tick(); setInterval(tick, 1000);

  function showToast(msg, isErr) {{
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.className = isErr ? 'err' : '';
    t.style.display = 'block';
    setTimeout(() => t.style.display = 'none', 3000);
  }}

  function clearForm() {{
    ['f-name','f-qr','f-price','f-rarity','f-set','f-desc'].forEach(id => document.getElementById(id).value = '');
    document.getElementById('f-cat').value = 'General';
    document.getElementById('f-stock').value = '1';
  }}

  async function addProduct() {{
    const name = document.getElementById('f-name').value.trim();
    const qrCode = document.getElementById('f-qr').value.trim();
    const price = parseFloat(document.getElementById('f-price').value) || 0;
    const category = document.getElementById('f-cat').value.trim() || 'General';
    const rarity = document.getElementById('f-rarity').value.trim();
    const setCode = document.getElementById('f-set').value.trim();
    const description = document.getElementById('f-desc').value.trim();
    const stock = parseInt(document.getElementById('f-stock').value) || 0;

    if (!name || !qrCode) {{
      showToast('Name and QR Code are required', true);
      return;
    }}

    try {{
      const resp = await fetch('/push/inventory', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ qrCode, name, price, category, rarity, setCode, description, stockQuantity: stock }})
      }});
      const data = await resp.json();
      if (resp.ok) {{
        showToast('Product saved — tablet will sync on next refresh');
        clearForm();
        setTimeout(() => location.reload(), 1200);
      }} else {{
        showToast(data.error || 'Failed to save product', true);
      }}
    }} catch(e) {{
      showToast('Network error: ' + e.message, true);
    }}
  }}

  async function deleteProduct(qrCode) {{
    if (!confirm('Delete ' + qrCode + ' from inventory?')) return;
    try {{
      const resp = await fetch('/admin/inventory/' + encodeURIComponent(qrCode), {{ method: 'DELETE' }});
      if (resp.ok) {{
        showToast('Product deleted');
        setTimeout(() => location.reload(), 800);
      }} else {{
        showToast('Delete failed', true);
      }}
    }} catch(e) {{
      showToast('Error: ' + e.message, true);
    }}
  }}

  // Click a row to pre-fill the edit form
  document.getElementById('inv-table').addEventListener('click', function(e) {{
    const row = e.target.closest('tr[data-qr]');
    if (!row) return;
    document.getElementById('f-name').value = row.dataset.name || '';
    document.getElementById('f-qr').value = row.dataset.qr || '';
    document.getElementById('f-price').value = row.dataset.price || '';
    document.getElementById('f-cat').value = row.dataset.cat || 'General';
    document.getElementById('f-stock').value = row.dataset.stock || '1';
    window.scrollTo({{top: 0, behavior: 'smooth'}});
  }});

  async function syncFromCloud(force) {{
    const btn = event.target;
    const result = document.getElementById('sync-result');
    btn.disabled = true;
    btn.textContent = '⏳ Syncing…';
    result.style.display = 'block';
    result.style.color = '#aaa';
    result.textContent = 'Contacting your Replit sites…';
    try {{
      const url = '/admin/sync-from-cloud' + (force ? '?force=1' : '');
      const resp = await fetch(url, {{ method: 'POST' }});
      const data = await resp.json();
      if (data.skipped) {{
        result.style.color = '#FFD700';
        result.textContent = `Already have ${{data.existing}} products. Use Force Re-Sync to refresh them.`;
      }} else {{
        result.style.color = '#4caf50';
        result.textContent = `✓ Synced ${{data.upserted}} products from your Replit sites. Tablet will update on next refresh.`;
        setTimeout(() => location.reload(), 2000);
      }}
    }} catch(e) {{
      result.style.color = '#f44336';
      result.textContent = 'Sync failed: ' + e.message;
    }}
    btn.disabled = false;
    btn.textContent = force ? '🔄 Force Re-Sync All' : '☁️ Sync Now (new products only)';
  }}
</script>
</body>
</html>"""
    from flask import make_response
    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


@app.route("/download/apk", methods=["GET"])
def download_apk():
    """Serve the latest debug APK for sideloading onto the tablet."""
    import os
    from flask import send_file, abort
    apk_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "hanryx-pos", "app", "build", "outputs", "apk", "debug", "app-debug.apk"
    )
    if not os.path.exists(apk_path):
        abort(404, description="APK not built yet — run the Build APK workflow first.")
    return send_file(
        apk_path,
        mimetype="application/vnd.android.package-archive",
        as_attachment=True,
        download_name="HanryxVault-POS.apk"
    )


@app.route("/admin/sales", methods=["GET"])
def admin_sales():
    db = get_db()
    rows = db.execute("SELECT * FROM sales ORDER BY timestamp_ms DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/admin/inventory", methods=["GET"])
def admin_inventory_get():
    db = get_db()
    rows = db.execute("SELECT * FROM inventory ORDER BY name ASC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/admin/inventory", methods=["POST"])
def admin_inventory_post():
    """
    Add or update a product in local Pi inventory.
    Body: { qrCode, name, price, category, stock }
    """
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    qr_code = data.get("qrCode") or data.get("qr_code")
    name = data.get("name")
    if not qr_code or not name:
        return jsonify({"error": "qrCode and name are required"}), 400

    db = get_db()
    db.execute("""
        INSERT INTO inventory (qr_code, name, price, category, rarity, set_code, description, stock, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(qr_code) DO UPDATE SET
            name        = excluded.name,
            price       = excluded.price,
            category    = excluded.category,
            rarity      = excluded.rarity,
            set_code    = excluded.set_code,
            description = excluded.description,
            stock       = excluded.stock,
            last_updated= excluded.last_updated
    """, (
        qr_code,
        name,
        float(data.get("price", 0.0)),
        data.get("category", "General"),
        data.get("rarity", ""),
        data.get("setCode") or data.get("set_code", ""),
        data.get("description", ""),
        int(data.get("stock", 0)),
        _now_ms(),
    ))
    db.commit()
    return jsonify({"ok": True, "qrCode": qr_code, "name": name}), 201


@app.route("/admin/inventory/<qr_code>", methods=["DELETE"])
def admin_inventory_delete(qr_code):
    db = get_db()
    db.execute("DELETE FROM inventory WHERE qr_code = ?", (qr_code,))
    db.commit()
    return jsonify({"ok": True, "deleted": qr_code})


@app.route("/admin/sync-from-cloud", methods=["POST"])
def admin_sync_from_cloud():
    """
    Pull products from both Replit inventory sites and upsert into Pi DB.
    Pass ?force=1 to re-sync even if inventory already has products.
    """
    force = request.args.get("force", "0") == "1"
    result = sync_inventory_from_cloud(force=force)
    return jsonify(result), 200


_update_lock = threading.Lock()
_update_status = {"running": False, "last_result": None}

def _run_import_script(args: list):
    """Run import_tcg_db.py in a background thread and store the result."""
    global _update_status
    script = os.path.join(os.path.dirname(__file__), "import_tcg_db.py")
    cmd = ["python3", script] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        _update_status["last_result"] = {
            "returncode": result.returncode,
            "stdout": result.stdout[-3000:],
            "stderr": result.stderr[-1000:],
        }
    except subprocess.TimeoutExpired:
        _update_status["last_result"] = {"returncode": -1, "stdout": "", "stderr": "Timed out after 600s"}
    except Exception as e:
        _update_status["last_result"] = {"returncode": -1, "stdout": "", "stderr": str(e)}
    finally:
        _update_status["running"] = False


@app.route("/admin/update-prices", methods=["POST"])
def admin_update_prices():
    """Remotely trigger `import_tcg_db.py --update-prices` without SSH.
    Runs in the background; poll /admin/update-status for progress."""
    with _update_lock:
        if _update_status["running"]:
            return jsonify({"status": "already_running", "message": "An update is already in progress"}), 409
        _update_status["running"] = True
        _update_status["last_result"] = None
    threading.Thread(target=_run_import_script, args=(["--update-prices"],), daemon=True).start()
    return jsonify({"status": "started", "message": "Price update running in background. Poll /admin/update-status for progress."}), 202


@app.route("/admin/update-db", methods=["POST"])
def admin_update_db():
    """Remotely trigger `import_tcg_db.py --update` to pull new sets + refresh prices.
    WARNING: slow (10+ minutes). Runs in background; poll /admin/update-status."""
    with _update_lock:
        if _update_status["running"]:
            return jsonify({"status": "already_running", "message": "An update is already in progress"}), 409
        _update_status["running"] = True
        _update_status["last_result"] = None
    threading.Thread(target=_run_import_script, args=(["--update"],), daemon=True).start()
    return jsonify({"status": "started", "message": "Full DB update running in background. Poll /admin/update-status for progress."}), 202


@app.route("/admin/update-status", methods=["GET"])
def admin_update_status():
    """Check whether a DB update job is running and see the last result."""
    return jsonify({
        "running": _update_status["running"],
        "last_result": _update_status["last_result"],
    }), 200


# ---------------------------------------------------------------------------
# Zettle OAuth + Payment routes
# ---------------------------------------------------------------------------

@app.route("/zettle/login", methods=["GET"])
def zettle_login():
    if not ZETTLE_CLIENT_ID or not ZETTLE_CLIENT_SECRET:
        return jsonify({"error": "ZETTLE_CLIENT_ID / ZETTLE_CLIENT_SECRET not set"}), 500
    url = (
        f"{ZETTLE_OAUTH_BASE}/authorize"
        f"?response_type=code"
        f"&client_id={urllib.parse.quote(ZETTLE_CLIENT_ID)}"
        f"&redirect_uri={urllib.parse.quote(ZETTLE_REDIRECT_URI)}"
        f"&scope=READ:PURCHASE+WRITE:PURCHASE"
    )
    return redirect(url)


@app.route("/zettle/callback", methods=["GET"])
def zettle_callback():
    code  = request.args.get("code", "")
    error = request.args.get("error", "")
    if error or not code:
        return jsonify({"error": error or "Missing code"}), 400
    try:
        result = _token_post({
            "grant_type":   "authorization_code",
            "code":         code,
            "redirect_uri": ZETTLE_REDIRECT_URI,
        })
        _store_tokens(result)
        print("[zettle] Authenticated successfully via DuckDNS callback")
        html = f"""<!DOCTYPE html><html>
<head><title>Zettle Authorized</title>
<meta http-equiv="refresh" content="1;url={ZETTLE_APP_SCHEME}"></head>
<body style="font-family:sans-serif;background:#111;color:#FFD700;text-align:center;padding:60px">
<h2>&#10003; Zettle Connected</h2>
<p style="color:#aaa">Returning to HanryxVault POS&hellip;</p>
<script>setTimeout(()=>location.href="{ZETTLE_APP_SCHEME}",500)</script>
</body></html>"""
        return html, 200, {"Content-Type": "text/html"}
    except Exception as e:
        print(f"[zettle] Callback error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/zettle/auth", methods=["GET"])
def zettle_auth():
    token = _refresh_token_if_needed()
    return jsonify({"authenticated": bool(token)})


@app.route("/zettle/pay", methods=["POST"])
def zettle_pay():
    token = _refresh_token_if_needed()
    if not token:
        return jsonify({"error": "Not authenticated — visit /zettle/login first"}), 401
    body      = request.get_json(force=True, silent=True) or {}
    amount    = body.get("amount")
    currency  = body.get("currency", "USD")
    reference = body.get("reference", "")
    if not amount:
        return jsonify({"error": "amount is required"}), 400
    payload = json.dumps({
        "type":       "CARD",
        "amount":     amount,
        "currency":   currency,
        "references": {"paymentReference": reference},
    }).encode()
    req = urllib.request.Request(
        f"{ZETTLE_POS_BASE}/v1/payments",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return jsonify({"ok": True, "payment": json.loads(resp.read())})
    except urllib.error.HTTPError as e:
        body_err = e.read().decode()
        if e.code == 401:
            with _token_lock:
                _zettle_state["access_token"] = None
            return jsonify({"error": "Token expired", "detail": body_err}), 401
        return jsonify({"error": f"Zettle API error {e.code}", "detail": body_err}), e.code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# /inventory/decrement — cloud-compatible alias for /inventory/deduct
# The tablet posts to this endpoint after a sale when pointing at Pi-only mode.
# ---------------------------------------------------------------------------

@app.route("/inventory/decrement", methods=["POST"])
def inventory_decrement():
    """
    Cloud-compatible alias for /inventory/deduct.
    Accepts [{ qrCode, quantity }] and decrements stock in local DB.
    stock never goes below 0.
    """
    data = request.get_json(force=True, silent=True)
    if not data or not isinstance(data, list):
        return jsonify({"error": "Expected a JSON array"}), 400
    db      = get_db()
    updated = 0
    for item in data:
        qr  = (item.get("qrCode") or item.get("qr_code") or "").strip()
        qty = int(item.get("quantity") or 1)
        if not qr or qty <= 0:
            continue
        result = db.execute(
            "UPDATE inventory SET stock = MAX(0, stock - ?), last_updated = ? WHERE qr_code = ?",
            (qty, _now_ms(), qr)
        )
        if result.rowcount > 0:
            updated += 1
    db.commit()
    return jsonify({"updated": updated}), 200


# ---------------------------------------------------------------------------
# /sales — sale history log (powers local market price avg on the tablet)
# ---------------------------------------------------------------------------

@app.route("/sales", methods=["POST"])
def record_sale_history():
    """
    Receives: { items: [{name, price, quantity}], sold_at: <epoch_ms> }
    Stores each line item in sale_history for local market price calculation.
    """
    data = request.get_json(force=True, silent=True) or {}
    items   = data.get("items", [])
    sold_at = int(data.get("sold_at") or _now_ms())
    if not items:
        return jsonify({"ok": True, "recorded": 0}), 200
    db = get_db()
    recorded = 0
    for item in items:
        name  = (item.get("name") or "").strip()
        price = float(item.get("price") or 0)
        qty   = int(item.get("quantity") or 1)
        if not name or price <= 0:
            continue
        db.execute(
            "INSERT INTO sale_history (name, price, quantity, sold_at) VALUES (?, ?, ?, ?)",
            (name, price, qty, sold_at)
        )
        recorded += 1
    db.commit()
    return jsonify({"ok": True, "recorded": recorded}), 200


@app.route("/sales", methods=["GET"])
def get_sale_history():
    """Returns recent sale history as JSON (last 500 line items)."""
    db   = get_db()
    rows = db.execute(
        "SELECT name, price, quantity, sold_at FROM sale_history ORDER BY sold_at DESC LIMIT 500"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# ---------------------------------------------------------------------------
# /market/price — local market price lookup (Pi-only mode, no internet scraping)
# Uses stored sale history to compute a local avg. Returns sensible defaults
# so the tablet's Market Price card always shows something useful.
# ---------------------------------------------------------------------------

@app.route("/market/price", methods=["POST"])
def market_price():
    """
    Pi-local market price endpoint.
    Priority: 1) your store's own 30-day sales history
              2) imported TCG card database (via import_tcg_db.py --tcgdb)
              3) store_price passed in by the tablet
    Body: { name, language, store_price, set_code, card_number }
    """
    data        = request.get_json(force=True, silent=True) or {}
    name_raw    = (data.get("name") or "").strip()
    name        = name_raw.lower()
    lang        = (data.get("language") or "EN").upper()
    store_price = float(data.get("store_price") or 0)
    set_code    = (data.get("set_code") or "").strip().upper()
    card_number = (data.get("card_number") or "").strip()

    local_avg       = 0.0
    local_sales_30d = 0
    tcgdb_price     = 0.0
    tcgdb_name      = ""
    tcgdb_set       = ""
    tcgdb_rarity    = ""

    db = get_db()

    # ── 1. Your store's 30-day sales history ────────────────────────────────
    if name:
        ago = int((_time.time() - 30 * 86400) * 1000)
        row = db.execute("""
            SELECT AVG(price) as avg_price, COUNT(*) as cnt
            FROM sale_history
            WHERE LOWER(name) LIKE ? AND sold_at >= ?
        """, (f"%{name[:30]}%", ago)).fetchone()
        if row and row["avg_price"]:
            local_avg       = round(float(row["avg_price"]), 2)
            local_sales_30d = int(row["cnt"])

    # ── 2. Local TCG card database (imported via import_tcg_db.py) ──────────
    try:
        # Try exact set+number match first (most precise)
        if set_code and card_number:
            qr_key = f"{set_code}-{card_number}".upper()
            row = db.execute(
                "SELECT name, price, rarity, set_code, description FROM inventory WHERE qr_code=? AND price > 0",
                (qr_key,)
            ).fetchone()
            if row:
                tcgdb_price  = round(float(row["price"]), 2)
                tcgdb_name   = row["name"]
                tcgdb_set    = row["set_code"]
                tcgdb_rarity = row["rarity"]

        # Fall back to name search if no exact match
        if tcgdb_price == 0.0 and name:
            rows = db.execute("""
                SELECT name, price, rarity, set_code FROM inventory
                WHERE LOWER(name) LIKE ? AND price > 0
                ORDER BY price DESC
                LIMIT 10
            """, (f"%{name[:25]}%",)).fetchall()
            if rows:
                prices = [float(r["price"]) for r in rows]
                import statistics as _stat
                tcgdb_price  = round(_stat.median(prices), 2)
                tcgdb_name   = rows[0]["name"]
                tcgdb_set    = rows[0]["set_code"]
                tcgdb_rarity = rows[0]["rarity"]
    except Exception:
        pass  # inventory table may not exist yet (no import run)

    # ── 3. Weighted market average ───────────────────────────────────────────
    # Sales history is highest trust (your actual data), then TCG DB, then store price
    if local_avg > 0 and tcgdb_price > 0:
        market_avg = round((local_avg * 0.6) + (tcgdb_price * 0.4), 2)
    elif local_avg > 0:
        market_avg = local_avg
    elif tcgdb_price > 0:
        market_avg = tcgdb_price
    else:
        market_avg = store_price

    buy_price = round(market_avg * 0.5, 2)
    trade_val = round(local_avg * 0.85 if local_avg > 0 else market_avg * 0.5, 2)

    lang_mult  = _get_lang_multiplier(lang)   # synced from cloud every 6 hours
    lang_flags = {"EN": "🇺🇸", "JP": "🇯🇵", "KR": "🇰🇷", "CN": "🇨🇳"}

    total_samples = local_sales_30d + (1 if tcgdb_price > 0 else 0)
    confidence    = "HIGH" if total_samples >= 5 else ("MEDIUM" if total_samples >= 2 else "LOW")

    if local_avg > 0 and tcgdb_price > 0:
        insight = f"Blended: your {local_sales_30d} recent sale(s) avg ${local_avg} + local TCG DB ${tcgdb_price} ({tcgdb_rarity or 'General'})."
    elif local_avg > 0:
        insight = f"Based on {local_sales_30d} of your recent sale(s) at ${local_avg} avg — no TCG DB match."
    elif tcgdb_price > 0:
        insight = f"From your local TCG card database: {tcgdb_name} ({tcgdb_set}) — {tcgdb_rarity or 'General'} @ ${tcgdb_price}. No local sales yet."
    else:
        insight = "No local data for this card. Run: python3 import_tcg_db.py --tcgdb your_cards.json"

    return jsonify({
        "name":              name_raw,
        "language":          lang,
        "language_flag":     lang_flags.get(lang, "🏳️"),
        "sources": {
            "local_sales": {"avg": local_avg,   "count": local_sales_30d},
            "tcgdb":       {"avg": tcgdb_price, "count": 1 if tcgdb_price > 0 else 0},
        },
        "weighted_avg":       round(market_avg * lang_mult, 2),
        "total_samples":      total_samples,
        "confidence":         confidence,
        "trend":              {"direction": "STABLE", "pct": 0.0},
        "ai_insight":         insight,
        "ai_suggested_price": round(market_avg * lang_mult, 2),
        "buy_price":          round(buy_price  * lang_mult, 2),
        "trade_value":        round(trade_val  * lang_mult, 2),
        "store_price":        store_price,
        "price_badge":        "FAIR",
        "from_cache":         False,
        "tournament_context": "",
        "local_avg":          local_avg,
        "local_sales_30d":    local_sales_30d,
        "tcgdb_price":        tcgdb_price,
        "tcgdb_match":        tcgdb_name,
    }), 200


# ---------------------------------------------------------------------------
# Cloud heartbeat — keeps the Network Monitor dashboard up to date
# ---------------------------------------------------------------------------

CLOUD_MONITOR_URL = "https://updated-hanryx-vault-pos-system.replit.app/pi/heartbeat"
CLOUD_SCAN_BASE   = "https://updated-hanryx-vault-pos-system.replit.app"
HEARTBEAT_INTERVAL = 30  # seconds

# ---------------------------------------------------------------------------
# Language discount factors — synced from cloud portal every 6 hours
# Falls back to sensible defaults if cloud is unreachable
# ---------------------------------------------------------------------------
_LANG_FACTORS_DEFAULT = {"EN": 1.0, "JP": 0.80, "KR": 0.55, "CN": 0.60}
_lang_factors_cache: dict = dict(_LANG_FACTORS_DEFAULT)
_lang_factors_lock = threading.Lock()

def _sync_language_factors():
    """Fetch language discount factors from cloud and cache in memory."""
    global _lang_factors_cache
    try:
        req = urllib.request.Request(
            f"{CLOUD_SCAN_BASE}/market/language-factors",
            headers={"User-Agent": "HanryxVaultPi/1.0", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
        factors = data.get("factors", {})
        if factors:
            merged = dict(_LANG_FACTORS_DEFAULT)  # always keep EN=1.0 as baseline
            for lang, pct_off in factors.items():
                try:
                    merged[str(lang).upper()] = round(1.0 - float(pct_off) / 100.0, 4)
                except (TypeError, ValueError):
                    pass
            with _lang_factors_lock:
                _lang_factors_cache = merged
            print(f"[lang-factors] Synced from cloud: {merged}", flush=True)
    except Exception as e:
        print(f"[lang-factors] Could not sync from cloud, using cached/defaults: {e}", flush=True)

def _lang_factors_sync_loop():
    _sync_language_factors()           # once at startup
    while True:
        _time.sleep(6 * 3600)          # then every 6 hours
        _sync_language_factors()

threading.Thread(target=_lang_factors_sync_loop, daemon=True, name="lang-factors-sync").start()

def _get_lang_multiplier(lang: str) -> float:
    """Return the price multiplier for this language (EN=1.0, JP≈0.80, etc.)."""
    with _lang_factors_lock:
        return _lang_factors_cache.get(lang.upper(), 1.0)

def _cloud_scan_relay_loop():
    """
    Background thread: pulls pending scans from the cloud hub every 2 s
    and inserts them into the local scan_queue so the tablet picks them up.

    Flow: Expo app → POST cloud /scan → Pi relay → local scan_queue → tablet cart.
    """
    import urllib.request, json as _rjson, sqlite3 as _rsq
    _time.sleep(12)  # wait a bit longer than heartbeat so Flask is definitely up
    while True:
        try:
            # 1. Fetch oldest pending scan from cloud
            req = urllib.request.Request(
                f"{CLOUD_SCAN_BASE}/scan/pending",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = _rjson.loads(resp.read().decode())
            scan_id  = data.get("id", 0)
            qr_code  = (data.get("qrCode") or "").strip()

            if scan_id > 0 and qr_code:
                # 2. Insert into local scan_queue (tablet will pick it up within 1.5 s)
                conn = _rsq.connect(DB_PATH)
                conn.execute(
                    "INSERT INTO scan_queue (qr_code) VALUES (?)", (qr_code,)
                )
                conn.commit()
                conn.close()
                print(f"[cloud-relay] Queued from cloud: {qr_code}")

                # 3. Ack on cloud so it is not delivered again
                ack_req = urllib.request.Request(
                    f"{CLOUD_SCAN_BASE}/scan/ack/{scan_id}",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(ack_req, timeout=5)
        except Exception:
            pass  # cloud unreachable or nothing pending
        _time.sleep(2)


def _heartbeat_loop():
    """Background thread: POST a heartbeat to the cloud server every 30 s."""
    import urllib.request, json as _hbjson, sqlite3 as _sq
    _time.sleep(10)  # wait for Flask to fully start
    while True:
        try:
            conn = _sq.connect(DB_PATH)
            count = conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
            conn.close()
        except Exception:
            count = 0
        payload = _hbjson.dumps({
            "product_count": count,
            "version": "1.0",
        }).encode()
        try:
            req = urllib.request.Request(
                CLOUD_MONITOR_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=8)
        except Exception:
            pass  # silently skip if cloud is unreachable
        _time.sleep(HEARTBEAT_INTERVAL)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import socket as _sock
    PORT = int(os.environ.get("PORT", 8080))
    try:
        local_ip = _sock.gethostbyname(_sock.gethostname())
    except Exception:
        local_ip = "192.168.x.x"

    init_db()
    # Auto-populate from cloud sources if inventory is empty
    sync_inventory_from_cloud(force=False)
    _cleanup_scan_queue()
    # Cloud→Pi scan relay (picks up Expo scans sent to cloud hub, if cloud is reachable)
    _relay_thread = threading.Thread(target=_cloud_scan_relay_loop, daemon=True, name="cloud-scan-relay")
    _relay_thread.start()
    # Cloud heartbeat (optional — silently skipped if cloud unreachable)
    _hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat")
    _hb_thread.start()
    print("=" * 60)
    print("  HanryxVault POS — Raspberry Pi Server")
    print(f"  Running on  http://0.0.0.0:{PORT}")
    print(f"  Dashboard   http://{local_ip}:{PORT}/admin")
    print(f"  Inventory   http://{local_ip}:{PORT}/inventory")
    print(f"  Zettle auth http://{local_ip}:{PORT}/zettle/login")
    print("=" * 60)
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
