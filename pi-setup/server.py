"""
HanryxVault POS — Raspberry Pi Backend Server
Runs behind nginx on port 8080 (nginx handles 80/443 → 8080).

Performance improvements over original:
  - Gunicorn WSGI server (multi-worker, replaces Flask dev server)
  - SQLite WAL mode + PRAGMA optimizations
  - Connection-per-thread via threading.local (safe for gunicorn workers)
  - Startup cloud-sync runs in background thread (non-blocking)
  - Scan-queue cleanup runs hourly via background timer
  - Flask response compression via flask-compress

Endpoints consumed by the tablet app:
  GET  /health              — connectivity check
  GET  /inventory           — product catalogue (?q=search&since=ms)
  POST /sync/sales          — receives completed SaleEntity JSON from tablet
  POST /inventory/deduct    — receives SoldItemEntry list to decrement stock
  POST /push/inventory      — push products from scanner/websites (JSON)
  POST /push/inventory/csv  — bulk import products from CSV file

Scanner relay (Expo scanner app on phone):
  POST /scan                — queue a scanned QR code (normalises Pokémon TCG URLs automatically)
  GET  /scan/pending        — tablet polls this; now includes resolvedProduct if card matched
  POST /scan/ack/<id>       — tablet marks scan as handled
  GET  /scan/stream         — SSE: instant push instead of polling

Card lookup + enrichment (website camera scanner / tablet):
  GET  /card/lookup?q=charizard          — fuzzy name search (local inventory)
  GET  /card/lookup?qr=<raw_scan>        — resolve any scan value to a card
  GET  /card/lookup?name=X&set=SV1&num=1 — explicit fields
  POST /card/lookup                      — same, JSON body
  GET  /card/enrich?qr=SV1-1            — local + full TCG API data + market price + image
  POST /card/enrich                      — same, JSON body
  GET  /card/condition/<qr>             — get NM/LP/MP/HP/DMG condition for a card
  POST /card/condition/<qr>             — set condition + notes

Admin — card utilities:
  GET  /admin/export-cards              — JSON export for website bulk import
  GET  /admin/export-cards?fmt=csv      — CSV download
  GET  /admin/export-cards?enrich=1     — include TCG images + market prices
  GET  /admin/webhook-config            — check if auto-push webhook is configured
  POST /admin/webhook-config            — set webhook URL (auto-pushes new cards to site)

Zettle OAuth:
  GET  /zettle/auth         — begin OAuth flow
  GET  /zettle/callback     — OAuth callback (must be HTTPS)
  GET  /zettle/status       — token status

Receipt printer (Bluetooth SPP / USB ESC/POS thermal):
  POST /print/receipt       — print receipt (non-blocking, sale JSON body)
  GET  /print/status        — which printer device is currently connected

Admin dashboard:
  GET  /admin               — web UI (today's sales + inventory)
  GET  /admin/sales         — JSON dump of all sales
  GET  /admin/inventory     — JSON dump of full inventory
  POST /admin/inventory     — add/update product
  DELETE /admin/inventory/<qr_code> — remove product
  POST /admin/sync-from-cloud — force re-sync from Replit sites
  GET  /download/apk        — download latest debug APK

Run manually (dev):
  python3 server.py

Run via gunicorn (production — handled by systemd):
  gunicorn -w 4 -b 127.0.0.1:8080 --timeout 60 server:app
"""

import sqlite3
import json
import datetime
import hashlib
import html as _html
import os
import re
import subprocess
import threading
import time as _time
import urllib.parse
import urllib.request
import urllib.error
import base64
import csv
import io
from flask import Flask, request, jsonify, redirect, g
from flask_compress import Compress
from cachetools import TTLCache

# ---------------------------------------------------------------------------
# Zettle OAuth + Payment configuration
# ---------------------------------------------------------------------------

ZETTLE_CLIENT_ID     = os.environ.get("ZETTLE_CLIENT_ID", "")
ZETTLE_CLIENT_SECRET = os.environ.get("ZETTLE_CLIENT_SECRET", "")
ZETTLE_REDIRECT_URI  = os.environ.get("ZETTLE_REDIRECT_URI", "https://hanryxvault.tailcfc0a3.ts.net/zettle/callback")
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


def _persist_tokens():
    """Write current token state to DB so it survives server restarts."""
    try:
        db = sqlite3.connect(DB_PATH)
        db.execute("PRAGMA busy_timeout=3000")
        with _token_lock:
            payload = json.dumps(_zettle_state)
        db.execute(
            "INSERT OR REPLACE INTO server_state (key, value) VALUES ('zettle_tokens', ?)",
            (payload,)
        )
        db.commit()
        db.close()
    except Exception as e:
        print(f"[zettle] Token persist failed: {e}")


def _load_tokens_from_db():
    """Restore persisted Zettle tokens from DB on startup."""
    try:
        db = sqlite3.connect(DB_PATH)
        row = db.execute(
            "SELECT value FROM server_state WHERE key='zettle_tokens'"
        ).fetchone()
        db.close()
        if row:
            saved = json.loads(row[0])
            with _token_lock:
                _zettle_state.update(saved)
            if _zettle_state.get("access_token"):
                print("[zettle] Restored tokens from DB — no re-auth needed")
    except Exception as e:
        print(f"[zettle] Token restore failed (first run?): {e}")


def _store_tokens(result):
    with _token_lock:
        _zettle_state["access_token"]  = result.get("access_token")
        _zettle_state["refresh_token"] = result.get("refresh_token")
        _zettle_state["expires_at"]    = _time.time() + result.get("expires_in", 7200) - 60
    _persist_tokens()


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


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
Compress(app)  # gzip all responses automatically

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vault_pos.db")

# ---------------------------------------------------------------------------
# In-memory caches — dramatically reduces SQLite hits on hot endpoints
# ---------------------------------------------------------------------------
_cache_lock      = threading.Lock()
_inventory_cache = TTLCache(maxsize=1, ttl=30)    # /inventory — 30 s TTL
_scan_cache      = TTLCache(maxsize=1, ttl=1)     # /scan/pending — 1 s TTL
_health_cache    = TTLCache(maxsize=1, ttl=5)     # /health — 5 s TTL
_cache_stats     = {"inventory_hits": 0, "inventory_misses": 0,
                    "scan_hits": 0,      "scan_misses": 0}

# ---------------------------------------------------------------------------
# Pokémon TCG API — config + in-memory cache
# ---------------------------------------------------------------------------
_TCG_API_BASE   = "https://api.pokemontcg.io/v2"
_PTCG_API_KEY   = os.environ.get("PTCG_API_KEY", "")  # optional; free tier = 1k/day, with key = 20k/day
_tcg_cache_lock = threading.Lock()
_tcg_mem_cache: dict = {}    # card_id → {"data": {...}, "fetched_ms": int}
_TCG_MEM_TTL_MS = 3_600_000  # 1 hour in-memory; DB stores 24 hours

# ---------------------------------------------------------------------------
# Server start time — used by /health uptime field
# ---------------------------------------------------------------------------
_server_start_time = _time.time()

# ---------------------------------------------------------------------------
# SSE (Server-Sent Events) for real-time scan push
#   Connects once; scanner events arrive instantly instead of being polled.
#   Existing /scan/pending polling continues to work — no app changes needed.
# ---------------------------------------------------------------------------
import queue as _queue_mod

_sse_lock            = threading.Lock()
_sse_scan_subscribers: list = []   # one Queue per connected SSE client


def _sse_broadcast_scan(qr_code: str):
    """Push a new scan to every connected SSE client instantly."""
    with _sse_lock:
        dead = []
        for q in _sse_scan_subscribers:
            try:
                q.put_nowait(qr_code)
            except Exception:
                dead.append(q)
        for q in dead:
            try:
                _sse_scan_subscribers.remove(q)
            except ValueError:
                pass



def _cache_get(cache, key):
    with _cache_lock:
        return cache.get(key)


def _cache_set(cache, key, value):
    with _cache_lock:
        cache[key] = value


def _invalidate_inventory():
    """Call whenever inventory data changes so next request re-reads from DB."""
    with _cache_lock:
        _inventory_cache.clear()

CLOUD_INVENTORY_SOURCES = [
    "https://inventory-scanner-ngansen84.replit.app/api/inventory",
    "https://hanryxvault.app/api/products",
]

# ---------------------------------------------------------------------------
# Database — thread-local connections (gunicorn multi-worker safe)
# ---------------------------------------------------------------------------

def get_db():
    """Return a per-request SQLite connection stored on Flask's g object."""
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-131072")    # 128 MB page cache per connection (negative = KiB)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA busy_timeout=5000")     # wait up to 5 s on lock instead of crashing
        conn.execute("PRAGMA mmap_size=268435456")   # 256 MB memory-mapped I/O — free read speedup
        conn.execute("PRAGMA optimize")              # let SQLite pick query plans once at open
        g.db = conn
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
    db.execute("PRAGMA cache_size=-131072")
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
            received_at     INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000),
            source          TEXT NOT NULL DEFAULT 'local'
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

        CREATE INDEX IF NOT EXISTS idx_scan_pending     ON scan_queue(processed, id);
        CREATE INDEX IF NOT EXISTS idx_sales_timestamp  ON sales(timestamp_ms);
        CREATE INDEX IF NOT EXISTS idx_sales_received   ON sales(received_at);
        CREATE INDEX IF NOT EXISTS idx_stock_qr         ON stock_deductions(qr_code);
        CREATE INDEX IF NOT EXISTS idx_stock_received   ON stock_deductions(deducted_at);

        CREATE TABLE IF NOT EXISTS sale_history (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            name     TEXT NOT NULL,
            price    REAL NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            sold_at  INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)
        );

        CREATE INDEX IF NOT EXISTS idx_sale_history_name ON sale_history(name, sold_at);

        CREATE TABLE IF NOT EXISTS server_state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS card_tcg_cache (
            card_id     TEXT PRIMARY KEY,
            data_json   TEXT NOT NULL,
            fetched_ms  INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)
        );

        CREATE TABLE IF NOT EXISTS card_conditions (
            qr_code    TEXT PRIMARY KEY,
            condition  TEXT NOT NULL DEFAULT 'NM',
            notes      TEXT NOT NULL DEFAULT '',
            updated_ms INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)
        );
    """)
    db.commit()

    # ── Safe migration for existing databases ────────────────────────────────
    # Add 'source' column if it was created before this version.
    # SQLite doesn't support IF NOT EXISTS for ALTER TABLE, so check manually.
    existing_cols = {r[1] for r in db.execute("PRAGMA table_info(sales)").fetchall()}
    if "source" not in existing_cols:
        db.execute("ALTER TABLE sales ADD COLUMN source TEXT NOT NULL DEFAULT 'local'")
        db.commit()
        print("[DB] Migration: added sales.source column")

    inv_cols = {r[1] for r in db.execute("PRAGMA table_info(inventory)").fetchall()}
    if "image_url" not in inv_cols:
        db.execute("ALTER TABLE inventory ADD COLUMN image_url TEXT NOT NULL DEFAULT ''")
        db.commit()
        print("[DB] Migration: added inventory.image_url column")
    if "tcg_id" not in inv_cols:
        db.execute("ALTER TABLE inventory ADD COLUMN tcg_id TEXT NOT NULL DEFAULT ''")
        db.commit()
        print("[DB] Migration: added inventory.tcg_id column")

    db.close()
    print("[DB] Initialized vault_pos.db")


# ---------------------------------------------------------------------------
# Cloud inventory sync
# ---------------------------------------------------------------------------

def sync_inventory_from_cloud(force: bool = False) -> dict:
    """Pull inventory from both Replit cloud sources and upsert into local DB."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    if not force:
        count = db.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
        if count > 0:
            db.close()
            print(f"[cloud-sync] Inventory has {count} products — skipping auto-sync")
            return {"skipped": True, "existing": count}

    total_upserted = 0
    total_skipped  = 0
    results        = {}

    for url in CLOUD_INVENTORY_SOURCES:
        try:
            req = urllib.request.Request(
                url, headers={"Accept": "application/json", "User-Agent": "HanryxVaultPi/2.0"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                items = json.loads(resp.read().decode())

            if not isinstance(items, list):
                items = items.get("items") or items.get("products") or items.get("inventory") or []

            upserted = 0
            for item in items:
                qr   = (item.get("qrCode") or item.get("qr_code") or item.get("barcode") or item.get("id") or "").strip()
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
                        item.get("setCode") or item.get("set_code") or item.get("setName") or "",
                        item.get("description") or "",
                        int(item.get("stock") or item.get("stockQuantity") or item.get("quantity") or 0),
                        int(_time.time() * 1000),
                    ))
                    upserted += 1
                except Exception as row_err:
                    print(f"[cloud-sync] Row error ({url}): {row_err}")
                    total_skipped += 1

            db.commit()
            total_upserted += upserted
            results[url] = {"ok": True, "upserted": upserted}
            print(f"[cloud-sync] {url} → {upserted} upserted")

        except Exception as e:
            results[url] = {"ok": False, "error": str(e)}
            print(f"[cloud-sync] Failed {url}: {e}")

    db.close()
    return {"upserted": total_upserted, "skipped": total_skipped, "sources": results}


def _cleanup_scan_queue():
    """Delete processed scans older than 1 hour — runs every hour in background."""
    try:
        cutoff = int((_time.time() - 3600) * 1000)
        db = sqlite3.connect(DB_PATH)
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("DELETE FROM scan_queue WHERE processed = 1 AND scanned_at < ?", (cutoff,))
        db.commit()
        deleted = db.execute("SELECT changes()").fetchone()[0]
        db.close()
        print(f"[cleanup] Removed {deleted} stale scan_queue rows")
    except Exception as e:
        print(f"[cleanup] scan_queue cleanup failed: {e}")
    threading.Timer(3600, _cleanup_scan_queue).start()


def _wal_checkpoint():
    """
    Periodically checkpoint the WAL file so it doesn't grow unboundedly.
    TRUNCATE mode checkpoints and then removes (truncates) the WAL file entirely,
    returning reads to full speed. Runs every 30 minutes.
    Without this, the WAL can grow to 100s of MB over days of trading, making
    every read slower because SQLite has to scan the WAL for changes.
    """
    try:
        db = sqlite3.connect(DB_PATH)
        db.execute("PRAGMA busy_timeout=10000")
        # TRUNCATE: checkpoint and then zero the WAL — cleanest reset
        result = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        db.close()
        # result = (busy, log, checkpointed) — busy=1 means writer was active
        if result and result[0] == 0:
            print(f"[wal] Checkpoint OK — {result[2]} of {result[1]} frames written")
        else:
            print(f"[wal] Checkpoint deferred (writer active) — will retry next cycle")
    except Exception as e:
        print(f"[wal] Checkpoint failed: {e}")
    threading.Timer(1800, _wal_checkpoint).start()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ms():
    return int(_time.time() * 1000)


# ---------------------------------------------------------------------------
# Pokémon card QR / scan-code helpers
# ---------------------------------------------------------------------------

# Pokémon TCG URL patterns (what their official QR codes produce when scanned)
#   https://www.pokemon.com/us/pokemon-trading-card-game/...?series=XY&set=BASE&number=4
#   https://tcg.pokemon.com/en-us/...
#   ptcg://card/SV1/001  (deep-link used by the companion app)
_PTCG_DOMAIN_RE = re.compile(r'pokemon\.com|ptcg://', re.IGNORECASE)


def _normalize_qr(raw: str) -> str:
    """
    Normalise a raw scanner value so it matches what's stored as qr_code in the DB.

    Handles:
      • Plain SET-NUMBER codes (already canonical)            → returned as-is
      • ptcg://card/SET/NUMBER  (companion app deep-link)    → SET-NUMBER
      • pokemon.com URLs with ?set=&number= query params     → SET-NUMBER
      • tcg.pokemon.com URLs                                 → SET-NUMBER
      • pkmncards.com slugs  /card/slug-number               → best guess
      • ptcgo.com / ptcgolive.com URLs with ?set=&card=      → SET-NUMBER
      • limitlesstcg.com /cards/SET/NUMBER                   → SET-NUMBER
      • pokellector.com / bulbapedia.net card links          → SET-NUMBER fallback
      • Energy/trainer plain names                           → returned as-is
    """
    raw = raw.strip()
    if not raw:
        return raw

    # Already canonical — plain code with no URL characters
    if not (raw.startswith("http") or raw.startswith("ptcg://")):
        # Normalise casing for SET-NUMBER patterns: sv1-1 → SV1-1
        m = re.match(r'^([A-Za-z0-9]{2,8})-(\d{1,4}[a-zA-Z]?)$', raw)
        if m:
            return f"{m.group(1).upper()}-{m.group(2).lstrip('0') or '0'}"
        return raw

    try:
        parsed = urllib.parse.urlparse(raw)
        host   = parsed.hostname or ""
        path   = parsed.path
        qs     = urllib.parse.parse_qs(parsed.query)

        # ── Deep-link: ptcg://card/SET/NUMBER ───────────────────────────────
        if raw.startswith("ptcg://"):
            parts = path.strip("/").split("/")
            if len(parts) >= 2:
                return f"{parts[-2].upper()}-{parts[-1].lstrip('0') or '0'}"

        # ── pokemon.com  (all regional subdomains) ───────────────────────────
        if "pokemon.com" in host:
            set_code = (qs.get("set") or qs.get("series") or qs.get("setCode") or [""])[0].strip().upper()
            card_num = (qs.get("number") or qs.get("card") or qs.get("num") or [""])[0].strip().lstrip("0") or "0"
            if set_code and card_num != "0":
                return f"{set_code}-{card_num}"
            # /cards/sv1-001 path style
            tail = path.rstrip("/").split("/")[-1]
            m = re.match(r'^([A-Za-z0-9]+)-0*(\d+[a-zA-Z]?)$', tail)
            if m:
                return f"{m.group(1).upper()}-{m.group(2)}"

        # ── ptcgo.com / ptcgolive.com ────────────────────────────────────────
        if "ptcgo" in host or "ptcgolive" in host:
            set_code = (qs.get("set") or qs.get("setId") or [""])[0].strip().upper()
            card_num = (qs.get("card") or qs.get("number") or qs.get("num") or [""])[0].strip().lstrip("0") or "0"
            if set_code and card_num != "0":
                return f"{set_code}-{card_num}"

        # ── limitlesstcg.com/cards/SET/NUMBER ────────────────────────────────
        if "limitlesstcg" in host:
            parts = [p for p in path.strip("/").split("/") if p]
            if len(parts) >= 2 and parts[0].lower() in ("cards", "card"):
                return f"{parts[1].upper()}-{parts[2].lstrip('0') or '0'}" if len(parts) >= 3 else parts[1].upper()

        # ── pkmncards.com/card/<slug> ─────────────────────────────────────────
        # Slug format: "charizard-base-set-4" → BASE1-4 requires name DB lookup;
        # best we can do here is extract the trailing number and prior segment
        if "pkmncards.com" in host:
            parts = [p for p in path.strip("/").split("/") if p]
            if parts:
                slug = parts[-1]  # e.g. "charizard-base-set-4"
                m = re.search(r'-(\w{2,8})-(\d+[a-zA-Z]?)$', slug)
                if m:
                    return f"{m.group(1).upper()}-{m.group(2).lstrip('0') or '0'}"
                m2 = re.search(r'(\d+[a-zA-Z]?)$', slug)
                if m2:
                    return slug.upper()

        # ── Generic fallback: extract SET-NUM from path ──────────────────────
        # Matches anything like /sv1-001, /BASE/4, /cards/xy3/15
        m = re.search(r'/([A-Za-z0-9]{2,8})[-/]0*(\d+[a-zA-Z]?)(?:/|$|\?)', path)
        if m:
            return f"{m.group(1).upper()}-{m.group(2)}"

        # Last path segment
        tail = path.rstrip("/").split("/")[-1]
        if tail and len(tail) > 2:
            return tail.upper()

    except Exception:
        pass

    return raw  # give up — use raw as-is


def _tokenize(text: str) -> list[str]:
    """Split a card name into searchable tokens, ignoring small words."""
    _STOP = {"the", "a", "an", "of", "in", "ex", "v", "vmax", "vstar", "gx"}
    return [t for t in re.split(r'[\s\-_/\\,\.]+', text.lower()) if t and t not in _STOP]


def _score_card(name: str, set_code: str, qr_code: str, tokens: list[str]) -> int:
    """
    Return a relevance score (higher = better match) for a candidate card
    against a list of search tokens.  Purely in-Python — no extra DB round-trip.
    """
    score     = 0
    name_lc   = name.lower()
    set_lc    = set_code.lower()
    qr_lc     = qr_code.lower()
    name_toks = _tokenize(name)

    for t in tokens:
        if t in name_lc:    score += 2
        if t in name_toks:  score += 3  # exact token boundary match
        if t in set_lc:     score += 1
        if t in qr_lc:      score += 1

    # Bonus: all tokens matched
    if all(t in name_lc for t in tokens):
        score += 5

    return score


def _card_lookup(db, q: str = "", qr: str = "", name: str = "",
                 set_code: str = "", card_num: str = "",
                 limit: int = 10) -> list[dict]:
    """
    Fuzzy card lookup.  Priority order:
      1. Exact qr_code match (fast path — used by scanner)
      2. Normalised QR → qr_code match
      3. Set code + card number (extracted from name or explicit params)
      4. Tokenised name search
    Returns at most `limit` results sorted by relevance.
    """
    def _row_to_dict(r) -> dict:
        keys = r.keys() if hasattr(r, "keys") else []
        return {
            "qrCode":        r["qr_code"],
            "name":          r["name"],
            "price":         r["price"],
            "category":      r["category"] or "General",
            "rarity":        r["rarity"] or "",
            "setCode":       r["set_code"] or "",
            "description":   r["description"] or "",
            "stockQuantity": r["stock"],
            "lastUpdated":   r["last_updated"],
            "imageUrl":      r["image_url"] if "image_url" in keys else "",
            "tcgId":         r["tcg_id"]    if "tcg_id"    in keys else "",
        }

    # 1 — exact qr match
    if qr:
        row = db.execute(
            "SELECT * FROM inventory WHERE qr_code = ? LIMIT 1", (qr,)
        ).fetchone()
        if row:
            return [_row_to_dict(row)]

        # 1b — normalised qr
        norm = _normalize_qr(qr)
        if norm != qr:
            row = db.execute(
                "SELECT * FROM inventory WHERE qr_code = ? LIMIT 1", (norm,)
            ).fetchone()
            if row:
                return [_row_to_dict(row)]

        # treat qr text as search terms if no exact match
        if not q:
            q = norm

    # 2 — explicit set + number
    if set_code and card_num:
        rows = db.execute("""
            SELECT * FROM inventory
            WHERE UPPER(set_code) = UPPER(?)
              AND (name LIKE ? OR qr_code LIKE ?)
            ORDER BY name ASC LIMIT ?
        """, (set_code, f"%{card_num}%", f"%{card_num}%", limit)).fetchall()
        if rows:
            return [_row_to_dict(r) for r in rows]

    # 3 — try to extract set+number from q (e.g. "SV1 001" or "sv1-001")
    if q:
        _SET_NUM_RE = re.compile(r'\b([A-Za-z]{2,6})\s*[-/]?\s*0*(\d{1,4})\b')
        m = _SET_NUM_RE.search(q)
        if m:
            s, n = m.group(1).upper(), m.group(2)
            rows = db.execute("""
                SELECT * FROM inventory
                WHERE UPPER(set_code) = ? AND (name LIKE ? OR qr_code LIKE ?)
                ORDER BY name ASC LIMIT ?
            """, (s, f"%{n}%", f"%{n}%", limit)).fetchall()
            if rows:
                return [_row_to_dict(r) for r in rows]

    # 4 — tokenised name search with scoring
    if not q and name:
        q = name
    if not q:
        return []

    tokens = _tokenize(q)
    if not tokens:
        return []

    # Pull candidates that contain at least one token (LIKE OR chain)
    like_clauses = " OR ".join(["LOWER(name) LIKE ?" for _ in tokens])
    like_args    = [f"%{t}%" for t in tokens]
    rows = db.execute(f"""
        SELECT * FROM inventory
        WHERE {like_clauses}
        ORDER BY name ASC
        LIMIT 200
    """, like_args).fetchall()

    if not rows:
        return []

    scored = sorted(
        rows,
        key=lambda r: _score_card(r["name"], r["set_code"] or "", r["qr_code"], tokens),
        reverse=True,
    )
    return [_row_to_dict(r) for r in scored[:limit]]


# ---------------------------------------------------------------------------
# Pokémon TCG API helpers — fetch, search, enrich
# ---------------------------------------------------------------------------

def _tcg_headers() -> dict:
    h = {"Accept": "application/json"}
    if _PTCG_API_KEY:
        h["X-Api-Key"] = _PTCG_API_KEY
    return h


def _tcg_db_get(card_id: str) -> dict | None:
    """Return SQLite-cached TCG data if younger than 24 h, else None."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA busy_timeout=2000")
        row = conn.execute(
            "SELECT data_json, fetched_ms FROM card_tcg_cache WHERE card_id=?", (card_id,)
        ).fetchone()
        conn.close()
        if row and (_now_ms() - row[1]) < 86_400_000:
            return json.loads(row[0])
    except Exception:
        pass
    return None


def _tcg_db_set(card_id: str, data: dict):
    """Persist a TCG API response to the SQLite cache."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA busy_timeout=2000")
        conn.execute(
            "INSERT OR REPLACE INTO card_tcg_cache (card_id, data_json, fetched_ms) VALUES (?,?,?)",
            (card_id, json.dumps(data, ensure_ascii=False), _now_ms()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _tcg_fetch(card_id: str) -> dict | None:
    """
    Fetch a single card from api.pokemontcg.io/v2/cards/{id}.
    Cache layers: in-memory (1 h) → SQLite (24 h) → live API.
    Returns the raw 'data' object from the API, or None on miss.
    """
    cid = card_id.lower().strip()
    if not cid:
        return None

    # 1 — in-memory
    with _tcg_cache_lock:
        hit = _tcg_mem_cache.get(cid)
        if hit and (_now_ms() - hit["fetched_ms"]) < _TCG_MEM_TTL_MS:
            return hit["data"]

    # 2 — SQLite
    cached = _tcg_db_get(cid)
    if cached:
        with _tcg_cache_lock:
            _tcg_mem_cache[cid] = {"data": cached, "fetched_ms": _now_ms()}
        return cached

    # 3 — live API
    try:
        url = f"{_TCG_API_BASE}/cards/{urllib.parse.quote(cid, safe='')}"
        req = urllib.request.Request(url, headers=_tcg_headers())
        with urllib.request.urlopen(req, timeout=7) as resp:
            body = json.loads(resp.read())
            data = body.get("data")
            if data:
                _tcg_db_set(cid, data)
                with _tcg_cache_lock:
                    _tcg_mem_cache[cid] = {"data": data, "fetched_ms": _now_ms()}
                return data
    except Exception as e:
        print(f"[tcg] fetch '{cid}' failed: {e}")
    return None


def _tcg_search(name: str = "", set_id: str = "", number: str = "",
                limit: int = 5) -> list[dict]:
    """
    Search api.pokemontcg.io by name / set / number.
    Warms the per-card in-memory + DB cache as a side-effect.
    """
    parts = []
    if name:
        clean = re.sub(r'["\']', '', name).strip()
        parts.append(f'name:"{clean}"')
    if set_id:
        parts.append(f"set.id:{set_id.lower()}")
    if number:
        parts.append(f"number:{number.lstrip('0') or '0'}")
    if not parts:
        return []

    url = (f"{_TCG_API_BASE}/cards?"
           + urllib.parse.urlencode({"q": " ".join(parts),
                                     "pageSize": limit,
                                     "orderBy": "-set.releaseDate"}))
    try:
        req = urllib.request.Request(url, headers=_tcg_headers())
        with urllib.request.urlopen(req, timeout=9) as resp:
            results = json.loads(resp.read()).get("data", [])
            for card in results:
                cid = card.get("id", "").lower()
                if cid:
                    _tcg_db_set(cid, card)
                    with _tcg_cache_lock:
                        _tcg_mem_cache[cid] = {"data": card, "fetched_ms": _now_ms()}
            return results
    except Exception as e:
        print(f"[tcg] search failed ('{' '.join(parts)}'): {e}")
    return []


def _tcg_to_summary(card: dict) -> dict:
    """
    Flatten a raw TCG API card object into a clean dict that merges with
    local inventory format and feeds the price overlay / website upload.
    """
    images = card.get("images", {})
    tcgp   = card.get("tcgplayer", {})
    prices = tcgp.get("prices", {})

    price_tiers: dict = {}
    for tier, pdata in prices.items():
        if isinstance(pdata, dict):
            price_tiers[tier] = {
                "low":       pdata.get("low"),
                "mid":       pdata.get("mid"),
                "high":      pdata.get("high"),
                "market":    pdata.get("market"),
                "directLow": pdata.get("directLow"),
            }

    # Best single market price: holofoil > normal > reverseHolo > first available
    market_price: float | None = None
    for tier in ("holofoil", "normal", "reverseHolofoil", "1stEditionHolofoil",
                 "unlimitedHolofoil", "1stEditionNormal"):
        if tier in price_tiers and price_tiers[tier].get("market"):
            market_price = price_tiers[tier]["market"]
            break
    if market_price is None and price_tiers:
        first = next(iter(price_tiers.values()))
        market_price = first.get("market") or first.get("mid")

    tcg_set = card.get("set", {})
    return {
        "tcgId":       card.get("id"),
        "name":        card.get("name"),
        "supertype":   card.get("supertype"),
        "subtypes":    card.get("subtypes", []),
        "hp":          card.get("hp"),
        "types":       card.get("types", []),
        "evolvesFrom": card.get("evolvesFrom"),
        "rarity":      card.get("rarity"),
        "number":      card.get("number"),
        "artist":      card.get("artist"),
        "flavorText":  card.get("flavorText"),
        "nationalDex": card.get("nationalPokedexNumbers", []),
        "set": {
            "id":          tcg_set.get("id"),
            "name":        tcg_set.get("name"),
            "series":      tcg_set.get("series"),
            "ptcgoCode":   tcg_set.get("ptcgoCode"),
            "total":       tcg_set.get("total"),
            "releaseDate": tcg_set.get("releaseDate"),
            "images":      tcg_set.get("images", {}),
        },
        "images": {
            "small": images.get("small"),
            "large": images.get("large"),
        },
        "tcgplayer": {
            "url":         tcgp.get("url"),
            "updatedAt":   tcgp.get("updatedAt"),
            "marketPrice": market_price,
            "priceTiers":  price_tiers,
        },
        "legalities": card.get("legalities", {}),
        "attacks":    card.get("attacks", []),
        "weaknesses": card.get("weaknesses", []),
        "abilities":  card.get("abilities", []),
        "retreatCost": card.get("convertedRetreatCost"),
    }


def _enrich_with_tcg(local_result: dict | None, qr_code: str) -> dict:
    """
    Merge a local inventory result with live TCG API data.

    Returns a dict with:
      • All local inventory fields (if card is in local DB)
      • "tcgData"          — clean TCG summary (images, prices, set info)
      • "inLocalInventory" — bool
      • "isDuplicate"      — True if already in stock (qty > 0)
      • "suggestedPrice"   — TCG market price when no local price exists
      • "imageUrl"         — large card image URL (auto-filled from TCG if absent)
    """
    out: dict = dict(local_result) if local_result else {}
    out["inLocalInventory"] = bool(local_result)
    out["isDuplicate"]      = bool(local_result and (local_result.get("stockQuantity") or 0) > 0)

    # Derive canonical TCG card id from qr_code  e.g. "SV1-1" → "sv1-1"
    cid = qr_code.lower().strip()

    tcg_raw = _tcg_fetch(cid)

    # Fallback: search by set+number or name
    if not tcg_raw:
        m = re.match(r'^([a-z0-9]+)-(\d+[a-z]?)$', cid)
        local_name = (local_result or {}).get("name", "")
        if m:
            hits = _tcg_search(name=local_name, set_id=m.group(1), number=m.group(2), limit=1)
        elif local_name:
            hits = _tcg_search(name=local_name, limit=1)
        else:
            # Last resort: treat qr_code tokens as a name search
            hits = _tcg_search(name=qr_code.replace("-", " "), limit=1)
        if hits:
            tcg_raw = hits[0]

    if tcg_raw:
        summary = _tcg_to_summary(tcg_raw)
        out["tcgData"] = summary

        # Auto-fill empty local fields from TCG data
        if not out.get("name") and summary.get("name"):
            out["name"]    = summary["name"]
        if not out.get("rarity") and summary.get("rarity"):
            out["rarity"]  = summary["rarity"]
        if not out.get("setCode") and summary.get("set", {}).get("ptcgoCode"):
            out["setCode"] = summary["set"]["ptcgoCode"]
        # Image URL: prefer local override, fall back to TCG large image
        if not out.get("imageUrl"):
            img = summary.get("images", {}).get("large") or summary.get("images", {}).get("small")
            if img:
                out["imageUrl"] = img
        # Suggest market price when product has no local price
        mkt = summary.get("tcgplayer", {}).get("marketPrice")
        if not out.get("price") and mkt:
            out["suggestedPrice"] = round(mkt, 2)

    return out


def _fire_webhook(payload: dict):
    """POST card data to the configured webhook URL in a background thread (non-blocking)."""
    try:
        db  = sqlite3.connect(DB_PATH)
        row = db.execute("SELECT value FROM server_state WHERE key='webhook_url'").fetchone()
        db.close()
        url = row[0].strip() if row and row[0] else ""
        if not url:
            return
        data  = json.dumps(payload, ensure_ascii=False).encode()
        req   = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json", "X-Source": "HanryxVault-Pi"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            print(f"[webhook] pushed card '{payload.get('name', '?')}' → {resp.status}")
    except Exception as e:
        print(f"[webhook] push failed: {e}")


def _cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return response


@app.after_request
def after_request(response):
    return _cors(response)


_worker_init_done = False

@app.before_request
def handle_options():
    # One-time per-worker init (gunicorn preforking — each worker is its own process)
    global _worker_init_done
    if not _worker_init_done:
        _worker_init_done = True
        _load_tokens_from_db()

    if request.method == "OPTIONS":
        return _cors(jsonify({}))


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    cached = _cache_get(_health_cache, "h")
    if cached:
        return jsonify(cached)
    db = get_db()
    inv_count     = db.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
    sale_count    = db.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
    pending_scans = db.execute(
        "SELECT COUNT(*) FROM scan_queue WHERE processed=0"
    ).fetchone()[0]

    # Satellite sales (from trade-show Pi) vs local sales
    sat_sales = 0
    try:
        sat_sales = db.execute(
            "SELECT COUNT(*) FROM sales WHERE source='satellite'"
        ).fetchone()[0]
    except Exception:
        pass  # column may not exist on very old DBs

    # DB file sizes
    db_size_mb  = 0.0
    wal_size_mb = 0.0
    try:
        db_size_mb  = round(os.path.getsize(DB_PATH) / (1024 * 1024), 2)
        wal_path    = DB_PATH + "-wal"
        if os.path.exists(wal_path):
            wal_size_mb = round(os.path.getsize(wal_path) / (1024 * 1024), 2)
    except Exception:
        pass

    data = {
        "status":          "ok",
        "server":          "HanryxVault Pi",
        "version":         "2.0",
        "time_ms":         int(_time.time() * 1000),
        "uptime_s":        int(_time.time() - _server_start_time),
        "inventory":       inv_count,
        "total_sales":     sale_count,
        "satellite_sales": sat_sales,
        "local_sales":     sale_count - sat_sales,
        "pending_scans":   pending_scans,
        "sse_clients":     len(_sse_scan_subscribers),
        "db_size_mb":      db_size_mb,
        "wal_size_mb":     wal_size_mb,
    }
    _cache_set(_health_cache, "h", data)
    return jsonify(data)


@app.route("/cache/stats", methods=["GET"])
def cache_stats():
    """Performance stats — consumed by desktop monitor."""
    return jsonify({
        "inventory_cache_size": len(_inventory_cache),
        "scan_cache_size":      len(_scan_cache),
        "stats":                _cache_stats,
    })


# ---------------------------------------------------------------------------
# Zettle OAuth
# ---------------------------------------------------------------------------

@app.route("/zettle/auth", methods=["GET"])
def zettle_auth():
    import secrets
    state = secrets.token_urlsafe(16)
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id":     ZETTLE_CLIENT_ID,
        "redirect_uri":  ZETTLE_REDIRECT_URI,
        "scope":         "READ:FINANCE WRITE:PAYMENT",
        "state":         state,
    })
    return redirect(f"{ZETTLE_OAUTH_BASE}/authorize?{params}")


@app.route("/zettle/callback", methods=["GET"])
def zettle_callback():
    code  = request.args.get("code", "")
    error = request.args.get("error", "")
    if error or not code:
        return jsonify({"error": error or "missing code"}), 400
    try:
        result = _token_post({
            "grant_type":   "authorization_code",
            "code":         code,
            "redirect_uri": ZETTLE_REDIRECT_URI,
        })
        _store_tokens(result)
        return redirect(ZETTLE_APP_SCHEME + "?success=1")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/zettle/status", methods=["GET"])
def zettle_status():
    with _token_lock:
        has_token = bool(_zettle_state.get("access_token"))
        expires   = _zettle_state.get("expires_at", 0.0)
    return jsonify({
        "authenticated": has_token,
        "expires_in_s":  max(0, int(expires - _time.time())) if has_token else 0,
    })


# ---------------------------------------------------------------------------
# Scanner relay
# ---------------------------------------------------------------------------

@app.route("/scan", methods=["POST"])
def scan_post():
    data    = request.get_json(force=True, silent=True) or {}
    qr_code = (data.get("qrCode") or data.get("qr_code") or data.get("code") or "").strip()
    if not qr_code:
        return jsonify({"error": "qrCode is required"}), 400

    # Normalise Pokémon TCG URL QR codes → canonical SET-NUMBER key.
    # Stores the normalised form so exact-match lookups work even when the
    # physical QR contains a full URL.
    normalised = _normalize_qr(qr_code)
    store_code = normalised  # what goes into the DB

    db = get_db()
    db.execute("INSERT INTO scan_queue (qr_code) VALUES (?)", (store_code,))
    db.commit()
    # Bust the 1 s scan cache so the tablet picks this up on its very next poll
    with _cache_lock:
        _scan_cache.clear()
    # Also push instantly to any SSE clients (tablet with /scan/stream)
    _sse_broadcast_scan(store_code)

    if normalised != qr_code:
        print(f"[scan] Queued (normalised): {qr_code!r} → {store_code!r}")
    else:
        print(f"[scan] Queued: {store_code}")

    return jsonify({"ok": True, "queued": store_code, "original": qr_code}), 201


@app.route("/scan/pending", methods=["GET"])
def scan_pending():
    cached = _cache_get(_scan_cache, "p")
    if cached is not None:
        _cache_stats["scan_hits"] += 1
        return jsonify(cached)
    _cache_stats["scan_misses"] += 1
    db  = get_db()
    row = db.execute(
        "SELECT id, qr_code FROM scan_queue WHERE processed = 0 ORDER BY id ASC LIMIT 1"
    ).fetchone()
    if not row:
        result = {"id": 0, "qrCode": ""}
    else:
        qr_code = row["qr_code"]
        result  = {"id": row["id"], "qrCode": qr_code}
        # Attach a resolved product (local inventory + TCG enrichment) so the
        # tablet gets name, price, image, market data in a single response.
        matches = _card_lookup(db, qr=qr_code, limit=1)
        local   = matches[0] if matches else None
        enriched = _enrich_with_tcg(local, qr_code)
        if enriched.get("name") or enriched.get("tcgData"):
            result["resolvedProduct"] = enriched
    _cache_set(_scan_cache, "p", result)
    return jsonify(result)


@app.route("/scan/ack/<int:scan_id>", methods=["POST"])
def scan_ack(scan_id):
    db = get_db()
    db.execute("UPDATE scan_queue SET processed = 1 WHERE id = ?", (scan_id,))
    db.commit()
    # Clear scan cache so next poll immediately returns the next pending item
    with _cache_lock:
        _scan_cache.clear()
    return jsonify({"ok": True, "acked": scan_id})


# ---------------------------------------------------------------------------
# SSE scan stream — real-time alternative to polling /scan/pending
# ---------------------------------------------------------------------------

@app.route("/scan/stream", methods=["GET"])
def scan_stream():
    """
    Server-Sent Events endpoint.  Connect once; barcode scan events are pushed
    instantly instead of polling /scan/pending every 1.5 s.

    Event format:  data: {"qrCode": "<code>"}

    Heartbeat comment every 15 s keeps the connection alive through nginx.
    The existing /scan/pending + /scan/ack polling flow still works — no app
    changes are required to benefit from this endpoint.
    """
    def _generate():
        q = _queue_mod.Queue()
        with _sse_lock:
            _sse_scan_subscribers.append(q)
        try:
            while True:
                try:
                    qr_code = q.get(timeout=15)
                    yield f"data: {json.dumps({'qrCode': qr_code})}\n\n"
                except _queue_mod.Empty:
                    yield ": heartbeat\n\n"  # keeps nginx from closing the connection
        finally:
            with _sse_lock:
                try:
                    _sse_scan_subscribers.remove(q)
                except ValueError:
                    pass

    return Response(
        _generate(),
        content_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",   # tell nginx: do not buffer this response
            "Connection":        "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Card lookup — fuzzy / multi-field search for Pokémon (and any) cards
# ---------------------------------------------------------------------------

@app.route("/card/lookup", methods=["GET"])
def card_lookup():
    """
    Fuzzy card lookup used by the website and any scanner client.

    Query params (at least one required):
      qr    — raw scanner value (URL or plain code); normalised automatically
      q     — general search string (name fragment, set+number, etc.)
      name  — card name (can be partial)
      set   — set code, e.g. "SV1", "XY3", "BW"
      num   — card number within the set, e.g. "001" or "1"
      limit — max results to return (default 10, max 50)

    Returns:
      {"results": [...], "count": N, "query": {...}}

    Examples:
      GET /card/lookup?q=charizard           → all Charizard cards
      GET /card/lookup?q=SV1-001             → card 1 from Scarlet & Violet base
      GET /card/lookup?qr=https://pokemon.com/...?set=SV1&number=1
      GET /card/lookup?name=pikachu&set=sv1  → Pikachu in SV1
    """
    qr       = request.args.get("qr",    "").strip()
    q        = request.args.get("q",     "").strip()
    name     = request.args.get("name",  "").strip()
    set_code = request.args.get("set",   "").strip().upper()
    card_num = request.args.get("num",   "").strip().lstrip("0")
    try:
        limit = min(int(request.args.get("limit", 10)), 50)
    except (ValueError, TypeError):
        limit = 10

    if not any([qr, q, name, set_code]):
        return jsonify({"error": "Provide at least one of: qr, q, name, set"}), 400

    db      = get_db()
    results = _card_lookup(db, q=q, qr=qr, name=name,
                           set_code=set_code, card_num=card_num, limit=limit)

    return jsonify({
        "results": results,
        "count":   len(results),
        "query":   {
            "qr": qr, "q": q, "name": name,
            "set": set_code, "num": card_num,
        },
    })


@app.route("/card/lookup", methods=["POST"])
def card_lookup_post():
    """
    POST version of /card/lookup — used by the website camera scanner.

    Body (JSON):
      {"qr": "...", "q": "...", "name": "...", "set": "...", "num": "...", "limit": 10}

    Same response as GET version.
    """
    body     = request.get_json(silent=True) or {}
    qr       = (body.get("qr")   or "").strip()
    q        = (body.get("q")    or "").strip()
    name     = (body.get("name") or "").strip()
    set_code = (body.get("set")  or "").strip().upper()
    card_num = (body.get("num")  or "").strip().lstrip("0")
    try:
        limit = min(int(body.get("limit", 10)), 50)
    except (ValueError, TypeError):
        limit = 10

    if not any([qr, q, name, set_code]):
        return jsonify({"error": "Provide at least one of: qr, q, name, set"}), 400

    db      = get_db()
    results = _card_lookup(db, q=q, qr=qr, name=name,
                           set_code=set_code, card_num=card_num, limit=limit)
    return jsonify({"results": results, "count": len(results)})


# ---------------------------------------------------------------------------
# Card enrichment — local inventory + full TCG API data merged
# ---------------------------------------------------------------------------

@app.route("/card/enrich", methods=["GET", "POST"])
def card_enrich():
    """
    Return the richest possible card data by combining local inventory with
    the Pokémon TCG API (cached 24 h).

    GET  /card/enrich?qr=SV1-1
    GET  /card/enrich?name=Charizard&set=sv3
    POST /card/enrich  {"qr":"SV1-1"}

    Response includes:
      • All local inventory fields (price, stock, rarity, …)
      • tcgData: {name, hp, types, rarity, set, images{small,large},
                  attacks, weaknesses, tcgplayer{marketPrice, priceTiers}}
      • inLocalInventory: bool
      • isDuplicate: bool — card already in stock
      • suggestedPrice: float — TCG market price when no local price set
      • imageUrl: large card image
    """
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        qr   = (body.get("qr") or body.get("qrCode") or "").strip()
        name = (body.get("name") or "").strip()
        set_code = (body.get("set") or "").strip()
        num      = (body.get("num") or "").strip().lstrip("0")
    else:
        qr       = request.args.get("qr",   "").strip()
        name     = request.args.get("name", "").strip()
        set_code = request.args.get("set",  "").strip()
        num      = request.args.get("num",  "").strip().lstrip("0")

    # Build a canonical qr code to look up
    if not qr:
        if set_code and num:
            qr = f"{set_code.upper()}-{num}"
        elif name:
            qr = name  # will be used as text search below

    if not qr:
        return jsonify({"error": "Provide qr, name, or set+num"}), 400

    norm_qr = _normalize_qr(qr)
    db      = get_db()
    matches = _card_lookup(db, qr=norm_qr, name=name,
                           set_code=set_code, card_num=num, limit=1)
    local   = matches[0] if matches else None
    result  = _enrich_with_tcg(local, norm_qr)
    result["normalizedQr"] = norm_qr
    return jsonify(result)


# ---------------------------------------------------------------------------
# Card condition — NM / LP / MP / HP / DMG per qr_code
# ---------------------------------------------------------------------------

_CONDITIONS = {"NM", "LP", "MP", "HP", "DMG"}

@app.route("/card/condition/<path:qr_code>", methods=["GET"])
def card_condition_get(qr_code):
    db  = get_db()
    row = db.execute(
        "SELECT condition, notes, updated_ms FROM card_conditions WHERE qr_code=?", (qr_code,)
    ).fetchone()
    if row:
        return jsonify({"qrCode": qr_code, "condition": row["condition"],
                        "notes": row["notes"], "updatedMs": row["updated_ms"]})
    return jsonify({"qrCode": qr_code, "condition": "NM", "notes": "", "updatedMs": None})


@app.route("/card/condition/<path:qr_code>", methods=["POST"])
def card_condition_set(qr_code):
    """
    POST /card/condition/SV1-1
    Body: {"condition": "LP", "notes": "minor corner wear"}

    condition must be one of: NM, LP, MP, HP, DMG
    """
    body      = request.get_json(silent=True) or {}
    condition = (body.get("condition") or "NM").strip().upper()
    notes     = (body.get("notes") or "").strip()[:500]
    if condition not in _CONDITIONS:
        return jsonify({"error": f"condition must be one of {sorted(_CONDITIONS)}"}), 400
    db = get_db()
    db.execute("""
        INSERT INTO card_conditions (qr_code, condition, notes, updated_ms)
        VALUES (?,?,?,?)
        ON CONFLICT(qr_code) DO UPDATE SET
            condition=excluded.condition, notes=excluded.notes, updated_ms=excluded.updated_ms
    """, (qr_code, condition, notes, _now_ms()))
    db.commit()
    return jsonify({"ok": True, "qrCode": qr_code, "condition": condition})


# ---------------------------------------------------------------------------
# Bulk export — JSON or CSV for website upload
# ---------------------------------------------------------------------------

@app.route("/admin/export-cards", methods=["GET"])
def admin_export_cards():
    """
    Export Pokémon card inventory in a format ready for bulk import on the
    HanRYX website.

    GET /admin/export-cards           → JSON array
    GET /admin/export-cards?fmt=csv   → CSV download
    GET /admin/export-cards?cat=Trading+Card  → filter by category
    GET /admin/export-cards?enrich=1  → include TCG images + market prices (slow!)

    Each row contains: qrCode, name, price, category, rarity, setCode,
    description, stock, imageUrl, tcgId, condition, tcgMarketPrice
    """
    fmt      = request.args.get("fmt", "json").lower()
    cat      = request.args.get("cat", "").strip()
    do_enrich = request.args.get("enrich", "0") == "1"

    db = get_db()
    if cat:
        rows = db.execute(
            "SELECT * FROM inventory WHERE LOWER(category) LIKE ? ORDER BY name ASC",
            (f"%{cat.lower()}%",)
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM inventory ORDER BY name ASC").fetchall()

    # Fetch conditions in one query
    cond_rows = db.execute("SELECT qr_code, condition, notes FROM card_conditions").fetchall()
    cond_map  = {r["qr_code"]: {"condition": r["condition"], "notes": r["notes"]}
                 for r in cond_rows}

    cards = []
    for r in rows:
        qr = r["qr_code"]
        c  = cond_map.get(qr, {})
        entry = {
            "qrCode":      qr,
            "name":        r["name"],
            "price":       r["price"],
            "category":    r["category"] or "General",
            "rarity":      r["rarity"] or "",
            "setCode":     r["set_code"] or "",
            "description": r["description"] or "",
            "stock":       r["stock"],
            "imageUrl":    r["image_url"] if "image_url" in r.keys() else "",
            "tcgId":       r["tcg_id"]    if "tcg_id"    in r.keys() else "",
            "condition":   c.get("condition", "NM"),
            "conditionNotes": c.get("notes", ""),
            "lastUpdated": r["last_updated"],
        }
        if do_enrich:
            enriched = _enrich_with_tcg(entry, qr)
            mkt = (enriched.get("tcgData") or {}).get("tcgplayer", {}).get("marketPrice")
            if mkt:
                entry["tcgMarketPrice"] = mkt
            if enriched.get("imageUrl") and not entry["imageUrl"]:
                entry["imageUrl"] = enriched["imageUrl"]
        else:
            entry["tcgMarketPrice"] = None
        cards.append(entry)

    if fmt == "csv":
        fields = ["qrCode","name","price","stock","category","rarity","setCode",
                  "condition","imageUrl","tcgId","tcgMarketPrice","description",
                  "conditionNotes","lastUpdated"]
        buf = io.StringIO()
        w   = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(cards)
        from flask import Response as _Resp
        return _Resp(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=hanryx_inventory.csv"},
        )

    return jsonify({"count": len(cards), "cards": cards})


# ---------------------------------------------------------------------------
# Webhook config — auto-push new cards to the HanRYX website
# ---------------------------------------------------------------------------

@app.route("/admin/webhook-config", methods=["GET"])
def webhook_config_get():
    """Return whether a webhook URL is configured (never reveals the URL itself)."""
    db  = get_db()
    row = db.execute("SELECT value FROM server_state WHERE key='webhook_url'").fetchone()
    has = bool(row and row["value"] and row["value"].strip())
    return jsonify({"configured": has})


@app.route("/admin/webhook-config", methods=["POST"])
def webhook_config_set():
    """
    Configure the webhook URL that receives new card data automatically.

    POST /admin/webhook-config
    Body: {"url": "https://hanryxvault.app/api/pi-ingest"}
          {"url": ""}  ← clears the webhook

    The Pi will POST card JSON to this URL whenever a product is saved via
    /admin/inventory and the card has tcgData or an imageUrl.
    """
    body = request.get_json(silent=True) or {}
    url  = (body.get("url") or "").strip()
    db   = get_db()
    db.execute(
        "INSERT OR REPLACE INTO server_state (key, value) VALUES ('webhook_url', ?)", (url,)
    )
    db.commit()
    action = "cleared" if not url else "saved"
    return jsonify({"ok": True, "action": action})


# ---------------------------------------------------------------------------
# Satellite authentication
#   The trade-show Pi includes X-Satellite-Token on every POST to this server.
#   The shared secret is stored in the server_state table under key
#   'satellite_token'.  If no token is configured, all requests are accepted
#   (safe default until you explicitly lock it down).
# ---------------------------------------------------------------------------

_satellite_token_cache: str | None = None   # lazily loaded, cleared on DB write


def _load_satellite_token() -> str | None:
    global _satellite_token_cache
    if _satellite_token_cache is not None:
        return _satellite_token_cache or None
    try:
        row = get_db().execute(
            "SELECT value FROM server_state WHERE key='satellite_token'"
        ).fetchone()
        _satellite_token_cache = row[0] if row else ""
    except Exception:
        _satellite_token_cache = ""
    return _satellite_token_cache or None


def _validate_satellite_token() -> tuple[bool, str]:
    """
    Returns (ok, error_message).
    If no token is configured on the home Pi, all callers are accepted so the
    system works out-of-the-box before the token is set up.
    """
    expected = _load_satellite_token()
    if not expected:
        return True, ""                         # no token configured — open
    provided = request.headers.get("X-Satellite-Token", "")
    if provided == expected:
        return True, ""
    return False, "Invalid or missing satellite token"


# ---------------------------------------------------------------------------
# Sales sync
# ---------------------------------------------------------------------------

@app.route("/sync/sales", methods=["POST"])
def sync_sales():
    data = request.get_json(force=True, silent=True)
    ok, err = _validate_satellite_token()
    if not ok:
        return jsonify({"error": err}), 401

    if not data or not isinstance(data, list):
        return jsonify({"error": "Expected a JSON array of sales"}), 400

    # source: "satellite" when pushed from trade-show Pi, "local" otherwise
    source = request.headers.get("X-Source", "local")

    db       = get_db()
    inserted = 0
    skipped  = 0

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
                     cash_received, change_given, is_refunded, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                sale.get("source", source),
            ))
            if db.execute("SELECT changes()").fetchone()[0] > 0:
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"[sync/sales] Error on {transaction_id}: {e}")
            skipped += 1

    db.commit()
    print(f"[sync/sales] source={source} inserted={inserted} skipped={skipped}")
    return jsonify({"inserted": inserted, "skipped": skipped, "source": source}), 200


# ---------------------------------------------------------------------------
# Inventory deduction
# ---------------------------------------------------------------------------

@app.route("/inventory/deduct", methods=["POST"])
def inventory_deduct():
    data = request.get_json(force=True, silent=True)

    ok, err = _validate_satellite_token()
    if not ok:
        return jsonify({"error": err}), 401

    if not data or not isinstance(data, list):
        return jsonify({"error": "Expected a JSON array of sold items"}), 400

    db           = get_db()
    deducted     = 0
    unknown      = 0
    oversold     = 0
    stock_levels = {}    # qr_code → new stock level (returned to satellite)

    for item in data:
        qr_code    = item.get("qrCode", "")
        name       = item.get("name", "Unknown")
        quantity   = int(item.get("quantity", 1))
        unit_price = float(item.get("unitPrice", 0.0))
        line_total = float(item.get("lineTotal", unit_price * quantity))

        db.execute("""
            INSERT INTO stock_deductions (qr_code, name, quantity, unit_price, line_total)
            VALUES (?, ?, ?, ?, ?)
        """, (qr_code, name, quantity, unit_price, line_total))

        # Check stock BEFORE deducting so we can flag an oversell
        before = db.execute(
            "SELECT stock FROM inventory WHERE qr_code = ?", (qr_code,)
        ).fetchone()

        result = db.execute("""
            UPDATE inventory
            SET stock = MAX(0, stock - ?), last_updated = ?
            WHERE qr_code = ?
        """, (quantity, _now_ms(), qr_code))

        if result.rowcount > 0:
            deducted += 1
            after_stock = db.execute(
                "SELECT stock FROM inventory WHERE qr_code = ?", (qr_code,)
            ).fetchone()
            new_stock = after_stock[0] if after_stock else 0
            stock_levels[qr_code] = new_stock
            if before and before[0] < quantity:
                oversold += 1
                print(f"[inventory/deduct] OVERSELL {qr_code}: "
                      f"had {before[0]}, sold {quantity} → clamped to 0")
        else:
            unknown += 1

    db.commit()
    _invalidate_inventory()
    print(f"[inventory/deduct] deducted={deducted} oversold={oversold} unknown_sku={unknown}")
    return jsonify({
        "deducted":     deducted,
        "unknown_skus": unknown,
        "oversold":     oversold,      # items where satellite sold more than was in stock
        "stock_levels": stock_levels,  # qr_code → new stock level after deduction
    }), 200


# ---------------------------------------------------------------------------
# Inventory read (tablet catalogue)
# ---------------------------------------------------------------------------

@app.route("/inventory", methods=["GET"])
def get_inventory():
    search = request.args.get("q", "").strip().lower()
    since  = request.args.get("since", "")

    # Use cache only for the common unfiltered full-catalogue request
    if not search and not since:
        cached = _cache_get(_inventory_cache, "all")
        if cached is not None:
            _cache_stats["inventory_hits"] += 1
            return jsonify(cached)
        _cache_stats["inventory_misses"] += 1

    db     = get_db()

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

    products = [{
        "qrCode":        r["qr_code"],
        "name":          r["name"],
        "price":         r["price"],
        "category":      r["category"] or "General",
        "rarity":        r["rarity"] or "",
        "setCode":       r["set_code"] or "",
        "description":   r["description"] or "",
        "stockQuantity": r["stock"],
        "lastUpdated":   r["last_updated"],
    } for r in rows]

    if not search and not since:
        _cache_set(_inventory_cache, "all", products)

    # ETag — lets the tablet skip re-processing an identical catalogue
    etag = hashlib.md5(json.dumps(products, sort_keys=True).encode()).hexdigest()
    if request.headers.get("If-None-Match") == etag:
        return "", 304
    resp = jsonify(products)
    resp.headers["ETag"]          = etag
    resp.headers["Cache-Control"] = "private, max-age=28"
    return resp


# ---------------------------------------------------------------------------
# Inventory push (from websites / scanner)
# ---------------------------------------------------------------------------

@app.route("/push/inventory", methods=["POST"])
def push_inventory():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    items    = data if isinstance(data, list) else [data]
    db       = get_db()
    upserted = 0
    errors   = 0

    for item in items:
        qr_code = item.get("qrCode") or item.get("qr_code") or item.get("barcode") or item.get("id")
        name    = item.get("name") or item.get("title") or item.get("productName")
        if not qr_code or not name:
            errors += 1
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
                str(qr_code), str(name),
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
    _invalidate_inventory()
    return jsonify({"upserted": upserted, "errors": errors}), 200


@app.route("/push/inventory/csv", methods=["POST"])
def push_inventory_csv():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "Upload a CSV with field 'file'"}), 400

    content  = f.read().decode("utf-8-sig")
    reader   = csv.DictReader(io.StringIO(content))
    db       = get_db()
    upserted = 0
    skipped  = 0

    for row in reader:
        qr_code = (row.get("qrCode") or row.get("barcode") or row.get("qr_code") or "").strip()
        name    = (row.get("name") or row.get("title") or "").strip()
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
                row.get("category", "General"), row.get("rarity", ""),
                row.get("setCode") or row.get("set_code", ""),
                row.get("description", ""), stock, _now_ms(),
            ))
            upserted += 1
        except Exception as e:
            print(f"[csv] Row error: {e} — {row}")
            skipped += 1

    db.commit()
    _invalidate_inventory()
    return jsonify({"upserted": upserted, "skipped": skipped}), 200


# ---------------------------------------------------------------------------
# Admin — sync from cloud
# ---------------------------------------------------------------------------

@app.route("/admin/sync-from-cloud", methods=["POST"])
def admin_sync_cloud():
    force  = request.args.get("force", "0") == "1"
    result = sync_inventory_from_cloud(force=force)
    _invalidate_inventory()
    return jsonify(result)


# ---------------------------------------------------------------------------
# Admin — satellite token management
# ---------------------------------------------------------------------------

@app.route("/admin/set-satellite-token", methods=["POST"])
def admin_set_satellite_token():
    """
    Register the shared secret for satellite-Pi authentication.
    Called on the HOME Pi after running setup-satellite.sh on the trade-show Pi.

    Usage:
        curl -s -X POST http://localhost:8080/admin/set-satellite-token \\
             -H 'Content-Type: application/json' \\
             -d '{"token":"<64-char-hex>"}'
    """
    body  = request.get_json(silent=True) or {}
    token = (body.get("token") or "").strip()
    if not token:
        return jsonify({"error": "token is required"}), 400
    if len(token) < 16:
        return jsonify({"error": "token is too short (minimum 16 characters)"}), 400

    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO server_state(key, value) VALUES ('satellite_token', ?)",
        (token,)
    )
    db.commit()
    # Invalidate health cache so the next /health shows updated state
    _cache_set(_health_cache, "h", None)
    return jsonify({"ok": True, "message": "Satellite token saved — sync auth is now active"}), 200


@app.route("/admin/satellite-token-status", methods=["GET"])
def admin_satellite_token_status():
    """Check whether a satellite token is currently registered (never reveals the token value)."""
    db  = get_db()
    row = db.execute(
        "SELECT value FROM server_state WHERE key='satellite_token'"
    ).fetchone()
    has_token = bool(row and row["value"])
    return jsonify({"has_token": has_token})


# ---------------------------------------------------------------------------
# Admin — JSON endpoints
# ---------------------------------------------------------------------------

@app.route("/admin/sales", methods=["GET"])
def admin_sales():
    db   = get_db()
    rows = db.execute("SELECT * FROM sales ORDER BY timestamp_ms DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/admin/inventory", methods=["GET"])
def admin_inventory_json():
    db   = get_db()
    rows = db.execute("SELECT * FROM inventory ORDER BY name ASC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/admin/inventory", methods=["POST"])
def admin_add_product():
    data    = request.get_json(force=True, silent=True) or {}
    qr_code = (data.get("qrCode") or data.get("qr_code") or "").strip()
    name    = (data.get("name") or "").strip()
    if not qr_code or not name:
        return jsonify({"error": "qrCode and name are required"}), 400

    image_url = (data.get("imageUrl") or data.get("image_url") or "").strip()
    tcg_id    = (data.get("tcgId")    or data.get("tcg_id")    or "").strip()

    db = get_db()
    db.execute("""
        INSERT INTO inventory
            (qr_code, name, price, category, rarity, set_code, description, stock,
             image_url, tcg_id, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(qr_code) DO UPDATE SET
            name=excluded.name, price=excluded.price, category=excluded.category,
            rarity=excluded.rarity, set_code=excluded.set_code,
            description=excluded.description, stock=excluded.stock,
            image_url=CASE WHEN excluded.image_url!='' THEN excluded.image_url ELSE image_url END,
            tcg_id=CASE WHEN excluded.tcg_id!=''    THEN excluded.tcg_id    ELSE tcg_id    END,
            last_updated=excluded.last_updated
    """, (
        qr_code, name,
        float(data.get("price", 0)), data.get("category", "General"),
        data.get("rarity", ""), data.get("setCode") or data.get("set_code", ""),
        data.get("description", ""), int(data.get("stock", 0)),
        image_url, tcg_id, _now_ms(),
    ))
    db.commit()
    _invalidate_inventory()

    # Fire webhook in background (non-blocking) if configured
    webhook_payload = {
        "event":       "card_saved",
        "qrCode":      qr_code,
        "name":        name,
        "price":       float(data.get("price", 0)),
        "category":    data.get("category", "General"),
        "rarity":      data.get("rarity", ""),
        "setCode":     data.get("setCode") or data.get("set_code", ""),
        "stock":       int(data.get("stock", 0)),
        "imageUrl":    image_url,
        "tcgId":       tcg_id,
        "savedAt":     _now_ms(),
    }
    threading.Thread(target=_fire_webhook, args=(webhook_payload,), daemon=True).start()

    return jsonify({"ok": True, "qrCode": qr_code})


@app.route("/admin/inventory/<qr_code>", methods=["DELETE"])
def admin_delete_product(qr_code):
    db = get_db()
    db.execute("DELETE FROM inventory WHERE qr_code = ?", (qr_code,))
    db.commit()
    _invalidate_inventory()
    return jsonify({"ok": True, "deleted": qr_code})


# ---------------------------------------------------------------------------
# Receipt Printer — ESC/POS over Bluetooth (/dev/rfcomm0) or USB (/dev/usb/lp0)
# ---------------------------------------------------------------------------

_PRINTER_CONF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "printer.conf")

# ESC/POS byte commands — no external library needed
_ESC             = b'\x1b'
_GS              = b'\x1d'
_PR_INIT         = _ESC + b'@'           # initialise printer
_PR_BOLD_ON      = _ESC + b'E\x01'
_PR_BOLD_OFF     = _ESC + b'E\x00'
_PR_CENTER       = _ESC + b'a\x01'
_PR_LEFT         = _ESC + b'a\x00'
_PR_DOUBLE       = _ESC + b'!\x30'       # double width + double height
_PR_NORMAL       = _ESC + b'!\x00'
_PR_CUT          = _GS  + b'V\x42\x00'  # partial cut + feed
_PR_LF           = b'\n'
_PR_DIVIDER_WIDE = b'-' * 42 + b'\n'    # 80 mm paper (42 chars)
_PR_DIVIDER_NARR = b'-' * 32 + b'\n'    # 58 mm paper (32 chars)


def _load_printer_conf() -> dict:
    conf = {
        "printer_path":   None,
        "printer_type":   "auto",
        "printer_usb_path": "/dev/usb/lp0",
        "receipt_header":   "HanryxVault",
        "receipt_subheader": "Trading Card Shop",
        "receipt_footer":   "hanryxvault.cards",
    }
    if os.path.exists(_PRINTER_CONF_PATH):
        with open(_PRINTER_CONF_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    conf[k.strip()] = v.strip() or None
    return conf


def _open_printer():
    """Return an open writable file handle to the first available printer."""
    conf = _load_printer_conf()

    # Order of preference: Bluetooth rfcomm → USB lp → rfcomm1 fallback
    candidates = []
    if conf.get("printer_path"):
        candidates.append(conf["printer_path"])
    candidates += ["/dev/rfcomm0", "/dev/usb/lp0", "/dev/rfcomm1", "/dev/ttyUSB0"]

    for path in candidates:
        if os.path.exists(path):
            try:
                fh = open(path, "wb")
                return fh, path, conf
            except OSError:
                continue

    # Last resort: CUPS lp command
    return None, "cups", conf


def _format_receipt(sale: dict, conf: dict) -> bytes:
    """Build ESC/POS byte string for one sale receipt."""
    header    = (conf.get("receipt_header")    or "HanryxVault").encode()
    subheader = (conf.get("receipt_subheader") or "Trading Card Shop").encode()
    footer    = (conf.get("receipt_footer")    or "hanryxvault.cards").encode()

    divider = _PR_DIVIDER_NARR   # default 58 mm

    timestamp = sale.get("timestamp", 0)
    if timestamp:
        dt_str = datetime.datetime.fromtimestamp(timestamp / 1000).strftime("%Y-%m-%d  %H:%M")
    else:
        dt_str = datetime.datetime.now().strftime("%Y-%m-%d  %H:%M")

    txn_id  = (sale.get("transactionId") or sale.get("transaction_id") or "")[:16]
    method  = sale.get("paymentMethod")  or sale.get("payment_method") or "CARD"
    items   = sale.get("items", [])

    # ── Build receipt bytes ──────────────────────────────────────────────────
    out = bytearray()
    out += _PR_INIT
    out += _PR_LF

    # Header
    out += _PR_CENTER + _PR_DOUBLE + header + _PR_LF
    out += _PR_NORMAL + subheader  + _PR_LF + _PR_LEFT
    out += _PR_LF + divider

    # Date / transaction
    out += f"{dt_str}\n".encode()
    if txn_id:
        out += f"Txn: {txn_id}\n".encode()
    out += divider

    # Line items
    for item in items:
        name  = (item.get("name") or "Item")[:22]
        qty   = int(item.get("quantity") or 1)
        price = float(item.get("unitPrice") or item.get("price") or 0)
        total = float(item.get("lineTotal") or (price * qty))
        line  = f"{name:<22} ${total:>7.2f}\n"
        if qty > 1:
            line = f"  x{qty} {name:<19} ${total:>7.2f}\n"
        out += line.encode()

    out += divider

    # Totals
    subtotal = float(sale.get("subtotal",   0))
    tax      = float(sale.get("taxAmount",  0))
    tip      = float(sale.get("tipAmount",  0))
    total    = float(sale.get("totalAmount",0))
    out += f"{'Subtotal':<22} ${subtotal:>7.2f}\n".encode()
    if tax > 0:
        out += f"{'Tax':<22} ${tax:>7.2f}\n".encode()
    if tip > 0:
        out += f"{'Tip':<22} ${tip:>7.2f}\n".encode()
    out += _PR_BOLD_ON
    out += f"{'TOTAL':<22} ${total:>7.2f}\n".encode()
    out += _PR_BOLD_OFF

    # Payment
    out += f"Payment: {method}\n".encode()
    cash = float(sale.get("cashReceived") or sale.get("cash_received") or 0)
    if cash > 0:
        change = float(sale.get("changeGiven") or sale.get("change_given") or 0)
        out += f"Cash: ${cash:.2f}  Change: ${change:.2f}\n".encode()

    out += divider
    out += _PR_CENTER
    out += f"{footer.decode()}\n".encode()
    out += b"Thank you!\n"
    out += _PR_NORMAL + _PR_LEFT

    # Feed + cut
    out += _PR_LF * 4
    out += _PR_CUT

    return bytes(out)


def _do_print(sale: dict):
    """Background-thread print job — tries BT/USB/CUPS in order."""
    fh, path, conf = _open_printer()

    try:
        receipt_bytes = _format_receipt(sale, conf)

        if fh is not None:
            fh.write(receipt_bytes)
            fh.flush()
            fh.close()
            print(f"[print] Receipt sent to {path}", flush=True)

        elif path == "cups":
            # Write temp file and submit via lp
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
                tmp.write(receipt_bytes)
                tmp_path = tmp.name
            result = subprocess.run(
                ["lp", "-o", "raw", tmp_path],
                capture_output=True, timeout=10
            )
            os.unlink(tmp_path)
            if result.returncode == 0:
                print(f"[print] Receipt submitted via CUPS lp", flush=True)
            else:
                print(f"[print] CUPS lp failed: {result.stderr.decode()}", flush=True)

        else:
            print("[print] No printer found — receipt not printed", flush=True)

    except Exception as e:
        print(f"[print] Print error: {e}", flush=True)
        if fh:
            try:
                fh.close()
            except Exception:
                pass


@app.route("/print/receipt", methods=["POST"])
def print_receipt():
    """
    Print a receipt on the connected Bluetooth or USB thermal printer.
    Body: sale JSON (same format as /sync/sales items).
    Tablet app calls this after every completed sale.
    Non-blocking — returns immediately, prints in background.
    """
    sale = request.get_json(force=True, silent=True) or {}
    if not sale:
        return jsonify({"error": "Sale JSON body required"}), 400

    threading.Thread(target=_do_print, args=(sale,), daemon=True).start()
    return jsonify({"ok": True, "queued": True}), 202


@app.route("/print/status", methods=["GET"])
def print_status():
    """Returns which printer device is currently available."""
    _, path, conf = _open_printer()
    return jsonify({
        "printer_available": path is not None,
        "printer_path":      path,
        "bt_mac":            conf.get("printer_bt_mac"),
    })


# ---------------------------------------------------------------------------
# APK download
# ---------------------------------------------------------------------------

APK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hanryxvault.apk")


@app.route("/download/apk", methods=["GET"])
def download_apk():
    if not os.path.exists(APK_PATH):
        return jsonify({"error": "APK not found on server"}), 404
    from flask import send_file
    return send_file(APK_PATH, as_attachment=True, download_name="hanryxvault.apk")


# ---------------------------------------------------------------------------
# Admin dashboard (HTML)
# ---------------------------------------------------------------------------

@app.route("/admin", methods=["GET"])
def admin_dashboard():
    db = get_db()

    midnight_ms = int(datetime.datetime.combine(
        datetime.date.today(), datetime.time.min
    ).timestamp() * 1000)

    today_sales = db.execute("""
        SELECT COUNT(*) as count,
               COALESCE(SUM(total_amount), 0) as revenue,
               COALESCE(SUM(tax_amount),   0) as tax,
               COALESCE(SUM(tip_amount),   0) as tips
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

    e = _html.escape  # shorthand — always escape user-sourced strings into HTML

    rows_recent = "".join(
        f"<tr><td>{e(r['transaction_id'][:12])}…</td><td>${r['total_amount']:.2f}</td>"
        f"<td>{e(r['payment_method'])}</td><td>{e(r['employee_id'])}</td>"
        f"<td>{fmt_time(r['timestamp_ms'])}</td></tr>"
        for r in recent_sales
    )

    rows_low = "".join(
        f"<tr style='color:{'#f44336' if r['stock']==0 else '#ff9800'}'>"
        f"<td>{e(r['name'])}</td><td>{e(r['qr_code'])}</td>"
        f"<td><b>{r['stock']}</b></td><td>${r['price']:.2f}</td><td>{e(r['category'])}</td></tr>"
        for r in low_stock
    ) or "<tr><td colspan='5' style='color:#4caf50'>All stock levels healthy ✓</td></tr>"

    rows_inv = "".join(
        f"<tr data-qr=\"{e(r['qr_code'], quote=True)}\" "
        f"data-name=\"{e(r['name'], quote=True)}\" data-price=\"{r['price']}\" "
        f"data-cat=\"{e(r['category'], quote=True)}\" data-stock=\"{r['stock']}\" style=\"cursor:pointer\">"
        f"<td>{e(r['name'])}</td><td><code style='color:#aaa'>{e(r['qr_code'])}</code></td>"
        f"<td>{r['stock']}</td><td>${r['price']:.2f}</td><td>{e(r['category'])}</td>"
        f"<td><button class='btn-del' onclick=\"event.stopPropagation();deleteProduct('{e(r['qr_code'], quote=True)}')\">DEL</button></td></tr>"
        for r in inventory
    ) or "<tr><td colspan='6' style='color:#555;text-align:center;padding:20px'>No products yet</td></tr>"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HanryxVault POS — Pi Dashboard</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d0d0d;color:#e0e0e0;padding:24px}}
  h1{{color:#FFD700;font-size:22px;margin-bottom:4px}}
  .subtitle{{color:#666;font-size:13px;margin-bottom:24px}}
  .cards{{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:28px}}
  .card{{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:10px;padding:18px 24px;min-width:160px;flex:1}}
  .card label{{color:#888;font-size:11px;letter-spacing:1px;text-transform:uppercase}}
  .card .value{{color:#FFD700;font-size:28px;font-weight:900;margin-top:4px}}
  .card .value.green{{color:#4caf50}}
  h2{{color:#aaa;font-size:13px;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:10px;margin-top:28px}}
  table{{width:100%;border-collapse:collapse;font-size:13px}}
  th{{text-align:left;color:#555;padding:8px 10px;border-bottom:1px solid #222}}
  td{{padding:8px 10px;border-bottom:1px solid #1a1a1a}}
  tr:hover td{{background:#1a1a1a}}
  a{{color:#FFD700;text-decoration:none}}
  a:hover{{text-decoration:underline}}
  .form-panel{{background:#111;border:1px solid #2a2a2a;border-radius:10px;padding:20px;margin-top:28px}}
  .form-panel h2{{margin-top:0;margin-bottom:16px}}
  .form-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px}}
  .form-grid input,.form-grid select{{width:100%;background:#1a1a1a;border:1px solid #333;border-radius:6px;color:#e0e0e0;padding:8px 10px;font-size:13px}}
  .form-grid input:focus,.form-grid select:focus{{outline:none;border-color:#FFD700}}
  .form-grid label{{display:block;color:#666;font-size:10px;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px}}
  .btn-gold{{background:#FFD700;color:#000;border:none;border-radius:6px;padding:10px 24px;font-weight:900;font-size:13px;cursor:pointer;letter-spacing:1px}}
  .btn-gold:hover{{background:#ffe033}}
  .btn-del{{background:none;border:1px solid #c62828;color:#c62828;border-radius:4px;padding:3px 8px;font-size:11px;cursor:pointer}}
  .btn-del:hover{{background:#c62828;color:#fff}}
  #toast{{position:fixed;bottom:24px;right:24px;background:#4caf50;color:#fff;padding:12px 20px;border-radius:8px;font-weight:bold;display:none;z-index:99}}
  #toast.err{{background:#c62828}}

  /* ── Price flash overlay ─────────────────────────────────────────────── */
  #price-flash{{
    position:fixed;inset:0;
    background:rgba(0,0,0,0.72);
    backdrop-filter:blur(3px);
    display:flex;flex-direction:column;
    align-items:center;justify-content:center;
    z-index:200;
    pointer-events:none;
    opacity:0;
    transition:opacity 0.18s ease;
  }}
  #price-flash.visible{{opacity:1;pointer-events:auto}}
  #pf-name{{
    color:rgba(255,255,255,0.92);
    font-size:clamp(22px,4vw,44px);
    font-weight:700;
    letter-spacing:0.5px;
    text-align:center;
    max-width:80vw;
    margin-bottom:8px;
    text-shadow:0 2px 12px rgba(0,0,0,0.8);
  }}
  #pf-meta{{
    color:rgba(180,180,180,0.8);
    font-size:clamp(13px,1.8vw,18px);
    letter-spacing:2px;
    text-transform:uppercase;
    margin-bottom:20px;
    text-align:center;
  }}
  #pf-price{{
    color:#FFD700;
    font-size:clamp(56px,12vw,130px);
    font-weight:900;
    letter-spacing:-2px;
    line-height:1;
    text-shadow:0 0 60px rgba(255,215,0,0.35);
    text-align:center;
  }}
  #pf-stock{{
    margin-top:18px;
    font-size:clamp(12px,1.5vw,16px);
    color:rgba(160,160,160,0.7);
    letter-spacing:1px;
  }}
  #pf-stock.low{{color:rgba(255,80,80,0.85)}}
  #pf-bar{{
    position:absolute;bottom:0;left:0;
    height:4px;background:#FFD700;
    width:100%;
    transform-origin:left;
    transform:scaleX(1);
  }}
  #pf-notfound{{
    color:rgba(255,255,255,0.45);
    font-size:clamp(18px,3vw,32px);
    font-weight:300;
    letter-spacing:3px;
    text-transform:uppercase;
  }}
</style>
</head>
<body>
<h1>HanryxVault POS</h1>
<div class="subtitle">Raspberry Pi Dashboard &nbsp;·&nbsp; <span id="clock"></span>
  &nbsp;·&nbsp; <a href="/admin/sales">Sales JSON</a>
  &nbsp;·&nbsp; <a href="/admin/inventory">Inventory JSON</a>
  &nbsp;·&nbsp; <a href="/zettle/status">Zettle Status</a>
  &nbsp;·&nbsp; <a href="/download/apk" style="background:#FFD700;color:#000;padding:3px 10px;border-radius:4px;font-weight:bold;">⬇ APK</a>
</div>

<div class="cards">
  <div class="card"><label>Today's Sales</label><div class="value green">{today_sales['count']}</div></div>
  <div class="card"><label>Revenue Today</label><div class="value">${today_sales['revenue']:.2f}</div></div>
  <div class="card"><label>Tax Collected</label><div class="value">${today_sales['tax']:.2f}</div></div>
  <div class="card"><label>Tips Today</label><div class="value">${today_sales['tips']:.2f}</div></div>
</div>

<div class="form-panel" style="border-color:#4caf50;background:#001a05">
  <h2 style="color:#4caf50">SYNC PRODUCTS FROM REPLIT SITES</h2>
  <p style="color:#aaa;font-size:13px;margin-bottom:12px">
    Pulls your full product catalogue from both Replit inventory websites.
    Use Force Re-Sync to refresh products that already exist.
  </p>
  <button class="btn-gold" onclick="syncCloud(false)" style="margin-right:12px">Sync New Products</button>
  <button class="btn-gold" style="background:#ff9800" onclick="syncCloud(true)">Force Re-Sync All</button>
  <div id="sync-result" style="margin-top:12px;font-size:13px;color:#aaa"></div>
</div>

<h2>Recent Sales</h2>
<table>
<thead><tr><th>Transaction</th><th>Total</th><th>Method</th><th>Employee</th><th>Time</th></tr></thead>
<tbody id="tbody-sales">{rows_recent or '<tr><td colspan="5" style="color:#555;text-align:center;padding:20px">No sales yet today</td></tr>'}</tbody>
</table>

<h2>Low Stock (≤5)</h2>
<table>
<thead><tr><th>Product</th><th>QR Code</th><th>Stock</th><th>Price</th><th>Category</th></tr></thead>
<tbody>{rows_low}</tbody>
</table>

<div class="form-panel">
  <h2>Add / Update Product</h2>
  <div style="display:flex;gap:10px;margin-bottom:14px;align-items:flex-end;flex-wrap:wrap">
    <div style="flex:1;min-width:180px">
      <label style="display:block;color:#666;font-size:10px;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px">QR Code / Set-Number *</label>
      <input id="f-qr" placeholder="SV1-1 or PRODUCT-001" style="width:100%;background:#1a1a1a;border:1px solid #333;border-radius:6px;color:#e0e0e0;padding:8px 10px;font-size:13px">
    </div>
    <button class="btn-gold" onclick="prefillFromTCG()" style="background:#6366f1;white-space:nowrap" title="Auto-fill name, rarity, set, image & market price from the TCG API">
      ⚡ Prefill from TCG API
    </button>
  </div>
  <div id="prefill-status" style="font-size:12px;color:#6366f1;margin-bottom:10px;display:none"></div>
  <div id="prefill-img" style="margin-bottom:14px;display:none">
    <img id="f-img-preview" src="" alt="card" style="height:120px;border-radius:8px;border:1px solid #333">
  </div>
  <div class="form-grid">
    <div><label>Name *</label><input id="f-name" placeholder="Charizard"></div>
    <div><label>Price</label><input id="f-price" type="number" step="0.01" placeholder="0.00"></div>
    <div><label>Category</label><input id="f-cat" placeholder="Trading Card"></div>
    <div><label>Rarity</label><input id="f-rarity" placeholder="Rare Holo"></div>
    <div><label>Set Code</label><input id="f-set" placeholder="SV1"></div>
    <div><label>Stock</label><input id="f-stock" type="number" placeholder="0"></div>
    <div><label>Description</label><input id="f-desc" placeholder="..."></div>
    <div><label>Image URL</label><input id="f-imgurl" placeholder="https://..."></div>
    <div><label>TCG ID</label><input id="f-tcgid" placeholder="sv1-1"></div>
  </div>
  <div style="margin-top:14px;display:flex;gap:10px;flex-wrap:wrap;align-items:center">
    <button class="btn-gold" onclick="addProduct()">Save Product</button>
    <div id="mkt-price-hint" style="font-size:12px;color:#888;display:none">
      TCG market: <span id="mkt-price-val" style="color:#FFD700;font-weight:bold"></span>
    </div>
  </div>
</div>

<h2>Full Inventory ({len(inventory)} products)</h2>
<table>
<thead><tr><th>Name</th><th>QR Code</th><th>Stock</th><th>Price</th><th>Category</th><th></th></tr></thead>
<tbody id="tbody-inv">{rows_inv}</tbody>
</table>

<div id="toast"></div>

<!-- ── Price flash overlay ──────────────────────────────────────────── -->
<div id="price-flash" onclick="pfDismiss()">
  <div id="pf-name"></div>
  <div id="pf-meta"></div>
  <div id="pf-price"></div>
  <div id="pf-stock"></div>
  <div id="pf-notfound" style="display:none"></div>
  <div id="pf-bar"></div>
</div>

<script>
/* ── Price flash overlay logic ──────────────────────────────────────── */
let _pfTimer    = null;
let _pfBarTimer = null;
const PF_DURATION = 4000;  // ms the overlay stays visible

function pfShow(data) {{
  const hasProduct = data && (data.name || (data.tcgData && data.tcgData.name));
  const name    = (data && (data.name || (data.tcgData && data.tcgData.name))) || "";
  const price   = data && (data.price || data.suggestedPrice || (data.tcgData && data.tcgData.tcgplayer && data.tcgData.tcgplayer.marketPrice));
  const rarity  = (data && (data.rarity || (data.tcgData && data.tcgData.rarity))) || "";
  const setName = (data && data.tcgData && data.tcgData.set && data.tcgData.set.name) || (data && data.setCode) || "";
  const stock   = data && data.stockQuantity != null ? data.stockQuantity : null;
  const isDup   = data && data.isDuplicate;

  const flash  = document.getElementById('price-flash');
  const pfName = document.getElementById('pf-name');
  const pfMeta = document.getElementById('pf-meta');
  const pfPric = document.getElementById('pf-price');
  const pfStk  = document.getElementById('pf-stock');
  const pfNF   = document.getElementById('pf-notfound');
  const pfBar  = document.getElementById('pf-bar');

  if (!hasProduct) {{
    pfName.textContent = '';
    pfMeta.textContent = '';
    pfPric.textContent = '';
    pfStk.textContent  = '';
    pfNF.style.display = 'block';
    pfNF.textContent   = 'Card not found in inventory';
  }} else {{
    pfNF.style.display = 'none';
    pfName.textContent = name;
    const metaParts = [];
    if (rarity)  metaParts.push(rarity);
    if (setName) metaParts.push(setName);
    pfMeta.textContent = metaParts.join(' · ');
    pfPric.textContent = price != null ? '$' + Number(price).toFixed(2) : '—';
    if (isDup && stock != null) {{
      pfStk.textContent  = stock + ' in stock  ·  DUPLICATE SCAN';
      pfStk.className    = 'low';
    }} else if (stock != null && stock <= 3) {{
      pfStk.textContent  = stock === 0 ? 'OUT OF STOCK' : stock + ' remaining';
      pfStk.className    = stock === 0 ? 'low' : '';
    }} else {{
      pfStk.textContent  = '';
      pfStk.className    = '';
    }}
  }}

  // Show overlay
  clearTimeout(_pfTimer);
  pfBar.style.transition = 'none';
  pfBar.style.transform  = 'scaleX(1)';
  flash.classList.add('visible');

  // Animate the progress bar shrinking over PF_DURATION ms
  requestAnimationFrame(() => {{
    requestAnimationFrame(() => {{
      pfBar.style.transition = `transform ${{PF_DURATION}}ms linear`;
      pfBar.style.transform  = 'scaleX(0)';
    }});
  }});

  _pfTimer = setTimeout(pfDismiss, PF_DURATION);
}}

function pfDismiss() {{
  clearTimeout(_pfTimer);
  document.getElementById('price-flash').classList.remove('visible');
}}

/* ── SSE scan stream → price flash ─────────────────────────────────── */
function connectScanStream() {{
  const es = new EventSource('/scan/stream');
  es.onmessage = async (evt) => {{
    try {{
      const {{qrCode}} = JSON.parse(evt.data);
      if (!qrCode) return;
      // Fetch enriched data from the Pi
      const resp = await fetch('/card/enrich?' + new URLSearchParams({{qr: qrCode}}));
      if (resp.ok) {{
        const data = await resp.json();
        pfShow(data);
      }} else {{
        pfShow(null);
      }}
    }} catch(e) {{ pfShow(null); }}
  }};
  es.onerror = () => {{
    es.close();
    setTimeout(connectScanStream, 5000);  // reconnect after 5 s
  }};
}}
connectScanStream();

function toast(msg, err=false) {{
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = err ? 'err' : '';
  t.style.display='block';
  setTimeout(()=>t.style.display='none', 3500);
}}

function clock() {{
  document.getElementById('clock').textContent = new Date().toLocaleTimeString();
}}
setInterval(clock, 1000); clock();

async function prefillFromTCG() {{
  const qr = document.getElementById('f-qr').value.trim();
  if (!qr) {{ toast('Enter a QR code or Set-Number first (e.g. SV1-1)', true); return; }}
  const statusEl = document.getElementById('prefill-status');
  statusEl.textContent = 'Looking up TCG data…'; statusEl.style.display = 'block';
  try {{
    const r = await fetch('/card/enrich?' + new URLSearchParams({{qr}}));
    const d = await r.json();
    const t = d.tcgData || {{}};
    if (d.name)    document.getElementById('f-name').value   = d.name;
    if (t.rarity || d.rarity)
      document.getElementById('f-rarity').value = t.rarity || d.rarity;
    if (t.set && t.set.ptcgoCode)
      document.getElementById('f-set').value = t.set.ptcgoCode;
    if (d.imageUrl) {{
      document.getElementById('f-imgurl').value = d.imageUrl;
      document.getElementById('f-img-preview').src = d.imageUrl;
      document.getElementById('prefill-img').style.display = 'block';
    }}
    if (t.tcgId || d.tcgId)
      document.getElementById('f-tcgid').value = t.tcgId || d.tcgId || '';
    const mkt = t.tcgplayer && t.tcgplayer.marketPrice;
    if (mkt) {{
      document.getElementById('mkt-price-hint').style.display = 'flex';
      document.getElementById('mkt-price-val').textContent = '$' + mkt.toFixed(2);
      if (!document.getElementById('f-price').value)
        document.getElementById('f-price').value = mkt.toFixed(2);
    }}
    if (d.isDuplicate) {{
      statusEl.textContent = 'Already in stock (' + (d.stockQuantity||0) + ' units) — updating.';
      statusEl.style.color = '#ff9800';
    }} else if (d.inLocalInventory) {{
      statusEl.textContent = 'Found in local inventory — fields pre-filled.';
      statusEl.style.color = '#4caf50';
    }} else if (t.name) {{
      statusEl.textContent = 'Fetched from TCG API — verify price before saving.';
      statusEl.style.color = '#6366f1';
    }} else {{
      statusEl.textContent = 'Card not found in TCG API — fill in manually.';
      statusEl.style.color = '#f59e0b';
    }}
  }} catch(e) {{
    statusEl.textContent = 'TCG lookup failed: ' + e.message;
    statusEl.style.color = '#f44336';
  }}
}}

async function addProduct() {{
  const body = {{
    qrCode:      document.getElementById('f-qr').value.trim(),
    name:        document.getElementById('f-name').value.trim(),
    price:       parseFloat(document.getElementById('f-price').value) || 0,
    category:    document.getElementById('f-cat').value.trim() || 'General',
    rarity:      document.getElementById('f-rarity').value.trim(),
    setCode:     document.getElementById('f-set').value.trim(),
    stock:       parseInt(document.getElementById('f-stock').value) || 0,
    description: document.getElementById('f-desc').value.trim(),
    imageUrl:    document.getElementById('f-imgurl').value.trim(),
    tcgId:       document.getElementById('f-tcgid').value.trim(),
  }};
  if (!body.qrCode || !body.name) {{ toast('QR Code and Name are required', true); return; }}
  const r = await fetch('/admin/inventory', {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}});
  if (r.ok) {{ toast('Product saved!'); setTimeout(()=>location.reload(), 1000); }}
  else toast('Error saving product', true);
}}

async function deleteProduct(qr) {{
  if (!confirm('Delete ' + qr + '?')) return;
  const r = await fetch('/admin/inventory/' + encodeURIComponent(qr), {{method:'DELETE'}});
  if (r.ok) {{ toast('Deleted'); setTimeout(()=>location.reload(), 800); }}
  else toast('Error deleting', true);
}}

async function syncCloud(force) {{
  document.getElementById('sync-result').textContent = 'Syncing…';
  const r = await fetch('/admin/sync-from-cloud?force=' + (force?'1':'0'), {{method:'POST'}});
  const d = await r.json();
  document.getElementById('sync-result').textContent =
    d.skipped ? `Skipped — already have ${{d.existing}} products (use Force Re-Sync)` :
    `Done — upserted: ${{d.upserted}}, skipped rows: ${{d.skipped}}`;
}}
</script>
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    _load_tokens_from_db()
    threading.Thread(target=sync_inventory_from_cloud, daemon=True).start()
    _cleanup_scan_queue()
    _wal_checkpoint()
    print("[server] Starting HanryxVault POS on http://0.0.0.0:8080")
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
