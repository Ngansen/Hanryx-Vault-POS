"""
HanryxVault POS — Raspberry Pi Backend Server
Runs behind nginx on port 8080 (nginx handles 80/443 → 8080).

Performance improvements over original:
  - Gunicorn WSGI server (multi-worker, replaces Flask dev server)
  - PostgreSQL database via psycopg2 connection pool
  - Connection pool shared across gunicorn workers (ThreadedConnectionPool)
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

QR code generation:
  GET  /admin/qr/<qr_code>  — PNG QR image for a single card (no auth, cacheable)
  GET  /admin/qr-sheet      — print-ready page of all in-stock card labels
  GET  /admin/qr-sheet?q=charizard  — filter by name
  GET  /admin/qr-sheet?cat=Singles  — filter by category
  GET  /admin/qr-sheet?zero=1       — include out-of-stock items
  GET  /admin/qr-sheet?cols=3       — labels per row (2–5, default 4)

Run manually (dev):
  python3 server.py

Run via gunicorn (production — handled by systemd):
  gunicorn -w 4 -b 127.0.0.1:8080 --timeout 60 server:app
"""

import psycopg2
import psycopg2.extras
import psycopg2.pool
import json
try:
    import orjson as _orjson
    def _orjson_dumps(obj, **kw):
        opts = 0
        if kw.get("sort_keys"): opts |= _orjson.OPT_SORT_KEYS
        if kw.get("indent"):    opts |= _orjson.OPT_INDENT_2
        return _orjson.dumps(obj, option=opts or None).decode()
    json.loads = _orjson.loads
    json.dumps = _orjson_dumps
    _ORJSON = True
except ImportError:
    _ORJSON = False
import datetime
import functools
import hashlib
import html as _html
import logging
import os
import re
import subprocess
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor as _TPE
import urllib.parse
import base64
import csv
import io
import requests as _requests
import redis as _redis_mod
try:
    from bs4 import BeautifulSoup as _BS4
    _BS4_OK = True
except ImportError:
    _BS4_OK = False
try:
    from deep_translator import GoogleTranslator as _GoogleTranslator
    _TRANSLATE_OK = True
except ImportError:
    _TRANSLATE_OK = False
import qrcode as _qrcode
import qrcode.constants as _qrcode_const
from flask import Flask, request, jsonify, redirect, g, session, render_template_string, Response, send_file
from flask_compress import Compress
from cachetools import TTLCache

# ---------------------------------------------------------------------------
# Structured JSON logging — every line is machine-parseable
# ---------------------------------------------------------------------------
import uuid as _uuid_mod

class _JsonFormatter(logging.Formatter):
    """Emit log records as JSON lines for easy ingestion / grep."""
    def format(self, record: logging.LogRecord) -> str:
        doc = {
            "ts":      self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level":   record.levelname,
            "logger":  record.name,
            "msg":     record.getMessage(),
        }
        # Inject Flask request context when available
        try:
            from flask import has_request_context, g, request as _req
            if has_request_context():
                doc["request_id"] = getattr(g, "request_id", "")
                doc["method"]     = _req.method
                doc["path"]       = _req.path
                doc["ip"]         = _req.remote_addr
        except Exception:
            pass
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc, ensure_ascii=False)

_json_handler = logging.StreamHandler()
_json_handler.setFormatter(_JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_json_handler])
log = logging.getLogger("hanryxvault")

# ---------------------------------------------------------------------------
# Sentry error tracking — enabled if SENTRY_DSN env var is set
# ---------------------------------------------------------------------------
_SENTRY_DSN = os.environ.get("SENTRY_DSN", "").strip()
if _SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            integrations=[FlaskIntegration()],
            traces_sample_rate=0.05,   # 5 % of requests traced
            send_default_pii=False,
        )
        log.info("[sentry] Error tracking enabled")
    except ImportError:
        log.warning("[sentry] sentry-sdk not installed — error tracking disabled")

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
    resp = _requests.post(
        f"{ZETTLE_OAUTH_BASE}/token",
        data=form_data,
        headers={
            "Authorization": f"Basic {_basic_auth()}",
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _persist_tokens():
    """Write current token state to DB so it survives server restarts."""
    try:
        db = _direct_db()
        with _token_lock:
            payload = json.dumps(_zettle_state)
        db.execute(
            "INSERT INTO server_state (key, value) VALUES ('zettle_tokens', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (payload,)
        )
        db.commit()
        db.close()
    except Exception as e:
        log.error("[zettle] Token persist failed: %s", e)


def _load_tokens_from_db():
    """Restore persisted Zettle tokens from DB on startup."""
    try:
        db = _direct_db()
        row = db.execute(
            "SELECT value FROM server_state WHERE key='zettle_tokens'"
        ).fetchone()
        db.close()
        if row:
            saved = json.loads(row[0])
            with _token_lock:
                _zettle_state.update(saved)
            if _zettle_state.get("access_token"):
                log.info("[zettle] Restored tokens from DB — no re-auth needed")
    except Exception as e:
        log.warning("[zettle] Token restore failed (first run?): %s", e)


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
        log.error("[zettle] Token refresh failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
Compress(app)  # gzip all responses automatically

# Session secret — required for admin login cookies and Zettle CSRF state
app.secret_key = os.environ.get("SESSION_SECRET") or os.environ.get(
    "SECRET_KEY", "dev-secret-change-me-in-production"
)
app.config["SESSION_COOKIE_HTTPONLY"]  = True
app.config["SESSION_COOKIE_SAMESITE"]  = "Lax"
app.config["SESSION_COOKIE_SECURE"]    = os.environ.get("HTTPS_ONLY", "0") == "1"

# Admin panel password — set ADMIN_PASSWORD env var on the Pi
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "hanryxvault")

# ---------------------------------------------------------------------------
# Enterprise: JWT + TOTP imports (graceful degradation if not installed)
# ---------------------------------------------------------------------------
try:
    import jwt as _jwt
    _JWT_AVAILABLE = True
except ImportError:
    _JWT_AVAILABLE = False

try:
    import pyotp as _pyotp
    _PYOTP_AVAILABLE = True
except ImportError:
    _PYOTP_AVAILABLE = False

_JWT_SECRET = os.environ.get("JWT_SECRET", app.secret_key)
_JWT_ALGO   = "HS256"
_JWT_TTL_H  = int(os.environ.get("JWT_TTL_HOURS", "24"))

# LAN CIDR allowlist for open (no-auth) APK endpoints.
# Default: accept any RFC-1918 address. Override via LAN_CIDRS env var.
# e.g. LAN_CIDRS=192.168.1.0/24,10.0.0.0/8
import ipaddress as _ipaddress
_LAN_CIDRS_RAW = os.environ.get("LAN_CIDRS", "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8")
_LAN_NETWORKS  = [_ipaddress.ip_network(c.strip()) for c in _LAN_CIDRS_RAW.split(",") if c.strip()]


def _is_lan(ip: str) -> bool:
    try:
        addr = _ipaddress.ip_address(ip)
        return any(addr in net for net in _LAN_NETWORKS)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Enterprise: Request ID + security headers middleware
# ---------------------------------------------------------------------------

@app.before_request
def _inject_request_id():
    g.request_id = request.headers.get("X-Request-ID") or _uuid_mod.uuid4().hex[:12]
    g.admin_user  = session.get("admin_user", "anonymous")


@app.after_request
def _add_security_headers(resp):
    resp.headers["X-Request-ID"]          = getattr(g, "request_id", "")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"]        = "SAMEORIGIN"
    resp.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
    # CSP: restrictive for API, relaxed for admin HTML pages
    if resp.content_type and "text/html" in resp.content_type:
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data: https:;"
        )
    if os.environ.get("HTTPS_ONLY") == "1":
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp


# ---------------------------------------------------------------------------
# Enterprise: JWT token auth decorator for APK / Expo endpoints
# ---------------------------------------------------------------------------

def _verify_jwt(token: str) -> dict | None:
    """Return decoded payload or None on failure."""
    if not _JWT_AVAILABLE:
        return None
    try:
        return _jwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALGO])
    except Exception:
        return None


def require_api_token(fn):
    """
    Allow request if ANY of:
      1. Valid Bearer JWT in Authorization header
      2. Valid ?token= query param
      3. Source IP is on the configured LAN subnet (local hardware)
    Returns 401 JSON otherwise.
    """
    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        # LAN bypass — local Pi hardware always trusted
        if _is_lan(request.remote_addr):
            return fn(*args, **kwargs)
        # JWT check
        auth = request.headers.get("Authorization", "")
        token = ""
        if auth.startswith("Bearer "):
            token = auth[7:].strip()
        if not token:
            token = request.args.get("token", "") or (request.get_json(silent=True) or {}).get("token", "")
        if token and _verify_jwt(token):
            return fn(*args, **kwargs)
        return jsonify({"error": "Unauthorized — provide a valid Bearer token or connect from LAN"}), 401
    return _wrapped


# ---------------------------------------------------------------------------
# Enterprise: Audit log writer + decorator
# ---------------------------------------------------------------------------

def _audit_write(action: str, resource: str = "", detail: str = ""):
    """Write one row to audit_log. Captures request context immediately, writes in background."""
    try:
        actor  = session.get("admin_user", "anonymous")
        ip     = request.remote_addr
        req_id = getattr(g, "request_id", "")
    except Exception:
        actor = ip = req_id = ""

    def _do_safe():
        try:
            db = _direct_db()
            db.execute(
                "INSERT INTO audit_log (action, actor, resource, detail, ip, request_id) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (action, actor, resource, detail[:2000], ip, req_id),
            )
            db.commit()
            db.close()
        except Exception as _e:
            log.debug("[audit] write failed: %s", _e)

    _bg(_do_safe)


def audit_action(action: str, resource_fn=None):
    """
    Decorator.  Writes an audit_log row after a successful (non-4xx/5xx) call.

    Usage:
      @audit_action("inventory.delete", resource_fn=lambda: request.view_args.get("qr_code"))
      def delete_item(qr_code): ...
    """
    def _decorator(fn):
        @functools.wraps(fn)
        def _wrapped(*args, **kwargs):
            result = fn(*args, **kwargs)
            try:
                status = result[1] if isinstance(result, tuple) else 200
                if status < 400:
                    resource = resource_fn() if resource_fn else ""
                    _audit_write(action, str(resource))
            except Exception:
                pass
            return result
        return _wrapped
    return _decorator

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://vaultpos:vaultpos@localhost:5432/vaultpos",
)

# ---------------------------------------------------------------------------
# PostgreSQL connection pool + SQLite-compatible wrapper
# ---------------------------------------------------------------------------

_pg_pool: "psycopg2.pool.ThreadedConnectionPool | None" = None


def _get_pool() -> "psycopg2.pool.ThreadedConnectionPool":
    global _pg_pool
    if _pg_pool is None:
        _pg_pool = psycopg2.pool.ThreadedConnectionPool(1, 10, dsn=DATABASE_URL)
    return _pg_pool


class _PgConn:
    """
    Wraps a raw psycopg2 connection to mimic the sqlite3 connection API so
    that all existing db.execute() / db.commit() call-sites stay unchanged.
    • Converts SQLite '?' placeholders → psycopg2 '%s' automatically.
    • Returns DictCursor rows that support both r["col"] and r[0] access.
    """

    def __init__(self, raw_conn):
        self._conn = raw_conn

    def execute(self, sql: str, params=()):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(sql.replace("?", "%s"), params or ())
        return cur

    def executemany(self, sql: str, params_list):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.executemany(sql.replace("?", "%s"), params_list)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        try:
            self._conn.commit()
        except Exception:
            pass
        _get_pool().putconn(self._conn)

    # Compatibility no-ops for code that sets conn.row_factory = sqlite3.Row
    @property
    def row_factory(self):
        return None

    @row_factory.setter
    def row_factory(self, _):
        pass


def _direct_db() -> _PgConn:
    """Open a pooled connection for use outside a Flask request context."""
    return _PgConn(_get_pool().getconn())

# ---------------------------------------------------------------------------
# In-memory caches — dramatically reduces SQLite hits on hot endpoints
# ---------------------------------------------------------------------------
_cache_lock      = threading.Lock()
_inventory_cache = TTLCache(maxsize=1,   ttl=30)   # /inventory — 30 s TTL
_scan_cache      = TTLCache(maxsize=1,   ttl=1)    # /scan/pending — 1 s TTL
_health_cache    = TTLCache(maxsize=1,   ttl=5)    # /health — 5 s TTL
_qr_scan_cache   = TTLCache(maxsize=500, ttl=300)  # /card/scan — 5 min per QR code
_cache_stats     = {"inventory_hits": 0, "inventory_misses": 0,
                    "scan_hits": 0,      "scan_misses": 0}

# ---------------------------------------------------------------------------
# Redis — lazy singleton, cross-worker pub/sub + L2 cache
# ---------------------------------------------------------------------------
_redis_client_obj  = None
_redis_client_lock = threading.Lock()
_REDIS_SCAN_CHAN   = "hanryx:scans"
_REDIS_INV_KEY     = "hv:inv:all"
_REDIS_QR_PREFIX   = "hv:qr:"


def _redis():
    """Return a live redis.Redis client, or None if Redis is not reachable."""
    global _redis_client_obj
    if _redis_client_obj is not None:
        return _redis_client_obj
    with _redis_client_lock:
        if _redis_client_obj is not None:
            return _redis_client_obj
        url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
        try:
            c = _redis_mod.Redis.from_url(
                url,
                socket_connect_timeout=2,
                socket_timeout=2,
                decode_responses=False,
            )
            c.ping()
            _redis_client_obj = c
            log.info("[redis] Connected to %s", url)
        except Exception as _e:
            log.warning("[redis] Not available (%s) — SSE/cache will use in-process fallback", _e)
        return _redis_client_obj


def _rcache_get(key: str):
    """Get a JSON-encoded value from Redis. Returns None on miss or error."""
    try:
        r = _redis()
        if not r:
            return None
        raw = r.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _rcache_set(key: str, value, ttl: int = 30):
    """Store a JSON-encoded value in Redis with a TTL (seconds)."""
    try:
        r = _redis()
        if r:
            r.set(key, json.dumps(value), ex=ttl)
    except Exception:
        pass


def _rcache_del(*keys: str):
    """Delete one or more keys from Redis."""
    try:
        r = _redis()
        if r and keys:
            r.delete(*keys)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Worker thread pool — replaces ad-hoc daemon threads for fire-and-forget work
# ---------------------------------------------------------------------------
_worker_pool = _TPE(max_workers=8, thread_name_prefix="hvault-worker")

def _bg(fn, *args, **kwargs):
    """Submit a fire-and-forget task to the shared worker pool."""
    try:
        _worker_pool.submit(fn, *args, **kwargs)
    except Exception as _e:
        log.warning("[worker] submit failed for %s: %s", getattr(fn, "__name__", fn), _e)

def _queue_unsynced(qr_code: str, change_type: str = "stock", delta: int = 0):
    """
    Record a stock/price change that has not yet been pushed to the storefront.
    Runs in the background so it never blocks the request path.
    change_type: 'stock' | 'price' | 'new' | 'delete'
    """
    def _write():
        try:
            db = _direct_db()
            db.execute(
                "INSERT INTO unsynced_changes (qr_code, change_type, delta) VALUES (%s, %s, %s)",
                (qr_code, change_type, delta),
            )
            db.commit()
            db.close()
        except Exception as _e:
            log.debug("[unsynced] write failed: %s", _e)
    _bg(_write)

# ---------------------------------------------------------------------------
# HTTP retry wrapper — automatic exponential back-off on transient failures
# ---------------------------------------------------------------------------
def _http_get(url, *, retries=3, backoff=0.5, timeout=10, headers=None, **kwargs):
    """GET with retries on network / timeout errors. Raises on final failure."""
    last_exc = None
    for attempt in range(retries):
        try:
            r = _requests.get(url, timeout=timeout, headers=headers or {}, **kwargs)
            r.raise_for_status()
            return r
        except (_requests.Timeout, _requests.ConnectionError) as exc:
            last_exc = exc
            if attempt < retries - 1:
                _time.sleep(backoff * (2 ** attempt))
        except _requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code < 500:
                raise
            last_exc = exc
            if attempt < retries - 1:
                _time.sleep(backoff * (2 ** attempt))
    raise last_exc

def _http_post(url, *, retries=2, backoff=0.5, timeout=10, headers=None,
               json_body=None, data=None, **kwargs):
    """POST with retries on network / timeout errors. Raises on final failure."""
    last_exc = None
    for attempt in range(retries):
        try:
            r = _requests.post(url, timeout=timeout, headers=headers or {},
                               json=json_body, data=data, **kwargs)
            r.raise_for_status()
            return r
        except (_requests.Timeout, _requests.ConnectionError) as exc:
            last_exc = exc
            if attempt < retries - 1:
                _time.sleep(backoff * (2 ** attempt))
        except _requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code < 500:
                raise
            last_exc = exc
            if attempt < retries - 1:
                _time.sleep(backoff * (2 ** attempt))
    raise last_exc

# ---------------------------------------------------------------------------
# Smart Scan Engine — in-memory rapidfuzz index + learning cache
# ---------------------------------------------------------------------------
try:
    from rapidfuzz import fuzz as _fuzz, process as _rfprocess
    _RAPIDFUZZ_AVAILABLE = True
except ImportError:
    _RAPIDFUZZ_AVAILABLE = False
    log.warning("[smart-scan] rapidfuzz not installed — fuzzy matching disabled (pip install rapidfuzz)")


def _smart_normalize(text: str) -> str:
    """Strip everything except lowercase letters and digits for index comparison."""
    return re.sub(r'[^a-z0-9]', '', text.lower())


def _detect_variant(name: str, rarity: str, description: str) -> str:
    """
    Infer card variant from metadata fields.
    Priority: 1st Ed > Reverse Holo > Rainbow > Secret > Gold > Full Art >
              VSTAR > VMAX > V > GX > EX > Holo > Promo > ""
    """
    combined = f"{name} {rarity} {description}".lower()
    name_lc  = name.lower()
    rar_lc   = rarity.lower()
    if "1st edition" in combined or "first edition" in combined:
        return "1st Edition"
    if "reverse holo" in combined or "reverse" in rar_lc:
        return "Reverse Holo"
    if "rainbow" in combined:
        return "Rainbow Rare"
    if "secret" in rar_lc:
        return "Secret Rare"
    if "gold" in rar_lc:
        return "Gold"
    if "full art" in combined:
        return "Full Art"
    if "vstar" in name_lc:
        return "VSTAR"
    if "vmax" in name_lc:
        return "VMAX"
    if re.search(r'\bv\b', name_lc):
        return "V"
    if "gx" in name_lc:
        return "GX"
    if re.search(r'\bex\b', name_lc):
        return "EX"
    if "holo" in combined:
        return "Holo"
    if "promo" in combined:
        return "Promo"
    return ""


_SET_YEAR_MAP: dict[str, int] = {
    "SV":   2023,  # Scarlet & Violet
    "SWSH": 2020,  # Sword & Shield
    "SM":   2017,  # Sun & Moon
    "XY":   2014,  # XY era
    "BW":   2011,  # Black & White
    "HGSS": 2010,  # HeartGold SoulSilver
    "PL":   2009,  # Platinum
    "DP":   2007,  # Diamond & Pearl
    "EX":   2003,  # EX era (vintage)
    "BASE": 1999,  # Base Set
    "GY":   1999,  # Gym Heroes/Challenge
    "TR":   2000,  # Team Rocket
    "N1":   2000,  # Neo Genesis
    "N2":   2000,  # Neo Discovery
    "N4":   2001,  # Neo Destiny
}


def _set_year_from_code(set_code: str) -> int:
    """
    Return the approximate release year for a known Pokémon set-code prefix.
    Checks longest prefix first so 'SWSH' beats 'SW'.
    Returns 0 if unknown.
    """
    sc = set_code.upper()
    for prefix in sorted(_SET_YEAR_MAP, key=len, reverse=True):
        if sc.startswith(prefix):
            return _SET_YEAR_MAP[prefix]
    # Heuristic: bare numeric suffix might encode year (e.g. BW01→2011)
    m = re.match(r'[A-Z]+(\d{2})', sc)
    if m:
        yr2 = int(m.group(1))
        if 99 <= yr2 <= 99:   return 1900 + yr2  # 99 = 1999
        if 0  <= yr2 <= 30:   return 2000 + yr2
    return 0


def _parse_release_year(release_date: str | None) -> int:
    """
    Parse a TCG API releaseDate string ('YYYY/MM/DD' or 'YYYY-MM-DD') into a year int.
    Returns 0 on failure.
    """
    if not release_date:
        return 0
    m = re.match(r'(\d{4})', str(release_date))
    return int(m.group(1)) if m else 0


def _extract_card_number(qr_code: str, name: str = "") -> str:
    """
    Extract the numeric card number from a QR/barcode string.
    Examples:
      'SV1-025'    → '25'
      'SV1EN-025'  → '25'
      'SWSH01-001' → '1'
    Falls back to parsing 'NNN/TTT' patterns from the card name.
    Returns empty string if nothing can be extracted.
    """
    # Primary: SET-NUM format in qr_code (e.g. 'SV1-025', 'SWSH01-001')
    m = re.search(r'[A-Za-z]{2,8}[-/]0*(\d{1,4}[a-zA-Z]?)', qr_code)
    if m:
        return m.group(1).lstrip("0") or "0"
    # Fallback: 'NNN/TTT' in card name (e.g. '025/165')
    m = re.search(r'\b0*(\d{1,4})/\d+\b', name)
    if m:
        return m.group(1).lstrip("0") or "0"
    return ""


class _SmartScanner:
    """
    In-memory scan resolution pipeline.  Runs on each worker process independently.

    Pipeline priority (returns on first confident match):
      1. Learning cache — previous confirmed mappings, instant
      2. Exact in-memory qr_code match
      3. Set-code + card-number parse and match
      4. rapidfuzz WRatio name match (threshold 70 / 100)
      5. FAIL — returns top-3 suggestions

    Invalidated whenever inventory changes (_invalidate_inventory).
    Loaded lazily on first smart_scan() call after invalidation.
    """

    FUZZY_THRESHOLD  = 70    # minimum rapidfuzz WRatio score (0–100)
    MAX_INDEX_SIZE   = 10_000  # safety cap for very large catalogues

    def __init__(self):
        self._lock         = threading.Lock()
        self._loaded       = False
        self._index: list  = []        # list of item dicts (all inventory rows)
        self._names: list  = []        # parallel list of card names for rapidfuzz
        self._qr_map: dict = {}        # qr_code → item dict (fast exact lookup)
        self._learn: dict  = {}        # raw_qr → qr_code (scan learning cache)

    # ── Public interface ──────────────────────────────────────────────────────

    def invalidate(self):
        with self._lock:
            self._loaded = False
            self._index.clear()
            self._names.clear()
            self._qr_map.clear()
            # Intentionally preserve _learn — learned mappings survive invalidations

    def learn(self, raw_qr: str, qr_code: str):
        """Record that raw_qr resolved to qr_code so future scans are instant."""
        if raw_qr and qr_code and raw_qr != qr_code:
            with self._lock:
                self._learn[raw_qr] = qr_code

    def smart_scan(self, qr: str, db) -> dict:
        """
        Run the full resolution pipeline.  Returns:
          {
            "found":      bool,
            "confidence": float (0.0–1.0),
            "method":     str,
            "variant":    str,
            "item":       dict | None,    # inventory row as dict (if found)
            "suggestions": list           # top-3 name matches (if not found)
          }
        """
        if not _RAPIDFUZZ_AVAILABLE:
            return self._not_found(qr, [])

        self._ensure_loaded(db)
        qr = qr.strip()

        # 1 — Learning cache (fastest — previous confirmed mapping)
        result = self._check_learning(qr)
        if result:
            return result

        # 2 — Exact in-memory qr_code match
        result = self._exact_match(qr)
        if result:
            return result

        # 3 — Set + number parse
        result = self._set_number_match(qr)
        if result:
            return result

        # 4 — Fuzzy name match (rapidfuzz WRatio)
        result = self._fuzzy_match(qr)
        if result:
            return result

        # 5 — Failed — return top-3 suggestions
        return self._not_found(qr, self._get_suggestions(qr))

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _ensure_loaded(self, db):
        with self._lock:
            if self._loaded:
                return
        try:
            rows = db.execute(
                "SELECT qr_code, name, price, category, rarity, set_code, "
                "description, stock, image_url, tcg_id, condition, item_type, "
                "card_number, variant "
                "FROM inventory ORDER BY name ASC LIMIT %s",
                (self.MAX_INDEX_SIZE,)
            ).fetchall()
        except Exception as e:
            log.warning("[smart-scan] Could not load index: %s", e)
            return

        items = []
        names = []
        qr_map = {}
        for r in rows:
            item = dict(r)
            item["_norm_name"] = _smart_normalize(item.get("name") or "")
            # Use persisted variant; fall back to runtime detection for legacy rows
            if not item.get("variant"):
                item["variant"] = _detect_variant(
                    item.get("name") or "",
                    item.get("rarity") or "",
                    item.get("description") or "",
                )
            item["_variant"] = item["variant"]
            items.append(item)
            names.append(item.get("name") or "")
            qr_map[item["qr_code"]] = item

        with self._lock:
            self._index  = items
            self._names  = names
            self._qr_map = qr_map
            self._loaded = True
        log.info("[smart-scan] Index loaded: %d cards", len(items))

    def _build_result(self, item: dict, confidence: float, method: str) -> dict:
        return {
            "found":       True,
            "confidence":  round(confidence, 3),
            "method":      method,
            "variant":     item.get("_variant", ""),
            "item":        {k: v for k, v in item.items() if not k.startswith("_")},
            "suggestions": [],
        }

    def _not_found(self, qr: str, suggestions: list) -> dict:
        return {
            "found":       False,
            "confidence":  0.0,
            "method":      "none",
            "variant":     "",
            "item":        None,
            "suggestions": suggestions,
        }

    def _check_learning(self, qr: str) -> dict | None:
        with self._lock:
            qr_code = self._learn.get(qr)
            if not qr_code:
                return None
            item = self._qr_map.get(qr_code)
        if item:
            return self._build_result(item, 0.99, "learned")
        return None

    def _exact_match(self, qr: str) -> dict | None:
        with self._lock:
            item = self._qr_map.get(qr)
        if item:
            return self._build_result(item, 1.0, "exact")
        return None

    def _set_number_match(self, qr: str) -> dict | None:
        """Parse SET-NUM patterns like SV1-001 and match by set_code + number."""
        m = re.search(r'\b([A-Za-z]{2,8})[- ]?0*(\d{1,4}[a-zA-Z]?)\b', qr)
        if not m:
            return None
        set_code = m.group(1).upper()
        number   = m.group(2).lstrip("0") or "0"
        with self._lock:
            for item in self._index:
                if (item.get("set_code") or "").upper() == set_code:
                    qr_tail = (item.get("qr_code") or "").upper()
                    if qr_tail.endswith(f"-{number}") or qr_tail.endswith(number.zfill(3)):
                        return self._build_result(item, 0.95, "set_number")
        return None

    def _fuzzy_match(self, qr: str) -> dict | None:
        """Fuzzy name match using rapidfuzz WRatio (handles typos, partial names)."""
        with self._lock:
            names = list(self._names)
            index = list(self._index)
        if not names:
            return None

        result = _rfprocess.extractOne(
            qr,
            names,
            scorer=_fuzz.WRatio,
            score_cutoff=self.FUZZY_THRESHOLD,
        )
        if not result:
            return None

        matched_name, score, idx = result
        item = index[idx]
        confidence = score / 100.0
        method = "fuzzy_name" if confidence >= 0.85 else "fuzzy_low"
        return self._build_result(item, confidence, method)

    def _get_suggestions(self, qr: str) -> list:
        """Return top-3 fuzzy name suggestions when no match is confident enough."""
        with self._lock:
            names = list(self._names)
            index = list(self._index)
        if not names:
            return []
        results = _rfprocess.extract(qr, names, scorer=_fuzz.WRatio, limit=3)
        suggestions = []
        for name, score, idx in results:
            item = index[idx]
            suggestions.append({
                "name":       name,
                "qrCode":     item.get("qr_code"),
                "score":      round(score / 100.0, 3),
                "price":      item.get("price"),
                "rarity":     item.get("rarity") or "",
                "variant":    item.get("_variant", ""),
                "imageUrl":   item.get("image_url") or "",
            })
        return suggestions


_smart_scanner = _SmartScanner()


# ---------------------------------------------------------------------------
# Pokémon TCG API — config + in-memory cache
# ---------------------------------------------------------------------------
_TCG_API_BASE   = "https://api.pokemontcg.io/v2"
_PTCG_API_KEY   = os.environ.get("PTCG_API_KEY", "")  # optional; free tier = 1k/day, with key = 20k/day
_tcg_cache_lock = threading.Lock()

# OpenAI — card photo identification via GPT-4o Vision
_OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
_tcg_mem_cache: dict = {}    # card_id → {"data": {...}, "fetched_ms": int}
_TCG_MEM_TTL_MS = 3_600_000  # 1 hour in-memory; DB stores 24 hours

# ---------------------------------------------------------------------------
# Local card image cache — images are downloaded on first scan and served
# directly from the Pi, so they work offline and load faster on the LAN.
# ---------------------------------------------------------------------------
_CARD_IMAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "card-images")
os.makedirs(_CARD_IMAGES_DIR, exist_ok=True)

# Local SQLite TCG card database built by import_tcg_db.py.
# Queried before the live API so enrichment works fully offline.
_LOCAL_TCG_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pokedex_local.db")

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
    """
    Push a new scan to every connected SSE client.
    Primary path: Redis pub/sub (cross-worker, all gunicorn processes receive it).
    Fallback: in-process queue list (single worker, no Redis).
    """
    # Redis broadcast — reaches SSE clients on *all* workers
    try:
        r = _redis()
        if r:
            r.publish(_REDIS_SCAN_CHAN, json.dumps({"qrCode": qr_code}))
    except Exception as _e:
        log.debug("[sse] Redis publish failed: %s", _e)
    # In-process fallback — covers clients on this worker when Redis is down
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


def _invalidate_inventory(qr_code: str | None = None):
    """Call whenever inventory data changes so next request re-reads from DB.
    Pass qr_code to also evict that specific entry from the fast-scan cache."""
    with _cache_lock:
        _inventory_cache.clear()
        if qr_code:
            _qr_scan_cache.pop(qr_code, None)
        else:
            _qr_scan_cache.clear()
    # Also invalidate the smart scanner in-memory index
    _smart_scanner.invalidate()
    # Bust Redis L2 cache
    if qr_code:
        _rcache_del(_REDIS_INV_KEY, f"{_REDIS_QR_PREFIX}{qr_code}")
    else:
        _rcache_del(_REDIS_INV_KEY)

_cloud_sources_env = os.environ.get("CLOUD_INVENTORY_SOURCES", "")
CLOUD_INVENTORY_SOURCES = (
    [s.strip() for s in _cloud_sources_env.split(",") if s.strip()]
    if _cloud_sources_env
    else [
        "https://inventory-scanner-ngansen84.replit.app/api/inventory",
        "https://hanryxvault.app/api/products",
    ]
)

# ---------------------------------------------------------------------------
# GitHub inventory sync config
# ---------------------------------------------------------------------------
# Set GITHUB_TOKEN (classic PAT with repo:read scope) and GITHUB_INVENTORY_REPO
# in your .env to enable pulling inventory directly from a private GitHub repo.
# The importer auto-detects JSON / CSV / TSV by file extension.
#
#   GITHUB_TOKEN=ghp_xxxxxxxxxxxx
#   GITHUB_INVENTORY_REPO=Ngansen/Inventory-Scanner
#   GITHUB_INVENTORY_FILE=inventory.json   # path inside the repo (default: inventory.json)
#   GITHUB_INVENTORY_BRANCH=main           # branch (default: main)
#
_GITHUB_TOKEN          = os.environ.get("GITHUB_TOKEN", "").strip()
_GITHUB_INVENTORY_REPO = os.environ.get("GITHUB_INVENTORY_REPO", "Ngansen/Inventory-Scanner").strip()
_GITHUB_INVENTORY_FILE = os.environ.get("GITHUB_INVENTORY_FILE", "").strip()
_GITHUB_INVENTORY_BRANCH = os.environ.get("GITHUB_INVENTORY_BRANCH", "main").strip()

# Two-way sync: URL of the HanRyx-Vault storefront running on this Pi.
# When a sale is recorded here, the POS pushes stock decrements back to the
# storefront so the public website never shows "in stock" for a sold-out card.
# On the Pi this is the internal Docker address; externally it's the public URL.
STOREFRONT_URL = os.environ.get(
    "STOREFRONT_URL",
    "http://storefront:3000",   # default: Docker-internal address on the Pi
).rstrip("/")

# ---------------------------------------------------------------------------
# Database — thread-local connections (gunicorn multi-worker safe)
# ---------------------------------------------------------------------------

def get_db() -> _PgConn:
    """Return a per-request PostgreSQL connection (from pool) stored on Flask's g."""
    if "db" not in g:
        g.db = _PgConn(_get_pool().getconn())
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        if exc:
            db.rollback()
        db.close()


_NOW_MS_PG = "(EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT"


# ---------------------------------------------------------------------------
# Pricing engine  (ported from Card-Scanner-AI shared/schema.ts)
# ---------------------------------------------------------------------------

_LANGUAGE_PRICE_RULES: dict = {
    "English": 1.0,  "EN": 1.0,
    "Japanese": 0.55, "JP": 0.55,
    "Korean": 0.40,   "KR": 0.40,
}
_ITEM_TYPE_UNDERCUT: dict = {
    "Single": 0.95,
    "Sealed": 0.97,
    "Graded": 1.10,
}
_GRADE_MULTIPLIER: dict = {
    "10": 2.5, "9.5": 1.9, "9": 1.5, "8.5": 1.25, "8": 1.0,
}
_ROUNDING_RULES: list = [(5, 0.25), (20, 0.50), (100, 1.00), (9999, 5.00)]


def _round_price(price: float) -> float:
    for limit, step in _ROUNDING_RULES:
        if price <= limit:
            return round(round(price / step) * step, 2)
    return round(price, 2)


def _calculate_final_price(base: float, language: str = "English",
                            item_type: str = "Single", grade: str = "") -> float:
    """Apply language discount, grade premium, item-type undercut, then round."""
    p = base * _LANGUAGE_PRICE_RULES.get(language, 1.0)
    if grade:
        p *= _GRADE_MULTIPLIER.get(grade, 1.0)
    p *= _ITEM_TYPE_UNDERCUT.get(item_type, 1.0)
    return _round_price(p)


def init_db():
    db = _direct_db()

    _ddl_statements = [
        f"""CREATE TABLE IF NOT EXISTS sales (
            id              BIGSERIAL PRIMARY KEY,
            transaction_id  TEXT UNIQUE NOT NULL,
            timestamp_ms    BIGINT NOT NULL,
            subtotal        DOUBLE PRECISION NOT NULL DEFAULT 0,
            tax_amount      DOUBLE PRECISION NOT NULL DEFAULT 0,
            tip_amount      DOUBLE PRECISION NOT NULL DEFAULT 0,
            total_amount    DOUBLE PRECISION NOT NULL DEFAULT 0,
            payment_method  TEXT NOT NULL DEFAULT 'UNKNOWN',
            employee_id     TEXT NOT NULL DEFAULT 'UNKNOWN',
            items_json      TEXT NOT NULL DEFAULT '[]',
            cash_received   DOUBLE PRECISION NOT NULL DEFAULT 0,
            change_given    DOUBLE PRECISION NOT NULL DEFAULT 0,
            is_refunded     INTEGER NOT NULL DEFAULT 0,
            received_at     BIGINT NOT NULL DEFAULT {_NOW_MS_PG},
            source          TEXT NOT NULL DEFAULT 'local'
        )""",
        f"""CREATE TABLE IF NOT EXISTS inventory (
            qr_code         TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            price           DOUBLE PRECISION NOT NULL DEFAULT 0,
            category        TEXT NOT NULL DEFAULT 'General',
            rarity          TEXT NOT NULL DEFAULT '',
            set_code        TEXT NOT NULL DEFAULT '',
            description     TEXT NOT NULL DEFAULT '',
            stock           INTEGER NOT NULL DEFAULT 0,
            last_updated    BIGINT NOT NULL DEFAULT {_NOW_MS_PG},
            image_url       TEXT NOT NULL DEFAULT '',
            tcg_id          TEXT NOT NULL DEFAULT ''
        )""",
        f"""CREATE TABLE IF NOT EXISTS stock_deductions (
            id              BIGSERIAL PRIMARY KEY,
            transaction_id  TEXT,
            qr_code         TEXT NOT NULL,
            name            TEXT NOT NULL,
            quantity        INTEGER NOT NULL,
            unit_price      DOUBLE PRECISION NOT NULL,
            line_total      DOUBLE PRECISION NOT NULL,
            deducted_at     BIGINT NOT NULL DEFAULT {_NOW_MS_PG}
        )""",
        f"""CREATE TABLE IF NOT EXISTS scan_queue (
            id          BIGSERIAL PRIMARY KEY,
            qr_code     TEXT NOT NULL,
            scanned_at  BIGINT NOT NULL DEFAULT {_NOW_MS_PG},
            processed   INTEGER NOT NULL DEFAULT 0
        )""",
        "CREATE INDEX IF NOT EXISTS idx_scan_pending    ON scan_queue(processed, id)",
        "CREATE INDEX IF NOT EXISTS idx_sales_timestamp ON sales(timestamp_ms)",
        "CREATE INDEX IF NOT EXISTS idx_sales_received  ON sales(received_at)",
        "CREATE INDEX IF NOT EXISTS idx_stock_qr        ON stock_deductions(qr_code)",
        "CREATE INDEX IF NOT EXISTS idx_stock_received  ON stock_deductions(deducted_at)",
        f"""CREATE TABLE IF NOT EXISTS sale_history (
            id       BIGSERIAL PRIMARY KEY,
            name     TEXT NOT NULL,
            price    DOUBLE PRECISION NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            sold_at  BIGINT NOT NULL DEFAULT {_NOW_MS_PG}
        )""",
        "CREATE INDEX IF NOT EXISTS idx_sale_history_name ON sale_history(name, sold_at)",
        """CREATE TABLE IF NOT EXISTS server_state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS card_tcg_cache (
            card_id     TEXT PRIMARY KEY,
            data_json   TEXT NOT NULL,
            fetched_ms  BIGINT NOT NULL DEFAULT {_NOW_MS_PG}
        )""",
        f"""CREATE TABLE IF NOT EXISTS card_conditions (
            qr_code    TEXT PRIMARY KEY,
            condition  TEXT NOT NULL DEFAULT 'NM',
            notes      TEXT NOT NULL DEFAULT '',
            updated_ms BIGINT NOT NULL DEFAULT {_NOW_MS_PG}
        )""",
        f"""CREATE TABLE IF NOT EXISTS price_history (
            id           BIGSERIAL PRIMARY KEY,
            card_id      TEXT NOT NULL,
            card_name    TEXT NOT NULL DEFAULT '',
            market_price DOUBLE PRECISION NOT NULL,
            fetched_ms   BIGINT NOT NULL DEFAULT {_NOW_MS_PG}
        )""",
        "CREATE INDEX IF NOT EXISTS idx_price_hist ON price_history(card_id, fetched_ms)",
        f"""CREATE TABLE IF NOT EXISTS scan_log (
            id         BIGSERIAL PRIMARY KEY,
            qr_code    TEXT NOT NULL,
            card_name  TEXT NOT NULL DEFAULT '',
            matched    INTEGER NOT NULL DEFAULT 0,
            price      DOUBLE PRECISION NOT NULL DEFAULT 0,
            scanned_at BIGINT NOT NULL DEFAULT {_NOW_MS_PG}
        )""",
        "CREATE INDEX IF NOT EXISTS idx_scan_log ON scan_log(scanned_at)",
        """CREATE TABLE IF NOT EXISTS wg_peer_names (
            pubkey        TEXT PRIMARY KEY,
            friendly_name TEXT NOT NULL DEFAULT ''
        )""",
        f"""CREATE TABLE IF NOT EXISTS goals (
            id           BIGSERIAL PRIMARY KEY,
            title        TEXT NOT NULL,
            type         TEXT NOT NULL DEFAULT 'card_count',
            target_value INTEGER NOT NULL DEFAULT 1,
            target_set   TEXT NOT NULL DEFAULT '',
            completed    INTEGER NOT NULL DEFAULT 0,
            created_at   BIGINT NOT NULL DEFAULT {_NOW_MS_PG}
        )""",

        # ── Trade-in tables ──────────────────────────────────────────────────
        f"""CREATE TABLE IF NOT EXISTS trade_ins (
            id           BIGSERIAL PRIMARY KEY,
            reference    TEXT UNIQUE NOT NULL,
            customer     TEXT NOT NULL DEFAULT 'Walk-in',
            status       TEXT NOT NULL DEFAULT 'open',
            total_value  DOUBLE PRECISION NOT NULL DEFAULT 0,
            notes        TEXT NOT NULL DEFAULT '',
            created_at   BIGINT NOT NULL DEFAULT {_NOW_MS_PG},
            completed_at BIGINT
        )""",
        f"""CREATE TABLE IF NOT EXISTS trade_in_items (
            id           BIGSERIAL PRIMARY KEY,
            trade_in_id  BIGINT NOT NULL REFERENCES trade_ins(id) ON DELETE CASCADE,
            qr_code      TEXT NOT NULL,
            name         TEXT NOT NULL,
            condition    TEXT NOT NULL DEFAULT 'NM',
            offered_price DOUBLE PRECISION NOT NULL DEFAULT 0,
            market_price  DOUBLE PRECISION NOT NULL DEFAULT 0,
            accepted      INTEGER NOT NULL DEFAULT 1
        )""",
        "CREATE INDEX IF NOT EXISTS idx_trade_in_items ON trade_in_items(trade_in_id)",

        # ── Bundle tables ────────────────────────────────────────────────────
        f"""CREATE TABLE IF NOT EXISTS bundles (
            id           BIGSERIAL PRIMARY KEY,
            name         TEXT NOT NULL,
            description  TEXT NOT NULL DEFAULT '',
            bundle_price DOUBLE PRECISION NOT NULL DEFAULT 0,
            sold         INTEGER NOT NULL DEFAULT 0,
            created_at   BIGINT NOT NULL DEFAULT {_NOW_MS_PG}
        )""",
        f"""CREATE TABLE IF NOT EXISTS bundle_items (
            id         BIGSERIAL PRIMARY KEY,
            bundle_id  BIGINT NOT NULL REFERENCES bundles(id) ON DELETE CASCADE,
            qr_code    TEXT NOT NULL,
            name       TEXT NOT NULL,
            quantity   INTEGER NOT NULL DEFAULT 1,
            unit_price DOUBLE PRECISION NOT NULL DEFAULT 0
        )""",
        "CREATE INDEX IF NOT EXISTS idx_bundle_items ON bundle_items(bundle_id)",

        # ── Purchase-order tables ────────────────────────────────────────────
        f"""CREATE TABLE IF NOT EXISTS purchase_orders (
            id           BIGSERIAL PRIMARY KEY,
            reference    TEXT UNIQUE NOT NULL,
            supplier     TEXT NOT NULL DEFAULT 'Unknown',
            status       TEXT NOT NULL DEFAULT 'draft',
            notes        TEXT NOT NULL DEFAULT '',
            total_cost   DOUBLE PRECISION NOT NULL DEFAULT 0,
            created_at   BIGINT NOT NULL DEFAULT {_NOW_MS_PG},
            received_at  BIGINT
        )""",
        f"""CREATE TABLE IF NOT EXISTS purchase_order_items (
            id            BIGSERIAL PRIMARY KEY,
            order_id      BIGINT NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
            qr_code       TEXT NOT NULL,
            name          TEXT NOT NULL,
            qty_ordered   INTEGER NOT NULL DEFAULT 1,
            qty_received  INTEGER NOT NULL DEFAULT 0,
            unit_cost     DOUBLE PRECISION NOT NULL DEFAULT 0
        )""",
        "CREATE INDEX IF NOT EXISTS idx_po_items ON purchase_order_items(order_id)",

        # ── Layby (hold) tables ──────────────────────────────────────────────
        f"""CREATE TABLE IF NOT EXISTS laybys (
            id           BIGSERIAL PRIMARY KEY,
            reference    TEXT UNIQUE NOT NULL,
            customer     TEXT NOT NULL DEFAULT 'Walk-in',
            status       TEXT NOT NULL DEFAULT 'open',
            total_price  DOUBLE PRECISION NOT NULL DEFAULT 0,
            deposit_paid DOUBLE PRECISION NOT NULL DEFAULT 0,
            notes        TEXT NOT NULL DEFAULT '',
            due_date     TEXT NOT NULL DEFAULT '',
            created_at   BIGINT NOT NULL DEFAULT {_NOW_MS_PG},
            completed_at BIGINT
        )""",
        f"""CREATE TABLE IF NOT EXISTS layby_items (
            id         BIGSERIAL PRIMARY KEY,
            layby_id   BIGINT NOT NULL REFERENCES laybys(id) ON DELETE CASCADE,
            qr_code    TEXT NOT NULL,
            name       TEXT NOT NULL,
            quantity   INTEGER NOT NULL DEFAULT 1,
            unit_price DOUBLE PRECISION NOT NULL DEFAULT 0
        )""",
        f"""CREATE TABLE IF NOT EXISTS layby_payments (
            id        BIGSERIAL PRIMARY KEY,
            layby_id  BIGINT NOT NULL REFERENCES laybys(id) ON DELETE CASCADE,
            amount    DOUBLE PRECISION NOT NULL DEFAULT 0,
            method    TEXT NOT NULL DEFAULT 'cash',
            notes     TEXT NOT NULL DEFAULT '',
            paid_at   BIGINT NOT NULL DEFAULT {_NOW_MS_PG}
        )""",
        "CREATE INDEX IF NOT EXISTS idx_layby_items    ON layby_items(layby_id)",
        "CREATE INDEX IF NOT EXISTS idx_layby_payments ON layby_payments(layby_id)",

        # ── End-of-day reconciliation ────────────────────────────────────────
        f"""CREATE TABLE IF NOT EXISTS eod_reconciliations (
            id              BIGSERIAL PRIMARY KEY,
            date_str        TEXT UNIQUE NOT NULL,
            opening_float   DOUBLE PRECISION NOT NULL DEFAULT 0,
            closing_float   DOUBLE PRECISION NOT NULL DEFAULT 0,
            expected_cash   DOUBLE PRECISION NOT NULL DEFAULT 0,
            actual_cash     DOUBLE PRECISION NOT NULL DEFAULT 0,
            discrepancy     DOUBLE PRECISION NOT NULL DEFAULT 0,
            total_sales     DOUBLE PRECISION NOT NULL DEFAULT 0,
            cash_sales      DOUBLE PRECISION NOT NULL DEFAULT 0,
            card_sales      DOUBLE PRECISION NOT NULL DEFAULT 0,
            transaction_count INTEGER NOT NULL DEFAULT 0,
            notes           TEXT NOT NULL DEFAULT '',
            created_at      BIGINT NOT NULL DEFAULT {_NOW_MS_PG}
        )""",

        # ── Enterprise: Audit log ────────────────────────────────────────────
        f"""CREATE TABLE IF NOT EXISTS audit_log (
            id         BIGSERIAL PRIMARY KEY,
            ts_ms      BIGINT NOT NULL DEFAULT {_NOW_MS_PG},
            actor      TEXT NOT NULL DEFAULT 'anonymous',
            action     TEXT NOT NULL,
            resource   TEXT NOT NULL DEFAULT '',
            detail     TEXT NOT NULL DEFAULT '',
            ip         TEXT NOT NULL DEFAULT '',
            request_id TEXT NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX IF NOT EXISTS idx_audit_ts     ON audit_log(ts_ms DESC)",
        "CREATE INDEX IF NOT EXISTS idx_audit_actor  ON audit_log(actor)",
        "CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action)",

        # ── Enterprise: API tokens (JWT issuance) ────────────────────────────
        f"""CREATE TABLE IF NOT EXISTS api_tokens (
            id          BIGSERIAL PRIMARY KEY,
            label       TEXT NOT NULL,
            token_hash  TEXT UNIQUE NOT NULL,
            created_by  TEXT NOT NULL DEFAULT 'admin',
            scopes      TEXT NOT NULL DEFAULT 'scan,sales',
            expires_at  BIGINT,
            revoked     INTEGER NOT NULL DEFAULT 0,
            created_at  BIGINT NOT NULL DEFAULT {_NOW_MS_PG},
            last_used   BIGINT
        )""",

        # ── Enterprise: TOTP 2FA secrets ─────────────────────────────────────
        f"""CREATE TABLE IF NOT EXISTS totp_secrets (
            id         BIGSERIAL PRIMARY KEY,
            username   TEXT UNIQUE NOT NULL DEFAULT 'admin',
            secret     TEXT NOT NULL,
            enabled    INTEGER NOT NULL DEFAULT 0,
            created_at BIGINT NOT NULL DEFAULT {_NOW_MS_PG}
        )""",

        # ── Enterprise: Idempotency keys for /sales ──────────────────────────
        f"""CREATE TABLE IF NOT EXISTS sales_idempotency (
            idempotency_key TEXT PRIMARY KEY,
            sale_id         BIGINT,
            response_json   TEXT NOT NULL DEFAULT '{{}}',
            created_at      BIGINT NOT NULL DEFAULT {_NOW_MS_PG}
        )""",

        # ── Enterprise: Returns / refunds ─────────────────────────────────────
        f"""CREATE TABLE IF NOT EXISTS returns (
            id           BIGSERIAL PRIMARY KEY,
            reference    TEXT UNIQUE NOT NULL,
            original_sale_id BIGINT,
            reason       TEXT NOT NULL DEFAULT '',
            refund_amount DOUBLE PRECISION NOT NULL DEFAULT 0,
            refund_method TEXT NOT NULL DEFAULT 'original',
            status       TEXT NOT NULL DEFAULT 'pending',
            notes        TEXT NOT NULL DEFAULT '',
            created_by   TEXT NOT NULL DEFAULT 'admin',
            created_at   BIGINT NOT NULL DEFAULT {_NOW_MS_PG},
            completed_at BIGINT
        )""",
        f"""CREATE TABLE IF NOT EXISTS return_items (
            id          BIGSERIAL PRIMARY KEY,
            return_id   BIGINT NOT NULL REFERENCES returns(id) ON DELETE CASCADE,
            qr_code     TEXT NOT NULL,
            name        TEXT NOT NULL,
            quantity    INTEGER NOT NULL DEFAULT 1,
            unit_price  DOUBLE PRECISION NOT NULL DEFAULT 0,
            restock     INTEGER NOT NULL DEFAULT 1
        )""",
        "CREATE INDEX IF NOT EXISTS idx_return_items ON return_items(return_id)",

        # ── Enterprise: Suppliers ─────────────────────────────────────────────
        f"""CREATE TABLE IF NOT EXISTS suppliers (
            id          BIGSERIAL PRIMARY KEY,
            name        TEXT UNIQUE NOT NULL,
            contact     TEXT NOT NULL DEFAULT '',
            email       TEXT NOT NULL DEFAULT '',
            phone       TEXT NOT NULL DEFAULT '',
            address     TEXT NOT NULL DEFAULT '',
            notes       TEXT NOT NULL DEFAULT '',
            created_at  BIGINT NOT NULL DEFAULT {_NOW_MS_PG}
        )""",

        # ── Enterprise: Low-stock alert configuration ─────────────────────────
        f"""CREATE TABLE IF NOT EXISTS low_stock_config (
            qr_code   TEXT PRIMARY KEY,
            threshold INTEGER NOT NULL DEFAULT 1,
            alerted   INTEGER NOT NULL DEFAULT 0,
            updated   BIGINT  NOT NULL DEFAULT {_NOW_MS_PG}
        )""",

        # ── Storefront sync queue — tracks unsynchronised stock/price changes ──
        f"""CREATE TABLE IF NOT EXISTS unsynced_changes (
            id          BIGSERIAL PRIMARY KEY,
            qr_code     TEXT NOT NULL,
            change_type TEXT NOT NULL DEFAULT 'stock',
            delta       INTEGER NOT NULL DEFAULT 0,
            synced      INTEGER NOT NULL DEFAULT 0,
            created_at  BIGINT  NOT NULL DEFAULT {_NOW_MS_PG}
        )""",
        "CREATE INDEX IF NOT EXISTS idx_unsynced_qr ON unsynced_changes(qr_code)",
        "CREATE INDEX IF NOT EXISTS idx_unsynced_pending ON unsynced_changes(synced) WHERE synced = 0",

        # ── Intelligent pricing engine: translation cache ──────────────────────
        f"""CREATE TABLE IF NOT EXISTS translation_cache (
            original    TEXT NOT NULL,
            lang        TEXT NOT NULL,
            translated  TEXT NOT NULL,
            created_at  BIGINT NOT NULL DEFAULT {_NOW_MS_PG},
            PRIMARY KEY (original, lang)
        )""",

        # ── Intelligent pricing engine: pricing cache ──────────────────────────
        f"""CREATE TABLE IF NOT EXISTS pricing_cache (
            query       TEXT PRIMARY KEY,
            pricing     JSONB NOT NULL,
            created_at  BIGINT NOT NULL DEFAULT {_NOW_MS_PG}
        )""",
    ]

    for stmt in _ddl_statements:
        db.execute(stmt)
    db.commit()

    # ── Safe migration: add columns that may be missing on older installs ────
    def _col_exists(table, col):
        r = db.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name=%s AND column_name=%s",
            (table, col),
        ).fetchone()
        return r is not None

    for col, ddl in [
        ("source",          "ALTER TABLE sales     ADD COLUMN source          TEXT NOT NULL DEFAULT 'local'"),
        ("image_url",       "ALTER TABLE inventory ADD COLUMN image_url       TEXT NOT NULL DEFAULT ''"),
        ("tcg_id",          "ALTER TABLE inventory ADD COLUMN tcg_id          TEXT NOT NULL DEFAULT ''"),
        ("language",        "ALTER TABLE inventory ADD COLUMN language         TEXT NOT NULL DEFAULT 'English'"),
        ("condition",       "ALTER TABLE inventory ADD COLUMN condition        TEXT NOT NULL DEFAULT 'NM'"),
        ("item_type",       "ALTER TABLE inventory ADD COLUMN item_type        TEXT NOT NULL DEFAULT 'Single'"),
        ("grading_company", "ALTER TABLE inventory ADD COLUMN grading_company  TEXT NOT NULL DEFAULT ''"),
        ("grade",           "ALTER TABLE inventory ADD COLUMN grade            TEXT NOT NULL DEFAULT ''"),
        ("cert_number",     "ALTER TABLE inventory ADD COLUMN cert_number      TEXT NOT NULL DEFAULT ''"),
        ("back_image_url",  "ALTER TABLE inventory ADD COLUMN back_image_url   TEXT NOT NULL DEFAULT ''"),
        ("purchase_price",  "ALTER TABLE inventory ADD COLUMN purchase_price   DOUBLE PRECISION NOT NULL DEFAULT 0"),
        ("sale_price",      "ALTER TABLE inventory ADD COLUMN sale_price       DOUBLE PRECISION NOT NULL DEFAULT 0"),
        ("tags",            "ALTER TABLE inventory ADD COLUMN tags             TEXT NOT NULL DEFAULT ''"),
        ("featured",        "ALTER TABLE inventory ADD COLUMN featured         INTEGER NOT NULL DEFAULT 0"),
        ("listed_for_sale", "ALTER TABLE inventory ADD COLUMN listed_for_sale  INTEGER NOT NULL DEFAULT 1"),
        ("search_key",      "ALTER TABLE inventory ADD COLUMN search_key       TEXT NOT NULL DEFAULT ''"),
        ("card_number",     "ALTER TABLE inventory ADD COLUMN card_number      TEXT    NOT NULL DEFAULT ''"),
        ("variant",         "ALTER TABLE inventory ADD COLUMN variant          TEXT    NOT NULL DEFAULT ''"),
        ("release_year",    "ALTER TABLE inventory ADD COLUMN release_year     INTEGER NOT NULL DEFAULT 0"),
    ]:
        table = "sales" if col == "source" else "inventory"
        if not _col_exists(table, col):
            db.execute(ddl)
            db.commit()
            log.info("[DB] Migration: added %s.%s column", table, col)

    # Backfill card_number for rows that have a SET-NUM qr_code but empty card_number
    try:
        db.execute("""
            UPDATE inventory
            SET card_number = LTRIM(
                SUBSTRING(qr_code FROM '[A-Za-z]{2,8}[-/]0*([0-9]{1,4}[A-Za-z]?)'),
                '0'
            )
            WHERE card_number = ''
              AND qr_code ~ '[A-Za-z]{2,8}[-/][0-9]'
        """)
        db.commit()
        log.info("[DB] Backfilled card_number column")
    except Exception as _be:
        log.warning("[DB] card_number backfill skipped: %s", _be)

    # Backfill release_year from set_code for existing rows
    try:
        db.execute("""
            UPDATE inventory
            SET release_year = CASE
                WHEN UPPER(set_code) LIKE 'SV%'   THEN 2023
                WHEN UPPER(set_code) LIKE 'SWSH%' THEN 2020
                WHEN UPPER(set_code) LIKE 'SM%'   THEN 2017
                WHEN UPPER(set_code) LIKE 'XY%'   THEN 2014
                WHEN UPPER(set_code) LIKE 'BW%'   THEN 2011
                WHEN UPPER(set_code) LIKE 'HGSS%' THEN 2010
                WHEN UPPER(set_code) LIKE 'PL%'   THEN 2009
                WHEN UPPER(set_code) LIKE 'DP%'   THEN 2007
                WHEN UPPER(set_code) LIKE 'EX%'   THEN 2003
                WHEN UPPER(set_code) LIKE 'BASE%' THEN 1999
                ELSE 0
            END
            WHERE release_year = 0 AND set_code != ''
        """)
        db.commit()
        log.info("[DB] Backfilled release_year column")
    except Exception as _rye:
        log.warning("[DB] release_year backfill skipped: %s", _rye)

    # Add indexes for fast number/year searches
    try:
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_inventory_card_number ON inventory(card_number)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_inventory_release_year ON inventory(release_year)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_inventory_rarity ON inventory(rarity)"
        )
        db.commit()
    except Exception:
        pass

    db.close()
    log.info("[DB] Initialized PostgreSQL database")


# ---------------------------------------------------------------------------
# Cloud inventory sync
# ---------------------------------------------------------------------------

def _sync_from_github(db, force: bool = False) -> dict:
    """
    Pull inventory from the Inventory-Scanner GitHub repo via its live API.

    The Inventory-Scanner app exposes GET /api/inventory which returns all
    scanned products in the format the POS expects:
      { qrCode, name, price, category, description, sku }

    The URL is read from INVENTORY_SCANNER_URL env var (set this to the
    deployed Replit URL of your Inventory-Scanner app, e.g.
    https://inventory-scanner.yourusername.repl.co/api/inventory).

    Falls back to the GitHub raw API to fetch the latest data file directly
    if INVENTORY_SCANNER_URL is not set.
    """
    scanner_url = os.environ.get("INVENTORY_SCANNER_URL", "").strip()
    token       = _GITHUB_TOKEN

    results = {}

    # ── 1. Try the live API endpoint first ─────────────────────────────────
    if scanner_url:
        try:
            resp = _requests.get(
                scanner_url,
                headers={"Accept": "application/json", "User-Agent": "HanryxVaultPi/2.0"},
                timeout=15,
            )
            resp.raise_for_status()
            items = resp.json()
            if not isinstance(items, list):
                items = items.get("items") or items.get("products") or items.get("inventory") or []

            upserted = 0
            for item in items:
                qr   = (item.get("qrCode") or item.get("qr_code") or item.get("barcode") or "").strip()
                name = (item.get("name") or item.get("title") or "").strip()
                if not qr or not name:
                    continue
                db.execute("""
                    INSERT INTO inventory (qr_code, name, price, category, rarity, set_code, description, stock, last_updated)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(qr_code) DO UPDATE SET
                        name=excluded.name, price=excluded.price,
                        category=excluded.category, description=excluded.description,
                        last_updated=excluded.last_updated
                """, (
                    qr, name,
                    float(item.get("price") or 0),
                    item.get("category") or "General",
                    "", "",
                    item.get("description") or "",
                    int(item.get("stockQuantity") or item.get("stock") or 0),
                    int(_time.time() * 1000),
                ))
                upserted += 1
            db.commit()
            log.info("[github-sync] Live API → %d products upserted", upserted)
            results["live_api"] = {"ok": True, "url": scanner_url, "upserted": upserted}
            return results
        except Exception as e:
            log.warning("[github-sync] Live API failed (%s): %s — trying GitHub fallback", scanner_url, e)
            results["live_api"] = {"ok": False, "error": str(e)}

    # ── 2. Fallback: pull via GitHub API (works even if the app is sleeping) ─
    if not token:
        log.warning("[github-sync] No GITHUB_TOKEN set — cannot fetch from GitHub")
        results["github"] = {"ok": False, "error": "GITHUB_TOKEN not set"}
        return results

    try:
        repo   = _GITHUB_INVENTORY_REPO
        branch = _GITHUB_INVENTORY_BRANCH or "master"
        # Fetch /api/inventory response cached in the repo, or fall back to
        # reading the scans table export if it exists
        gh_headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "HanryxVaultPi/2.0",
        }
        # Check if an inventory export file exists in the repo
        candidate_files = (
            [_GITHUB_INVENTORY_FILE] if _GITHUB_INVENTORY_FILE
            else ["inventory.json", "inventory.csv", "data/inventory.json", "data/inventory.csv",
                  "export/inventory.json", "export/inventory.csv"]
        )
        raw_url = None
        for fname in candidate_files:
            check = _requests.get(
                f"https://api.github.com/repos/{repo}/contents/{fname}?ref={branch}",
                headers=gh_headers, timeout=10,
            )
            if check.status_code == 200:
                raw_url = check.json().get("download_url")
                log.info("[github-sync] Found inventory file: %s", fname)
                break

        if not raw_url:
            results["github"] = {"ok": False, "error": "No inventory file found in repo — set INVENTORY_SCANNER_URL"}
            return results

        file_resp = _requests.get(raw_url, timeout=15)
        file_resp.raise_for_status()
        ct = file_resp.headers.get("Content-Type", "")

        if "json" in ct or raw_url.endswith(".json"):
            items = file_resp.json()
            if not isinstance(items, list):
                items = items.get("items") or items.get("products") or items.get("inventory") or []
        else:
            # CSV
            reader = csv.DictReader(io.StringIO(file_resp.text))
            items  = list(reader)

        _ts  = int(_time.time() * 1000)
        rows = []
        for item in items:
            qr   = (item.get("qrCode") or item.get("qr_code") or item.get("barcode") or "").strip()
            name = (item.get("name") or item.get("title") or "").strip()
            if not qr or not name:
                continue
            rows.append((
                qr, name,
                float(item.get("price") or 0),
                item.get("category") or "General",
                "", "",
                item.get("description") or "",
                int(item.get("stockQuantity") or item.get("stock") or 0),
                _ts,
            ))
        if rows:
            db.executemany("""
                INSERT INTO inventory (qr_code, name, price, category, rarity, set_code, description, stock, last_updated)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(qr_code) DO UPDATE SET
                    name=excluded.name, price=excluded.price,
                    category=excluded.category, description=excluded.description,
                    last_updated=excluded.last_updated
            """, rows)
        upserted = len(rows)
        db.commit()
        log.info("[github-sync] GitHub file → %d products upserted", upserted)
        results["github"] = {"ok": True, "upserted": upserted}
    except Exception as e:
        log.error("[github-sync] GitHub fetch failed: %s", e)
        results["github"] = {"ok": False, "error": str(e)}

    return results


def sync_inventory_from_cloud(force: bool = False) -> dict:
    """Pull inventory from both Replit cloud sources and upsert into local DB."""
    db = _direct_db()

    if not force:
        count = db.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
        if count > 0:
            db.close()
            log.info("[cloud-sync] Inventory has %d products — skipping auto-sync", count)
            return {"skipped": True, "existing": count}

    total_upserted = 0
    total_skipped  = 0
    results        = {}

    for url in CLOUD_INVENTORY_SOURCES:
        try:
            resp = _requests.get(
                url,
                headers={"Accept": "application/json", "User-Agent": "HanryxVaultPi/2.0"},
                timeout=15,
            )
            resp.raise_for_status()
            items = resp.json()

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
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    log.warning("[cloud-sync] Row error (%s): %s", url, row_err)
                    total_skipped += 1

            db.commit()
            total_upserted += upserted
            results[url] = {"ok": True, "upserted": upserted}
            log.info("[cloud-sync] %s → %d upserted", url, upserted)

        except Exception as e:
            db.rollback() if hasattr(db, "rollback") else None
            results[url] = {"ok": False, "error": str(e)}
            log.error("[cloud-sync] Failed %s: %s", url, e)

    # ── Also pull from Inventory-Scanner GitHub repo ─────────────────────
    gh_results = _sync_from_github(db, force=force)
    for k, v in gh_results.items():
        results[f"github:{k}"] = v
        if v.get("upserted"):
            total_upserted += v["upserted"]

    db.close()
    return {"upserted": total_upserted, "skipped": total_skipped, "sources": results}


def _cleanup_scan_queue():
    """Delete processed scans older than 1 hour — runs every hour in background."""
    try:
        cutoff = int((_time.time() - 3600) * 1000)
        db = _direct_db()
        cur = db.execute(
            "DELETE FROM scan_queue WHERE processed = 1 AND scanned_at < %s", (cutoff,)
        )
        deleted = cur.rowcount
        db.close()
        log.info("[cleanup] Removed %d stale scan_queue rows", deleted)
    except Exception as e:
        log.error("[cleanup] scan_queue cleanup failed: %s", e)
    threading.Timer(3600, _cleanup_scan_queue).start()


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
    # Keep variant keywords (ex, gx, v, vmax, vstar) — they are searchable
    _STOP = {"the", "a", "an", "of", "in"}
    return [t for t in re.split(r'[\s\-_/\\,\.]+', text.lower()) if t and t not in _STOP]


def _score_card(name: str, set_code: str, qr_code: str, tokens: list[str],
                card_number: str = "", variant: str = "",
                rarity: str = "", release_year: int = 0) -> int:
    """
    Return a relevance score (higher = better match) for a candidate card
    against a list of search tokens.  Purely in-Python — no extra DB round-trip.

    Bonuses:
      +8  — token exactly equals card_number  (e.g. '25' → number '25')
      +8  — token is a 4-digit year matching release_year  (e.g. '2023')
      +4  — token appears in variant string  (e.g. 'ex', 'vmax', 'holo')
      +3  — token appears in rarity string   (e.g. 'ultra', 'rare', 'secret')
      +3  — token is an exact word-boundary token in the name
      +2  — token appears anywhere in the name
      +1  — token appears in set_code or qr_code
      +5  — ALL tokens matched somewhere in the name (full-name bonus)
    """
    score      = 0
    name_lc    = name.lower()
    set_lc     = set_code.lower()
    qr_lc      = qr_code.lower()
    variant_lc = variant.lower()
    rarity_lc  = rarity.lower()
    name_toks  = _tokenize(name)

    for t in tokens:
        if card_number and t == card_number:               score += 8
        if release_year and t.isdigit() and len(t) == 4 \
                and int(t) == release_year:                score += 8
        if variant_lc and t in variant_lc:                 score += 4
        if rarity_lc  and t in rarity_lc:                  score += 3
        if t in name_lc:                                    score += 2
        if t in name_toks:                                  score += 3
        if t in set_lc:                                     score += 1
        if t in qr_lc:                                      score += 1

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
      3. Set code + card number — uses card_number column (exact, fast)
      3b. Extract SET-NUM pattern from free-text query
      4. Number-only search (card_num across all sets)
      5. Tokenised name + variant search with relevance scoring
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
            "cardNumber":    r["card_number"]   if "card_number"   in keys else "",
            "variant":       r["variant"]       if "variant"       in keys else "",
            "releaseYear":   r["release_year"]  if "release_year"  in keys else 0,
            "description":   r["description"] or "",
            "stockQuantity": r["stock"],
            "lastUpdated":   r["last_updated"],
            "imageUrl":      r["image_url"] if "image_url" in keys else "",
            "tcgId":         r["tcg_id"]    if "tcg_id"    in keys else "",
        }

    # 1 — exact qr match
    if qr:
        row = db.execute(
            "SELECT * FROM inventory WHERE qr_code = %s LIMIT 1", (qr,)
        ).fetchone()
        if row:
            return [_row_to_dict(row)]

        # 1b — normalised qr
        norm = _normalize_qr(qr)
        if norm != qr:
            row = db.execute(
                "SELECT * FROM inventory WHERE qr_code = %s LIMIT 1", (norm,)
            ).fetchone()
            if row:
                return [_row_to_dict(row)]

        # treat qr text as search terms if no exact match
        if not q:
            q = norm

    # 2 — explicit set + number (uses card_number column — exact, indexed)
    if set_code and card_num:
        num_norm = card_num.lstrip("0") or "0"
        rows = db.execute("""
            SELECT * FROM inventory
            WHERE UPPER(set_code) = UPPER(%s) AND card_number = %s
            ORDER BY name ASC LIMIT %s
        """, (set_code, num_norm, limit)).fetchall()
        if not rows:
            # Fallback: LIKE on name/qr for legacy rows without card_number populated
            rows = db.execute("""
                SELECT * FROM inventory
                WHERE UPPER(set_code) = UPPER(%s)
                  AND (name LIKE %s OR qr_code LIKE %s)
                ORDER BY name ASC LIMIT %s
            """, (set_code, f"%{card_num}%", f"%{card_num}%", limit)).fetchall()
        if rows:
            return [_row_to_dict(r) for r in rows]

    # 3 — try to extract SET-NUM pattern from free-text query (e.g. "SV1 001" or "sv1-001")
    if q:
        _SET_NUM_RE = re.compile(r'\b([A-Za-z]{2,8})\s*[-/]?\s*0*(\d{1,4})\b')
        m = _SET_NUM_RE.search(q)
        if m:
            s, n = m.group(1).upper(), m.group(2).lstrip("0") or "0"
            rows = db.execute("""
                SELECT * FROM inventory
                WHERE UPPER(set_code) = %s AND card_number = %s
                ORDER BY name ASC LIMIT %s
            """, (s, n, limit)).fetchall()
            if not rows:
                rows = db.execute("""
                    SELECT * FROM inventory
                    WHERE UPPER(set_code) = %s AND (name LIKE %s OR qr_code LIKE %s)
                    ORDER BY name ASC LIMIT %s
                """, (s, f"%{n}%", f"%{n}%", limit)).fetchall()
            if rows:
                return [_row_to_dict(r) for r in rows]

    # 4 — number-only search: card_num without set_code → all sets, ordered by set
    if card_num and not set_code:
        num_norm = card_num.lstrip("0") or "0"
        rows = db.execute("""
            SELECT * FROM inventory
            WHERE card_number = %s
            ORDER BY set_code ASC, name ASC LIMIT %s
        """, (num_norm, limit)).fetchall()
        if rows:
            return [_row_to_dict(r) for r in rows]

    # 5 — tokenised name + variant + rarity search with relevance scoring
    if not q and name:
        q = name
    if not q:
        return []

    tokens = _tokenize(q)
    if not tokens:
        return []

    # Year-only fast path: if the only useful token is a 4-digit year, filter by release_year
    year_tokens = [t for t in tokens if t.isdigit() and len(t) == 4 and 1996 <= int(t) <= 2030]
    non_year    = [t for t in tokens if t not in year_tokens]
    if year_tokens and not non_year:
        yr = int(year_tokens[0])
        rows = db.execute("""
            SELECT * FROM inventory
            WHERE release_year = %s
            ORDER BY name ASC LIMIT %s
        """, (yr, limit)).fetchall()
        return [_row_to_dict(r) for r in rows]

    # Candidates: match at least one token in name, variant, OR rarity
    like_clauses = " OR ".join(
        ["LOWER(name) LIKE %s"    for _ in tokens] +
        ["LOWER(variant) LIKE %s" for _ in tokens] +
        ["LOWER(rarity) LIKE %s"  for _ in tokens]
    )
    like_args = [f"%{t}%" for t in tokens] * 3
    rows = db.execute(f"""
        SELECT * FROM inventory
        WHERE {like_clauses}
        ORDER BY name ASC
        LIMIT 200
    """, like_args).fetchall()

    if not rows:
        return []

    def _r(key, row, default=""):
        return row[key] if key in row.keys() else default

    scored = sorted(
        rows,
        key=lambda r: _score_card(
            r["name"], r["set_code"] or "", r["qr_code"], tokens,
            card_number  = _r("card_number",  r),
            variant      = _r("variant",      r),
            rarity       = _r("rarity",       r),
            release_year = _r("release_year", r, 0),
        ),
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
    """Return PostgreSQL-cached TCG data if younger than 24 h, else None."""
    try:
        conn = _direct_db()
        row = conn.execute(
            "SELECT data_json, fetched_ms FROM card_tcg_cache WHERE card_id=%s", (card_id,)
        ).fetchone()
        conn.close()
        if row and (_now_ms() - row["fetched_ms"]) < 86_400_000:
            return json.loads(row["data_json"])
    except Exception:
        pass
    return None


def _tcg_db_set(card_id: str, data: dict):
    """Persist a TCG API response to the PostgreSQL cache."""
    try:
        conn = _direct_db()
        conn.execute(
            "INSERT INTO card_tcg_cache (card_id, data_json, fetched_ms) VALUES (%s,%s,%s) "
            "ON CONFLICT (card_id) DO UPDATE SET data_json=EXCLUDED.data_json, fetched_ms=EXCLUDED.fetched_ms",
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
        resp = _requests.get(url, headers=_tcg_headers(), timeout=7)
        resp.raise_for_status()
        data = resp.json().get("data")
        if data:
            _tcg_db_set(cid, data)
            with _tcg_cache_lock:
                _tcg_mem_cache[cid] = {"data": data, "fetched_ms": _now_ms()}
            return data
    except Exception as e:
        log.error("[tcg] fetch '%s' failed: %s", cid, e)
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
        resp = _requests.get(url, headers=_tcg_headers(), timeout=9)
        resp.raise_for_status()
        results = resp.json().get("data", [])
        for card in results:
            cid = card.get("id", "").lower()
            if cid:
                _tcg_db_set(cid, card)
                with _tcg_cache_lock:
                    _tcg_mem_cache[cid] = {"data": card, "fetched_ms": _now_ms()}
        return results
    except Exception as e:
        log.error("[tcg] search failed ('%s'): %s", " ".join(parts), e)
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
        db  = _direct_db()
        row = db.execute("SELECT value FROM server_state WHERE key='webhook_url'").fetchone()
        db.close()
        url = row[0].strip() if row and row[0] else ""
        if not url:
            return
        resp = _requests.post(
            url,
            json=payload,
            headers={"X-Source": "HanryxVault-Pi"},
            timeout=8,
        )
        resp.raise_for_status()
        log.info("[webhook] pushed card '%s' → %s", payload.get('name', '?'), resp.status_code)
    except Exception as e:
        log.error("[webhook] push failed: %s", e)


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

    # PostgreSQL DB size (replaces old SQLite file-size stats)
    db_size_mb  = 0.0
    wal_size_mb = 0.0  # not applicable for PostgreSQL
    try:
        row = get_db().execute(
            "SELECT pg_database_size(current_database())"
        ).fetchone()
        if row:
            db_size_mb = round(row[0] / (1024 * 1024), 2)
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


@app.route("/admin/monitor-stats", methods=["GET"])
def admin_monitor_stats():
    """JSON dashboard for the desktop monitor (Windows + Pi). No auth required on LAN."""
    db = get_db()
    midnight = int(datetime.datetime.combine(
        datetime.date.today(), datetime.time.min
    ).timestamp() * 1000)

    today = db.execute("""
        SELECT COUNT(*) as cnt,
               COALESCE(SUM(total_amount),0) as rev,
               COALESCE(SUM(tip_amount),0)   as tips
        FROM sales WHERE timestamp_ms >= ?
    """, (midnight,)).fetchone()

    total_sales   = db.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
    inv_count     = db.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
    low_stock     = db.execute(
        "SELECT COUNT(*) FROM inventory WHERE stock <= 5 AND stock > 0"
    ).fetchone()[0]
    out_stock     = db.execute(
        "SELECT COUNT(*) FROM inventory WHERE stock = 0"
    ).fetchone()[0]
    pending_scans = db.execute(
        "SELECT COUNT(*) FROM scan_queue WHERE processed=0"
    ).fetchone()[0]

    open_trade_ins = 0
    try:
        open_trade_ins = db.execute(
            "SELECT COUNT(*) FROM trade_ins WHERE status='open'"
        ).fetchone()[0]
    except Exception:
        pass

    open_laybys = 0
    layby_outstanding = 0.0
    try:
        r = db.execute("""
            SELECT COUNT(*),
                   COALESCE(SUM(total_amount - paid_amount), 0)
            FROM laybys WHERE status='active'
        """).fetchone()
        open_laybys       = r[0]
        layby_outstanding = float(r[1])
    except Exception:
        pass

    open_pos = 0
    try:
        open_pos = db.execute(
            "SELECT COUNT(*) FROM purchase_orders WHERE status IN ('draft','ordered')"
        ).fetchone()[0]
    except Exception:
        pass

    eod_today = False
    try:
        eod_today = bool(db.execute(
            "SELECT 1 FROM eod_reconciliations WHERE date_str=? LIMIT 1",
            (datetime.date.today().isoformat(),)
        ).fetchone())
    except Exception:
        pass

    period_ms  = midnight - (30 * 86400 * 1000)
    pl_revenue = 0.0
    pl_cogs    = 0.0
    try:
        rows = db.execute("""
            SELECT si.quantity, si.unit_price, i.purchase_price
            FROM sale_items si
            JOIN sales s ON s.id = si.sale_id
            LEFT JOIN inventory i ON i.qr_code = si.qr_code
            WHERE s.timestamp_ms >= ?
        """, (period_ms,)).fetchall()
        for r in rows:
            pl_revenue += float(r[1] or 0) * int(r[0] or 0)
            pl_cogs    += float(r[2] or 0) * int(r[0] or 0)
    except Exception:
        pass
    pl_profit = pl_revenue - pl_cogs
    pl_margin = round(pl_profit / pl_revenue * 100, 1) if pl_revenue > 0 else 0.0

    db_size_mb = 0.0
    try:
        row = db.execute("SELECT pg_database_size(current_database())").fetchone()
        if row:
            db_size_mb = round(row[0] / (1024 * 1024), 2)
    except Exception:
        pass

    return jsonify({
        "today_sales":       today["cnt"],
        "today_revenue":     round(float(today["rev"]), 2),
        "today_tips":        round(float(today["tips"]), 2),
        "total_sales":       total_sales,
        "inv_count":         inv_count,
        "low_stock":         low_stock,
        "out_stock":         out_stock,
        "pending_scans":     pending_scans,
        "open_trade_ins":    open_trade_ins,
        "open_laybys":       open_laybys,
        "layby_outstanding": round(layby_outstanding, 2),
        "open_pos":          open_pos,
        "eod_today":         eod_today,
        "pl_30d_revenue":    round(pl_revenue, 2),
        "pl_30d_cogs":       round(pl_cogs, 2),
        "pl_30d_profit":     round(pl_profit, 2),
        "pl_30d_margin":     pl_margin,
        "db_size_mb":        db_size_mb,
        "uptime_s":          int(_time.time() - _server_start_time),
    })


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

@app.route("/zettle/login", methods=["GET"])
def zettle_login():
    """APK-compatible alias — redirects to Zettle OAuth without CSRF state check."""
    if not ZETTLE_CLIENT_ID or not ZETTLE_CLIENT_SECRET:
        return jsonify({"error": "ZETTLE_CLIENT_ID / ZETTLE_CLIENT_SECRET not configured"}), 500
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id":     ZETTLE_CLIENT_ID,
        "redirect_uri":  ZETTLE_REDIRECT_URI,
        "scope":         "READ:PURCHASE WRITE:PURCHASE READ:FINANCE WRITE:PAYMENT",
    })
    return redirect(f"{ZETTLE_OAUTH_BASE}/authorize?{params}")


@app.route("/zettle/auth", methods=["GET"])
def zettle_auth():
    import secrets
    state = secrets.token_urlsafe(32)
    session["zettle_oauth_state"] = state
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
    code          = request.args.get("code", "")
    error         = request.args.get("error", "")
    returned_state = request.args.get("state", "")
    expected_state = session.pop("zettle_oauth_state", None)

    if not expected_state or returned_state != expected_state:
        log.warning("[zettle] CSRF state mismatch in OAuth callback")
        return jsonify({"error": "invalid_state", "code": "csrf_check_failed"}), 400
    if error or not code:
        return jsonify({"error": error or "missing code", "code": "oauth_error"}), 400
    try:
        result = _token_post({
            "grant_type":   "authorization_code",
            "code":         code,
            "redirect_uri": ZETTLE_REDIRECT_URI,
        })
        _store_tokens(result)
        return redirect(ZETTLE_APP_SCHEME + "?success=1")
    except Exception as e:
        log.error("[zettle] callback token exchange failed: %s", e)
        return jsonify({"error": str(e), "code": "token_exchange_failed"}), 500


@app.route("/zettle/status", methods=["GET"])
def zettle_status():
    with _token_lock:
        has_token = bool(_zettle_state.get("access_token"))
        expires   = _zettle_state.get("expires_at", 0.0)
    return jsonify({
        "authenticated": has_token,
        "expires_in_s":  max(0, int(expires - _time.time())) if has_token else 0,
    })


@app.route("/zettle/pay", methods=["POST"])
def zettle_pay():
    """Initiate a card payment via the Zettle POS API (called by the APK)."""
    token = _refresh_token_if_needed()
    if not token:
        return jsonify({"error": "Not authenticated — visit /zettle/login first"}), 401

    body      = request.get_json(force=True, silent=True) or {}
    amount    = body.get("amount")
    currency  = body.get("currency", "GBP")
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
            return jsonify({"error": "Token expired — re-authenticate via /zettle/login", "detail": body_err}), 401
        return jsonify({"error": f"Zettle API error {e.code}", "detail": body_err}), e.code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
    db.execute("INSERT INTO scan_queue (qr_code) VALUES (%s)", (store_code,))
    db.commit()
    # Bust the 1 s scan cache so the tablet picks this up on its very next poll
    with _cache_lock:
        _scan_cache.clear()
    # Also push instantly to any SSE clients (tablet with /scan/stream)
    _sse_broadcast_scan(store_code)

    # Write to scan_log for Scan History tab (best-effort, non-blocking)
    try:
        _sl_matches = _card_lookup(db, qr=store_code, limit=1)
        _sl_local   = _sl_matches[0] if _sl_matches else None
        db.execute(
            "INSERT INTO scan_log (qr_code, card_name, matched, price, scanned_at) VALUES (%s,%s,%s,%s,%s)",
            (
                store_code,
                _sl_local.get("name", "")  if _sl_local else "",
                1 if _sl_local else 0,
                _sl_local.get("price", 0.0) if _sl_local else 0.0,
                _now_ms(),
            )
        )
        db.commit()
    except Exception as _sl_err:
        log.error("[scan_log] write failed: %s", _sl_err)

    if normalised != qr_code:
        log.info("[scan] Queued (normalised): %r → %r", qr_code, store_code)
    else:
        log.info("[scan] Queued: %s", store_code)

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
    resp = jsonify(result)
    resp.headers["Deprecation"] = "true"
    resp.headers["Link"] = '</scan/stream>; rel="successor-version"'
    resp.headers["Sunset"] = "Sat, 01 Jan 2026 00:00:00 GMT"
    return resp


@app.route("/scan/ack/<int:scan_id>", methods=["POST"])
def scan_ack(scan_id):
    db = get_db()
    db.execute("UPDATE scan_queue SET processed = 1 WHERE id = %s", (scan_id,))
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
        r = _redis()
        if r:
            # ── Redis pub/sub path (cross-worker) ────────────────────────────
            pubsub = r.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(_REDIS_SCAN_CHAN)
            try:
                while True:
                    msg = pubsub.get_message(timeout=15)
                    if msg and msg.get("type") == "message":
                        yield f"data: {msg['data'].decode('utf-8', errors='replace')}\n\n"
                    else:
                        yield ": heartbeat\n\n"
            finally:
                try:
                    pubsub.unsubscribe(_REDIS_SCAN_CHAN)
                    pubsub.close()
                except Exception:
                    pass
        else:
            # ── In-process queue fallback (single worker / no Redis) ──────────
            q = _queue_mod.Queue()
            with _sse_lock:
                _sse_scan_subscribers.append(q)
            try:
                while True:
                    try:
                        qr_code = q.get(timeout=15)
                        yield f"data: {json.dumps({'qrCode': qr_code})}\n\n"
                    except _queue_mod.Empty:
                        yield ": heartbeat\n\n"
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
            "X-Accel-Buffering": "no",
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
# Fast scanner endpoint — exact QR match + in-memory cache for real-time use
# ---------------------------------------------------------------------------

@app.route("/card/scan", methods=["GET", "OPTIONS"])
def card_scan_fast():
    """
    Ultra-low-latency QR scan lookup for real-time mobile scanner clients.

    Exact QR match only (no fuzzy search). Results cached in-memory for 5 min
    so repeated scans of the same card respond without any DB hit.

    Supports CORS so the mobile app can call it directly (bypassing the scanner
    server proxy) to eliminate one full network round trip.

    GET /card/scan?qr=SV1-4

    Response matches the LookupResult shape the mobile app expects:
      { found, code, name, sku, description, price, stock, rawResponse }
    """
    # Handle CORS preflight
    if request.method == "OPTIONS":
        resp = app.make_default_options_response()
        resp.headers["Access-Control-Allow-Origin"]  = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-KEY"
        return resp

    qr = request.args.get("qr", "").strip()
    if not qr:
        return jsonify({"error": "qr parameter is required"}), 400

    # Check in-memory cache first
    with _cache_lock:
        cached = _qr_scan_cache.get(qr)
    if cached is not None:
        resp = jsonify(cached)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["X-Cache"] = "HIT"
        return resp

    # Exact DB lookup (primary key — extremely fast)
    db  = get_db()
    row = db.execute(
        "SELECT qr_code, name, price, category, rarity, set_code, description, "
        "stock, image_url, tcg_id FROM inventory WHERE qr_code = %s LIMIT 1",
        (qr,)
    ).fetchone()

    if not row:
        # Try once with normalised QR
        norm = _normalize_qr(qr)
        if norm != qr:
            row = db.execute(
                "SELECT qr_code, name, price, category, rarity, set_code, description, "
                "stock, image_url, tcg_id FROM inventory WHERE qr_code = %s LIMIT 1",
                (norm,)
            ).fetchone()

    # ── Smart scan fallback — rapidfuzz fuzzy match when exact DB lookup fails ──
    smart_result = None
    if not row:
        smart_result = _smart_scanner.smart_scan(qr, db)
        if smart_result and smart_result["found"]:
            item = smart_result["item"]
            # Teach the scanner so future identical scans skip fuzzy entirely
            _smart_scanner.learn(qr, item["qr_code"])
            # Trigger background enrichment if this card has no TCG data yet
            if not item.get("tcg_id"):
                def _bg_enrich(qr_code=item["qr_code"]):
                    try:
                        _enrich_with_tcg(None, qr_code)
                    except Exception as _ee:
                        log.debug("[smart-scan] bg enrich failed for %s: %s", qr_code, _ee)
                _bg(_bg_enrich)

    def _row_to_scan_result(r, confidence=1.0, method="exact", variant=""):
        return {
            "found":      True,
            "code":       r["qr_code"],
            "name":       r["name"],
            "sku":        r.get("set_code") or r["qr_code"],
            "description": r.get("description") or "",
            "price":      str(r["price"])  if r["price"]  is not None else None,
            "stock":      str(r["stock"])  if r["stock"]  is not None else None,
            "confidence": confidence,
            "method":     method,
            "variant":    variant or _detect_variant(
                r.get("name") or "", r.get("rarity") or "", r.get("description") or ""
            ),
            "rawResponse": {
                "qrCode":        r["qr_code"],
                "name":          r["name"],
                "price":         r["price"],
                "category":      r.get("category") or "General",
                "rarity":        r.get("rarity")   or "",
                "setCode":       r.get("set_code") or "",
                "description":   r.get("description") or "",
                "stockQuantity": r["stock"],
                "imageUrl":      r.get("image_url") or "",
                "tcgId":         r.get("tcg_id")   or "",
            },
        }

    if row:
        result = _row_to_scan_result(row, confidence=1.0, method="exact")
    elif smart_result and smart_result["found"]:
        item = smart_result["item"]
        result = _row_to_scan_result(
            item,
            confidence=smart_result["confidence"],
            method=smart_result["method"],
            variant=smart_result["variant"],
        )
    else:
        # Nothing found — include fuzzy suggestions so the app can show "Did you mean?"
        suggestions = (smart_result or {}).get("suggestions", []) if smart_result else []
        result = {
            "found":       False,
            "code":        qr,
            "name":        None,
            "sku":         None,
            "description": None,
            "price":       None,
            "stock":       None,
            "confidence":  0.0,
            "method":      "none",
            "variant":     "",
            "rawResponse": None,
            "suggestions": suggestions,
        }

    # Only cache confident matches — low-confidence fuzzy results should re-evaluate
    # as new inventory is added.
    if result.get("found") and result.get("confidence", 1.0) >= 0.85:
        with _cache_lock:
            _qr_scan_cache[qr] = result

    resp = jsonify(result)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["X-Cache"] = "MISS"
    return resp


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

    # Persist market price to price_history for trend tracking
    _ph_price = (result.get("tcgData") or {}).get("tcgplayer", {}).get("marketPrice")
    if _ph_price:
        try:
            db.execute(
                "INSERT INTO price_history (card_id, card_name, market_price, fetched_ms) "
                "VALUES (%s,%s,%s,%s)",
                (norm_qr, result.get("name") or norm_qr, float(_ph_price), _now_ms())
            )
            db.commit()
        except Exception:
            pass

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
        VALUES (%s,%s,%s,%s)
        ON CONFLICT(qr_code) DO UPDATE SET
            condition=excluded.condition, notes=excluded.notes, updated_ms=excluded.updated_ms
    """, (qr_code, condition, notes, _now_ms()))
    db.commit()
    return jsonify({"ok": True, "qrCode": qr_code, "condition": condition})


# ---------------------------------------------------------------------------
# Bulk export — JSON or CSV for website upload
# ---------------------------------------------------------------------------

@app.route("/admin/export-cards", methods=["GET"])
@require_admin
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
            "SELECT * FROM inventory WHERE LOWER(category) LIKE %s ORDER BY name ASC",
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
# QR code generation — single image + full-inventory print sheet
# ---------------------------------------------------------------------------

def _make_qr_png(text: str, box_size: int = 6, border: int = 2) -> bytes:
    """Generate a QR code PNG for *text* and return raw bytes."""
    qr = _qrcode.QRCode(
        version=None,
        error_correction=_qrcode_const.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@app.route("/admin/qr/<path:qr_code>", methods=["GET"])
def admin_qr_image(qr_code: str):
    """
    GET /admin/qr/<qr_code>
    Returns a QR code PNG for the given qr_code string.
    No login required so <img> tags in the print sheet load without auth cookies.
    """
    size = min(max(int(request.args.get("size", 6)), 2), 15)
    try:
        png = _make_qr_png(qr_code, box_size=size)
    except Exception as e:
        log.error("[qr] generation failed for %r: %s", qr_code, e)
        return ("QR error", 500)
    return Response(png, mimetype="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.route("/admin/qr-sheet", methods=["GET"])
@require_admin
def admin_qr_sheet():
    """
    GET /admin/qr-sheet
    GET /admin/qr-sheet?q=charizard     — filter by name
    GET /admin/qr-sheet?cat=Singles     — filter by category
    GET /admin/qr-sheet?zero=1          — include out-of-stock items too
    GET /admin/qr-sheet?cols=3          — labels per row (2–5, default 4)

    Returns a print-ready HTML page with one QR label per inventory card.
    Each label shows: QR code image, card name, set code, price, stock, qr_code text.
    Print from the browser (Ctrl+P / Cmd+P) — the page CSS hides the nav bar.
    """
    q    = request.args.get("q", "").strip().lower()
    cat  = request.args.get("cat", "").strip().lower()
    zero = request.args.get("zero", "0") == "1"
    try:
        cols = min(max(int(request.args.get("cols", 4)), 2), 5)
    except ValueError:
        cols = 4

    db   = get_db()
    rows = db.execute(
        "SELECT qr_code, name, price, category, rarity, set_code, stock "
        "FROM inventory ORDER BY name ASC"
    ).fetchall()

    items = []
    for r in rows:
        if not zero and (r["stock"] or 0) <= 0:
            continue
        if q and q not in (r["name"] or "").lower() and q not in (r["qr_code"] or "").lower():
            continue
        if cat and cat not in (r["category"] or "").lower():
            continue
        items.append(dict(r))

    # Build label HTML
    label_width_pct = 100 // cols
    labels_html = ""
    for it in items:
        qr    = it["qr_code"] or ""
        name  = (it["name"] or "Unknown").replace("<", "&lt;").replace(">", "&gt;")
        price = f"£{it['price']:.2f}" if it.get("price") else "—"
        stock = it.get("stock") or 0
        setc  = (it.get("set_code") or "").replace("<", "&lt;")
        cat_d = (it.get("category") or "").replace("<", "&lt;")
        stock_style = "color:#c00;font-weight:bold" if stock == 0 else ("color:#e07000" if stock <= 2 else "color:#060")
        labels_html += f"""
<div class="label">
  <img src="/admin/qr/{urllib.parse.quote(qr, safe='')}" alt="QR:{qr}" class="qr-img">
  <div class="lname">{name}</div>
  <div class="lmeta">{setc}{' · ' + cat_d if cat_d and cat_d != setc else ''}</div>
  <div class="lprice">{price} &nbsp; <span style="{stock_style}">Stock: {stock}</span></div>
  <div class="lcode">{qr.replace('<','&lt;')}</div>
</div>"""

    total    = len(items)
    filter_s = ""
    if q:
        filter_s += f" · search: <b>{q}</b>"
    if cat:
        filter_s += f" · category: <b>{cat}</b>"
    if not zero:
        filter_s += " · in-stock only"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>QR Label Sheet — HanryxVault</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: Arial, sans-serif; background: #fff; color: #000; }}

  /* ── Screen toolbar ─────────────────────────── */
  .toolbar {{
    background: #1a1a1a; color: #fff; padding: 10px 16px;
    display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  }}
  .toolbar h1 {{ font-size: 16px; font-weight: bold; flex: 1; }}
  .toolbar a  {{ color: #FFD700; font-size: 13px; }}
  .toolbar .info {{ font-size: 13px; color: #aaa; }}
  .toolbar form {{ display:flex; gap:8px; align-items:center; }}
  .toolbar input {{ padding:4px 8px; border-radius:4px; border:none; font-size:13px; }}
  .toolbar select {{ padding:4px; border-radius:4px; border:none; font-size:13px; }}
  .toolbar button {{ padding:5px 14px; border:none; border-radius:4px; cursor:pointer;
                     background:#FFD700; font-weight:bold; font-size:13px; }}

  /* ── Label grid ─────────────────────────────── */
  .grid {{
    display: flex;
    flex-wrap: wrap;
    padding: 8mm;
    gap: 4mm;
  }}
  .label {{
    width: calc({label_width_pct}% - 4mm);
    border: 1px solid #bbb;
    border-radius: 4px;
    padding: 4mm;
    display: flex;
    flex-direction: column;
    align-items: center;
    page-break-inside: avoid;
    background: #fff;
  }}
  .qr-img  {{ width: 100%; max-width: 120px; height: auto; display: block; margin-bottom: 3mm; }}
  .lname   {{ font-size: 10pt; font-weight: bold; text-align: center; line-height: 1.2;
               margin-bottom: 1mm; max-width: 100%; word-break: break-word; }}
  .lmeta   {{ font-size: 8pt; color: #555; text-align: center; margin-bottom: 1mm; }}
  .lprice  {{ font-size: 9pt; text-align: center; margin-bottom: 1mm; }}
  .lcode   {{ font-size: 7pt; color: #777; text-align: center; word-break: break-all; }}

  /* ── Print overrides ────────────────────────── */
  @media print {{
    .toolbar {{ display: none !important; }}
    .grid    {{ padding: 6mm; gap: 3mm; }}
    body     {{ background: #fff; }}
  }}
</style>
</head>
<body>

<div class="toolbar">
  <h1>🏷 QR Label Sheet — HanryxVault</h1>
  <span class="info">{total} label{'s' if total != 1 else ''}{filter_s}</span>
  <form method="get" action="/admin/qr-sheet">
    <input name="q"    placeholder="Search name…" value="{q}">
    <input name="cat"  placeholder="Category…"    value="{cat}">
    <select name="cols">
      {''.join(f'<option value="{c}"{"selected" if c == cols else ""}>{c} per row</option>' for c in range(2,6))}
    </select>
    <label style="font-size:13px;color:#ccc">
      <input type="checkbox" name="zero" value="1" {'checked' if zero else ''}> incl. out-of-stock
    </label>
    <button type="submit">Filter</button>
    <button type="button" onclick="window.print()">🖨 Print</button>
  </form>
  <a href="/admin">← Dashboard</a>
</div>

<div class="grid">
{labels_html if labels_html else '<p style="padding:20px;color:#888">No items match the current filter.</p>'}
</div>

</body>
</html>"""

    return Response(html, mimetype="text/html")


# ---------------------------------------------------------------------------
# Webhook config — auto-push new cards to the HanRYX website
# ---------------------------------------------------------------------------

@app.route("/admin/webhook-config", methods=["GET"])
@require_admin
def webhook_config_get():
    """Return whether a webhook URL is configured (never reveals the URL itself)."""
    db  = get_db()
    row = db.execute("SELECT value FROM server_state WHERE key='webhook_url'").fetchone()
    has = bool(row and row["value"] and row["value"].strip())
    return jsonify({"configured": has})


@app.route("/admin/webhook-config", methods=["POST"])
@require_admin
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
        "INSERT INTO server_state (key, value) VALUES ('webhook_url', %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (url,)
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

    db            = get_db()
    inserted      = 0
    skipped       = 0
    items_for_sf  = []   # collect sold items for two-way storefront sync

    for sale in data:
        transaction_id = sale.get("transactionId") or sale.get("transaction_id")
        if not transaction_id:
            skipped += 1
            continue
        try:
            cur = db.execute("""
                INSERT INTO sales
                    (transaction_id, timestamp_ms, subtotal, tax_amount, tip_amount,
                     total_amount, payment_method, employee_id, items_json,
                     cash_received, change_given, is_refunded, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (transaction_id) DO NOTHING
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
            if cur.rowcount > 0:
                inserted += 1
                # Record each line item in sale_history + email alert + storefront sync
                items  = sale.get("items", [])
                method = sale.get("paymentMethod", "")
                for item in items:
                    iname  = item.get("name", "")
                    iprice = float(item.get("unitPrice") or item.get("price") or 0)
                    iqty   = int(item.get("quantity") or 1)
                    iqr    = item.get("qrCode") or item.get("qr_code") or ""
                    if iname and iprice > 0:
                        try:
                            db.execute(
                                "INSERT INTO sale_history (name, price, quantity, sold_at) "
                                "VALUES (%s, %s, %s, %s)",
                                (iname, iprice, iqty, sale.get("timestamp", _now_ms()))
                            )
                        except Exception as _sh_err:
                            log.debug("[sale_history] write failed: %s", _sh_err)
                        _send_sale_email(iname, iprice, method, iqty)
                        if iqr:
                            items_for_sf.append({"qrCode": iqr, "name": iname, "quantity": iqty})
            else:
                skipped += 1
        except Exception as e:
            log.error("[sync/sales] Error on %s: %s", transaction_id, e)
            skipped += 1

    db.commit()

    # Two-way sync: push stock decrements to the storefront (background, non-blocking)
    if items_for_sf:
        _push_stock_to_storefront(items_for_sf)

    log.info("[sync/sales] source=%s inserted=%d skipped=%d storefront_items=%d",
             source, inserted, skipped, len(items_for_sf))
    return jsonify({"inserted": inserted, "skipped": skipped, "source": source}), 200


@app.route("/sales", methods=["POST"])
def record_sale_history():
    """
    APK lightweight sale recorder.
    Accepts: { items: [{name, price, quantity}], sold_at: <epoch_ms> }
    Idempotency: send X-Idempotency-Key header (or idempotency_key in body) — duplicate
    requests within 24 h return the original response without double-recording.
    No auth required — called by the APK on the local network.
    """
    data    = request.get_json(force=True, silent=True) or {}
    items   = data.get("items", [])
    sold_at = int(data.get("sold_at") or _now_ms())

    # ── Idempotency check ────────────────────────────────────────────────────
    idem_key = (
        request.headers.get("X-Idempotency-Key")
        or data.get("idempotency_key", "")
    ).strip()
    if idem_key:
        db = get_db()
        existing = db.execute(
            "SELECT response_json FROM sales_idempotency WHERE idempotency_key = %s "
            "AND created_at > %s",
            (idem_key, _now_ms() - 86_400_000),   # 24 h window
        ).fetchone()
        if existing:
            resp = jsonify(json.loads(existing["response_json"]))
            resp.headers["X-Idempotency-Replayed"] = "true"
            return resp

    if not items:
        return jsonify({"ok": True, "recorded": 0}), 200

    db   = get_db()
    rows = []
    for item in items:
        name  = (item.get("name") or "").strip()
        price = float(item.get("price") or 0)
        qty   = int(item.get("quantity") or 1)
        if not name or price <= 0:
            continue
        rows.append((name, price, qty, sold_at))
    if rows:
        db.executemany(
            "INSERT INTO sale_history (name, price, quantity, sold_at) VALUES (%s, %s, %s, %s)",
            rows,
        )
    recorded = len(rows)
    db.commit()
    log.info("[/sales POST] recorded=%d idem_key=%s", recorded, idem_key or "none")

    result = {"ok": True, "recorded": recorded}
    if idem_key:
        try:
            db.execute(
                "INSERT INTO sales_idempotency (idempotency_key, response_json) VALUES (%s, %s) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
                (idem_key, json.dumps(result))
            )
            db.commit()
        except Exception:
            pass

    _audit_write("sale.record", f"items={recorded}", f"sold_at={sold_at}")
    return jsonify(result), 200


@app.route("/sales", methods=["GET"])
def get_sale_history_public():
    """
    APK-facing read endpoint — returns the last 500 sale line items as JSON.
    Optional ?limit=N and ?name=... query params.
    """
    limit  = min(int(request.args.get("limit", 500)), 1000)
    name_q = request.args.get("name", "").strip().lower()
    db     = get_db()
    if name_q:
        rows = db.execute(
            "SELECT name, price, quantity, sold_at FROM sale_history "
            "WHERE LOWER(name) LIKE %s ORDER BY sold_at DESC LIMIT %s",
            (f"%{name_q}%", limit)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT name, price, quantity, sold_at FROM sale_history "
            "ORDER BY sold_at DESC LIMIT %s", (limit,)
        ).fetchall()
    return jsonify([dict(r) for r in rows])


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
    items_for_sf = []    # for two-way storefront sync

    for item in data:
        qr_code    = item.get("qrCode", "")
        name       = item.get("name", "Unknown")
        quantity   = int(item.get("quantity", 1))
        unit_price = float(item.get("unitPrice", 0.0))
        line_total = float(item.get("lineTotal", unit_price * quantity))

        db.execute("""
            INSERT INTO stock_deductions (qr_code, name, quantity, unit_price, line_total)
            VALUES (%s, %s, %s, %s, %s)
        """, (qr_code, name, quantity, unit_price, line_total))

        # Check stock BEFORE deducting so we can flag an oversell
        before = db.execute(
            "SELECT stock FROM inventory WHERE qr_code = %s", (qr_code,)
        ).fetchone()

        result = db.execute("""
            UPDATE inventory
            SET stock = MAX(0, stock - %s), last_updated = %s
            WHERE qr_code = %s
        """, (quantity, _now_ms(), qr_code))

        if result.rowcount > 0:
            deducted += 1
            after_stock = db.execute(
                "SELECT stock FROM inventory WHERE qr_code = %s", (qr_code,)
            ).fetchone()
            new_stock = after_stock[0] if after_stock else 0
            stock_levels[qr_code] = new_stock
            if qr_code:
                items_for_sf.append({"qrCode": qr_code, "name": name, "quantity": quantity})
            if before and before[0] < quantity:
                oversold += 1
                log.warning("[inventory/deduct] OVERSELL %s: had %s, sold %d → clamped to 0",
                            qr_code, before[0], quantity)
        else:
            unknown += 1

    db.commit()
    _invalidate_inventory()

    # Two-way sync: push stock decrements to the storefront (background, non-blocking)
    if items_for_sf:
        _push_stock_to_storefront(items_for_sf)

    log.info("[inventory/deduct] deducted=%d oversold=%d unknown_sku=%d", deducted, oversold, unknown)
    return jsonify({
        "deducted":     deducted,
        "unknown_skus": unknown,
        "oversold":     oversold,      # items where satellite sold more than was in stock
        "stock_levels": stock_levels,  # qr_code → new stock level after deduction
    }), 200


@app.route("/inventory/decrement", methods=["POST"])
def inventory_decrement():
    """
    APK / cloud-compatible alias for /inventory/deduct.
    Accepts [{ qrCode, quantity }] — lighter payload, no auth token required
    (used by the Android APK on the local network).
    Stock never goes below 0.
    """
    data = request.get_json(force=True, silent=True)
    if not data or not isinstance(data, list):
        return jsonify({"error": "Expected a JSON array of {qrCode, quantity}"}), 400

    db      = get_db()
    updated = 0
    errors  = []

    for item in data:
        qr  = (item.get("qrCode") or item.get("qr_code") or "").strip()
        qty = int(item.get("quantity") or 1)
        if not qr or qty <= 0:
            continue
        try:
            result = db.execute(
                "UPDATE inventory SET stock = GREATEST(0, stock - %s), last_updated = %s WHERE qr_code = %s",
                (qty, _now_ms(), qr)
            )
            if result.rowcount > 0:
                updated += 1
                _invalidate_inventory(qr_code=qr)
                _queue_unsynced(qr, "stock", -qty)
        except Exception as e:
            errors.append({"qrCode": qr, "error": str(e)})

    db.commit()
    log.info("[inventory/decrement] updated=%d errors=%d", updated, len(errors))
    resp = {"updated": updated}
    if errors:
        resp["errors"] = errors
    return jsonify(resp), 200


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
            WHERE (LOWER(name) LIKE %s OR LOWER(qr_code) LIKE %s OR LOWER(category) LIKE %s)
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
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            log.error("[push/inventory] Error on %s: %s", qr_code, e)
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
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            log.warning("[csv] Row error: %s — %s", e, row)
            skipped += 1

    db.commit()
    _invalidate_inventory()
    return jsonify({"upserted": upserted, "skipped": skipped}), 200


# ---------------------------------------------------------------------------
# Admin — sync from cloud
# ---------------------------------------------------------------------------

@app.route("/admin/sync-from-cloud", methods=["POST"])
@require_admin
def admin_sync_cloud():
    force  = request.args.get("force", "0") == "1"
    result = sync_inventory_from_cloud(force=force)
    _invalidate_inventory()
    return jsonify(result)


@app.route("/admin/sync-scanner", methods=["POST"])
@require_admin
def admin_sync_scanner():
    """
    POST /admin/sync-scanner          — import only new items from Inventory-Scanner
    POST /admin/sync-scanner?force=1  — re-import / overwrite existing items too

    Pulls from INVENTORY_SCANNER_URL (live API) if set, otherwise falls back
    to fetching via the GitHub API using GITHUB_TOKEN.
    """
    force = request.args.get("force", "0") == "1"
    db    = get_db()

    if not force:
        count = db.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
        if count > 0:
            return jsonify({"skipped": True, "existing": count})

    result = _sync_from_github(db)
    _invalidate_inventory()

    total = sum(v.get("upserted", 0) for v in result.values() if isinstance(v, dict))
    ok    = any(v.get("ok") for v in result.values() if isinstance(v, dict))

    if not ok:
        errors = [v.get("error", "unknown") for v in result.values() if isinstance(v, dict) and not v.get("ok")]
        return jsonify({"error": "; ".join(errors), "sources": result}), 502

    return jsonify({"upserted": total, "sources": result})


# ---------------------------------------------------------------------------
# Feature 1: Card Photo Identification via GPT-4o Vision
# ---------------------------------------------------------------------------

@app.route("/card/identify-image", methods=["POST"])
@require_admin
def card_identify_image():
    """
    POST /card/identify-image
    Body (JSON): { "image": "<base64-encoded JPEG/PNG>" }
    Returns enriched card data using GPT-4o Vision then TCG API.
    """
    if not _OPENAI_API_KEY:
        return jsonify({"error": "OPENAI_API_KEY not configured on this Pi"}), 503

    data    = request.get_json(silent=True) or {}
    img_b64 = (data.get("image") or "").strip()
    if not img_b64:
        return jsonify({"error": "image (base64) is required"}), 400

    # Remove data-URI prefix if present
    if "," in img_b64:
        img_b64 = img_b64.split(",", 1)[1]

    try:
        resp = _requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {_OPENAI_API_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "model": "gpt-4o",
                "max_tokens": 200,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "You are a Pokémon TCG expert. Identify the card in this photo. "
                                    "Reply ONLY with a compact JSON object with these keys: "
                                    "name (card name, e.g. 'Charizard'), "
                                    "set_code (e.g. 'SV1'), "
                                    "number (collector number e.g. '4'), "
                                    "rarity (e.g. 'Rare Holo'), "
                                    "condition (NM/LP/MP/HP/DMG). "
                                    "If you cannot identify the card return {\"error\": \"unidentified\"}."
                                ),
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{img_b64}",
                                    "detail": "high",
                                },
                            },
                        ],
                    }
                ],
            },
            timeout=30,
        )
        resp.raise_for_status()
        raw_text = resp.json()["choices"][0]["message"]["content"].strip()
        # Strip markdown fences if GPT wraps it
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        identified = json.loads(raw_text)
    except json.JSONDecodeError:
        return jsonify({"error": "GPT response not valid JSON", "raw": raw_text}), 502
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    if "error" in identified:
        return jsonify({"identified": False, "reason": identified["error"]}), 200

    # Build a QR/lookup code from set+number, e.g. "SV1-4"
    set_c  = (identified.get("set_code") or "").upper()
    number = (identified.get("number") or "").strip()
    qr_guess = f"{set_c}-{number}" if set_c and number else ""

    enriched = _enrich_with_tcg(None, qr_guess) if qr_guess else {}

    return jsonify({
        "identified":  True,
        "gpt":         identified,
        "qr_guess":    qr_guess,
        "enriched":    enriched,
    })


# ---------------------------------------------------------------------------
# Feature 2a: Stock Check API — lets Inventory-Scanner app query live stock
# ---------------------------------------------------------------------------

@app.route("/api/stock-check", methods=["GET"])
def api_stock_check():
    """
    GET /api/stock-check?codes=SV1-1,SV1-2,PKM-abc
    Returns a dict of qr_code → {name, stock, price} for each requested code.
    No auth required — read-only, public info.
    """
    raw    = request.args.get("codes", "")
    codes  = [c.strip() for c in raw.split(",") if c.strip()]
    if not codes:
        return jsonify({"error": "codes param required"}), 400
    if len(codes) > 100:
        return jsonify({"error": "max 100 codes per request"}), 400

    db = get_db()
    placeholders = ",".join(["%s"] * len(codes))
    rows = db.execute(
        f"SELECT qr_code, name, stock, price FROM inventory WHERE qr_code IN ({placeholders})",
        tuple(codes)
    ).fetchall()

    result = {}
    for r in rows:
        result[r["qr_code"]] = {
            "name":  r["name"],
            "stock": r["stock"],
            "price": r["price"],
        }
    # Fill in codes not found
    for c in codes:
        if c not in result:
            result[c] = {"name": None, "stock": None, "price": None}

    return jsonify(result)


# ---------------------------------------------------------------------------
# Feature 2b: Real-time single-item push from Scanner to POS inventory
# ---------------------------------------------------------------------------

@app.route("/api/push-scan", methods=["POST"])
def api_push_scan():
    """
    POST /api/push-scan
    Body: { "qr_code": "SV1-4", "name": "Charizard", "price": 12.50,
            "category": "Pokemon", "description": "...", "stock_delta": 1 }
    Upserts the card into POS inventory and increments stock by stock_delta (default 1).
    Requires the session API key in header X-Api-Key or body field api_key.
    """
    data     = request.get_json(silent=True) or {}
    api_key  = (request.headers.get("X-Api-Key") or data.get("api_key") or "").strip()
    qr_code  = (data.get("qr_code") or "").strip()
    name     = (data.get("name") or "").strip()
    price    = float(data.get("price") or 0)
    category = (data.get("category") or "General").strip()
    desc     = (data.get("description") or "").strip()
    delta    = max(1, int(data.get("stock_delta") or 1))

    if not qr_code or not name:
        return jsonify({"error": "qr_code and name are required"}), 400

    # Validate API key — must match an existing scanner session key
    db = get_db()
    try:
        key_row = db.execute(
            "SELECT id FROM sessions WHERE api_key = %s LIMIT 1", (api_key,)
        ).fetchone()
    except Exception:
        key_row = None  # sessions table lives in the scanner DB, allow open if no table

    # If we have session validation available and key is wrong, reject
    if api_key and key_row is None:
        # Try falling back — let it through if no sessions table exists on POS
        try:
            db.execute("SELECT 1 FROM sessions LIMIT 1")
            return jsonify({"error": "invalid api_key"}), 401
        except Exception:
            pass  # sessions table not on this DB — open access

    db.execute("""
        INSERT INTO inventory (qr_code, name, price, category, description, stock, last_updated)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(qr_code) DO UPDATE SET
            stock        = inventory.stock + excluded.stock,
            last_updated = excluded.last_updated
    """, (qr_code, name, price, category, desc, delta, _now_ms()))
    db.commit()
    _invalidate_inventory(qr_code)  # targeted eviction — only this QR

    row = db.execute(
        "SELECT stock FROM inventory WHERE qr_code = %s", (qr_code,)
    ).fetchone()
    new_stock = row["stock"] if row else delta

    return jsonify({"ok": True, "qr_code": qr_code, "new_stock": new_stock})


# ---------------------------------------------------------------------------
# Feature 3: Trade-in Flow
# ---------------------------------------------------------------------------

def _trade_in_ref():
    return f"TI-{_now_ms()}"


@app.route("/admin/trade-in", methods=["GET"])
@require_admin
def admin_trade_in_list():
    db    = get_db()
    open_ = db.execute(
        "SELECT * FROM trade_ins WHERE status='open' ORDER BY created_at DESC LIMIT 50"
    ).fetchall()
    done  = db.execute(
        "SELECT * FROM trade_ins WHERE status!='open' ORDER BY created_at DESC LIMIT 30"
    ).fetchall()
    nav   = _admin_nav("trade-in")
    css   = _admin_css()

    def _row(t, show_actions=True):
        ts     = datetime.datetime.fromtimestamp(t["created_at"] / 1000).strftime("%d/%m/%Y %H:%M")
        status = t["status"].upper()
        col    = "#4ade80" if t["status"] == "open" else ("#f87171" if t["status"] == "cancelled" else "#facc15")
        btn    = (
            f'<button class="btn-gold" onclick="openTi({t["id"]})" style="background:#2563eb;margin-right:6px">▶ Open</button>'
            f'<button class="btn-gold" onclick="cancelTi({t["id"]})" style="background:#7f1d1d;font-size:11px">✕ Cancel</button>'
            if show_actions else ""
        )
        return (
            f"<tr><td><b>{t['reference']}</b></td><td>{t['customer']}</td>"
            f"<td style='color:{col}'>{status}</td>"
            f"<td style='color:#facc15'>${t['total_value']:.2f}</td>"
            f"<td style='color:#888;font-size:12px'>{ts}</td>"
            f"<td>{btn}</td></tr>"
        )

    open_rows = "".join(_row(t) for t in open_) or "<tr><td colspan='6' style='color:#666'>No open trade-ins</td></tr>"
    done_rows = "".join(_row(t, False) for t in done) or "<tr><td colspan='5' style='color:#666'>No history yet</td></tr>"

    return render_template_string(f"""<!DOCTYPE html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trade-In | HanryxVault POS</title>{css}</head><body>
{nav}
<div class="admin-content">
<h1 style="color:#facc15">🔁 Trade-In Manager</h1>

<div class="form-panel" style="border-color:#4ade80;background:#001a05;margin-bottom:20px">
<h2 style="color:#4ade80">New Trade-In</h2>
<div style="display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end">
  <div>
    <label style="color:#aaa;font-size:12px">Customer Name</label><br>
    <input id="ti-customer" type="text" placeholder="Walk-in" style="width:200px">
  </div>
  <div>
    <label style="color:#aaa;font-size:12px">Notes</label><br>
    <input id="ti-notes" type="text" placeholder="Optional notes" style="width:250px">
  </div>
  <button class="btn-gold" onclick="createTi()" style="background:#4ade80;color:#000">+ Create Trade-In</button>
</div>
</div>

<h2 style="color:#4ade80">Open Trade-Ins</h2>
<table><thead><tr><th>Reference</th><th>Customer</th><th>Status</th><th>Total Value</th><th>Created</th><th>Actions</th></tr></thead>
<tbody>{open_rows}</tbody></table>

<h2 style="color:#888;margin-top:24px">History (Last 30)</h2>
<table><thead><tr><th>Reference</th><th>Customer</th><th>Status</th><th>Total Value</th><th>Date</th></tr></thead>
<tbody>{done_rows}</tbody></table>
</div>

<!-- Trade-in detail modal -->
<div id="ti-modal" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:#000a;z-index:1000;overflow:auto">
  <div style="max-width:760px;margin:40px auto;background:#111;border:1px solid #333;border-radius:8px;padding:24px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <h2 id="ti-modal-title" style="color:#facc15;margin:0">Trade-In</h2>
      <button onclick="document.getElementById('ti-modal').style.display='none'" style="background:none;border:none;color:#aaa;font-size:22px;cursor:pointer">✕</button>
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px">
      <input id="ti-qr" type="text" placeholder="QR Code / Card Code" style="flex:1;min-width:160px">
      <input id="ti-name" type="text" placeholder="Card Name" style="flex:2;min-width:180px">
      <select id="ti-cond" style="min-width:80px"><option>NM</option><option>LP</option><option>MP</option><option>HP</option><option>DMG</option></select>
      <input id="ti-offered" type="number" step="0.01" placeholder="$ Offer" style="width:90px">
      <input id="ti-market" type="number" step="0.01" placeholder="$ Market" style="width:90px">
      <button class="btn-gold" onclick="addTiItem()" style="background:#4ade80;color:#000">+ Add</button>
    </div>
    <table id="ti-items-table" style="margin-bottom:16px">
      <thead><tr><th>Card</th><th>Condition</th><th>Offer</th><th>Market</th><th></th></tr></thead>
      <tbody id="ti-items-body"><tr><td colspan='5' style='color:#666'>No items yet</td></tr></tbody>
    </table>
    <div style="display:flex;justify-content:space-between;align-items:center">
      <div style="font-size:16px;color:#facc15">Total: $<span id="ti-total">0.00</span></div>
      <button class="btn-gold" id="ti-complete-btn" onclick="completeTi()" style="background:#4ade80;color:#000;font-size:15px">✅ Complete Trade-In (Add to Inventory)</button>
    </div>
    <div id="ti-msg" style="margin-top:10px;font-size:13px;color:#aaa"></div>
  </div>
</div>

<script>
let _activeTiId = null;
let _activeTiItems = [];

async function createTi() {{
  const customer = document.getElementById('ti-customer').value.trim() || 'Walk-in';
  const notes    = document.getElementById('ti-notes').value.trim();
  const r = await fetch('/admin/trade-in/create', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{customer, notes}})
  }});
  const d = await r.json();
  if (d.error) {{ alert(d.error); return; }}
  openTi(d.id);
}}

async function openTi(id) {{
  _activeTiId = id;
  const r = await fetch('/admin/trade-in/' + id);
  const d = await r.json();
  document.getElementById('ti-modal-title').textContent = d.reference + ' — ' + d.customer;
  _activeTiItems = d.items || [];
  renderTiItems();
  document.getElementById('ti-modal').style.display = 'block';
}}

function renderTiItems() {{
  const tbody = document.getElementById('ti-items-body');
  if (!_activeTiItems.length) {{
    tbody.innerHTML = "<tr><td colspan='5' style='color:#666'>No items yet</td></tr>";
    document.getElementById('ti-total').textContent = '0.00';
    return;
  }}
  let total = 0;
  tbody.innerHTML = _activeTiItems.map(it => {{
    total += parseFloat(it.offered_price || 0);
    return `<tr>
      <td><b>${{it.name}}</b><br><small style="color:#888">${{it.qr_code}}</small></td>
      <td>${{it.condition}}</td>
      <td style="color:#4ade80">$${{parseFloat(it.offered_price).toFixed(2)}}</td>
      <td style="color:#aaa">$${{parseFloat(it.market_price||0).toFixed(2)}}</td>
      <td><button onclick="removeTiItem(${{it.id}})" style="background:none;border:1px solid #7f1d1d;color:#f87171;border-radius:4px;cursor:pointer;padding:2px 8px">✕</button></td>
    </tr>`;
  }}).join('');
  document.getElementById('ti-total').textContent = total.toFixed(2);
}}

async function addTiItem() {{
  if (!_activeTiId) return;
  const qr_code      = document.getElementById('ti-qr').value.trim();
  const name         = document.getElementById('ti-name').value.trim();
  const condition    = document.getElementById('ti-cond').value;
  const offered_price = parseFloat(document.getElementById('ti-offered').value) || 0;
  const market_price  = parseFloat(document.getElementById('ti-market').value) || 0;
  if (!qr_code || !name) {{ alert('QR code and name are required'); return; }}
  const r = await fetch('/admin/trade-in/' + _activeTiId + '/add-item', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{qr_code, name, condition, offered_price, market_price}})
  }});
  const d = await r.json();
  if (d.error) {{ alert(d.error); return; }}
  _activeTiItems = d.items;
  renderTiItems();
  ['ti-qr','ti-name','ti-offered','ti-market'].forEach(id => document.getElementById(id).value = '');
}}

async function removeTiItem(itemId) {{
  if (!_activeTiId) return;
  const r = await fetch('/admin/trade-in/' + _activeTiId + '/remove-item/' + itemId, {{method:'POST'}});
  const d = await r.json();
  if (d.error) {{ alert(d.error); return; }}
  _activeTiItems = d.items;
  renderTiItems();
}}

async function completeTi() {{
  if (!_activeTiId || !_activeTiItems.length) {{ alert('Add at least one item first'); return; }}
  if (!confirm('Complete this trade-in? All accepted cards will be added to your POS inventory.')) return;
  const r = await fetch('/admin/trade-in/' + _activeTiId + '/complete', {{method:'POST'}});
  const d = await r.json();
  const msg = document.getElementById('ti-msg');
  if (d.error) {{ msg.textContent = '❌ ' + d.error; msg.style.color='#f87171'; return; }}
  msg.textContent = '✅ Done! ' + d.added + ' card(s) added to inventory.';
  msg.style.color = '#4ade80';
  setTimeout(() => location.reload(), 1500);
}}

async function cancelTi(id) {{
  if (!confirm('Cancel this trade-in?')) return;
  const r = await fetch('/admin/trade-in/' + id + '/cancel', {{method:'POST'}});
  if (r.ok) location.reload();
}}
</script>
</body></html>""")


@app.route("/admin/trade-in/create", methods=["POST"])
@require_admin
def admin_trade_in_create():
    data     = request.get_json(silent=True) or {}
    customer = (data.get("customer") or "Walk-in").strip()[:100]
    notes    = (data.get("notes") or "").strip()[:500]
    ref      = _trade_in_ref()
    db       = get_db()
    row = db.execute(
        "INSERT INTO trade_ins (reference, customer, notes) VALUES (%s,%s,%s) RETURNING id",
        (ref, customer, notes)
    ).fetchone()
    db.commit()
    return jsonify({"ok": True, "id": row["id"], "reference": ref})


@app.route("/admin/trade-in/<int:ti_id>", methods=["GET"])
@require_admin
def admin_trade_in_get(ti_id):
    db  = get_db()
    ti  = db.execute("SELECT * FROM trade_ins WHERE id=%s", (ti_id,)).fetchone()
    if not ti:
        return jsonify({"error": "Not found"}), 404
    items = db.execute(
        "SELECT * FROM trade_in_items WHERE trade_in_id=%s ORDER BY id", (ti_id,)
    ).fetchall()
    return jsonify({
        "id":          ti["id"],
        "reference":   ti["reference"],
        "customer":    ti["customer"],
        "status":      ti["status"],
        "total_value": ti["total_value"],
        "notes":       ti["notes"],
        "items":       [dict(i) for i in items],
    })


@app.route("/admin/trade-in/<int:ti_id>/add-item", methods=["POST"])
@require_admin
def admin_trade_in_add_item(ti_id):
    db   = get_db()
    ti   = db.execute("SELECT * FROM trade_ins WHERE id=%s AND status='open'", (ti_id,)).fetchone()
    if not ti:
        return jsonify({"error": "Trade-in not found or not open"}), 404
    data          = request.get_json(silent=True) or {}
    qr_code       = (data.get("qr_code") or "").strip()
    name          = (data.get("name") or "").strip()
    condition     = (data.get("condition") or "NM").strip()
    offered_price = float(data.get("offered_price") or 0)
    market_price  = float(data.get("market_price") or 0)
    if not qr_code or not name:
        return jsonify({"error": "qr_code and name required"}), 400
    db.execute(
        "INSERT INTO trade_in_items (trade_in_id,qr_code,name,condition,offered_price,market_price) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (ti_id, qr_code, name, condition, offered_price, market_price)
    )
    # Recalculate total
    total = db.execute(
        "SELECT COALESCE(SUM(offered_price),0) FROM trade_in_items WHERE trade_in_id=%s AND accepted=1",
        (ti_id,)
    ).fetchone()[0]
    db.execute("UPDATE trade_ins SET total_value=%s WHERE id=%s", (total, ti_id))
    db.commit()
    items = db.execute("SELECT * FROM trade_in_items WHERE trade_in_id=%s ORDER BY id", (ti_id,)).fetchall()
    return jsonify({"ok": True, "items": [dict(i) for i in items]})


@app.route("/admin/trade-in/<int:ti_id>/remove-item/<int:item_id>", methods=["POST"])
@require_admin
def admin_trade_in_remove_item(ti_id, item_id):
    db = get_db()
    db.execute("DELETE FROM trade_in_items WHERE id=%s AND trade_in_id=%s", (item_id, ti_id))
    total = db.execute(
        "SELECT COALESCE(SUM(offered_price),0) FROM trade_in_items WHERE trade_in_id=%s AND accepted=1",
        (ti_id,)
    ).fetchone()[0]
    db.execute("UPDATE trade_ins SET total_value=%s WHERE id=%s", (total, ti_id))
    db.commit()
    items = db.execute("SELECT * FROM trade_in_items WHERE trade_in_id=%s ORDER BY id", (ti_id,)).fetchall()
    return jsonify({"ok": True, "items": [dict(i) for i in items]})


@app.route("/admin/trade-in/<int:ti_id>/complete", methods=["POST"])
@require_admin
def admin_trade_in_complete(ti_id):
    db   = get_db()
    ti   = db.execute("SELECT * FROM trade_ins WHERE id=%s AND status='open'", (ti_id,)).fetchone()
    if not ti:
        return jsonify({"error": "Trade-in not found or not open"}), 404
    items = db.execute(
        "SELECT * FROM trade_in_items WHERE trade_in_id=%s AND accepted=1", (ti_id,)
    ).fetchall()
    if not items:
        return jsonify({"error": "No accepted items in this trade-in"}), 400

    added = 0
    for it in items:
        db.execute("""
            INSERT INTO inventory (qr_code, name, price, category, stock, last_updated)
            VALUES (%s, %s, %s, 'Trade-In', 1, %s)
            ON CONFLICT(qr_code) DO UPDATE SET
                stock        = inventory.stock + 1,
                last_updated = excluded.last_updated
        """, (it["qr_code"], it["name"], it["offered_price"], _now_ms()))
        db.execute("""
            INSERT INTO card_conditions (qr_code, condition, notes, updated_ms)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT(qr_code) DO UPDATE SET
                condition  = excluded.condition,
                notes      = excluded.notes,
                updated_ms = excluded.updated_ms
        """, (it["qr_code"], it["condition"], f"Trade-in {ti['reference']}", _now_ms()))
        added += 1

    db.execute(
        "UPDATE trade_ins SET status='completed', completed_at=%s WHERE id=%s",
        (_now_ms(), ti_id)
    )
    db.commit()
    _invalidate_inventory()
    return jsonify({"ok": True, "added": added})


@app.route("/admin/trade-in/<int:ti_id>/cancel", methods=["POST"])
@require_admin
def admin_trade_in_cancel(ti_id):
    db = get_db()
    db.execute(
        "UPDATE trade_ins SET status='cancelled' WHERE id=%s AND status='open'", (ti_id,)
    )
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Feature 4: Deck / Bundle Checkout
# ---------------------------------------------------------------------------

@app.route("/admin/bundles", methods=["GET"])
@require_admin
def admin_bundles():
    db      = get_db()
    bundles = db.execute(
        "SELECT b.*, COUNT(bi.id) as item_count "
        "FROM bundles b LEFT JOIN bundle_items bi ON bi.bundle_id=b.id "
        "GROUP BY b.id ORDER BY b.created_at DESC LIMIT 100"
    ).fetchall()
    nav     = _admin_nav("bundles")
    css     = _admin_css()

    def _brow(b):
        ts  = datetime.datetime.fromtimestamp(b["created_at"] / 1000).strftime("%d/%m/%Y")
        return (
            f"<tr>"
            f"<td><b>{_html.escape(b['name'])}</b><br><small style='color:#888'>{_html.escape(b['description'])}</small></td>"
            f"<td style='color:#facc15'>${b['bundle_price']:.2f}</td>"
            f"<td style='text-align:center'>{b['item_count']}</td>"
            f"<td style='text-align:center'>{b['sold']}</td>"
            f"<td style='color:#888;font-size:12px'>{ts}</td>"
            f"<td>"
            f"  <button class='btn-gold' onclick='openBundle({b[\"id\"]})' style='background:#2563eb;margin-right:4px'>▶ Manage</button>"
            f"  <button class='btn-gold' onclick='sellBundle({b[\"id\"]},{b[\"bundle_price\"]:.2f})' style='background:#4ade80;color:#000;margin-right:4px'>💳 Sell</button>"
            f"  <button class='btn-gold' onclick='deleteBundle({b[\"id\"]})' style='background:#7f1d1d;font-size:11px'>✕</button>"
            f"</td>"
            f"</tr>"
        )

    bundle_rows = "".join(_brow(b) for b in bundles) or "<tr><td colspan='6' style='color:#666'>No bundles yet — create one below</td></tr>"

    return render_template_string(f"""<!DOCTYPE html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bundles | HanryxVault POS</title>{css}</head><body>
{nav}
<div class="admin-content">
<h1 style="color:#facc15">📦 Deck & Bundle Builder</h1>

<!-- Create bundle -->
<div class="form-panel" style="border-color:#facc15;background:#0a0800;margin-bottom:20px">
<h2 style="color:#facc15">Create New Bundle</h2>
<div style="display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end">
  <div><label style="color:#aaa;font-size:12px">Bundle Name</label><br>
    <input id="b-name" type="text" placeholder="e.g. Starter Deck" style="width:220px"></div>
  <div><label style="color:#aaa;font-size:12px">Description</label><br>
    <input id="b-desc" type="text" placeholder="Optional" style="width:280px"></div>
  <div><label style="color:#aaa;font-size:12px">Bundle Price ($)</label><br>
    <input id="b-price" type="number" step="0.01" placeholder="0.00" style="width:110px"></div>
  <button class="btn-gold" onclick="createBundle()" style="background:#facc15;color:#000">+ Create Bundle</button>
</div>
</div>

<!-- Bundle list -->
<table>
<thead><tr><th>Bundle</th><th>Price</th><th>Cards</th><th>Sold</th><th>Created</th><th>Actions</th></tr></thead>
<tbody>{bundle_rows}</tbody>
</table>
</div>

<!-- Bundle detail modal -->
<div id="b-modal" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:#000a;z-index:1000;overflow:auto">
  <div style="max-width:800px;margin:40px auto;background:#111;border:1px solid #333;border-radius:8px;padding:24px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <h2 id="b-modal-title" style="color:#facc15;margin:0">Bundle</h2>
      <button onclick="document.getElementById('b-modal').style.display='none'" style="background:none;border:none;color:#aaa;font-size:22px;cursor:pointer">✕</button>
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px;align-items:flex-end">
      <div>
        <label style="color:#aaa;font-size:12px">Card QR Code</label><br>
        <input id="b-item-qr" type="text" placeholder="e.g. SV1-4" style="width:160px" oninput="autoFillBundleCard(this.value)">
      </div>
      <div>
        <label style="color:#aaa;font-size:12px">Card Name</label><br>
        <input id="b-item-name" type="text" placeholder="Auto-filled from inventory" style="width:220px">
      </div>
      <div>
        <label style="color:#aaa;font-size:12px">Qty</label><br>
        <input id="b-item-qty" type="number" value="1" min="1" style="width:60px">
      </div>
      <div>
        <label style="color:#aaa;font-size:12px">Unit Price ($)</label><br>
        <input id="b-item-price" type="number" step="0.01" value="0" style="width:90px">
      </div>
      <button class="btn-gold" onclick="addBundleItem()" style="background:#facc15;color:#000">+ Add Card</button>
    </div>
    <table id="b-items-table" style="margin-bottom:16px">
      <thead><tr><th>Card</th><th>Qty</th><th>Unit Price</th><th>Line Total</th><th></th></tr></thead>
      <tbody id="b-items-body"><tr><td colspan='5' style='color:#666'>No items yet</td></tr></tbody>
    </table>
    <div style="display:flex;justify-content:space-between;align-items:center">
      <div>
        <span style="color:#aaa;font-size:13px">Individual card total:</span>
        <span style="color:#fff" id="b-card-total">$0.00</span>
        &nbsp;|&nbsp;
        <span style="color:#aaa;font-size:13px">Bundle price:</span>
        <span style="color:#facc15;font-weight:bold" id="b-bundle-price-display">—</span>
      </div>
      <button class="btn-gold" id="b-sell-btn" onclick="sellActiveBundle()" style="background:#4ade80;color:#000;font-size:15px">💳 Sell This Bundle</button>
    </div>
    <div id="b-msg" style="margin-top:10px;font-size:13px;color:#aaa"></div>
  </div>
</div>

<!-- Sell confirm modal -->
<div id="sell-modal" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:#000c;z-index:2000;display:flex;align-items:center;justify-content:center">
  <div style="background:#111;border:1px solid #facc15;border-radius:8px;padding:28px;max-width:400px;width:90%">
    <h2 style="color:#facc15;margin-top:0">💳 Sell Bundle</h2>
    <p id="sell-modal-desc" style="color:#ccc"></p>
    <div style="margin-bottom:12px">
      <label style="color:#aaa;font-size:12px">Payment Method</label><br>
      <select id="sell-method" style="width:100%">
        <option value="CASH">Cash</option>
        <option value="CARD">Card / Zettle</option>
        <option value="MIXED">Mixed</option>
      </select>
    </div>
    <div id="sell-cash-row" style="margin-bottom:12px;display:none">
      <label style="color:#aaa;font-size:12px">Cash Received ($)</label><br>
      <input id="sell-cash" type="number" step="0.01" placeholder="0.00" style="width:100%">
    </div>
    <div style="display:flex;gap:10px">
      <button class="btn-gold" onclick="confirmSellBundle()" style="background:#4ade80;color:#000;flex:1">✅ Confirm Sale</button>
      <button class="btn-gold" onclick="document.getElementById('sell-modal').style.display='none'" style="background:#333;flex:1">Cancel</button>
    </div>
    <div id="sell-msg" style="margin-top:10px;font-size:13px;color:#aaa"></div>
  </div>
</div>

<script>
let _activeBundleId   = null;
let _activeBundleItems = [];
let _activeBundlePrice = 0;
let _sellBundleId = null;

document.getElementById('sell-method').addEventListener('change', function() {{
  document.getElementById('sell-cash-row').style.display = this.value === 'CASH' ? 'block' : 'none';
}});

async function createBundle() {{
  const name  = document.getElementById('b-name').value.trim();
  const desc  = document.getElementById('b-desc').value.trim();
  const price = parseFloat(document.getElementById('b-price').value) || 0;
  if (!name) {{ alert('Bundle name is required'); return; }}
  const r = await fetch('/admin/bundles/create', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{name, description: desc, bundle_price: price}})
  }});
  const d = await r.json();
  if (d.error) {{ alert(d.error); return; }}
  openBundle(d.id);
}}

async function openBundle(id) {{
  _activeBundleId = id;
  const r = await fetch('/admin/bundles/' + id);
  const d = await r.json();
  document.getElementById('b-modal-title').textContent = d.name;
  _activeBundleItems = d.items || [];
  _activeBundlePrice = d.bundle_price;
  document.getElementById('b-bundle-price-display').textContent = '$' + d.bundle_price.toFixed(2);
  renderBundleItems();
  document.getElementById('b-modal').style.display = 'block';
}}

function renderBundleItems() {{
  const tbody = document.getElementById('b-items-body');
  if (!_activeBundleItems.length) {{
    tbody.innerHTML = "<tr><td colspan='5' style='color:#666'>No cards yet</td></tr>";
    document.getElementById('b-card-total').textContent = '$0.00';
    return;
  }}
  let total = 0;
  tbody.innerHTML = _activeBundleItems.map(it => {{
    const line = it.quantity * it.unit_price;
    total += line;
    return `<tr>
      <td><b>${{it.name}}</b><br><small style="color:#888">${{it.qr_code}}</small></td>
      <td style="text-align:center">${{it.quantity}}</td>
      <td style="color:#facc15">$${{parseFloat(it.unit_price).toFixed(2)}}</td>
      <td style="color:#fff">$${{line.toFixed(2)}}</td>
      <td><button onclick="removeBundleItem(${{it.id}})" style="background:none;border:1px solid #7f1d1d;color:#f87171;border-radius:4px;cursor:pointer;padding:2px 8px">✕</button></td>
    </tr>`;
  }}).join('');
  document.getElementById('b-card-total').textContent = '$' + total.toFixed(2);
}}

async function autoFillBundleCard(qr) {{
  if (qr.length < 3) return;
  try {{
    const r = await fetch('/api/stock-check?codes=' + encodeURIComponent(qr));
    const d = await r.json();
    if (d[qr] && d[qr].name) {{
      document.getElementById('b-item-name').value  = d[qr].name;
      document.getElementById('b-item-price').value = d[qr].price || 0;
    }}
  }} catch(e) {{}}
}}

async function addBundleItem() {{
  if (!_activeBundleId) return;
  const qr_code    = document.getElementById('b-item-qr').value.trim();
  const name       = document.getElementById('b-item-name').value.trim();
  const quantity   = parseInt(document.getElementById('b-item-qty').value) || 1;
  const unit_price = parseFloat(document.getElementById('b-item-price').value) || 0;
  if (!qr_code || !name) {{ alert('QR code and name are required'); return; }}
  const r = await fetch('/admin/bundles/' + _activeBundleId + '/add-item', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{qr_code, name, quantity, unit_price}})
  }});
  const d = await r.json();
  if (d.error) {{ alert(d.error); return; }}
  _activeBundleItems = d.items;
  renderBundleItems();
  ['b-item-qr','b-item-name','b-item-price'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('b-item-qty').value = 1;
}}

async function removeBundleItem(itemId) {{
  if (!_activeBundleId) return;
  const r = await fetch('/admin/bundles/' + _activeBundleId + '/remove-item/' + itemId, {{method:'POST'}});
  const d = await r.json();
  if (d.error) {{ alert(d.error); return; }}
  _activeBundleItems = d.items;
  renderBundleItems();
}}

function sellBundle(id, price) {{
  _sellBundleId = id;
  document.getElementById('sell-modal-desc').textContent = 'Bundle price: $' + price.toFixed(2);
  document.getElementById('sell-msg').textContent = '';
  document.getElementById('sell-modal').style.display = 'flex';
}}

function sellActiveBundle() {{
  if (_activeBundleId) sellBundle(_activeBundleId, _activeBundlePrice);
}}

async function confirmSellBundle() {{
  if (!_sellBundleId) return;
  const method = document.getElementById('sell-method').value;
  const cash   = parseFloat(document.getElementById('sell-cash').value) || 0;
  const r = await fetch('/admin/bundles/' + _sellBundleId + '/sell', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{payment_method: method, cash_received: cash}})
  }});
  const d = await r.json();
  const msg = document.getElementById('sell-msg');
  if (d.error) {{ msg.textContent = '❌ ' + d.error; msg.style.color='#f87171'; return; }}
  msg.textContent = '✅ Sale complete! Change: $' + (d.change||0).toFixed(2);
  msg.style.color = '#4ade80';
  setTimeout(() => location.reload(), 1600);
}}

async function deleteBundle(id) {{
  if (!confirm('Delete this bundle? This cannot be undone.')) return;
  const r = await fetch('/admin/bundles/' + id, {{method:'DELETE'}});
  if (r.ok) location.reload();
}}
</script>
</body></html>""")


@app.route("/admin/bundles/create", methods=["POST"])
@require_admin
def admin_bundles_create():
    data         = request.get_json(silent=True) or {}
    name         = (data.get("name") or "").strip()[:120]
    description  = (data.get("description") or "").strip()[:500]
    bundle_price = float(data.get("bundle_price") or 0)
    if not name:
        return jsonify({"error": "name is required"}), 400
    db  = get_db()
    row = db.execute(
        "INSERT INTO bundles (name, description, bundle_price) VALUES (%s,%s,%s) RETURNING id",
        (name, description, bundle_price)
    ).fetchone()
    db.commit()
    return jsonify({"ok": True, "id": row["id"]})


@app.route("/admin/bundles/<int:bundle_id>", methods=["GET"])
@require_admin
def admin_bundle_get(bundle_id):
    db  = get_db()
    b   = db.execute("SELECT * FROM bundles WHERE id=%s", (bundle_id,)).fetchone()
    if not b:
        return jsonify({"error": "Not found"}), 404
    items = db.execute(
        "SELECT * FROM bundle_items WHERE bundle_id=%s ORDER BY id", (bundle_id,)
    ).fetchall()
    return jsonify({
        "id":           b["id"],
        "name":         b["name"],
        "description":  b["description"],
        "bundle_price": b["bundle_price"],
        "sold":         b["sold"],
        "items":        [dict(i) for i in items],
    })


@app.route("/admin/bundles/<int:bundle_id>/add-item", methods=["POST"])
@require_admin
def admin_bundle_add_item(bundle_id):
    db  = get_db()
    b   = db.execute("SELECT id FROM bundles WHERE id=%s", (bundle_id,)).fetchone()
    if not b:
        return jsonify({"error": "Bundle not found"}), 404
    data       = request.get_json(silent=True) or {}
    qr_code    = (data.get("qr_code") or "").strip()
    name       = (data.get("name") or "").strip()
    quantity   = max(1, int(data.get("quantity") or 1))
    unit_price = float(data.get("unit_price") or 0)
    if not qr_code or not name:
        return jsonify({"error": "qr_code and name required"}), 400
    db.execute(
        "INSERT INTO bundle_items (bundle_id,qr_code,name,quantity,unit_price) VALUES (%s,%s,%s,%s,%s)",
        (bundle_id, qr_code, name, quantity, unit_price)
    )
    db.commit()
    items = db.execute("SELECT * FROM bundle_items WHERE bundle_id=%s ORDER BY id", (bundle_id,)).fetchall()
    return jsonify({"ok": True, "items": [dict(i) for i in items]})


@app.route("/admin/bundles/<int:bundle_id>/remove-item/<int:item_id>", methods=["POST"])
@require_admin
def admin_bundle_remove_item(bundle_id, item_id):
    db = get_db()
    db.execute("DELETE FROM bundle_items WHERE id=%s AND bundle_id=%s", (item_id, bundle_id))
    db.commit()
    items = db.execute("SELECT * FROM bundle_items WHERE bundle_id=%s ORDER BY id", (bundle_id,)).fetchall()
    return jsonify({"ok": True, "items": [dict(i) for i in items]})


@app.route("/admin/bundles/<int:bundle_id>/sell", methods=["POST"])
@require_admin
def admin_bundle_sell(bundle_id):
    db   = get_db()
    b    = db.execute("SELECT * FROM bundles WHERE id=%s", (bundle_id,)).fetchone()
    if not b:
        return jsonify({"error": "Bundle not found"}), 404
    items = db.execute(
        "SELECT * FROM bundle_items WHERE bundle_id=%s", (bundle_id,)
    ).fetchall()
    if not items:
        return jsonify({"error": "Bundle has no items"}), 400

    # Check stock for every item
    for it in items:
        inv = db.execute(
            "SELECT stock FROM inventory WHERE qr_code=%s", (it["qr_code"],)
        ).fetchone()
        if not inv or inv["stock"] < it["quantity"]:
            return jsonify({"error": f"Insufficient stock for {it['name']} ({it['qr_code']})"}), 409

    data           = request.get_json(silent=True) or {}
    payment_method = (data.get("payment_method") or "CASH").upper()
    cash_received  = float(data.get("cash_received") or b["bundle_price"])
    total          = b["bundle_price"]
    change         = max(0.0, cash_received - total) if payment_method == "CASH" else 0.0

    # Deduct stock + record deductions
    tid = f"BUNDLE-{_now_ms()}"
    sale_items = []
    for it in items:
        db.execute(
            "UPDATE inventory SET stock=stock-%s, last_updated=%s WHERE qr_code=%s",
            (it["quantity"], _now_ms(), it["qr_code"])
        )
        db.execute("""
            INSERT INTO stock_deductions (transaction_id,qr_code,name,quantity,unit_price,line_total)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (tid, it["qr_code"], it["name"], it["quantity"],
              it["unit_price"], it["quantity"] * it["unit_price"]))
        sale_items.append({
            "qrCode": it["qr_code"], "name": it["name"],
            "qty": it["quantity"], "unitPrice": it["unit_price"],
        })

    db.execute("""
        INSERT INTO sales (transaction_id,timestamp_ms,subtotal,tax_amount,tip_amount,
                           total_amount,payment_method,employee_id,items_json,
                           cash_received,change_given,source)
        VALUES (%s,%s,%s,0,0,%s,%s,%s,%s,%s,%s,%s)
    """, (
        tid, _now_ms(), total, total,
        payment_method, "bundle-sell",
        json.dumps(sale_items),
        cash_received, change, "bundle",
    ))
    db.execute("UPDATE bundles SET sold=sold+1 WHERE id=%s", (bundle_id,))
    db.commit()
    _invalidate_inventory()

    return jsonify({
        "ok": True, "transaction_id": tid,
        "total": total, "change": change,
    })


@app.route("/admin/bundles/<int:bundle_id>", methods=["DELETE"])
@require_admin
def admin_bundle_delete(bundle_id):
    db = get_db()
    db.execute("DELETE FROM bundles WHERE id=%s", (bundle_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Admin — satellite token management
# ---------------------------------------------------------------------------

@app.route("/admin/set-satellite-token", methods=["POST"])
@require_admin
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
        "INSERT INTO server_state (key, value) VALUES ('satellite_token', %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (token,)
    )
    db.commit()
    # Invalidate health cache so the next /health shows updated state
    _cache_set(_health_cache, "h", None)
    return jsonify({"ok": True, "message": "Satellite token saved — sync auth is now active"}), 200


@app.route("/admin/satellite-token-status", methods=["GET"])
@require_admin
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
@require_admin
def admin_sales():
    db   = get_db()
    rows = db.execute("SELECT * FROM sales ORDER BY timestamp_ms DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/admin/inventory", methods=["GET"])
@require_admin
def admin_inventory_json():
    db   = get_db()
    rows = db.execute("SELECT * FROM inventory ORDER BY name ASC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/admin/inventory", methods=["POST"])
@require_admin
def admin_add_product():
    data    = request.get_json(force=True, silent=True) or {}
    qr_code = (data.get("qrCode") or data.get("qr_code") or "").strip()
    name    = (data.get("name") or "").strip()
    if not qr_code or not name:
        return jsonify({"error": "qrCode and name are required"}), 400

    image_url      = (data.get("imageUrl")     or data.get("image_url")      or "").strip()
    tcg_id         = (data.get("tcgId")        or data.get("tcg_id")         or "").strip()
    back_image_url = (data.get("backImageUrl") or data.get("back_image_url") or "").strip()
    language       = (data.get("language")      or "English").strip()
    condition      = (data.get("condition")     or "NM").strip()
    item_type      = (data.get("itemType")      or data.get("item_type")  or "Single").strip()
    grading_co     = (data.get("gradingCompany") or data.get("grading_company") or "").strip()
    grade          = (data.get("grade")         or "").strip()
    cert_number    = (data.get("certNumber")    or data.get("cert_number")    or "").strip()
    tags           = ",".join(data.get("tags") or []) if isinstance(data.get("tags"), list) \
                     else (data.get("tags") or "").strip()
    featured       = 1 if data.get("featured") in (True, 1, "1", "true") else 0
    listed_for_sale = 0 if data.get("listedForSale") in (False, 0, "0", "false") else 1

    base_price     = float(data.get("price", 0))
    purchase_price = float(data.get("purchasePrice") or data.get("purchase_price") or 0)
    sale_price     = float(data.get("salePrice")     or data.get("sale_price")     or 0)

    # Auto-apply pricing engine if caller sends base_market_price
    base_market = float(data.get("baseMarketPrice") or data.get("base_market_price") or 0)
    if base_market and not base_price:
        base_price = _calculate_final_price(base_market, language, item_type, grade)

    db = get_db()
    db.execute("""
        INSERT INTO inventory
            (qr_code, name, price, category, rarity, set_code, description, stock,
             image_url, tcg_id, last_updated,
             language, condition, item_type, grading_company, grade, cert_number,
             back_image_url, purchase_price, sale_price, tags, featured, listed_for_sale)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(qr_code) DO UPDATE SET
            name=excluded.name, price=excluded.price, category=excluded.category,
            rarity=excluded.rarity, set_code=excluded.set_code,
            description=excluded.description, stock=excluded.stock,
            image_url=CASE WHEN excluded.image_url!='' THEN excluded.image_url ELSE inventory.image_url END,
            tcg_id=CASE WHEN excluded.tcg_id!=''    THEN excluded.tcg_id    ELSE inventory.tcg_id    END,
            language=excluded.language, condition=excluded.condition,
            item_type=excluded.item_type, grading_company=excluded.grading_company,
            grade=excluded.grade, cert_number=excluded.cert_number,
            back_image_url=CASE WHEN excluded.back_image_url!='' THEN excluded.back_image_url ELSE inventory.back_image_url END,
            purchase_price=excluded.purchase_price, sale_price=excluded.sale_price,
            tags=excluded.tags, featured=excluded.featured,
            listed_for_sale=excluded.listed_for_sale, last_updated=excluded.last_updated
    """, (
        qr_code, name,
        base_price, data.get("category", "General"),
        data.get("rarity", ""), data.get("setCode") or data.get("set_code", ""),
        data.get("description", ""), int(data.get("stock", 0)),
        image_url, tcg_id, _now_ms(),
        language, condition, item_type, grading_co, grade, cert_number,
        back_image_url, purchase_price, sale_price, tags, featured, listed_for_sale,
    ))
    db.commit()
    _invalidate_inventory()

    # Fire webhook in background (non-blocking) if configured
    webhook_payload = {
        "event":       "card_saved",
        "qrCode":      qr_code,
        "name":        name,
        "price":       base_price,
        "category":    data.get("category", "General"),
        "rarity":      data.get("rarity", ""),
        "setCode":     data.get("setCode") or data.get("set_code", ""),
        "stock":       int(data.get("stock", 0)),
        "imageUrl":    image_url,
        "tcgId":       tcg_id,
        "language":    language,
        "condition":   condition,
        "itemType":    item_type,
        "grade":       grade,
        "tags":        tags,
        "savedAt":     _now_ms(),
    }
    _bg(_fire_webhook, webhook_payload)

    return jsonify({"ok": True, "qrCode": qr_code})


@app.route("/admin/inventory/<qr_code>", methods=["PATCH"])
@require_admin
def admin_patch_product(qr_code):
    """Partial update — toggle featured / listed_for_sale, or update stock / price."""
    data = request.get_json(force=True, silent=True) or {}
    db   = get_db()
    sets, vals = [], []
    if "featured" in data:
        sets.append("featured = %s")
        vals.append(1 if data["featured"] in (True, 1, "1", "true") else 0)
    if "listedForSale" in data or "listed_for_sale" in data:
        v = data.get("listedForSale", data.get("listed_for_sale"))
        sets.append("listed_for_sale = %s")
        vals.append(0 if v in (False, 0, "0", "false") else 1)
    if "stock" in data:
        sets.append("stock = %s")
        vals.append(int(data["stock"]))
    if "price" in data:
        sets.append("price = %s")
        vals.append(float(data["price"]))
    if not sets:
        return jsonify({"error": "No updatable fields provided"}), 400
    sets.append("last_updated = %s")
    vals.append(_now_ms())
    vals.append(qr_code)
    db.execute(f"UPDATE inventory SET {', '.join(sets)} WHERE qr_code = %s", vals)
    db.commit()
    _invalidate_inventory()
    return jsonify({"ok": True, "qrCode": qr_code})


@app.route("/admin/inventory/<qr_code>", methods=["DELETE"])
@require_admin
def admin_delete_product(qr_code):
    db = get_db()
    db.execute("DELETE FROM inventory WHERE qr_code = %s", (qr_code,))
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
            log.info("[print] Receipt sent to %s", path)

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
                log.info("[print] Receipt submitted via CUPS lp")
            else:
                log.error("[print] CUPS lp failed: %s", result.stderr.decode())

        else:
            log.warning("[print] No printer found — receipt not printed")

    except Exception as e:
        log.error("[print] Print error: %s", e)
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

    _bg(_do_print, sale)
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
# System monitoring helpers  (shared by /system/stats, /admin/system, /admin/logs)
# ---------------------------------------------------------------------------

def _sys_run(cmd: str) -> str:
    try:
        return subprocess.check_output(
            cmd, shell=True, stderr=subprocess.DEVNULL, timeout=3
        ).decode().strip()
    except Exception:
        return ""


def _sys_cpu_percent() -> float:
    try:
        line = _sys_run("top -bn1 | grep 'Cpu(s)'")
        idle = float(line.split(",")[3].split()[0])
        return round(100 - idle, 1)
    except Exception:
        try:
            avg = float(open("/proc/loadavg").read().split()[0])
            return round(min(avg * 12.5, 100), 1)
        except Exception:
            return 0.0


def _sys_cpu_temp() -> float:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(float(f.read().strip()) / 1000, 1)
    except Exception:
        return 0.0


def _sys_ram_info() -> dict:
    try:
        out = _sys_run("free -m | grep Mem")
        p = out.split()
        total, used = int(p[1]), int(p[2])
        return {"used_mb": used, "total_mb": total, "pct": round(used / total * 100, 1)}
    except Exception:
        return {"used_mb": 0, "total_mb": 0, "pct": 0}


def _sys_disk_info() -> dict:
    try:
        out = _sys_run("df -h / | tail -1")
        p = out.split()
        pct = int(p[4].replace("%", ""))
        return {"used": p[2], "total": p[1], "pct": pct}
    except Exception:
        return {"used": "?", "total": "?", "pct": 0}


def _sys_service_up(name: str) -> bool:
    return _sys_run(f"systemctl is-active {name} 2>/dev/null") == "active"


def _fmt_bytes(n: int) -> str:
    """Human-readable bytes: 1.2MB, 340KB, etc."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n //= 1024
    return f"{n:.1f}PB"


def _sys_wg_peer_list() -> list:
    """
    Parse `wg show wg0 dump` into a list of peer dicts.
    Merges friendly names from the wg_peer_names DB table.
    Returns [] when WireGuard is not running or wg is not installed.
    """
    try:
        raw = _sys_run("wg show wg0 dump 2>/dev/null")
        if not raw:
            return []
        lines = [l for l in raw.strip().splitlines() if l]
        if len(lines) < 2:
            return []
        # Fetch all name mappings at once with a direct connection
        try:
            _nc = _direct_db()
            names_map = {r["pubkey"]: r["friendly_name"] for r in
                         _nc.execute("SELECT pubkey, friendly_name FROM wg_peer_names").fetchall()}
            _nc.close()
        except Exception:
            names_map = {}
        now = int(_time.time())
        peers = []
        for line in lines[1:]:   # first line is the interface row
            parts = line.split("\t")
            if len(parts) < 8:
                continue
            pubkey    = parts[0]
            endpoint  = parts[2] if parts[2] != "(none)" else "—"
            allowed   = parts[3]
            hs_raw    = parts[4]
            rx_raw    = parts[5]
            tx_raw    = parts[6]
            hs = int(hs_raw) if hs_raw.isdigit() else 0
            if hs == 0:
                hs_str, hs_ok = "Never", False
            else:
                ago = now - hs
                if ago < 180:
                    hs_str, hs_ok = f"{ago}s ago", True
                elif ago < 3600:
                    hs_str, hs_ok = f"{ago // 60}m ago", ago < 300
                else:
                    hs_str = f"{ago // 3600}h {(ago % 3600) // 60}m ago"
                    hs_ok  = False
            peers.append({
                "pubkey":       pubkey,
                "short_key":    pubkey[:16] + "…",
                "friendly":     names_map.get(pubkey, ""),
                "endpoint":     endpoint,
                "allowed_ips":  allowed,
                "handshake":    hs_str,
                "handshake_ok": hs_ok,
                "rx":  _fmt_bytes(int(rx_raw) if rx_raw.isdigit() else 0),
                "tx":  _fmt_bytes(int(tx_raw) if tx_raw.isdigit() else 0),
            })
        return peers
    except Exception as _e:
        log.error("[wg] peer_list error: %s", _e)
        return []


def _sys_wg_peers() -> int:
    return len(_sys_wg_peer_list())


def _sparkline_svg(values: list, width: int = 336, height: int = 52,
                   color: str = "#FFD700") -> str:
    """Render a list of floats as a minimal SVG bar-chart sparkline."""
    if not values or max(values) == 0:
        return (f'<svg width="{width}" height="{height}">'
                f'<text x="50%" y="55%" text-anchor="middle" '
                f'fill="#333" font-size="11" font-family="sans-serif">'
                f'No sales data yet</text></svg>')
    mx = max(values)
    n  = len(values)
    bw = width / n
    gp = bw * 0.22
    rects = []
    for i, v in enumerate(values):
        bh = max(4, int((v / mx) * (height - 16)))
        x  = i * bw + gp / 2
        y  = height - 16 - bh
        rects.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" '
            f'width="{bw - gp:.1f}" height="{bh}" rx="3" '
            f'fill="{color}" opacity="0.82"/>'
        )
    return (f'<svg width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">'
            + "".join(rects) + "</svg>")


def _sys_ping(url: str) -> tuple:
    try:
        t0   = _time.time()
        resp = _requests.get(url, headers={"User-Agent": "HanryxVault-Monitor/1.0"}, timeout=4)
        ms   = int((_time.time() - t0) * 1000)
        return resp.status_code, ms
    except Exception:
        return 0, 0


_SYS_SERVICES = [
    ("POS Server", "hanryxvault"),
    ("nginx",      "nginx"),
    ("WireGuard",  "wg-quick@wg0"),
    ("fail2ban",   "fail2ban"),
]

_SYS_WEBSITES = [
    ("hanryxvault.cards", "https://hanryxvault.cards"),
    ("hanryxvault.app",   "https://hanryxvault.app"),
]

_SYS_LOG_SOURCES = {
    "hanryxvault": "/var/log/hanryxvault/error.log",
    "nginx":       "/var/log/nginx/error.log",
    "syslog":      "/var/log/syslog",
}

# ---------------------------------------------------------------------------
# Shared admin style + nav helper
# ---------------------------------------------------------------------------

_ADMIN_BASE_CSS = """
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d0d0d;color:#e0e0e0;padding:0}
  .admin-nav{display:flex;align-items:center;background:#111;border-bottom:1px solid #222;padding:0 20px;position:sticky;top:0;z-index:50;flex-wrap:wrap}
  .nav-logo{color:#FFD700;font-weight:900;font-size:16px;letter-spacing:1px;padding:14px 18px 14px 0;border-right:1px solid #222;margin-right:8px;white-space:nowrap}
  .nav-item{color:#777;text-decoration:none;padding:15px 14px;font-size:13px;font-weight:600;border-bottom:2px solid transparent;transition:.15s;white-space:nowrap}
  .nav-item:hover{color:#FFD700;text-decoration:none}
  .nav-active{color:#FFD700 !important;border-bottom-color:#FFD700}
  .nav-clock{margin-left:auto;color:#444;font-size:12px;padding:15px 0;white-space:nowrap}
  .wrap{padding:24px;max-width:1200px;margin:0 auto}
  h1{color:#FFD700;font-size:22px;margin-bottom:4px}
  .subtitle{color:#666;font-size:13px;margin-bottom:24px}
  h2{color:#aaa;font-size:11px;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:10px;margin-top:28px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{text-align:left;color:#555;padding:8px 10px;border-bottom:1px solid #222}
  td{padding:8px 10px;border-bottom:1px solid #1a1a1a}
  tr:hover td{background:#1a1a1a}
  a{color:#FFD700;text-decoration:none}
  a:hover{text-decoration:underline}
  #toast{position:fixed;bottom:24px;right:24px;background:#4caf50;color:#fff;padding:12px 20px;border-radius:8px;font-weight:bold;display:none;z-index:99}
  #toast.err{background:#c62828}
  .btn-gold{background:#FFD700;color:#000;border:none;border-radius:6px;padding:10px 22px;font-weight:900;font-size:13px;cursor:pointer;letter-spacing:.5px}
  .btn-gold:hover{background:#ffe033}
"""


def _admin_nav(active: str = "dashboard") -> str:
    pages = [
        ("dashboard", "/admin",              "🏠 Dashboard"),
        ("market",    "/admin/market",       "📈 Market"),
        ("trade-in",  "/admin/trade-in",     "🔁 Trade-In"),
        ("bundles",   "/admin/bundles",      "📦 Bundles"),
        ("csv",       "/admin/csv",           "📥 Import/Export"),
        ("purchases", "/admin/purchases",    "🛒 Purchases"),
        ("layby",     "/admin/layby",        "🏷️ Layby"),
        ("profit",    "/admin/profit-loss",  "💰 P&L"),
        ("eod",       "/admin/eod",          "🏧 End of Day"),
        ("system",    "/admin/system",       "⚙️ System"),
        ("logs",      "/admin/logs",         "📋 Logs"),
    ]
    items = "".join(
        f'<a href="{href}" class="nav-item{" nav-active" if k == active else ""}">{lbl}</a>'
        for k, href, lbl in pages
    )
    # Hub-dot: tiny coloured circle next to logo that goes green when POS is reachable
    # (same-origin /health check — reliable proxy for scan-hub daemon health)
    hub_dot = (
        '<span id="hub-dot" title="Scan Hub status" '
        'style="display:inline-block;width:7px;height:7px;border-radius:50%;'
        'background:#333;margin-left:6px;vertical-align:middle;'
        'transition:background .4s"></span>'
    )
    hub_js = (
        "<script>"
        "(function(){"
        "function hb(){fetch('/health',{signal:AbortSignal.timeout(1800)})"
        ".then(r=>{var d=document.getElementById('hub-dot');"
        "if(d)d.style.background=r.ok?'#4caf50':'#f44336';})"
        ".catch(()=>{var d=document.getElementById('hub-dot');"
        "if(d)d.style.background='#f44336';});};"
        "hb();setInterval(hb,20000);"
        "})();"
        "</script>"
    )
    return (
        f'<nav class="admin-nav">'
        f'<span class="nav-logo">🔐 HANRYX{hub_dot}</span>'
        f'{items}'
        f'<span class="nav-clock" id="clock"></span>'
        f'</nav>'
        f'{hub_js}'
    )


# ---------------------------------------------------------------------------
# System stats / logs API endpoints
# ---------------------------------------------------------------------------

@app.route("/system/stats", methods=["GET"])
@require_admin
def system_stats():
    svcs = {svc: _sys_service_up(svc) for _, svc in _SYS_SERVICES}
    sites = {}
    for name, url in _SYS_WEBSITES:
        sc, ms = _sys_ping(url)
        sites[name] = {"status": sc, "ms": ms, "ok": sc in (200, 301, 302)}
    try:
        row = get_db().execute(
            "SELECT pg_size_pretty(pg_database_size(current_database()))"
        ).fetchone()
        db_size = row[0] if row else "unavailable"
    except Exception:
        db_size = "unavailable"
    return jsonify({
        "cpu_pct":  _sys_cpu_percent(),
        "cpu_temp": _sys_cpu_temp(),
        "ram":      _sys_ram_info(),
        "disk":     _sys_disk_info(),
        "vpn":      {"active": svcs.get("wg-quick@wg0", False), "peers": _sys_wg_peers()},
        "services": svcs,
        "sites":    sites,
        "db_size":  db_size,
    })


@app.route("/system/logs", methods=["GET"])
@require_admin
def system_logs():
    svc  = request.args.get("service", "hanryxvault")
    n    = min(int(request.args.get("lines", 120)), 500)
    path = _SYS_LOG_SOURCES.get(svc)
    if path and os.path.exists(path):
        out = _sys_run(f"tail -n {n} {path!r}")
    else:
        out = _sys_run(f"journalctl -u {svc} -n {n} --no-pager 2>/dev/null")
    return jsonify({"service": svc, "log": out or "(no log entries found)"})


# ---------------------------------------------------------------------------
# CSV Import / Export
# ---------------------------------------------------------------------------

CSV_COLS = [
    "qr_code", "name", "price", "category", "rarity", "set_code",
    "description", "stock", "language", "condition", "item_type",
    "purchase_price", "sale_price", "tags",
]

@app.route("/admin/inventory/export", methods=["GET"])
@require_auth
def admin_inventory_export():
    db   = get_db()
    rows = db.execute(
        "SELECT qr_code,name,price,category,rarity,set_code,description,stock,"
        "language,condition,item_type,purchase_price,sale_price,tags "
        "FROM inventory ORDER BY name"
    ).fetchall()
    import io, csv as _csv
    buf = io.StringIO()
    w   = _csv.writer(buf)
    w.writerow(CSV_COLS)
    for r in rows:
        w.writerow([r[c] for c in CSV_COLS])
    data = buf.getvalue().encode()
    return app.response_class(
        data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=inventory.csv"},
    )


@app.route("/admin/inventory/import", methods=["POST"])
@require_auth
def admin_inventory_import():
    import io, csv as _csv
    f = request.files.get("csv_file")
    if not f:
        return jsonify({"error": "csv_file required"}), 400
    mode = request.form.get("mode", "upsert")   # upsert | replace_all
    try:
        text    = f.read().decode("utf-8-sig")
        reader  = _csv.DictReader(io.StringIO(text))
        rows    = list(reader)
    except Exception as e:
        return jsonify({"error": f"CSV parse error: {e}"}), 400

    required = {"qr_code", "name"}
    if not required.issubset(set(reader.fieldnames or [])):
        return jsonify({"error": "CSV must have qr_code and name columns"}), 400

    db = get_db()
    upserted = skipped = 0
    for row in rows:
        qr = (row.get("qr_code") or "").strip()
        nm = (row.get("name")    or "").strip()
        if not qr or not nm:
            skipped += 1
            continue
        def _f(k, default=0.0):
            try: return float(row.get(k) or default)
            except: return default
        def _i(k, default=0):
            try: return int(float(row.get(k) or default))
            except: return default
        db.execute("""
            INSERT INTO inventory (qr_code,name,price,category,rarity,set_code,description,
                stock,language,condition,item_type,purchase_price,sale_price,tags,last_updated)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(qr_code) DO UPDATE SET
                name=EXCLUDED.name, price=EXCLUDED.price, category=EXCLUDED.category,
                rarity=EXCLUDED.rarity, set_code=EXCLUDED.set_code,
                description=EXCLUDED.description, stock=EXCLUDED.stock,
                language=EXCLUDED.language, condition=EXCLUDED.condition,
                item_type=EXCLUDED.item_type, purchase_price=EXCLUDED.purchase_price,
                sale_price=EXCLUDED.sale_price, tags=EXCLUDED.tags,
                last_updated=EXCLUDED.last_updated
        """, (
            qr, nm, _f("price"), row.get("category","General"), row.get("rarity",""),
            row.get("set_code",""), row.get("description",""), _i("stock"),
            row.get("language","English"), row.get("condition","NM"),
            row.get("item_type","Single"), _f("purchase_price"), _f("sale_price"),
            row.get("tags",""), _now_ms(),
        ))
        upserted += 1
    db.commit()
    _invalidate_inventory()
    return jsonify({"ok": True, "upserted": upserted, "skipped": skipped})


@app.route("/admin/csv", methods=["GET"])
@require_auth
def admin_csv_page():
    nav = _admin_nav("csv")
    return f"""<!DOCTYPE html><html><head><title>CSV Import / Export</title>
{_ADMIN_CSS}</head><body>
{nav}
<div class="admin-content">
<h2>📥 CSV Import / Export</h2>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:24px;max-width:900px">

<div class="card">
<h3 style="margin-top:0">⬇️ Export Inventory</h3>
<p style="color:#aaa;font-size:14px">Download all {{}}<b id="inv-count">…</b>{{}}&nbsp;inventory cards as a CSV file you can open in Excel or Google Sheets.</p>
<a href="/admin/inventory/export" class="btn" style="display:inline-block;margin-top:8px">⬇️ Download CSV</a>
<script>fetch('/api/stock-check?codes=__count__').then(()=>{{}});
fetch('/admin/inventory/export',{{method:'HEAD'}}).catch(()=>{{}});
// Show count
fetch('/card/lookup?q=a&limit=1').catch(()=>{{}});
</script>
</div>

<div class="card">
<h3 style="margin-top:0">⬆️ Import from CSV</h3>
<p style="color:#aaa;font-size:14px">Upload a CSV to bulk-add or update cards. Required columns: <code>qr_code</code>, <code>name</code>.<br>
Optional: price, category, rarity, set_code, description, stock, language, condition, item_type, purchase_price, sale_price, tags.</p>
<form id="impForm" style="margin-top:12px">
  <input type="file" id="csvFile" accept=".csv" required style="color:#fff;margin-bottom:10px;display:block">
  <label style="color:#aaa;font-size:13px">Mode:&nbsp;
    <select id="impMode" style="background:#1e1e1e;color:#fff;border:1px solid #333;padding:4px 8px;border-radius:6px">
      <option value="upsert">Upsert (add new, update existing)</option>
    </select>
  </label>
  <button type="submit" class="btn" style="margin-top:12px">⬆️ Import</button>
</form>
<div id="impResult" style="margin-top:10px;font-size:14px"></div>
</div>

</div>

<div class="card" style="max-width:900px;margin-top:20px">
<h3 style="margin-top:0">📋 CSV Template</h3>
<p style="color:#aaa;font-size:13px">Download a blank template with all supported columns pre-filled:</p>
<a href="/admin/inventory/template" class="btn btn-secondary">📋 Download Template</a>
</div>

</div>
<script>
document.getElementById('impForm').onsubmit=async function(e){{
  e.preventDefault();
  const f=document.getElementById('csvFile').files[0];
  if(!f)return;
  const fd=new FormData();
  fd.append('csv_file',f);
  fd.append('mode',document.getElementById('impMode').value);
  document.getElementById('impResult').innerHTML='<span style="color:#aaa">Importing…</span>';
  const r=await fetch('/admin/inventory/import',{{method:'POST',body:fd}});
  const d=await r.json();
  if(d.ok){{
    document.getElementById('impResult').innerHTML=
      '<span style="color:#4caf50">✅ Done — '+d.upserted+' cards imported'+
      (d.skipped?' ('+d.skipped+' skipped)':'')+'. <a href="/admin">Back to dashboard</a></span>';
  }}else{{
    document.getElementById('impResult').innerHTML='<span style="color:#f44336">❌ '+d.error+'</span>';
  }}
}};
</script>
</body></html>"""


@app.route("/admin/inventory/template", methods=["GET"])
@require_auth
def admin_inventory_template():
    import io, csv as _csv
    buf = io.StringIO()
    w   = _csv.writer(buf)
    w.writerow(CSV_COLS)
    w.writerow(["SV1-001", "Bulbasaur", "2.50", "Pokemon", "Common",
                "SV1", "Scarlet & Violet base", "5", "English", "NM", "Single",
                "1.00", "2.50", "starter"])
    data = buf.getvalue().encode()
    return app.response_class(
        data, mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=inventory_template.csv"},
    )


# ---------------------------------------------------------------------------
# Purchase Orders
# ---------------------------------------------------------------------------

def _po_ref():
    import random, string
    return "PO-" + "".join(random.choices(string.digits, k=6))


@app.route("/admin/purchases", methods=["GET"])
@require_auth
def admin_purchases():
    db     = get_db()
    open_  = db.execute(
        "SELECT * FROM purchase_orders WHERE status IN ('draft','ordered') ORDER BY created_at DESC LIMIT 50"
    ).fetchall()
    closed = db.execute(
        "SELECT * FROM purchase_orders WHERE status IN ('received','cancelled') ORDER BY created_at DESC LIMIT 30"
    ).fetchall()
    nav = _admin_nav("purchases")

    def _row(p, show_actions=True):
        status_color = {"draft": "#aaa", "ordered": "#2196f3", "received": "#4caf50", "cancelled": "#f44336"}.get(p["status"], "#aaa")
        actions = ""
        if show_actions:
            if p["status"] == "draft":
                actions = (f'<button onclick="markOrdered({p["id"]})" class="btn btn-small">📤 Mark Ordered</button> '
                           f'<button onclick="openPO({p["id"]})" class="btn btn-small btn-secondary">+ Items</button> '
                           f'<button onclick="cancelPO({p["id"]})" class="btn btn-small btn-danger">Cancel</button>')
            elif p["status"] == "ordered":
                actions = (f'<button onclick="receivePO({p["id"]})" class="btn btn-small" style="background:#4caf50">📥 Receive</button> '
                           f'<button onclick="openPO({p["id"]})" class="btn btn-small btn-secondary">View Items</button> '
                           f'<button onclick="cancelPO({p["id"]})" class="btn btn-small btn-danger">Cancel</button>')
        ts = datetime.datetime.fromtimestamp(p["created_at"] / 1000).strftime("%d/%m/%y")
        return (f'<tr><td><b>{p["reference"]}</b></td><td>{p["supplier"]}</td>'
                f'<td><span style="color:{status_color}">{p["status"].upper()}</span></td>'
                f'<td>£{p["total_cost"]:.2f}</td><td>{ts}</td><td>{actions}</td></tr>')

    open_rows  = "".join(_row(p) for p in open_)  or "<tr><td colspan='6' style='color:#666'>No open orders</td></tr>"
    closed_rows = "".join(_row(p, False) for p in closed) or "<tr><td colspan='6' style='color:#666'>No history</td></tr>"

    return f"""<!DOCTYPE html><html><head><title>Purchase Orders</title>
{_ADMIN_CSS}</head><body>
{nav}
<div class="admin-content">
<h2>🛒 Purchase Orders</h2>

<div class="card" style="max-width:500px;margin-bottom:20px">
<h3 style="margin-top:0">New Purchase Order</h3>
<div style="display:grid;gap:8px">
  <input id="poSupplier" placeholder="Supplier name" style="background:#1e1e1e;color:#fff;border:1px solid #333;padding:8px 12px;border-radius:8px">
  <textarea id="poNotes" placeholder="Notes (optional)" rows="2" style="background:#1e1e1e;color:#fff;border:1px solid #333;padding:8px 12px;border-radius:8px;resize:vertical"></textarea>
  <button onclick="createPO()" class="btn">+ Create Purchase Order</button>
</div>
</div>

<div class="card" style="margin-bottom:16px">
<h3 style="margin-top:0">Open Orders</h3>
<table class="data-table"><thead><tr>
  <th>Reference</th><th>Supplier</th><th>Status</th><th>Total Cost</th><th>Date</th><th>Actions</th>
</tr></thead><tbody>{open_rows}</tbody></table>
</div>

<div class="card">
<h3 style="margin-top:0">History (last 30)</h3>
<table class="data-table"><thead><tr>
  <th>Reference</th><th>Supplier</th><th>Status</th><th>Total Cost</th><th>Date</th><th></th>
</tr></thead><tbody>{closed_rows}</tbody></table>
</div>

<!-- Item modal -->
<div id="poModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:999;align-items:center;justify-content:center">
<div style="background:#1a1a1a;border-radius:16px;padding:24px;width:560px;max-height:80vh;overflow-y:auto">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
    <h3 style="margin:0" id="poModalTitle">Order Items</h3>
    <button onclick="closePOModal()" style="background:none;border:none;color:#aaa;font-size:20px;cursor:pointer">✕</button>
  </div>
  <div id="poItemList" style="margin-bottom:16px"></div>
  <div style="display:grid;grid-template-columns:2fr 1fr 1fr auto;gap:8px;align-items:end" id="poAddRow">
    <input id="piName" placeholder="Card name / QR code" style="background:#252525;color:#fff;border:1px solid #333;padding:8px;border-radius:8px">
    <input id="piQty" type="number" value="1" min="1" placeholder="Qty" style="background:#252525;color:#fff;border:1px solid #333;padding:8px;border-radius:8px">
    <input id="piCost" type="number" step="0.01" value="0" placeholder="Unit cost £" style="background:#252525;color:#fff;border:1px solid #333;padding:8px;border-radius:8px">
    <button onclick="addPOItem()" class="btn btn-small">Add</button>
  </div>
</div>
</div>

</div>
<script>
let _activePOId=null;
async function createPO(){{
  const supplier=document.getElementById('poSupplier').value.trim()||'Unknown';
  const notes=document.getElementById('poNotes').value.trim();
  const r=await fetch('/admin/purchases/create',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{supplier,notes}})}});
  if(r.ok)location.reload();else alert('Error creating order');
}}
async function openPO(id){{
  _activePOId=id;
  await refreshPOItems();
  document.getElementById('poModal').style.display='flex';
}}
function closePOModal(){{document.getElementById('poModal').style.display='none';}}
async function refreshPOItems(){{
  const r=await fetch('/admin/purchases/'+_activePOId);
  const d=await r.json();
  document.getElementById('poModalTitle').textContent='Order: '+d.reference+' ('+d.supplier+')';
  document.getElementById('poItemList').innerHTML=d.items.length===0
    ?'<p style="color:#666">No items yet.</p>'
    :d.items.map(i=>'<div style="display:flex;justify-content:space-between;align-items:center;padding:8px;background:#252525;border-radius:8px;margin-bottom:6px">'
      +'<div><b>'+i.name+'</b><br><span style="color:#aaa;font-size:12px">'+i.qr_code+' &mdash; Qty: '+i.qty_ordered+' &times; £'+i.unit_cost.toFixed(2)+'</span></div>'
      +'<button onclick="removePOItem('+i.id+')" style="background:none;border:none;color:#f44336;cursor:pointer;font-size:18px">✕</button>'
      +'</div>').join('');
}}
async function addPOItem(){{
  const name=document.getElementById('piName').value.trim();
  const qty=parseInt(document.getElementById('piQty').value)||1;
  const cost=parseFloat(document.getElementById('piCost').value)||0;
  if(!name)return;
  const r=await fetch('/admin/purchases/'+_activePOId+'/add-item',{{
    method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{name,qty_ordered:qty,unit_cost:cost}})
  }});
  if(r.ok){{document.getElementById('piName').value='';await refreshPOItems();}}
  else alert('Error adding item');
}}
async function removePOItem(itemId){{
  await fetch('/admin/purchases/'+_activePOId+'/remove-item/'+itemId,{{method:'POST'}});
  await refreshPOItems();
}}
async function markOrdered(id){{
  if(!confirm('Mark this order as Ordered?'))return;
  await fetch('/admin/purchases/'+id+'/mark-ordered',{{method:'POST'}});
  location.reload();
}}
async function receivePO(id){{
  if(!confirm('Receive this order? All items will be added to inventory stock and purchase price updated.'))return;
  const r=await fetch('/admin/purchases/'+id+'/receive',{{method:'POST'}});
  const d=await r.json();
  if(d.ok){{alert('Received! '+d.items_received+' item types added to inventory.');location.reload();}}
  else alert('Error: '+d.error);
}}
async function cancelPO(id){{
  if(!confirm('Cancel this order?'))return;
  await fetch('/admin/purchases/'+id+'/cancel',{{method:'POST'}});
  location.reload();
}}
</script>
</body></html>"""


@app.route("/admin/purchases/create", methods=["POST"])
@require_auth
def admin_purchases_create():
    data     = request.get_json(silent=True) or {}
    supplier = (data.get("supplier") or "Unknown").strip()
    notes    = (data.get("notes") or "").strip()
    ref      = _po_ref()
    db       = get_db()
    row = db.execute(
        "INSERT INTO purchase_orders (reference, supplier, notes) VALUES (%s,%s,%s) RETURNING id",
        (ref, supplier, notes)
    ).fetchone()
    db.commit()
    return jsonify({"ok": True, "id": row["id"], "reference": ref})


@app.route("/admin/purchases/<int:po_id>", methods=["GET"])
@require_auth
def admin_purchases_get(po_id):
    db    = get_db()
    po    = db.execute("SELECT * FROM purchase_orders WHERE id=%s", (po_id,)).fetchone()
    if not po:
        return jsonify({"error": "not found"}), 404
    items = db.execute("SELECT * FROM purchase_order_items WHERE order_id=%s ORDER BY id", (po_id,)).fetchall()
    return jsonify({
        "id": po["id"], "reference": po["reference"], "supplier": po["supplier"],
        "status": po["status"], "total_cost": po["total_cost"], "notes": po["notes"],
        "items": [dict(i) for i in items],
    })


@app.route("/admin/purchases/<int:po_id>/add-item", methods=["POST"])
@require_auth
def admin_purchases_add_item(po_id):
    db   = get_db()
    po   = db.execute("SELECT * FROM purchase_orders WHERE id=%s AND status='draft'", (po_id,)).fetchone()
    if not po:
        return jsonify({"error": "order not found or not in draft"}), 404
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    qr   = (data.get("qr_code") or name).strip()
    qty  = max(1, int(data.get("qty_ordered") or 1))
    cost = float(data.get("unit_cost") or 0)
    if not name:
        return jsonify({"error": "name required"}), 400
    db.execute(
        "INSERT INTO purchase_order_items (order_id,qr_code,name,qty_ordered,unit_cost) VALUES (%s,%s,%s,%s,%s)",
        (po_id, qr, name, qty, cost)
    )
    db.execute(
        "UPDATE purchase_orders SET total_cost = (SELECT COALESCE(SUM(qty_ordered*unit_cost),0) FROM purchase_order_items WHERE order_id=%s) WHERE id=%s",
        (po_id, po_id)
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/admin/purchases/<int:po_id>/remove-item/<int:item_id>", methods=["POST"])
@require_auth
def admin_purchases_remove_item(po_id, item_id):
    db = get_db()
    db.execute("DELETE FROM purchase_order_items WHERE id=%s AND order_id=%s", (item_id, po_id))
    db.execute(
        "UPDATE purchase_orders SET total_cost = (SELECT COALESCE(SUM(qty_ordered*unit_cost),0) FROM purchase_order_items WHERE order_id=%s) WHERE id=%s",
        (po_id, po_id)
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/admin/purchases/<int:po_id>/mark-ordered", methods=["POST"])
@require_auth
def admin_purchases_mark_ordered(po_id):
    db = get_db()
    db.execute("UPDATE purchase_orders SET status='ordered' WHERE id=%s AND status='draft'", (po_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/admin/purchases/<int:po_id>/receive", methods=["POST"])
@require_auth
def admin_purchases_receive(po_id):
    db    = get_db()
    po    = db.execute("SELECT * FROM purchase_orders WHERE id=%s AND status='ordered'", (po_id,)).fetchone()
    if not po:
        return jsonify({"error": "order not found or not in ordered status"}), 404
    items = db.execute("SELECT * FROM purchase_order_items WHERE order_id=%s", (po_id,)).fetchall()
    received = 0
    for item in items:
        qty  = item["qty_ordered"]
        cost = item["unit_cost"]
        qr   = item["qr_code"]
        name = item["name"]
        db.execute("""
            INSERT INTO inventory (qr_code, name, price, stock, purchase_price, last_updated)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT(qr_code) DO UPDATE SET
                stock = inventory.stock + excluded.stock,
                purchase_price = CASE WHEN excluded.purchase_price > 0 THEN excluded.purchase_price ELSE inventory.purchase_price END,
                last_updated = excluded.last_updated
        """, (qr, name, cost, qty, cost, _now_ms()))
        db.execute("UPDATE purchase_order_items SET qty_received=%s WHERE id=%s", (qty, item["id"]))
        received += 1
    db.execute(
        "UPDATE purchase_orders SET status='received', received_at=%s WHERE id=%s",
        (_now_ms(), po_id)
    )
    db.commit()
    _invalidate_inventory()
    return jsonify({"ok": True, "items_received": received})


@app.route("/admin/purchases/<int:po_id>/cancel", methods=["POST"])
@require_auth
def admin_purchases_cancel(po_id):
    db = get_db()
    db.execute("UPDATE purchase_orders SET status='cancelled' WHERE id=%s", (po_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Profit & Loss + Trade-in P&L
# ---------------------------------------------------------------------------

@app.route("/admin/profit-loss", methods=["GET"])
@require_auth
def admin_profit_loss():
    period = request.args.get("period", "30")  # days
    try:
        days = int(period)
    except:
        days = 30
    since_ms = _now_ms() - days * 86_400_000
    db = get_db()

    # ── Revenue from sales ──────────────────────────────────────────────────
    rev_row = db.execute(
        "SELECT COALESCE(SUM(total_amount),0) as rev, COUNT(*) as cnt "
        "FROM sales WHERE is_refunded=0 AND timestamp_ms>=%s", (since_ms,)
    ).fetchone()
    revenue = rev_row["rev"]
    tx_count = rev_row["cnt"]

    # ── COGS — qty sold × purchase_price per card ───────────────────────────
    cogs_rows = db.execute("""
        SELECT sd.qr_code, sd.name,
               SUM(sd.quantity)   AS qty_sold,
               SUM(sd.line_total) AS revenue_line,
               COALESCE(i.purchase_price,0) AS purchase_price,
               SUM(sd.quantity) * COALESCE(i.purchase_price,0) AS cogs_line
        FROM stock_deductions sd
        LEFT JOIN inventory i ON i.qr_code = sd.qr_code
        WHERE sd.deducted_at >= %s
        GROUP BY sd.qr_code, sd.name, i.purchase_price
        ORDER BY revenue_line DESC
        LIMIT 50
    """, (since_ms,)).fetchall()

    total_cogs = sum(r["cogs_line"] for r in cogs_rows)
    gross_profit = revenue - total_cogs
    margin_pct   = (gross_profit / revenue * 100) if revenue else 0

    # ── Trade-in P&L ────────────────────────────────────────────────────────
    ti_rows = db.execute("""
        SELECT ti.reference, ti.customer, ti.completed_at,
               SUM(tii.offered_price) AS paid_out,
               SUM(tii.market_price)  AS market_val,
               COUNT(tii.id)          AS card_count
        FROM trade_ins ti
        JOIN trade_in_items tii ON tii.trade_in_id = ti.id AND tii.accepted=1
        WHERE ti.status='completed' AND ti.completed_at>=%s
        GROUP BY ti.id, ti.reference, ti.customer, ti.completed_at
        ORDER BY ti.completed_at DESC
        LIMIT 30
    """, (since_ms,)).fetchall()

    total_ti_paid   = sum(r["paid_out"]   for r in ti_rows)
    total_ti_market = sum(r["market_val"] for r in ti_rows)
    ti_uplift       = total_ti_market - total_ti_paid

    nav = _admin_nav("profit")

    def _period_btn(d, label):
        active = "background:#2196f3;" if str(d) == str(days) else ""
        return f'<a href="/admin/profit-loss?period={d}" class="btn btn-secondary" style="padding:6px 14px;font-size:13px;{active}">{label}</a>'

    period_btns = (
        _period_btn(7, "7d") + " " +
        _period_btn(30, "30d") + " " +
        _period_btn(90, "90d") + " " +
        _period_btn(365, "1yr")
    )

    card_rows = ""
    for r in cogs_rows:
        prof = r["revenue_line"] - r["cogs_line"]
        mgn  = (prof / r["revenue_line"] * 100) if r["revenue_line"] else 0
        col  = "#4caf50" if prof >= 0 else "#f44336"
        card_rows += (
            f'<tr><td>{r["name"]}</td><td>{r["qty_sold"]}</td>'
            f'<td>£{r["revenue_line"]:.2f}</td>'
            f'<td>£{r["cogs_line"]:.2f}</td>'
            f'<td style="color:{col}">£{prof:.2f}</td>'
            f'<td style="color:{col}">{mgn:.1f}%</td></tr>'
        )
    if not card_rows:
        card_rows = "<tr><td colspan='6' style='color:#666'>No sales in this period</td></tr>"

    ti_rows_html = ""
    for r in ti_rows:
        uplift = (r["market_val"] or 0) - (r["paid_out"] or 0)
        col    = "#4caf50" if uplift >= 0 else "#f44336"
        ts     = datetime.datetime.fromtimestamp((r["completed_at"] or 0) / 1000).strftime("%d/%m/%y") if r["completed_at"] else "—"
        ti_rows_html += (
            f'<tr><td>{r["reference"]}</td><td>{r["customer"]}</td>'
            f'<td>{r["card_count"]}</td>'
            f'<td>£{(r["paid_out"] or 0):.2f}</td>'
            f'<td>£{(r["market_val"] or 0):.2f}</td>'
            f'<td style="color:{col}">£{uplift:.2f}</td>'
            f'<td>{ts}</td></tr>'
        )
    if not ti_rows_html:
        ti_rows_html = "<tr><td colspan='7' style='color:#666'>No completed trade-ins in this period</td></tr>"

    profit_color = "#4caf50" if gross_profit >= 0 else "#f44336"
    ti_color     = "#4caf50" if ti_uplift   >= 0 else "#f44336"

    return f"""<!DOCTYPE html><html><head><title>Profit & Loss</title>
{_ADMIN_CSS}</head><body>
{nav}
<div class="admin-content">
<div style="display:flex;align-items:center;gap:12px;margin-bottom:20px">
  <h2 style="margin:0">💰 Profit & Loss</h2>
  <div style="display:flex;gap:6px">{period_btns}</div>
</div>

<!-- KPI row -->
<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:20px">
  <div class="card" style="text-align:center">
    <div style="color:#aaa;font-size:12px;margin-bottom:4px">REVENUE</div>
    <div style="font-size:26px;font-weight:700">£{revenue:.2f}</div>
    <div style="color:#aaa;font-size:12px">{tx_count} transactions</div>
  </div>
  <div class="card" style="text-align:center">
    <div style="color:#aaa;font-size:12px;margin-bottom:4px">COGS</div>
    <div style="font-size:26px;font-weight:700">£{total_cogs:.2f}</div>
    <div style="color:#aaa;font-size:12px">cost of goods sold</div>
  </div>
  <div class="card" style="text-align:center;border-color:{profit_color}40">
    <div style="color:#aaa;font-size:12px;margin-bottom:4px">GROSS PROFIT</div>
    <div style="font-size:26px;font-weight:700;color:{profit_color}">£{gross_profit:.2f}</div>
    <div style="color:{profit_color};font-size:12px">{margin_pct:.1f}% margin</div>
  </div>
  <div class="card" style="text-align:center;border-color:{ti_color}40">
    <div style="color:#aaa;font-size:12px;margin-bottom:4px">TRADE-IN UPLIFT</div>
    <div style="font-size:26px;font-weight:700;color:{ti_color}">£{ti_uplift:.2f}</div>
    <div style="color:#aaa;font-size:12px">paid £{total_ti_paid:.2f} / market £{total_ti_market:.2f}</div>
  </div>
</div>

<div class="card" style="margin-bottom:16px">
<h3 style="margin-top:0">📊 By Card — Top 50</h3>
<table class="data-table"><thead><tr>
  <th>Card</th><th>Qty Sold</th><th>Revenue</th><th>COGS</th><th>Profit</th><th>Margin</th>
</tr></thead><tbody>{card_rows}</tbody></table>
<p style="color:#555;font-size:12px;margin-top:8px">COGS uses current purchase_price from inventory. Set purchase prices on cards to get accurate margin figures.</p>
</div>

<div class="card">
<h3 style="margin-top:0">🔁 Trade-in P&L</h3>
<table class="data-table"><thead><tr>
  <th>Reference</th><th>Customer</th><th>Cards</th><th>Paid Out</th><th>Market Value</th><th>Uplift</th><th>Date</th>
</tr></thead><tbody>{ti_rows_html}</tbody></table>
</div>

</div></body></html>"""


# ---------------------------------------------------------------------------
# Layby (hold) system
# ---------------------------------------------------------------------------

def _layby_ref():
    import random, string
    return "LB-" + "".join(random.choices(string.digits, k=6))


@app.route("/admin/layby", methods=["GET"])
@require_auth
def admin_layby():
    db     = get_db()
    open_  = db.execute(
        "SELECT * FROM laybys WHERE status='open' ORDER BY created_at DESC LIMIT 50"
    ).fetchall()
    closed = db.execute(
        "SELECT * FROM laybys WHERE status!='open' ORDER BY created_at DESC LIMIT 30"
    ).fetchall()
    nav = _admin_nav("layby")

    def _row(lb, show_actions=True):
        balance = lb["total_price"] - lb["deposit_paid"]
        status_color = {"open": "#2196f3", "completed": "#4caf50", "cancelled": "#f44336"}.get(lb["status"], "#aaa")
        actions = ""
        if show_actions and lb["status"] == "open":
            actions = (f'<button onclick="openLB({lb["id"]})" class="btn btn-small">View/Pay</button> '
                       f'<button onclick="completeLB({lb["id"]})" class="btn btn-small" style="background:#4caf50">✅ Complete</button> '
                       f'<button onclick="cancelLB({lb["id"]})" class="btn btn-small btn-danger">Cancel</button>')
        due = lb["due_date"] or "—"
        ts  = datetime.datetime.fromtimestamp(lb["created_at"] / 1000).strftime("%d/%m/%y")
        return (f'<tr><td><b>{lb["reference"]}</b></td><td>{lb["customer"]}</td>'
                f'<td><span style="color:{status_color}">{lb["status"].upper()}</span></td>'
                f'<td>£{lb["total_price"]:.2f}</td>'
                f'<td>£{lb["deposit_paid"]:.2f}</td>'
                f'<td>£{balance:.2f}</td>'
                f'<td>{due}</td><td>{ts}</td><td>{actions}</td></tr>')

    open_rows  = "".join(_row(lb) for lb in open_)  or "<tr><td colspan='9' style='color:#666'>No open laybys</td></tr>"
    closed_rows = "".join(_row(lb, False) for lb in closed) or "<tr><td colspan='9' style='color:#666'>No history</td></tr>"

    return f"""<!DOCTYPE html><html><head><title>Layby</title>
{_ADMIN_CSS}</head><body>
{nav}
<div class="admin-content">
<h2>🏷️ Layby / Hold System</h2>

<div class="card" style="max-width:500px;margin-bottom:20px">
<h3 style="margin-top:0">New Layby</h3>
<div style="display:grid;gap:8px">
  <input id="lbCustomer" placeholder="Customer name" style="background:#1e1e1e;color:#fff;border:1px solid #333;padding:8px 12px;border-radius:8px">
  <input id="lbDeposit" type="number" step="0.01" placeholder="Deposit amount £" style="background:#1e1e1e;color:#fff;border:1px solid #333;padding:8px 12px;border-radius:8px">
  <input id="lbDue" type="date" style="background:#1e1e1e;color:#fff;border:1px solid #333;padding:8px 12px;border-radius:8px">
  <textarea id="lbNotes" placeholder="Notes (optional)" rows="2" style="background:#1e1e1e;color:#fff;border:1px solid #333;padding:8px 12px;border-radius:8px;resize:vertical"></textarea>
  <button onclick="createLB()" class="btn">+ Create Layby</button>
</div>
</div>

<div class="card" style="margin-bottom:16px">
<h3 style="margin-top:0">Open Laybys</h3>
<table class="data-table"><thead><tr>
  <th>Reference</th><th>Customer</th><th>Status</th><th>Total</th><th>Deposit</th><th>Balance</th><th>Due Date</th><th>Created</th><th>Actions</th>
</tr></thead><tbody>{open_rows}</tbody></table>
</div>

<div class="card">
<h3 style="margin-top:0">History</h3>
<table class="data-table"><thead><tr>
  <th>Reference</th><th>Customer</th><th>Status</th><th>Total</th><th>Deposit</th><th>Balance</th><th>Due Date</th><th>Created</th><th></th>
</tr></thead><tbody>{closed_rows}</tbody></table>
</div>

<!-- Layby detail modal -->
<div id="lbModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:999;align-items:center;justify-content:center">
<div style="background:#1a1a1a;border-radius:16px;padding:24px;width:620px;max-height:85vh;overflow-y:auto">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
    <h3 style="margin:0" id="lbModalTitle">Layby Detail</h3>
    <button onclick="closeLBModal()" style="background:none;border:none;color:#aaa;font-size:20px;cursor:pointer">✕</button>
  </div>
  <div id="lbDetail"></div>

  <h4>Add Item</h4>
  <div style="display:grid;grid-template-columns:2fr 1fr 1fr auto;gap:8px;align-items:end">
    <input id="lbItemQR" placeholder="QR code / card name" style="background:#252525;color:#fff;border:1px solid #333;padding:8px;border-radius:8px">
    <input id="lbItemQty" type="number" value="1" min="1" placeholder="Qty" style="background:#252525;color:#fff;border:1px solid #333;padding:8px;border-radius:8px">
    <input id="lbItemPrice" type="number" step="0.01" value="0" placeholder="Unit price £" style="background:#252525;color:#fff;border:1px solid #333;padding:8px;border-radius:8px">
    <button onclick="addLBItem()" class="btn btn-small">Add</button>
  </div>

  <h4>Record Payment</h4>
  <div style="display:grid;grid-template-columns:1fr 1fr 2fr auto;gap:8px;align-items:end">
    <input id="lbPayAmt" type="number" step="0.01" placeholder="Amount £" style="background:#252525;color:#fff;border:1px solid #333;padding:8px;border-radius:8px">
    <select id="lbPayMethod" style="background:#252525;color:#fff;border:1px solid #333;padding:8px;border-radius:8px">
      <option>cash</option><option>card</option><option>transfer</option><option>other</option>
    </select>
    <input id="lbPayNotes" placeholder="Notes" style="background:#252525;color:#fff;border:1px solid #333;padding:8px;border-radius:8px">
    <button onclick="addLBPayment()" class="btn btn-small">Pay</button>
  </div>
</div>
</div>

</div>
<script>
let _activeLBId=null;
async function createLB(){{
  const customer=document.getElementById('lbCustomer').value.trim()||'Walk-in';
  const deposit=parseFloat(document.getElementById('lbDeposit').value)||0;
  const due=document.getElementById('lbDue').value||'';
  const notes=document.getElementById('lbNotes').value.trim();
  const r=await fetch('/admin/layby/create',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{customer,deposit,due_date:due,notes}})}});
  if(r.ok)location.reload();else alert('Error creating layby');
}}
async function openLB(id){{
  _activeLBId=id;
  await refreshLBDetail();
  document.getElementById('lbModal').style.display='flex';
}}
function closeLBModal(){{document.getElementById('lbModal').style.display='none';}}
async function refreshLBDetail(){{
  const r=await fetch('/admin/layby/'+_activeLBId);
  const d=await r.json();
  document.getElementById('lbModalTitle').textContent='Layby: '+d.reference+' — '+d.customer;
  const balance=(d.total_price-d.deposit_paid).toFixed(2);
  const itemsHtml=d.items.length===0?'<p style="color:#666">No items yet.</p>':d.items.map(i=>
    '<div style="display:flex;justify-content:space-between;padding:8px;background:#252525;border-radius:8px;margin-bottom:4px">'
    +'<span><b>'+i.name+'</b> &times;'+i.quantity+' @ £'+i.unit_price.toFixed(2)+'</span>'
    +'<button onclick="removeLBItem('+i.id+')" style="background:none;border:none;color:#f44336;cursor:pointer">✕</button>'
    +'</div>').join('');
  const paymentsHtml=d.payments.length===0?'<p style="color:#666">No payments yet.</p>':d.payments.map(p=>
    '<div style="display:flex;justify-content:space-between;padding:6px 8px;background:#252525;border-radius:8px;margin-bottom:4px">'
    +'<span>£'+p.amount.toFixed(2)+' ('+p.method+')'+(p.notes?' — '+p.notes:'')+'</span>'
    +'<span style="color:#aaa;font-size:12px">'+new Date(p.paid_at).toLocaleDateString()+'</span>'
    +'</div>').join('');
  document.getElementById('lbDetail').innerHTML=
    '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:16px">'
    +'<div class="card" style="text-align:center;padding:12px"><div style="color:#aaa;font-size:11px">TOTAL</div><b>£'+d.total_price.toFixed(2)+'</b></div>'
    +'<div class="card" style="text-align:center;padding:12px"><div style="color:#aaa;font-size:11px">PAID</div><b style="color:#4caf50">£'+d.deposit_paid.toFixed(2)+'</b></div>'
    +'<div class="card" style="text-align:center;padding:12px"><div style="color:#aaa;font-size:11px">BALANCE</div><b style="color:'+(parseFloat(balance)>0?'#f44336':'#4caf50')+'">£'+balance+'</b></div>'
    +'</div>'
    +'<h4>Items</h4>'+itemsHtml
    +'<h4>Payments</h4>'+paymentsHtml;
}}
async function addLBItem(){{
  const qr=document.getElementById('lbItemQR').value.trim();
  const qty=parseInt(document.getElementById('lbItemQty').value)||1;
  const price=parseFloat(document.getElementById('lbItemPrice').value)||0;
  if(!qr)return;
  const r=await fetch('/admin/layby/'+_activeLBId+'/add-item',{{
    method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{qr_code:qr,name:qr,quantity:qty,unit_price:price}})
  }});
  if(r.ok){{document.getElementById('lbItemQR').value='';await refreshLBDetail();}}
  else alert('Error adding item');
}}
async function removeLBItem(itemId){{
  await fetch('/admin/layby/'+_activeLBId+'/remove-item/'+itemId,{{method:'POST'}});
  await refreshLBDetail();
}}
async function addLBPayment(){{
  const amount=parseFloat(document.getElementById('lbPayAmt').value)||0;
  if(amount<=0){{alert('Enter a valid amount');return;}}
  const method=document.getElementById('lbPayMethod').value;
  const notes=document.getElementById('lbPayNotes').value.trim();
  const r=await fetch('/admin/layby/'+_activeLBId+'/add-payment',{{
    method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{amount,method,notes}})
  }});
  if(r.ok){{document.getElementById('lbPayAmt').value='';await refreshLBDetail();location.reload();}}
  else alert('Error recording payment');
}}
async function completeLB(id){{
  if(!confirm('Complete this layby? Stock will be deducted and a sale recorded.'))return;
  const r=await fetch('/admin/layby/'+id+'/complete',{{method:'POST'}});
  const d=await r.json();
  if(d.ok){{alert('Layby completed!');location.reload();}}
  else alert('Error: '+d.error);
}}
async function cancelLB(id){{
  if(!confirm('Cancel this layby? No stock changes will be made.'))return;
  await fetch('/admin/layby/'+id+'/cancel',{{method:'POST'}});
  location.reload();
}}
</script>
</body></html>"""


@app.route("/admin/layby/create", methods=["POST"])
@require_auth
def admin_layby_create():
    data     = request.get_json(silent=True) or {}
    customer = (data.get("customer") or "Walk-in").strip()
    deposit  = float(data.get("deposit") or 0)
    due_date = (data.get("due_date") or "").strip()
    notes    = (data.get("notes") or "").strip()
    ref      = _layby_ref()
    db       = get_db()
    db.execute(
        "INSERT INTO laybys (reference,customer,deposit_paid,due_date,notes) VALUES (%s,%s,%s,%s,%s)",
        (ref, customer, deposit, due_date, notes)
    )
    db.commit()
    return jsonify({"ok": True, "reference": ref})


@app.route("/admin/layby/<int:lb_id>", methods=["GET"])
@require_auth
def admin_layby_get(lb_id):
    db       = get_db()
    lb       = db.execute("SELECT * FROM laybys WHERE id=%s", (lb_id,)).fetchone()
    if not lb:
        return jsonify({"error": "not found"}), 404
    items    = db.execute("SELECT * FROM layby_items WHERE layby_id=%s ORDER BY id", (lb_id,)).fetchall()
    payments = db.execute("SELECT * FROM layby_payments WHERE layby_id=%s ORDER BY paid_at", (lb_id,)).fetchall()
    return jsonify({
        "id": lb["id"], "reference": lb["reference"], "customer": lb["customer"],
        "status": lb["status"], "total_price": lb["total_price"],
        "deposit_paid": lb["deposit_paid"], "due_date": lb["due_date"], "notes": lb["notes"],
        "items":    [dict(i) for i in items],
        "payments": [dict(p) for p in payments],
    })


@app.route("/admin/layby/<int:lb_id>/add-item", methods=["POST"])
@require_auth
def admin_layby_add_item(lb_id):
    db   = get_db()
    lb   = db.execute("SELECT * FROM laybys WHERE id=%s AND status='open'", (lb_id,)).fetchone()
    if not lb:
        return jsonify({"error": "layby not found or not open"}), 404
    data = request.get_json(silent=True) or {}
    qr   = (data.get("qr_code") or "").strip()
    name = (data.get("name")    or qr).strip()
    qty  = max(1, int(data.get("quantity") or 1))
    price = float(data.get("unit_price") or 0)
    if not qr:
        return jsonify({"error": "qr_code required"}), 400
    # Try to auto-fill name and price from inventory
    inv = db.execute("SELECT name,price FROM inventory WHERE qr_code=%s", (qr,)).fetchone()
    if inv:
        if not name or name == qr:
            name  = inv["name"]
        if price == 0:
            price = inv["price"]
    db.execute(
        "INSERT INTO layby_items (layby_id,qr_code,name,quantity,unit_price) VALUES (%s,%s,%s,%s,%s)",
        (lb_id, qr, name, qty, price)
    )
    db.execute(
        "UPDATE laybys SET total_price=(SELECT COALESCE(SUM(quantity*unit_price),0) FROM layby_items WHERE layby_id=%s) WHERE id=%s",
        (lb_id, lb_id)
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/admin/layby/<int:lb_id>/remove-item/<int:item_id>", methods=["POST"])
@require_auth
def admin_layby_remove_item(lb_id, item_id):
    db = get_db()
    db.execute("DELETE FROM layby_items WHERE id=%s AND layby_id=%s", (item_id, lb_id))
    db.execute(
        "UPDATE laybys SET total_price=(SELECT COALESCE(SUM(quantity*unit_price),0) FROM layby_items WHERE layby_id=%s) WHERE id=%s",
        (lb_id, lb_id)
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/admin/layby/<int:lb_id>/add-payment", methods=["POST"])
@require_auth
def admin_layby_add_payment(lb_id):
    db     = get_db()
    lb     = db.execute("SELECT * FROM laybys WHERE id=%s AND status='open'", (lb_id,)).fetchone()
    if not lb:
        return jsonify({"error": "layby not found or not open"}), 404
    data   = request.get_json(silent=True) or {}
    amount = float(data.get("amount") or 0)
    method = (data.get("method") or "cash").strip()
    notes  = (data.get("notes")  or "").strip()
    if amount <= 0:
        return jsonify({"error": "amount must be positive"}), 400
    db.execute(
        "INSERT INTO layby_payments (layby_id,amount,method,notes) VALUES (%s,%s,%s,%s)",
        (lb_id, amount, method, notes)
    )
    db.execute(
        "UPDATE laybys SET deposit_paid=(SELECT COALESCE(SUM(amount),0) FROM layby_payments WHERE layby_id=%s) WHERE id=%s",
        (lb_id, lb_id)
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/admin/layby/<int:lb_id>/complete", methods=["POST"])
@require_auth
def admin_layby_complete(lb_id):
    db    = get_db()
    lb    = db.execute("SELECT * FROM laybys WHERE id=%s AND status='open'", (lb_id,)).fetchone()
    if not lb:
        return jsonify({"error": "layby not found or not open"}), 404
    items = db.execute("SELECT * FROM layby_items WHERE layby_id=%s", (lb_id,)).fetchall()
    if not items:
        return jsonify({"error": "no items on layby"}), 400
    # Check stock
    for item in items:
        row = db.execute("SELECT stock FROM inventory WHERE qr_code=%s", (item["qr_code"],)).fetchone()
        if row and row["stock"] < item["quantity"]:
            return jsonify({"error": f"Insufficient stock for {item['name']}"}), 400
    # Deduct stock and record sale
    import uuid
    tx_id   = str(uuid.uuid4())
    total   = lb["total_price"]
    deposit = lb["deposit_paid"]
    for item in items:
        db.execute(
            "UPDATE inventory SET stock=stock-%s, last_updated=%s WHERE qr_code=%s",
            (item["quantity"], _now_ms(), item["qr_code"])
        )
        db.execute(
            "INSERT INTO stock_deductions (transaction_id,qr_code,name,quantity,unit_price,line_total,deducted_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (tx_id, item["qr_code"], item["name"], item["quantity"],
             item["unit_price"], item["quantity"] * item["unit_price"], _now_ms())
        )
        _invalidate_inventory(item["qr_code"])
    db.execute(
        "INSERT INTO sales (transaction_id,timestamp_ms,total_amount,payment_method,employee_id,items_json,cash_received,source) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (tx_id, _now_ms(), total, "layby", "admin",
         json.dumps([{"name": i["name"], "price": i["unit_price"], "qty": i["quantity"]} for i in items]),
         deposit, "layby")
    )
    db.execute(
        "UPDATE laybys SET status='completed', completed_at=%s WHERE id=%s",
        (_now_ms(), lb_id)
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/admin/layby/<int:lb_id>/cancel", methods=["POST"])
@require_auth
def admin_layby_cancel(lb_id):
    db = get_db()
    db.execute("UPDATE laybys SET status='cancelled' WHERE id=%s", (lb_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# End-of-Day Cash Reconciliation
# ---------------------------------------------------------------------------

@app.route("/admin/eod", methods=["GET"])
@require_auth
def admin_eod():
    from datetime import date
    today_str = date.today().isoformat()
    db = get_db()

    today_start_ms = int(datetime.datetime.combine(date.today(), datetime.time.min).timestamp() * 1000)

    # Today's sales summary
    sales = db.execute(
        "SELECT payment_method, SUM(total_amount) as total, COUNT(*) as cnt "
        "FROM sales WHERE is_refunded=0 AND timestamp_ms>=%s "
        "GROUP BY payment_method", (today_start_ms,)
    ).fetchall()

    total_today   = sum(r["total"] for r in sales)
    cash_today    = sum(r["total"] for r in sales if r["payment_method"].lower() in ("cash","layby"))
    card_today    = sum(r["total"] for r in sales if r["payment_method"].lower() in ("card","zettle","stripe"))
    tx_today      = sum(r["cnt"]   for r in sales)

    # Layby payments today (cash component)
    lb_cash = db.execute(
        "SELECT COALESCE(SUM(amount),0) as total FROM layby_payments WHERE method='cash' AND paid_at>=%s",
        (today_start_ms,)
    ).fetchone()["total"]

    # Open laybys count
    open_laybys = db.execute("SELECT COUNT(*) as n FROM laybys WHERE status='open'").fetchone()["n"]

    # Recent reconciliation history
    history = db.execute(
        "SELECT * FROM eod_reconciliations ORDER BY date_str DESC LIMIT 14"
    ).fetchall()

    # Check if today already closed
    today_rec = db.execute("SELECT * FROM eod_reconciliations WHERE date_str=%s", (today_str,)).fetchone()

    nav = _admin_nav("eod")

    def _hist_row(r):
        disc_col = "#4caf50" if r["discrepancy"] >= -0.01 else "#f44336"
        return (f'<tr><td>{r["date_str"]}</td>'
                f'<td>£{r["total_sales"]:.2f}</td>'
                f'<td>£{r["cash_sales"]:.2f}</td>'
                f'<td>£{r["opening_float"]:.2f}</td>'
                f'<td>£{r["closing_float"]:.2f}</td>'
                f'<td>£{r["expected_cash"]:.2f}</td>'
                f'<td>£{r["actual_cash"]:.2f}</td>'
                f'<td style="color:{disc_col}">£{r["discrepancy"]:+.2f}</td>'
                f'<td style="color:#aaa;font-size:12px">{r["notes"][:40] if r["notes"] else "—"}</td></tr>')

    hist_rows = "".join(_hist_row(r) for r in history) or "<tr><td colspan='9' style='color:#666'>No history yet</td></tr>"

    breakdown_rows = "".join(
        f'<tr><td>{r["payment_method"].upper()}</td><td>{r["cnt"]}</td><td>£{r["total"]:.2f}</td></tr>'
        for r in sales
    ) or "<tr><td colspan='3' style='color:#666'>No sales today</td></tr>"

    already_closed = today_rec is not None
    close_disabled = "disabled" if already_closed else ""
    close_note = f'<p style="color:#4caf50">✅ Already closed for {today_str} — discrepancy was £{today_rec["discrepancy"]:+.2f}</p>' if already_closed else ""

    return f"""<!DOCTYPE html><html><head><title>End of Day</title>
{_ADMIN_CSS}</head><body>
{nav}
<div class="admin-content">
<h2>🏧 End of Day — {today_str}</h2>

<!-- Today KPIs -->
<div style="display:grid;grid-template-columns:repeat(5,1fr);gap:16px;margin-bottom:20px">
  <div class="card" style="text-align:center">
    <div style="color:#aaa;font-size:12px">TOTAL SALES</div>
    <div style="font-size:24px;font-weight:700">£{total_today:.2f}</div>
    <div style="color:#aaa;font-size:12px">{tx_today} transactions</div>
  </div>
  <div class="card" style="text-align:center">
    <div style="color:#aaa;font-size:12px">CASH SALES</div>
    <div style="font-size:24px;font-weight:700">£{cash_today:.2f}</div>
  </div>
  <div class="card" style="text-align:center">
    <div style="color:#aaa;font-size:12px">CARD / ZETTLE</div>
    <div style="font-size:24px;font-weight:700">£{card_today:.2f}</div>
  </div>
  <div class="card" style="text-align:center">
    <div style="color:#aaa;font-size:12px">LAYBY CASH PAID</div>
    <div style="font-size:24px;font-weight:700">£{lb_cash:.2f}</div>
  </div>
  <div class="card" style="text-align:center">
    <div style="color:#aaa;font-size:12px">OPEN LAYBYS</div>
    <div style="font-size:24px;font-weight:700">{open_laybys}</div>
  </div>
</div>

<div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px">

<!-- Payment breakdown -->
<div class="card">
<h3 style="margin-top:0">Today's Sales Breakdown</h3>
<table class="data-table"><thead><tr><th>Method</th><th>Transactions</th><th>Total</th></tr></thead>
<tbody>{breakdown_rows}</tbody></table>
</div>

<!-- Cash reconciliation form -->
<div class="card">
<h3 style="margin-top:0">💵 Cash Count</h3>
{close_note}
<div style="display:grid;gap:10px">
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
    <div>
      <label style="color:#aaa;font-size:12px">Opening Float £</label>
      <input id="eodOpen" type="number" step="0.01" placeholder="e.g. 50.00" {close_disabled}
        style="width:100%;box-sizing:border-box;background:#1e1e1e;color:#fff;border:1px solid #333;padding:8px;border-radius:8px;margin-top:4px">
    </div>
    <div>
      <label style="color:#aaa;font-size:12px">Actual Cash Count £</label>
      <input id="eodActual" type="number" step="0.01" placeholder="Count the till" {close_disabled}
        style="width:100%;box-sizing:border-box;background:#1e1e1e;color:#fff;border:1px solid #333;padding:8px;border-radius:8px;margin-top:4px"
        oninput="recalc()">
    </div>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
    <div style="background:#252525;border-radius:8px;padding:12px;text-align:center">
      <div style="color:#aaa;font-size:11px">EXPECTED CASH</div>
      <div style="font-size:20px;font-weight:700" id="eodExpected">£—</div>
      <div style="color:#555;font-size:11px">float + cash sales + layby</div>
    </div>
    <div style="background:#252525;border-radius:8px;padding:12px;text-align:center">
      <div style="color:#aaa;font-size:11px">DISCREPANCY</div>
      <div style="font-size:20px;font-weight:700" id="eodDisc">£—</div>
    </div>
  </div>
  <textarea id="eodNotes" placeholder="Notes (handover, issues, etc.)" rows="2" {close_disabled}
    style="background:#1e1e1e;color:#fff;border:1px solid #333;padding:8px;border-radius:8px;resize:vertical"></textarea>
  <button onclick="closeEOD()" class="btn" {close_disabled}
    style="{("opacity:.5;cursor:not-allowed;" if already_closed else "")}">
    🏧 Close Day & Save Reconciliation
  </button>
</div>
</div>

</div>

<!-- History -->
<div class="card">
<h3 style="margin-top:0">Reconciliation History</h3>
<table class="data-table"><thead><tr>
  <th>Date</th><th>Total Sales</th><th>Cash Sales</th><th>Opening Float</th><th>Closing Float</th>
  <th>Expected</th><th>Actual</th><th>Discrepancy</th><th>Notes</th>
</tr></thead><tbody>{hist_rows}</tbody></table>
</div>

</div>
<script>
const CASH_TODAY={cash_today:.2f};
const LB_CASH={lb_cash:.2f};
function recalc(){{
  const open=parseFloat(document.getElementById('eodOpen').value)||0;
  const actual=parseFloat(document.getElementById('eodActual').value);
  const expected=open+CASH_TODAY+LB_CASH;
  document.getElementById('eodExpected').textContent='£'+expected.toFixed(2);
  if(!isNaN(actual)){{
    const disc=actual-expected;
    const el=document.getElementById('eodDisc');
    el.textContent='£'+(disc>=0?'+':'')+disc.toFixed(2);
    el.style.color=disc>=-0.01?'#4caf50':'#f44336';
  }}
}}
document.getElementById('eodOpen').addEventListener('input',recalc);
recalc();
async function closeEOD(){{
  const open=parseFloat(document.getElementById('eodOpen').value)||0;
  const actual=parseFloat(document.getElementById('eodActual').value)||0;
  const notes=document.getElementById('eodNotes').value.trim();
  if(!confirm('Close the day and save reconciliation?'))return;
  const r=await fetch('/admin/eod/close',{{
    method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{opening_float:open,actual_cash:actual,notes}})
  }});
  const d=await r.json();
  if(d.ok){{location.reload();}}else{{alert('Error: '+d.error);}}
}}
</script>
</body></html>"""


@app.route("/admin/eod/close", methods=["POST"])
@require_auth
def admin_eod_close():
    from datetime import date
    today_str = date.today().isoformat()
    db = get_db()

    today_start_ms = int(datetime.datetime.combine(date.today(), datetime.time.min).timestamp() * 1000)

    data         = request.get_json(silent=True) or {}
    opening_float = float(data.get("opening_float") or 0)
    actual_cash  = float(data.get("actual_cash") or 0)
    notes        = (data.get("notes") or "").strip()

    # Compute today's totals
    sales = db.execute(
        "SELECT payment_method, SUM(total_amount) as total, COUNT(*) as cnt "
        "FROM sales WHERE is_refunded=0 AND timestamp_ms>=%s "
        "GROUP BY payment_method", (today_start_ms,)
    ).fetchall()

    total_sales = sum(r["total"] for r in sales)
    cash_sales  = sum(r["total"] for r in sales if r["payment_method"].lower() in ("cash","layby"))
    card_sales  = sum(r["total"] for r in sales if r["payment_method"].lower() in ("card","zettle","stripe"))
    tx_count    = sum(r["cnt"]   for r in sales)

    lb_cash = db.execute(
        "SELECT COALESCE(SUM(amount),0) as total FROM layby_payments WHERE method='cash' AND paid_at>=%s",
        (today_start_ms,)
    ).fetchone()["total"]

    expected_cash = opening_float + cash_sales + lb_cash
    closing_float = actual_cash - cash_sales - lb_cash  # what's left in the till as float
    discrepancy   = actual_cash - expected_cash

    try:
        db.execute("""
            INSERT INTO eod_reconciliations
                (date_str,opening_float,closing_float,expected_cash,actual_cash,
                 discrepancy,total_sales,cash_sales,card_sales,transaction_count,notes)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(date_str) DO UPDATE SET
                opening_float=EXCLUDED.opening_float, closing_float=EXCLUDED.closing_float,
                expected_cash=EXCLUDED.expected_cash, actual_cash=EXCLUDED.actual_cash,
                discrepancy=EXCLUDED.discrepancy, total_sales=EXCLUDED.total_sales,
                cash_sales=EXCLUDED.cash_sales, card_sales=EXCLUDED.card_sales,
                transaction_count=EXCLUDED.transaction_count, notes=EXCLUDED.notes
        """, (today_str, opening_float, closing_float, expected_cash, actual_cash,
              discrepancy, total_sales, cash_sales, card_sales, tx_count, notes))
        db.commit()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "ok": True, "date": today_str,
        "total_sales": total_sales, "cash_sales": cash_sales,
        "expected_cash": expected_cash, "actual_cash": actual_cash,
        "discrepancy": discrepancy,
    })


# ---------------------------------------------------------------------------
# Market search endpoint  (TCG variant list for the market page)
# ---------------------------------------------------------------------------

@app.route("/market/search", methods=["GET"])
def market_search_api():
    name = request.args.get("name", "").strip()
    if len(name) < 2:
        return jsonify({"results": [], "count": 0})
    results = _tcg_search(name=name, limit=24)
    out = []
    for r in results:
        s = _tcg_to_summary(r)
        tiers = {}
        for tier, pdata in (r.get("tcgplayer", {}).get("prices") or {}).items():
            mkt = pdata.get("market") or pdata.get("mid")
            if mkt:
                tiers[tier] = round(mkt, 2)
        best = min(tiers.values()) if tiers else None
        out.append({
            "id":         r.get("id", ""),
            "name":       r.get("name", ""),
            "set_name":   (r.get("set") or {}).get("name", ""),
            "number":     r.get("number", ""),
            "rarity":     r.get("rarity", ""),
            "image":      (r.get("images") or {}).get("small", ""),
            "price_tiers": tiers,
            "best_price": best,
        })
    return jsonify({"results": out, "count": len(out)})


# ---------------------------------------------------------------------------
# /admin/market  — Market Price Intelligence page
# ---------------------------------------------------------------------------

@app.route("/admin/market", methods=["GET"])
@require_admin
def admin_market():
    nav = _admin_nav("market")
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HanryxVault — Market Prices</title>
<style>
{_ADMIN_BASE_CSS}
  .search-row{{display:flex;gap:10px;margin-bottom:8px;flex-wrap:wrap}}
  .search-row input{{flex:1;min-width:200px;background:#141414;border:1px solid #2a2a2a;border-radius:8px;padding:12px 16px;color:#e0e0e0;font-size:15px;outline:none;transition:.2s}}
  .search-row input:focus{{border-color:#FFD700}}
  .search-row select{{background:#141414;border:1px solid #2a2a2a;border-radius:8px;padding:12px 14px;color:#e0e0e0;font-size:13px;outline:none;cursor:pointer}}
  .hint{{font-size:11px;color:#444;margin-bottom:22px}}
  /* variant strip */
  .vstrip-wrap{{display:none;margin-bottom:18px;background:#0f0f0f;border:1px solid #1e1e1e;border-radius:12px;padding:14px 16px}}
  .vstrip-wrap.visible{{display:block}}
  .vstrip-hdr{{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}}
  .vstrip-title{{font-size:11px;color:#555;text-transform:uppercase;letter-spacing:.8px}}
  .vstrip{{display:flex;gap:8px;overflow-x:auto;padding-bottom:4px;scrollbar-width:thin;scrollbar-color:#333 transparent}}
  .vstrip::-webkit-scrollbar{{height:4px}}.vstrip::-webkit-scrollbar-thumb{{background:#333;border-radius:2px}}
  .vcard{{background:#141414;border:1px solid #2a2a2a;border-radius:10px;padding:10px;cursor:pointer;min-width:120px;max-width:140px;flex-shrink:0;transition:.15s;text-align:center}}
  .vcard:hover{{border-color:#444;background:#1a1a1a}}
  .vcard.sel{{border-color:#FFD700;background:#1a1400}}
  .vcard img{{width:100%;border-radius:6px;min-height:55px;object-fit:contain;display:block;margin-bottom:6px}}
  .vcard .vp{{font-size:13px;font-weight:800;color:#FFD700}}
  .vcard .vs{{font-size:10px;color:#777;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}}
  .vcard .vn{{font-size:10px;color:#555}}
  /* result area */
  .result{{display:none;background:#141414;border:1px solid #222;border-radius:14px;padding:24px}}
  .result.visible{{display:block}}
  .res-layout{{display:flex;gap:24px;flex-wrap:wrap}}
  .res-img-col{{flex-shrink:0}}
  .res-img-col img{{width:160px;border-radius:10px;border:1px solid #333;display:block}}
  .res-img-col .no-img{{width:160px;height:220px;background:#1a1a1a;border-radius:10px;border:1px solid #222;display:flex;align-items:center;justify-content:center;color:#444;font-size:11px}}
  .res-detail{{flex:1;min-width:240px}}
  .res-name{{font-size:20px;font-weight:800;color:#fff;margin-bottom:4px}}
  .res-sub{{font-size:13px;color:#777;margin-bottom:14px}}
  .tag-row{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:16px}}
  .tag{{background:#1a1a1a;border:1px solid #333;border-radius:20px;padding:3px 10px;font-size:11px;color:#aaa}}
  .tag.hp{{background:#1a0000;border-color:#5a1a1a;color:#ff8a80}}
  .tag.type{{background:#001a2a;border-color:#1a4a5a;color:#80d4ff}}
  /* price tiers */
  .price-tiers{{background:#1a1a1a;border-radius:10px;overflow:hidden;margin-bottom:16px}}
  .tier-row{{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;border-bottom:1px solid #222}}
  .tier-row:last-child{{border-bottom:none}}
  .tier-row.best{{background:#1a1400}}
  .tier-label{{font-size:12px;color:#888}}
  .tier-price{{font-size:15px;font-weight:800;color:#FFD700}}
  .tier-na{{color:#444}}
  /* condition */
  .cond-row{{display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap}}
  .cond-row label{{font-size:12px;color:#666}}
  .cond-row select{{background:#141414;border:1px solid #333;border-radius:6px;padding:7px 10px;color:#e0e0e0;font-size:13px;outline:none}}
  .cond-row select:focus{{border-color:#FFD700}}
  /* market avg */
  .mkt-box{{background:#1a1400;border:1px solid #FFD70033;border-radius:10px;padding:16px 18px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;margin-bottom:16px}}
  .mkt-avg{{font-size:32px;font-weight:900;color:#FFD700}}
  .trade-val{{font-size:22px;font-weight:800;color:#60a5fa}}
  /* language grid */
  .lang-wrap{{background:#111827;border:1px solid #1e3a5f;border-radius:10px;padding:14px 16px;margin-bottom:16px}}
  .lang-title{{font-size:11px;font-weight:700;color:#60a5fa;letter-spacing:1px;text-transform:uppercase;margin-bottom:10px}}
  .lang-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}}
  @media(max-width:520px){{.lang-grid{{grid-template-columns:repeat(2,1fr)}}}}
  .lang-cell{{background:#0f172a;border-radius:8px;padding:10px;text-align:center}}
  .lang-flag{{font-size:18px;line-height:1;margin-bottom:3px}}
  .lang-code{{font-size:10px;font-weight:700;color:#94a3b8;letter-spacing:1px;margin-bottom:4px}}
  .lang-price{{font-size:15px;font-weight:900;color:#e0e0e0;margin-bottom:2px}}
  .lang-pct{{font-size:10px;font-weight:700;border-radius:4px;padding:2px 6px;display:inline-block}}
  /* local status */
  .local-box{{background:#0d1f0d;border:1px solid #15803d33;border-radius:8px;padding:12px 16px;display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;display:none}}
  .local-box.visible{{display:flex}}
  /* add button */
  .btn-add{{background:#4caf50;color:#000;border:none;border-radius:6px;padding:10px 22px;font-weight:900;font-size:13px;cursor:pointer}}
  .btn-add:hover{{background:#66bb6a}}
  /* empty state */
  .empty{{text-align:center;padding:48px 24px}}
  .empty-icon{{font-size:48px;margin-bottom:12px}}
  .empty-title{{font-size:16px;font-weight:700;margin-bottom:8px}}
  .empty-sub{{font-size:13px;color:#555;max-width:400px;margin:0 auto 20px;line-height:1.6}}
  .chips{{display:flex;gap:8px;justify-content:center;flex-wrap:wrap}}
  .chip{{background:#1a1a1a;border:1px solid #333;border-radius:20px;padding:6px 14px;font-size:12px;color:#aaa;cursor:pointer;transition:.15s}}
  .chip:hover{{border-color:#FFD700;color:#FFD700;background:#1a1400}}
  /* loading */
  .skeleton{{background:linear-gradient(90deg,#1a1a1a 25%,#222 50%,#1a1a1a 75%);background-size:200% 100%;animation:shimmer 1.3s infinite;border-radius:8px;height:20px;margin-bottom:8px}}
  @keyframes shimmer{{0%{{background-position:200% 0}}100%{{background-position:-200% 0}}}}
  .loading{{display:none}}.loading.visible{{display:block}}
  .err-box{{background:#1a0000;border:1px solid #7f1d1d;border-radius:10px;padding:14px;color:#f87171;font-size:13px;display:none;margin-bottom:16px}}
  .err-box.visible{{display:block}}
  .conf-badge{{display:inline-block;font-size:11px;font-weight:700;padding:3px 9px;border-radius:12px;margin-left:8px}}
  .conf-HIGH{{background:#14532d;color:#4ade80}}
  .conf-MED{{background:#7c2d12;color:#fbbf24}}
  .conf-LOW{{background:#1e1e2e;color:#94a3b8}}
</style>
</head>
<body>
{nav}
<div class="wrap">
  <h1>📈 Market Price Intelligence</h1>
  <p class="subtitle">Search any Pokémon card for live TCGPlayer prices, condition multipliers &amp; language variant pricing.</p>

  <div class="search-row">
    <input type="text" id="cardInput" placeholder="e.g. Charizard Holo Base Set  or  sv1-1" autocomplete="off">
    <select id="condSelect">
      <option value="1.00">Mint NM (100%)</option>
      <option value="0.85">LP (85%)</option>
      <option value="0.65">MP (65%)</option>
      <option value="0.40">HP (40%)</option>
      <option value="0.25">Damaged (25%)</option>
    </select>
    <select id="langSelect">
      <option value="EN">🇺🇸 EN</option>
      <option value="JP">🇯🇵 JP</option>
      <option value="KR">🇰🇷 KR</option>
      <option value="CN">🇨🇳 CN</option>
    </select>
  </div>
  <p class="hint">Type 2+ characters for instant search &bull; 400 ms debounce &bull; Click a variant card to select it</p>

  <div class="vstrip-wrap" id="vstripWrap">
    <div class="vstrip-hdr">
      <span class="vstrip-title" id="vstripTitle">Variants</span>
    </div>
    <div class="vstrip" id="vstrip"></div>
  </div>

  <div class="err-box" id="errBox"></div>

  <div class="loading" id="loading">
    <div class="skeleton" style="height:28px;width:45%;margin-bottom:16px"></div>
    <div style="display:flex;gap:20px">
      <div class="skeleton" style="width:160px;height:220px;flex-shrink:0"></div>
      <div style="flex:1">
        <div class="skeleton" style="height:20px;width:60%;margin-bottom:10px"></div>
        <div class="skeleton" style="height:14px;margin-bottom:8px"></div>
        <div class="skeleton" style="height:14px;width:80%;margin-bottom:8px"></div>
        <div class="skeleton" style="height:80px;margin-top:16px"></div>
      </div>
    </div>
  </div>

  <div class="result" id="result">
    <div class="res-layout">
      <div class="res-img-col">
        <img id="rImg" src="" alt="card" style="display:none">
        <div class="no-img" id="rNoImg">No Image</div>
      </div>
      <div class="res-detail">
        <div style="display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:4px">
          <span class="res-name" id="rName"></span>
          <span class="conf-badge" id="rConf"></span>
        </div>
        <div class="res-sub" id="rSub"></div>
        <div class="tag-row" id="rTags"></div>

        <div style="margin-bottom:8px"><span style="font-size:11px;color:#555;text-transform:uppercase;letter-spacing:1px">Price Tiers (TCGPlayer)</span></div>
        <div class="price-tiers" id="priceTiers"></div>

        <div class="cond-row" style="margin-top:14px">
          <label>Condition multiplier:</label>
          <select id="condDisplay" onchange="applyCond()">
            <option value="1.00">Mint NM (100%)</option>
            <option value="0.85">LP (85%)</option>
            <option value="0.65">MP (65%)</option>
            <option value="0.40">HP (40%)</option>
            <option value="0.25">Damaged (25%)</option>
          </select>
        </div>

        <div class="mkt-box">
          <div>
            <div style="font-size:10px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">📈 Market Average</div>
            <span class="mkt-avg" id="rAvg">—</span>
          </div>
          <div style="text-align:right">
            <div style="font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">🤝 Trade-In Value</div>
            <span class="trade-val" id="rTrade">—</span>
            <div style="font-size:10px;color:#555;margin-top:2px">Suggested cash buy</div>
          </div>
        </div>

        <div class="lang-wrap">
          <div class="lang-title">🌐 Language Variant Pricing</div>
          <div class="lang-grid" id="langGrid"></div>
        </div>

        <div class="local-box" id="localBox">
          <div>
            <div style="font-size:12px;color:#4ade80;font-weight:600">🏷️ In Your Inventory</div>
            <div style="font-size:11px;color:#555;margin-top:2px" id="localSub"></div>
          </div>
          <div style="font-size:18px;font-weight:800;color:#86efac" id="localPrice"></div>
        </div>

        <div style="display:flex;gap:10px;flex-wrap:wrap">
          <button class="btn-add" id="btnAdd" onclick="addToInventory()">+ Add to Inventory</button>
          <button class="btn-gold" onclick="location.href='/admin'" style="background:#1a1a2a;color:#aaa;border:1px solid #333">↩ Dashboard</button>
        </div>
      </div>
    </div>
  </div>

  <div class="empty" id="emptyState">
    <div class="empty-icon">🔍</div>
    <div class="empty-title">Search any card above</div>
    <div class="empty-sub">TCGPlayer market prices, all print variants with card images, condition &amp; language multipliers — all from your Pi.</div>
    <div class="chips">
      <span class="chip" onclick="qs('Charizard Holo')">Charizard Holo</span>
      <span class="chip" onclick="qs('Pikachu VMAX')">Pikachu VMAX</span>
      <span class="chip" onclick="qs('Umbreon VMAX')">Umbreon VMAX</span>
      <span class="chip" onclick="qs('Rayquaza EX')">Rayquaza EX</span>
      <span class="chip" onclick="qs('Blastoise Base')">Blastoise Base</span>
      <span class="chip" onclick="qs('Mewtwo ex')">Mewtwo ex</span>
    </div>
  </div>
</div>
<div id="toast"></div>

<script>
let _timer   = null;
let _vTimer  = null;
let _lastData = null;
let _rawTiers = {{}};
let _langFact = {{JP:20, KR:45, CN:40}};
let _selId    = null;

const inp   = document.getElementById('cardInput');
const condS = document.getElementById('condSelect');
const condD = document.getElementById('condDisplay');
const langS = document.getElementById('langSelect');

function clock() {{ document.getElementById('clock').textContent = new Date().toLocaleTimeString(); }}
setInterval(clock,1000); clock();

inp.addEventListener('input', () => {{
  clearTimeout(_timer); clearTimeout(_vTimer);
  if (!inp.value.trim()) {{ show('emptyState'); hide('result'); hide('loading'); hide('errBox'); hideVariants(); return; }}
  _timer  = setTimeout(doSearch, 400);
  _vTimer = setTimeout(fetchVariants, 280);
}});
inp.addEventListener('keydown', e => {{ if (e.key === 'Enter') {{ clearTimeout(_timer); clearTimeout(_vTimer); doSearch(); }} }});
condS.addEventListener('change', () => {{ condD.value = condS.value; if (_lastData) applyCond(); }});
condD.addEventListener('change', () => {{ condS.value = condD.value; if (_lastData) applyCond(); }});
langS.addEventListener('change', () => {{ if (_lastData) applyCond(); }});

function qs(name) {{ inp.value = name; inp.dispatchEvent(new Event('input')); }}
function show(id) {{ document.getElementById(id).classList.add('visible'); }}
function hide(id) {{ document.getElementById(id).classList.remove('visible'); }}

// ── Variant strip ─────────────────────────────────────────────────────────

async function fetchVariants() {{
  const name = inp.value.trim();
  if (name.length < 2) {{ hideVariants(); return; }}
  try {{
    const r = await fetch('/market/search?' + new URLSearchParams({{name}}));
    const d = await r.json();
    renderVariants(d.results || []);
  }} catch(e) {{ hideVariants(); }}
}}

function hideVariants() {{
  document.getElementById('vstrip').innerHTML = '';
  document.getElementById('vstripWrap').classList.remove('visible');
  _selId = null;
}}

function renderVariants(variants) {{
  const wrap  = document.getElementById('vstripWrap');
  const strip = document.getElementById('vstrip');
  const title = document.getElementById('vstripTitle');
  if (!variants.length) {{ wrap.classList.remove('visible'); return; }}
  wrap.classList.add('visible');
  title.textContent = variants.length + ' variant' + (variants.length===1?'':'s') + ' found';
  strip.innerHTML = variants.map((v,i) => {{
    const price = v.best_price != null ? '$' + v.best_price.toFixed(2) : 'N/A';
    const imgTag = v.image ? `<img src="${{v.image}}" loading="lazy" onerror="this.style.display='none'">` : '';
    const sel = v.id === _selId ? ' sel' : '';
    return `<div class="vcard${{sel}}" onclick="selectVariant('${{v.id}}','${{v.name.replace(/'/g,"\\\\'")}}')" title="${{v.set_name}} #${{v.number}}">
      ${{imgTag}}
      <div class="vp">${{price}}</div>
      <div class="vs">${{v.set_name||'—'}}</div>
      <div class="vn">#${{v.number}}${{v.rarity?' · '+v.rarity:''}}</div>
    </div>`;
  }}).join('');
}}

function selectVariant(id, name) {{
  _selId = id;
  // re-render to show selection
  document.querySelectorAll('.vcard').forEach(el => {{
    el.classList.toggle('sel', el.getAttribute('onclick').includes("'"+id+"'"));
  }});
  doSearchWithId(id);
}}

// ── Price fetch ───────────────────────────────────────────────────────────

async function doSearch() {{
  const raw = inp.value.trim();
  if (!raw) return;
  // if it looks like a card ID (sv1-1, base1-4, etc.) use enrich by QR
  if (/^[a-z0-9]{{2,8}}-\d+$/i.test(raw)) {{
    doSearchWithId(raw.toLowerCase());
  }} else {{
    doSearchByName(raw);
  }}
}}

async function doSearchByName(name) {{
  hide('result'); hide('errBox'); hide('emptyState'); show('loading');
  try {{
    const r = await fetch('/card/enrich?' + new URLSearchParams({{name}}));
    const d = await r.json();
    hide('loading');
    if (d.error) {{ showErr(d.error); return; }}
    renderResult(d);
  }} catch(e) {{ hide('loading'); showErr('Network error'); }}
}}

async function doSearchWithId(id) {{
  hide('result'); hide('errBox'); hide('emptyState'); show('loading');
  try {{
    const r = await fetch('/card/enrich?' + new URLSearchParams({{qr: id}}));
    const d = await r.json();
    hide('loading');
    if (d.error) {{ showErr(d.error); return; }}
    renderResult(d);
  }} catch(e) {{ hide('loading'); showErr('Network error'); }}
}}

function showErr(msg) {{
  const el = document.getElementById('errBox');
  el.textContent = '⚠ ' + msg;
  el.classList.add('visible');
}}

// ── Render result ─────────────────────────────────────────────────────────

function renderResult(d) {{
  _lastData = d;
  const t = d.tcgData || {{}};
  const name    = d.name || t.name || '—';
  const rarity  = d.rarity || t.rarity || '';
  const setName = (t.set && t.set.name) || d.setCode || '';
  const num     = t.number || '';
  const hp      = t.hp || '';
  const types   = t.types || [];
  const imgUrl  = d.imageUrl || (t.images && (t.images.large || t.images.small)) || '';

  // name + confidence
  document.getElementById('rName').textContent = name;
  const hasTCG = !!t.name;
  const conf   = document.getElementById('rConf');
  conf.textContent = hasTCG ? '✓ TCG Data' : 'No TCG data';
  conf.className   = 'conf-badge ' + (hasTCG ? 'conf-HIGH' : 'conf-LOW');

  // subtitle
  let sub = [];
  if (setName) sub.push(setName);
  if (num)     sub.push('#' + num);
  if (rarity)  sub.push(rarity);
  document.getElementById('rSub').textContent = sub.join(' · ') || 'Unknown card';

  // tags
  let tags = '';
  if (hp)    tags += `<span class="tag hp">HP ${{hp}}</span>`;
  types.forEach(ty => {{ tags += `<span class="tag type">${{ty}}</span>`; }});
  if (d.isDuplicate) tags += `<span class="tag" style="background:#1a0a00;border-color:#7c2d12;color:#f97316">⚠ Already In Stock</span>`;
  document.getElementById('rTags').innerHTML = tags || '<span class="tag">Trading Card</span>';

  // image
  const imgEl   = document.getElementById('rImg');
  const noImgEl = document.getElementById('rNoImg');
  if (imgUrl) {{ imgEl.src = imgUrl; imgEl.style.display = 'block'; noImgEl.style.display = 'none'; }}
  else         {{ imgEl.style.display = 'none'; noImgEl.style.display = 'flex'; }}

  // price tiers
  const priceTiersData = (t.tcgplayer && t.tcgplayer.priceTiers) || {{}};
  _rawTiers = {{}};
  const tierLabels = {{
    holofoil:       'Holo Rare',
    reverseHolofoil:'Reverse Holo',
    normal:         'Normal',
    '1stEditionHolofoil': '1st Ed. Holo',
    '1stEditionNormal':   '1st Ed. Normal',
  }};
  const tierOrder = ['holofoil','1stEditionHolofoil','reverseHolofoil','normal','1stEditionNormal'];
  for (const tier of tierOrder) {{
    const p = priceTiersData[tier];
    if (p && (p.market || p.mid)) _rawTiers[tier] = p.market || p.mid;
  }}
  // also use suggestedPrice / marketPrice as fallback
  if (!Object.keys(_rawTiers).length) {{
    const fb = d.suggestedPrice || t.tcgplayer && t.tcgplayer.marketPrice;
    if (fb) _rawTiers['market'] = fb;
  }}
  renderTiers();

  // local inventory
  const localBox = document.getElementById('localBox');
  if (d.inLocalInventory && d.price > 0) {{
    localBox.classList.add('visible');
    document.getElementById('localPrice').textContent = '$' + d.price.toFixed(2);
    document.getElementById('localSub').textContent =
      (d.stockQuantity||0) + ' in stock · your price';
  }} else {{
    localBox.classList.remove('visible');
  }}

  show('result');
  hide('emptyState');
}}

function renderTiers() {{
  const m    = parseFloat(condD.value) || 1;
  const tierLabels = {{holofoil:'Holo Rare',reverseHolofoil:'Reverse Holo',normal:'Normal','1stEditionHolofoil':'1st Ed. Holo','1stEditionNormal':'1st Ed. Normal',market:'Market'}}; 
  const html = Object.entries(_rawTiers).map(([tier, raw]) => {{
    const adj = (raw * m).toFixed(2);
    return `<div class="tier-row ${{tier==='holofoil'||tier==='market'?'best':''}}">
      <span class="tier-label">${{tierLabels[tier]||tier}}</span>
      <span class="tier-price">$${{adj}} <span style="font-size:10px;color:#888;font-weight:400">(raw $${{raw.toFixed(2)}})</span></span>
    </div>`;
  }}).join('') || '<div class="tier-row"><span class="tier-label">No pricing data</span><span class="tier-na">N/A</span></div>';
  document.getElementById('priceTiers').innerHTML = html;
  applyCond();
}}

function applyCond() {{
  if (!_lastData && !Object.keys(_rawTiers).length) return;
  const m = parseFloat(condD.value) || 1;
  renderTiersOnly(m);
  const d     = _lastData || {{}};
  const t     = d.tcgData || {{}};
  const raw   = Object.values(_rawTiers)[0] || d.suggestedPrice || (t.tcgplayer && t.tcgplayer.marketPrice) || 0;
  const mktM  = raw * m;
  const trade = mktM * 0.80;

  document.getElementById('rAvg').textContent   = raw > 0 ? '$' + mktM.toFixed(2) : '—';
  document.getElementById('rTrade').textContent = raw > 0 ? '$' + trade.toFixed(2) : '—';

  // language grid
  const lang = langS.value;
  const langs = [
    {{code:'EN',flag:'🇺🇸',cls:'',pct:null}},
    {{code:'JP',flag:'🇯🇵',cls:'pct-jp',pct:_langFact.JP}},
    {{code:'KR',flag:'🇰🇷',cls:'pct-kr',pct:_langFact.KR}},
    {{code:'CN',flag:'🇨🇳',cls:'pct-cn',pct:_langFact.CN}},
  ];
  document.getElementById('langGrid').innerHTML = langs.map(l => {{
    const price = l.pct ? mktM*(1-l.pct/100) : mktM;
    const badge = l.pct ? `<span class="lang-pct" style="background:#1a1a2e;color:#818cf8">${{l.pct}}% off</span>` : `<span class="lang-pct" style="background:#1a1400;color:#FFD700">Base</span>`;
    const sel   = l.code === lang ? 'border:1px solid #FFD700' : '';
    return `<div class="lang-cell" style="${{sel}}">
      <div class="lang-flag">${{l.flag}}</div>
      <div class="lang-code">${{l.code}}</div>
      <div class="lang-price">${{mktM>0?'$'+price.toFixed(2):'N/A'}}</div>
      ${{badge}}
    </div>`;
  }}).join('');
}}

function renderTiersOnly(m) {{
  const tierLabels = {{holofoil:'Holo Rare',reverseHolofoil:'Reverse Holo',normal:'Normal','1stEditionHolofoil':'1st Ed. Holo','1stEditionNormal':'1st Ed. Normal',market:'Market'}};
  if (!Object.keys(_rawTiers).length) return;
  document.getElementById('priceTiers').innerHTML = Object.entries(_rawTiers).map(([tier, raw]) => {{
    return `<div class="tier-row ${{tier==='holofoil'||tier==='market'?'best':''}}">
      <span class="tier-label">${{tierLabels[tier]||tier}}</span>
      <span class="tier-price">$${{(raw*m).toFixed(2)}} <span style="font-size:10px;color:#888;font-weight:400">(raw $${{raw.toFixed(2)}})</span></span>
    </div>`;
  }}).join('');
}}

// ── Add to inventory ──────────────────────────────────────────────────────

function addToInventory() {{
  if (!_lastData) return;
  const d    = _lastData;
  const t    = d.tcgData || {{}};
  const qr   = d.tcgId || _selId || '';
  // Use the condition-adjusted market price currently displayed, not raw
  const condM   = parseFloat(condD.value) || 1;
  const rawPrice = Object.values(_rawTiers)[0] || d.suggestedPrice
                   || (t.tcgplayer && t.tcgplayer.marketPrice) || d.price || 0;
  const adjPrice = parseFloat((rawPrice * condM).toFixed(2));
  const pre = {{
    qr:      qr,
    name:    d.name || t.name || '',
    price:   adjPrice,
    rarity:  d.rarity || t.rarity || '',
    set:     (t.set && t.set.ptcgoCode) || d.setCode || '',
    imgurl:  d.imageUrl || (t.images && (t.images.large || t.images.small)) || '',
    tcgid:   d.tcgId || t.id || '',
  }};
  sessionStorage.setItem('prefillData', JSON.stringify(pre));
  location.href = '/admin#add-product';
}}

function toast(msg, err=false) {{
  const t = document.getElementById('toast');
  t.textContent=msg; t.className=err?'err':'';
  t.style.display='block'; setTimeout(()=>t.style.display='none',3200);
}}
</script>
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# /admin/system  — System monitoring (desktop GUI as web page)
# ---------------------------------------------------------------------------

@app.route("/admin/system", methods=["GET"])
@require_admin
def admin_system():
    nav = _admin_nav("system")
    svc_rows = "".join(
        f'<tr><td>{name}</td>'
        f'<td><span class="dot" id="svc-{_html.escape(svc)}">●</span></td>'
        f'<td id="svc-lbl-{_html.escape(svc)}" style="color:#555">checking…</td>'
        f'<td><button onclick="svcAction(\'restart\',\'{_html.escape(svc,quote=True)}\')" class="act-btn">Restart</button>'
        f'<button onclick="svcAction(\'stop\',\'{_html.escape(svc,quote=True)}\')" class="act-btn stop">Stop</button></td></tr>'
        for name, svc in _SYS_SERVICES
    )
    site_rows = "".join(
        f'<tr><td><a href="{url}" target="_blank" style="color:#FFD700">{name}</a></td>'
        f'<td><span class="dot" id="site-{_html.escape(name)}">●</span></td>'
        f'<td id="site-lbl-{_html.escape(name)}" style="color:#555">checking…</td></tr>'
        for name, url in _SYS_WEBSITES
    )
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HanryxVault — System Monitor</title>
<style>
{_ADMIN_BASE_CSS}
  .stat-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px;margin-bottom:24px}}
  .stat-card{{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:10px;padding:18px 20px}}
  .stat-label{{font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}}
  .stat-value{{font-size:30px;font-weight:900;color:#FFD700;line-height:1}}
  .stat-sub{{font-size:11px;color:#555;margin-top:4px}}
  .bar-wrap{{background:#222;border-radius:4px;height:6px;margin-top:8px;overflow:hidden}}
  .bar-fill{{height:6px;border-radius:4px;background:linear-gradient(90deg,#FFD700,#f59e0b);transition:width .6s ease}}
  .bar-fill.warn{{background:linear-gradient(90deg,#ff9800,#f59e0b)}}
  .bar-fill.crit{{background:linear-gradient(90deg,#f44336,#c62828)}}
  .dot{{font-size:14px;color:#555;transition:.3s}}
  .dot.on{{color:#4caf50}}
  .dot.off{{color:#f44336}}
  .panel{{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:10px;padding:18px;margin-bottom:20px}}
  .panel-title{{font-size:11px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-bottom:14px}}
  .act-btn{{background:#1a1a2a;border:1px solid #3a3a5a;color:#a78bfa;border-radius:4px;padding:4px 10px;font-size:11px;cursor:pointer;margin-right:4px}}
  .act-btn:hover{{background:#2a2a3a}}
  .act-btn.stop{{border-color:#5a1a1a;color:#f87171}}
  .act-btn.stop:hover{{background:#2a1a1a}}
  .quick-actions{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:24px}}
  .qa-btn{{background:#141414;border:1px solid #333;color:#e0e0e0;border-radius:8px;padding:12px 18px;font-size:13px;font-weight:600;cursor:pointer;transition:.15s;display:flex;align-items:center;gap:8px}}
  .qa-btn:hover{{border-color:#FFD700;color:#FFD700;background:#1a1400}}
  #refreshStatus{{font-size:12px;color:#555;margin-left:auto;align-self:center}}
</style>
</head>
<body>
{nav}
<div class="wrap">
  <h1>⚙️ System Monitor</h1>
  <p class="subtitle">Live Raspberry Pi performance &bull; Services &bull; Websites &bull; VPN &bull; auto-refreshes every 3 s</p>

  <div class="quick-actions">
    <button class="qa-btn" onclick="quickAction('restart-server')">🔄 Restart POS Server</button>
    <button class="qa-btn" onclick="quickAction('backup-db')">💾 Backup Database</button>
    <button class="qa-btn" onclick="quickAction('sync-inventory')">📦 Sync Inventory</button>
    <button class="qa-btn" onclick="window.open('/admin/logs','_self')">📋 View Logs</button>
    <button class="qa-btn" onclick="window.open('/health')">❤️ Health Check</button>
    <span id="refreshStatus">Last refresh: —</span>
  </div>

  <div class="stat-grid">
    <div class="stat-card">
      <div class="stat-label">CPU Usage</div>
      <div class="stat-value" id="sCpu">—</div>
      <div class="bar-wrap"><div class="bar-fill" id="bCpu" style="width:0%"></div></div>
      <div class="stat-sub" id="sTemp">Temp: —°C</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">RAM Used</div>
      <div class="stat-value" id="sRam">—</div>
      <div class="bar-wrap"><div class="bar-fill" id="bRam" style="width:0%"></div></div>
      <div class="stat-sub" id="sRamSub">— MB / — MB</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Disk Used</div>
      <div class="stat-value" id="sDisk">—</div>
      <div class="bar-wrap"><div class="bar-fill" id="bDisk" style="width:0%"></div></div>
      <div class="stat-sub" id="sDiskSub">— / —</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">VPN (WireGuard)</div>
      <div class="stat-value" id="sVpn" style="font-size:18px;padding-top:8px">—</div>
      <div class="stat-sub" id="sVpnPeers">Connected clients: —</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Database Size</div>
      <div class="stat-value" id="sDb" style="font-size:18px;padding-top:8px">—</div>
      <div class="stat-sub">vault_pos.db</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">POS Server Ping</div>
      <div class="stat-value" id="sPing" style="font-size:18px;padding-top:8px">—</div>
      <div class="stat-sub" id="sPingSub">127.0.0.1:8080</div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-title">Services</div>
    <table>
      <thead><tr><th>Service</th><th>●</th><th>Status</th><th>Actions</th></tr></thead>
      <tbody>{svc_rows}</tbody>
    </table>
  </div>

  <div class="panel">
    <div class="panel-title">Websites</div>
    <table>
      <thead><tr><th>URL</th><th>●</th><th>Response</th></tr></thead>
      <tbody>{site_rows}</tbody>
    </table>
  </div>

  <div class="panel">
    <div class="panel-title" style="display:flex;align-items:center;gap:10px">
      WireGuard VPN Peers
      <span id="wgStatus" style="font-size:11px;color:#555;font-weight:400;margin-left:auto"></span>
    </div>
    <table id="wgPeerTable">
      <thead>
        <tr>
          <th>Device</th>
          <th>Allowed IPs</th>
          <th>Endpoint</th>
          <th>Last Handshake</th>
          <th>↓ RX</th>
          <th>↑ TX</th>
          <th>Label</th>
        </tr>
      </thead>
      <tbody id="wgPeerBody">
        <tr><td colspan="7" style="color:#555;text-align:center;padding:16px">Loading peers…</td></tr>
      </tbody>
    </table>

    <div style="margin-top:16px;display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end">
      <div>
        <div style="font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">Public Key (paste full key or first 16+ chars)</div>
        <input id="wgKeyInp" placeholder="e.g. abc123XYZ..." style="width:260px;background:#1a1a1a;border:1px solid #333;border-radius:6px;color:#e0e0e0;padding:8px 10px;font-size:12px;outline:none">
      </div>
      <div>
        <div style="font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">Friendly Name</div>
        <input id="wgNameInp" placeholder="e.g. Tablet, Phone, Satellite Pi" style="width:200px;background:#1a1a1a;border:1px solid #333;border-radius:6px;color:#e0e0e0;padding:8px 10px;font-size:12px;outline:none">
      </div>
      <button class="btn-gold" onclick="savePeerName()" style="padding:9px 20px">Save Label</button>
    </div>
  </div>
</div>
<div id="toast"></div>

<script>
function clock() {{ document.getElementById('clock').textContent = new Date().toLocaleTimeString(); }}
setInterval(clock,1000); clock();

function toast(msg, err=false) {{
  const t = document.getElementById('toast');
  t.textContent=msg; t.className=err?'err':'';
  t.style.display='block'; setTimeout(()=>t.style.display='none',3200);
}}

function bar(id, pct) {{
  const el = document.getElementById(id);
  if (!el) return;
  el.style.width = Math.min(pct,100)+'%';
  el.className = 'bar-fill' + (pct>85?' crit':pct>65?' warn':'');
}}

async function refresh() {{
  try {{
    const r = await fetch('/system/stats');
    const d = await r.json();

    // CPU
    document.getElementById('sCpu').textContent   = d.cpu_pct + '%';
    document.getElementById('sTemp').textContent  = 'Temp: ' + d.cpu_temp + '°C' + (d.cpu_temp > 75 ? ' 🔥' : d.cpu_temp > 65 ? ' ⚠' : '');
    bar('bCpu', d.cpu_pct);

    // RAM
    document.getElementById('sRam').textContent    = d.ram.pct + '%';
    document.getElementById('sRamSub').textContent = d.ram.used_mb + ' MB / ' + d.ram.total_mb + ' MB';
    bar('bRam', d.ram.pct);

    // Disk
    document.getElementById('sDisk').textContent    = d.disk.pct + '%';
    document.getElementById('sDiskSub').textContent = d.disk.used + ' / ' + d.disk.total;
    bar('bDisk', d.disk.pct);

    // VPN
    const vpnOk = d.vpn.active;
    document.getElementById('sVpn').textContent      = vpnOk ? '✓ Online' : '✗ Offline';
    document.getElementById('sVpn').style.color      = vpnOk ? '#4caf50' : '#f44336';
    document.getElementById('sVpnPeers').textContent = 'Connected clients: ' + d.vpn.peers;

    // DB
    document.getElementById('sDb').textContent = d.db_size;

    // Services
    Object.entries(d.services).forEach(([svc, on]) => {{
      const dot = document.getElementById('svc-' + svc);
      const lbl = document.getElementById('svc-lbl-' + svc);
      if (dot) {{ dot.className = 'dot ' + (on?'on':'off'); }}
      if (lbl) {{ lbl.textContent = on ? 'running' : 'stopped'; lbl.style.color = on?'#4caf50':'#f44336'; }}
    }});

    // Websites
    Object.entries(d.sites).forEach(([name, info]) => {{
      const dot = document.getElementById('site-' + name);
      const lbl = document.getElementById('site-lbl-' + name);
      if (dot) {{ dot.className = 'dot ' + (info.ok?'on':'off'); }}
      if (lbl) {{ lbl.textContent = info.ok ? info.status+' · '+info.ms+'ms' : 'offline'; lbl.style.color = info.ok?'#4caf50':'#f44336'; }}
    }});

    // Server self-ping
    document.getElementById('sPing').textContent    = d.sites['hanryxvault.cards'] && d.sites['hanryxvault.cards'].ok ? 'Online' : 'Online (local)';
    document.getElementById('sPing').style.color    = '#4caf50';

    document.getElementById('refreshStatus').textContent = 'Last refresh: ' + new Date().toLocaleTimeString();
  }} catch(e) {{
    document.getElementById('refreshStatus').textContent = 'Refresh failed: ' + e.message;
  }}
}}

async function svcAction(action, svc) {{
  toast('Running: systemctl ' + action + ' ' + svc + '…');
  try {{
    const r = await fetch('/system/service-action', {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{action,service:svc}})}});
    const d = await r.json();
    toast(d.ok ? action + ' sent ✓' : 'Error: ' + d.error, !d.ok);
    setTimeout(refresh, 1500);
  }} catch(e) {{ toast('Error: ' + e.message, true); }}
}}

async function quickAction(action) {{
  if (action === 'restart-server') {{
    if (!confirm('Restart the POS server? Active sessions will be dropped for ~3s.')) return;
    svcAction('restart', 'hanryxvault');
  }} else if (action === 'backup-db') {{
    toast('Triggering DB backup…');
    try {{
      const r = await fetch('/system/backup-db', {{method:'POST'}});
      const d = await r.json();
      toast(d.ok ? 'Backup created: ' + (d.path||'') : 'Error: '+d.error, !d.ok);
    }} catch(e) {{ toast('Error: '+e.message, true); }}
  }} else if (action === 'sync-inventory') {{
    toast('Syncing from cloud…');
    const r = await fetch('/admin/sync-from-cloud?force=0', {{method:'POST'}});
    const d = await r.json();
    toast('Done — upserted: ' + (d.upserted||0));
  }}
}}

async function refreshWgPeers() {{
  try {{
    const r = await fetch('/system/wg-peers');
    const d = await r.json();
    const peers = d.peers || [];
    const tbody = document.getElementById('wgPeerBody');
    const status = document.getElementById('wgStatus');
    if (!peers.length) {{
      tbody.innerHTML = '<tr><td colspan="7" style="color:#555;text-align:center;padding:16px">No WireGuard peers — VPN may be offline or no peers configured</td></tr>';
      status.textContent = 'Offline or no peers';
      return;
    }}
    status.textContent = peers.length + ' peer' + (peers.length!==1?'s':'') + ' · refreshed ' + new Date().toLocaleTimeString();
    tbody.innerHTML = peers.map(p => {{
      const nameCell = p.friendly
        ? `<span style="color:#FFD700;font-weight:700">${{p.friendly}}</span><br><span style="font-size:10px;color:#444">${{p.short_key}}</span>`
        : `<span style="color:#555;font-size:11px">${{p.short_key}}</span>`;
      const hsColor = p.handshake_ok ? '#4caf50' : (p.handshake==='Never'?'#555':'#f59e0b');
      return `<tr>
        <td>${{nameCell}}</td>
        <td style="font-size:11px;color:#888">${{p.allowed_ips}}</td>
        <td style="font-size:11px;color:#888">${{p.endpoint}}</td>
        <td style="color:${{hsColor}};font-size:12px">${{p.handshake}}</td>
        <td style="font-size:12px;color:#aaa">${{p.rx}}</td>
        <td style="font-size:12px;color:#aaa">${{p.tx}}</td>
        <td><button class="act-btn" style="font-size:10px" onclick="fillPeerKey('${{p.pubkey.replace(/'/g,"\\\\'")}}')"
            title="Copy key to label form">Label…</button></td>
      </tr>`;
    }}).join('');
  }} catch(e) {{
    document.getElementById('wgStatus').textContent = 'Error: ' + e.message;
  }}
}}

function fillPeerKey(pubkey) {{
  document.getElementById('wgKeyInp').value = pubkey;
  document.getElementById('wgNameInp').focus();
}}

async function savePeerName() {{
  const pubkey   = document.getElementById('wgKeyInp').value.trim();
  const friendly = document.getElementById('wgNameInp').value.trim();
  if (!pubkey) {{ toast('Paste the public key first', true); return; }}
  const r = await fetch('/system/wg-peer-name', {{
    method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{pubkey, name: friendly}})
  }});
  const d = await r.json();
  if (d.ok) {{
    toast('Label saved: ' + (friendly || '(cleared)'));
    document.getElementById('wgKeyInp').value  = '';
    document.getElementById('wgNameInp').value = '';
    refreshWgPeers();
  }} else toast('Error: ' + (d.error||'unknown'), true);
}}

refresh();
setInterval(refresh, 3000);
refreshWgPeers();
setInterval(refreshWgPeers, 10000);
</script>
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# /admin/logs  — Live log viewer
# ---------------------------------------------------------------------------

@app.route("/admin/logs", methods=["GET"])
@require_admin
def admin_logs():
    nav = _admin_nav("logs")
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HanryxVault — Logs</title>
<style>
{_ADMIN_BASE_CSS}
  .tab-bar{{display:flex;gap:0;border-bottom:1px solid #222;margin-bottom:20px}}
  .tab{{padding:11px 20px;font-size:13px;font-weight:600;color:#555;cursor:pointer;border-bottom:2px solid transparent;transition:.15s}}
  .tab:hover{{color:#aaa}}
  .tab.active{{color:#FFD700;border-bottom-color:#FFD700}}
  .tab-panel{{display:none}}
  .tab-panel.active{{display:block}}
  .log-controls{{display:flex;align-items:center;gap:12px;flex-wrap:wrap;background:#111;border:1px solid #222;border-radius:10px;padding:14px 18px;margin-bottom:16px}}
  .log-controls label{{font-size:12px;color:#666}}
  .log-controls select{{background:#1a1a1a;border:1px solid #333;border-radius:6px;padding:7px 10px;color:#e0e0e0;font-size:13px;outline:none}}
  .log-controls select:focus{{border-color:#FFD700}}
  .log-controls input[type=range]{{accent-color:#FFD700;width:100px}}
  .toggle{{display:flex;align-items:center;gap:6px;font-size:12px;color:#888;cursor:pointer;padding:7px 14px;background:#1a1a1a;border:1px solid #333;border-radius:6px}}
  .toggle input{{accent-color:#FFD700}}
  #logBox{{background:#060606;border:1px solid #1a1a1a;border-radius:10px;padding:16px;font-family:'Courier New',monospace;font-size:12px;color:#a0a0a0;height:65vh;overflow-y:auto;white-space:pre-wrap;word-break:break-all;line-height:1.55}}
  .log-status{{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}}
  .log-status-txt{{font-size:11px;color:#444}}
  .log-line-err{{color:#f87171}}
  .log-line-warn{{color:#fbbf24}}
  .log-line-ok{{color:#4ade80}}
  #searchInp{{background:#1a1a1a;border:1px solid #333;border-radius:6px;padding:7px 10px;color:#e0e0e0;font-size:13px;outline:none;width:200px}}
  #searchInp:focus{{border-color:#FFD700}}
  .highlight{{background:#FFD70044;border-radius:2px}}
  /* Scan History */
  .sh-controls{{display:flex;align-items:center;gap:12px;flex-wrap:wrap;background:#111;border:1px solid #222;border-radius:10px;padding:14px 18px;margin-bottom:16px}}
  .sh-tbl{{width:100%;border-collapse:collapse;font-size:12px}}
  .sh-tbl th{{text-align:left;color:#555;padding:8px 10px;border-bottom:1px solid #222;font-size:11px;text-transform:uppercase;letter-spacing:.8px}}
  .sh-tbl td{{padding:7px 10px;border-bottom:1px solid #111}}
  .sh-tbl tr:hover td{{background:#111}}
  .badge-match{{background:#1a3a1a;color:#4ade80;border-radius:4px;padding:2px 7px;font-size:10px;font-weight:700}}
  .badge-miss{{background:#2a1a1a;color:#f87171;border-radius:4px;padding:2px 7px;font-size:10px;font-weight:700}}
  #shStatus{{font-size:11px;color:#444;margin-bottom:8px}}
</style>
</head>
<body>
{nav}
<div class="wrap">
  <h1>📋 Logs</h1>
  <p class="subtitle">Service logs &amp; barcode scan history from your Pi.</p>

  <div class="tab-bar">
    <div class="tab active" id="tab-syslog" onclick="switchTab('syslog')">📄 System Log</div>
    <div class="tab" id="tab-scanlog" onclick="switchTab('scanlog')">📷 Scan History</div>
  </div>

  <!-- ── System Log ─────────────────────────────────────────────────── -->
  <div class="tab-panel active" id="panel-syslog">
    <div class="log-controls">
      <label>Service:</label>
      <select id="svcSelect" onchange="loadLogs()">
        <option value="hanryxvault">POS Server (hanryxvault)</option>
        <option value="nginx">nginx</option>
        <option value="syslog">syslog</option>
      </select>
      <label>Lines:</label>
      <input type="range" id="linesRange" min="20" max="500" step="20" value="120" oninput="linesLbl.textContent=this.value">
      <span id="linesLbl" style="font-size:12px;color:#888;min-width:32px">120</span>
      <label class="toggle"><input type="checkbox" id="autoRefresh" checked onchange="toggleAuto()"> Auto-refresh (5s)</label>
      <button onclick="loadLogs()" class="btn-gold" style="padding:8px 18px">↺ Refresh</button>
      <input type="text" id="searchInp" placeholder="Search in logs…" oninput="highlightSearch()">
    </div>
    <div class="log-status">
      <span class="log-status-txt" id="logStatus">Loading…</span>
      <span class="log-status-txt" id="logTime"></span>
    </div>
    <div id="logBox">Loading logs…</div>
  </div>

  <!-- ── Scan History ───────────────────────────────────────────────── -->
  <div class="tab-panel" id="panel-scanlog">
    <div class="sh-controls">
      <label style="font-size:12px;color:#666">Show last</label>
      <select id="shLimit" onchange="loadScanLog()">
        <option value="50">50 scans</option>
        <option value="200" selected>200 scans</option>
        <option value="500">500 scans</option>
      </select>
      <button onclick="loadScanLog()" class="btn-gold" style="padding:8px 18px">↺ Refresh</button>
      <span style="margin-left:auto;font-size:12px;color:#555" id="shMatchRate"></span>
    </div>
    <div id="shStatus">Loading scan history…</div>
    <div style="overflow-x:auto">
      <table class="sh-tbl">
        <thead><tr>
          <th>#</th><th>Time</th><th>QR Code</th>
          <th>Card Name</th><th>Match</th><th>Price</th>
        </tr></thead>
        <tbody id="shBody">
          <tr><td colspan="6" style="color:#555;text-align:center;padding:20px">Loading…</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</div>
<div id="toast"></div>

<script>
let _autoTimer = null;
function clock() {{ document.getElementById('clock').textContent = new Date().toLocaleTimeString(); }}
setInterval(clock,1000); clock();

function toast(msg,err=false){{const t=document.getElementById('toast');t.textContent=msg;t.className=err?'err':'';t.style.display='block';setTimeout(()=>t.style.display='none',3000);}}

// ── Tab switching ────────────────────────────────────────────────────────
function switchTab(id) {{
  ['syslog','scanlog'].forEach(t => {{
    document.getElementById('tab-'  +t).classList.toggle('active', t===id);
    document.getElementById('panel-'+t).classList.toggle('active', t===id);
  }});
  if (id==='scanlog') loadScanLog();
}}

// ── System Log tab ───────────────────────────────────────────────────────
async function loadLogs() {{
  const svc   = document.getElementById('svcSelect').value;
  const lines = document.getElementById('linesRange').value;
  document.getElementById('logStatus').textContent = 'Loading…';
  try {{
    const r = await fetch('/system/logs?' + new URLSearchParams({{service:svc,lines}}));
    const d = await r.json();
    const box = document.getElementById('logBox');
    const raw = d.log || '';
    box.innerHTML = raw.split('\\n').map(line => {{
      const cls = /error|exception|traceback|critical/i.test(line) ? 'log-line-err'
                : /warn/i.test(line)   ? 'log-line-warn'
                : /start|ready|listen|ok|success/i.test(line) ? 'log-line-ok'
                : '';
      const esc = line.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      return cls ? `<span class="${{cls}}">${{esc}}</span>` : esc;
    }}).join('\\n');
    box.scrollTop = box.scrollHeight;
    const n = raw.split('\\n').length;
    document.getElementById('logStatus').textContent = svc + ' · ' + n + ' lines';
    document.getElementById('logTime').textContent   = 'Updated: ' + new Date().toLocaleTimeString();
    highlightSearch();
  }} catch(e) {{
    document.getElementById('logBox').textContent = 'Error loading logs: ' + e.message;
    document.getElementById('logStatus').textContent = 'Error';
  }}
}}

function toggleAuto() {{
  clearInterval(_autoTimer);
  if (document.getElementById('autoRefresh').checked) {{
    _autoTimer = setInterval(loadLogs, 5000);
    toast('Auto-refresh on');
  }} else {{
    toast('Auto-refresh off');
  }}
}}

function highlightSearch() {{
  const q = document.getElementById('searchInp').value.trim();
  if (!q) return;
  const box = document.getElementById('logBox');
  box.innerHTML = box.innerHTML.replace(/<mark[^>]*>(.*?)<\/mark>/g, '$1');
  if (q.length < 2) return;
  const re = new RegExp('(' + q.replace(/[.*+?^${{}}()|[\\]\\\\]/g,'\\\\$&') + ')', 'gi');
  box.innerHTML = box.innerHTML.replace(re, '<mark class="highlight">$1</mark>');
}}

// ── Scan History tab ─────────────────────────────────────────────────────
async function loadScanLog() {{
  const limit = document.getElementById('shLimit').value;
  document.getElementById('shStatus').textContent = 'Loading…';
  try {{
    const r = await fetch('/admin/scan-log?limit=' + limit);
    const rows = await r.json();
    const matched = rows.filter(x => x.matched).length;
    const pct = rows.length ? Math.round(matched/rows.length*100) : 0;
    document.getElementById('shMatchRate').textContent =
      rows.length + ' scans · ' + matched + ' matched (' + pct + '%)';
    document.getElementById('shStatus').textContent =
      'Showing last ' + rows.length + ' scans · updated ' + new Date().toLocaleTimeString();
    if (!rows.length) {{
      document.getElementById('shBody').innerHTML =
        '<tr><td colspan="6" style="color:#555;text-align:center;padding:20px">No scans recorded yet</td></tr>';
      return;
    }}
    document.getElementById('shBody').innerHTML = rows.map((s,i) => {{
      const t = new Date(s.scannedAt).toLocaleString();
      const badge = s.matched
        ? '<span class="badge-match">✓ Matched</span>'
        : '<span class="badge-miss">✗ Not found</span>';
      const price = s.price > 0 ? '$' + s.price.toFixed(2) : '—';
      const name  = s.cardName || '<span style="color:#444">—</span>';
      const qr    = `<code style="color:#aaa;font-size:11px">${{s.qrCode}}</code>`;
      return `<tr><td style="color:#555">${{i+1}}</td><td style="color:#666;font-size:11px">${{t}}</td><td>${{qr}}</td><td>${{name}}</td><td>${{badge}}</td><td style="color:#FFD700">${{price}}</td></tr>`;
    }}).join('');
  }} catch(e) {{
    document.getElementById('shStatus').textContent = 'Error: ' + e.message;
  }}
}}

loadLogs();
toggleAuto();
</script>
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# New utility endpoints (stats-partial, export-inventory, scan-log,
#                        sell-one, wg-peers, wg-peer-name)
# ---------------------------------------------------------------------------

@app.route("/admin/stats-partial", methods=["GET"])
@require_admin
def admin_stats_partial():
    """Lightweight JSON snapshot for auto-refreshing the dashboard cards."""
    db = get_db()
    midnight_ms = int(datetime.datetime.combine(
        datetime.date.today(), datetime.time.min
    ).timestamp() * 1000)
    row = db.execute("""
        SELECT COUNT(*) as count,
               COALESCE(SUM(total_amount), 0) as revenue,
               COALESCE(SUM(tax_amount),   0) as tax,
               COALESCE(SUM(tip_amount),   0) as tips
        FROM sales WHERE timestamp_ms >= %s
    """, (midnight_ms,)).fetchone()
    inv_count  = db.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
    low_count  = db.execute("SELECT COUNT(*) FROM inventory WHERE stock <= 5").fetchone()[0]
    # 7-day daily revenue
    seven_days = []
    for i in range(6, -1, -1):
        d       = datetime.date.today() - datetime.timedelta(days=i)
        day_ms  = int(datetime.datetime.combine(d, datetime.time.min).timestamp() * 1000)
        next_ms = day_ms + 86_400_000
        rev = db.execute(
            "SELECT COALESCE(SUM(total_amount),0) FROM sales "
            "WHERE timestamp_ms >= %s AND timestamp_ms < %s",
            (day_ms, next_ms)
        ).fetchone()[0]
        seven_days.append({"label": d.strftime("%a"), "revenue": round(rev, 2)})
    return jsonify({
        "sales_count": row["count"],
        "revenue":     round(row["revenue"], 2),
        "tax":         round(row["tax"], 2),
        "tips":        round(row["tips"], 2),
        "inv_count":   inv_count,
        "low_stock":   low_count,
        "seven_days":  seven_days,
    })


@app.route("/admin/export-inventory", methods=["GET"])
@require_admin
def admin_export_inventory():
    """Download the full inventory as a CSV file."""
    from flask import Response as _Resp
    db     = get_db()
    rows   = db.execute("SELECT * FROM inventory ORDER BY name ASC").fetchall()
    fields = ["qr_code", "name", "price", "stock", "category", "rarity",
              "set_code", "description", "image_url", "tcg_id", "last_updated"]
    buf = io.StringIO()
    w   = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        keys = r.keys()
        w.writerow({f: (r[f] if f in keys else "") for f in fields})
    return _Resp(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=hanryxvault-inventory.csv"},
    )


@app.route("/admin/scan-log", methods=["GET"])
@require_admin
def admin_scan_log():
    """Return recent scan history for the Logs page Scan History tab."""
    try:
        limit = min(int(request.args.get("limit", 200)), 1000)
    except (ValueError, TypeError):
        limit = 200
    db   = get_db()
    rows = db.execute(
        "SELECT id, qr_code, card_name, matched, price, scanned_at "
        "FROM scan_log ORDER BY scanned_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    return jsonify([{
        "id":        r["id"],
        "qrCode":    r["qr_code"],
        "cardName":  r["card_name"],
        "matched":   bool(r["matched"]),
        "price":     r["price"],
        "scannedAt": r["scanned_at"],
    } for r in rows])


@app.route("/admin/sell-one/<path:qr_code>", methods=["POST"])
@require_admin
def admin_sell_one(qr_code):
    """
    Quick-sell: decrement stock by 1 and record a minimal sale entry.
    Used by the dashboard 'Sell 1' button for fast walk-up sales.
    """
    db  = get_db()
    row = db.execute(
        "SELECT name, price, stock FROM inventory WHERE qr_code = %s", (qr_code,)
    ).fetchone()
    if not row:
        return jsonify({"error": "Product not found"}), 404
    if row["stock"] <= 0:
        return jsonify({"error": "Out of stock"}), 409
    db.execute(
        "UPDATE inventory SET stock = stock - 1, last_updated = %s WHERE qr_code = %s",
        (_now_ms(), qr_code)
    )
    tid = f"QUICK-{_now_ms()}"
    db.execute("""
        INSERT INTO sales (transaction_id, timestamp_ms, subtotal, tax_amount,
                           tip_amount, total_amount, payment_method, employee_id,
                           items_json, source)
        VALUES (%s,%s,%s,0,0,%s,%s,%s,%s,%s)
    """, (
        tid, _now_ms(), row["price"], row["price"],
        "CASH", "dashboard",
        json.dumps([{"qrCode": qr_code, "name": row["name"],
                     "qty": 1, "unitPrice": row["price"]}]),
        "quick-sell",
    ))
    db.commit()
    _invalidate_inventory()
    return jsonify({"ok": True, "qrCode": qr_code, "newStock": row["stock"] - 1,
                    "price": row["price"]})


@app.route("/system/wg-peers", methods=["GET"])
@require_admin
def system_wg_peers_api():
    """Return parsed WireGuard peer list as JSON."""
    return jsonify({"peers": _sys_wg_peer_list()})


@app.route("/system/wg-peer-name", methods=["POST"])
@require_admin
def system_wg_peer_name():
    """Set a friendly name for a WireGuard peer public key."""
    data    = request.get_json(silent=True) or {}
    pubkey  = (data.get("pubkey") or "").strip()
    friendly = (data.get("name") or "").strip()[:64]
    if not pubkey:
        return jsonify({"error": "pubkey required"}), 400
    db = get_db()
    db.execute(
        "INSERT INTO wg_peer_names (pubkey, friendly_name) VALUES (%s,%s) "
        "ON CONFLICT(pubkey) DO UPDATE SET friendly_name=excluded.friendly_name",
        (pubkey, friendly)
    )
    db.commit()
    return jsonify({"ok": True, "pubkey": pubkey[:16] + "…", "name": friendly})


# ---------------------------------------------------------------------------
# /system/service-action  — restart / stop a systemd service
# ---------------------------------------------------------------------------

@app.route("/system/service-action", methods=["POST"])
@require_admin
def system_service_action():
    data    = request.get_json(silent=True) or {}
    action  = data.get("action", "")
    service = data.get("service", "")
    allowed_actions  = {"restart", "stop", "start", "status"}
    allowed_services = {svc for _, svc in _SYS_SERVICES}
    if action not in allowed_actions:
        return jsonify({"ok": False, "error": "Unknown action"}), 400
    if service not in allowed_services:
        return jsonify({"ok": False, "error": "Service not allowed"}), 400
    try:
        subprocess.Popen(
            ["sudo", "systemctl", action, service],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return jsonify({"ok": True, "message": f"{action} sent to {service}"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# /system/backup-db  — pg_dump the PostgreSQL database to a timestamped SQL file
# ---------------------------------------------------------------------------

@app.route("/system/backup-db", methods=["POST"])
@require_admin
def system_backup_db():
    import subprocess
    ts      = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup  = f"/app/data/vaultpos_backup_{ts}.sql"
    try:
        result = subprocess.run(
            ["pg_dump", "--no-password", DATABASE_URL, "-f", backup],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return jsonify({"ok": False, "error": result.stderr.strip()}), 500
        size = os.path.getsize(backup)
        return jsonify({"ok": True, "path": backup, "size_kb": round(size / 1024, 1)})
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "pg_dump not found — install postgresql-client"}), 500
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Admin authentication — login / logout / guard decorator
# ---------------------------------------------------------------------------

_ADMIN_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>HanryxVault — Admin Login</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:#0f0f0f;color:#e0e0e0;font-family:'Segoe UI',sans-serif;
         display:flex;align-items:center;justify-content:center;min-height:100vh}
    .card{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:12px;
          padding:40px 36px;width:340px;text-align:center}
    h1{font-size:1.3rem;margin-bottom:6px;color:#fff}
    p{font-size:.85rem;color:#888;margin-bottom:28px}
    input{width:100%;padding:10px 14px;border-radius:8px;border:1px solid #333;
          background:#111;color:#e0e0e0;font-size:.95rem;margin-bottom:16px}
    button{width:100%;padding:11px;border-radius:8px;border:none;
           background:#6366f1;color:#fff;font-size:.95rem;font-weight:600;cursor:pointer}
    button:hover{background:#4f46e5}
    .err{color:#f87171;font-size:.85rem;margin-bottom:14px}
  </style>
</head>
<body>
  <div class="card">
    <h1>🔐 HanryxVault Admin</h1>
    <p>Enter your admin password to continue</p>
    {% if error %}<div class="err">{{ error }}</div>{% endif %}
    <form method="POST" action="/admin/login">
      <input type="password" name="password" placeholder="Admin password" autofocus>
      <input type="hidden" name="next" value="{{ next }}">
      <button type="submit">Sign In</button>
    </form>
  </div>
</body>
</html>"""


def require_admin(fn):
    """Decorator that redirects unauthenticated requests to /admin/login."""
    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        if not session.get("admin_authenticated"):
            return redirect(f"/admin/login?next={request.path}")
        return fn(*args, **kwargs)
    return _wrapped


# ---------------------------------------------------------------------------
# Pricing engine API
# ---------------------------------------------------------------------------

@app.route("/admin/price-calc", methods=["GET"])
@require_admin
def admin_price_calc():
    """Return suggested sell price given base market price + card attributes."""
    try:
        base      = float(request.args.get("base", 0))
        language  = request.args.get("lang", "English")
        item_type = request.args.get("type", "Single")
        grade     = request.args.get("grade", "")
        return jsonify({
            "suggested_price": _calculate_final_price(base, language, item_type, grade),
            "language":        language,
            "item_type":       item_type,
            "grade":           grade,
            "multipliers": {
                "language":   _LANGUAGE_PRICE_RULES.get(language, 1.0),
                "grade":      _GRADE_MULTIPLIER.get(grade, 1.0) if grade else None,
                "item_type":  _ITEM_TYPE_UNDERCUT.get(item_type, 1.0),
            },
        })
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400


# ---------------------------------------------------------------------------
# Goals  (collection targets with progress tracking)
# ---------------------------------------------------------------------------

@app.route("/admin/goals", methods=["GET"])
@require_admin
def admin_goals_get():
    db = get_db()
    rows = db.execute(
        "SELECT id, title, type, target_value, target_set, completed, created_at "
        "FROM goals ORDER BY created_at DESC"
    ).fetchall()
    # Compute progress for each goal
    total_cards  = db.execute("SELECT COUNT(*) FROM inventory WHERE stock>0").fetchone()[0]
    total_value  = db.execute("SELECT COALESCE(SUM(price*stock),0) FROM inventory WHERE stock>0").fetchone()[0]
    goals = []
    for r in rows:
        g = {
            "id": r[0], "title": r[1], "type": r[2],
            "targetValue": r[3], "targetSet": r[4],
            "completed": bool(r[5]), "createdAt": r[6],
        }
        if g["type"] == "card_count":
            g["current"] = total_cards
        elif g["type"] == "value_target":
            g["current"] = float(total_value)
        elif g["type"] == "set_completion" and g["targetSet"]:
            g["current"] = db.execute(
                "SELECT COUNT(*) FROM inventory WHERE set_code=%s AND stock>0",
                (g["targetSet"],)
            ).fetchone()[0]
        else:
            g["current"] = 0
        goals.append(g)
    return jsonify(goals)


@app.route("/admin/goals", methods=["POST"])
@require_admin
def admin_goals_post():
    data = request.get_json(force=True, silent=True) or {}
    title  = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title is required"}), 400
    db = get_db()
    db.execute(
        "INSERT INTO goals (title, type, target_value, target_set) VALUES (%s, %s, %s, %s)",
        (title, data.get("type", "card_count"),
         int(data.get("targetValue") or data.get("target_value") or 1),
         data.get("targetSet") or data.get("target_set") or ""),
    )
    db.commit()
    return jsonify({"ok": True}), 201


@app.route("/admin/goals/<int:goal_id>", methods=["PATCH"])
@require_admin
def admin_goals_patch(goal_id):
    data = request.get_json(force=True, silent=True) or {}
    db   = get_db()
    row  = db.execute("SELECT id FROM goals WHERE id=%s", (goal_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    if "completed" in data:
        db.execute("UPDATE goals SET completed=%s WHERE id=%s",
                   (1 if data["completed"] else 0, goal_id))
    if "title" in data:
        db.execute("UPDATE goals SET title=%s WHERE id=%s",
                   (data["title"].strip(), goal_id))
    if "targetValue" in data or "target_value" in data:
        db.execute("UPDATE goals SET target_value=%s WHERE id=%s",
                   (int(data.get("targetValue") or data.get("target_value")), goal_id))
    db.commit()
    return jsonify({"ok": True})


@app.route("/admin/goals/<int:goal_id>", methods=["DELETE"])
@require_admin
def admin_goals_delete(goal_id):
    db = get_db()
    db.execute("DELETE FROM goals WHERE id=%s", (goal_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Collection sharing  (public read-only share link)
# ---------------------------------------------------------------------------

@app.route("/admin/share-token", methods=["GET"])
@require_admin
def admin_get_share_token():
    db  = get_db()
    row = db.execute("SELECT value FROM server_state WHERE key='share_token'").fetchone()
    return jsonify({"token": row[0] if row else None})


@app.route("/admin/share-token", methods=["POST"])
@require_admin
def admin_create_share_token():
    import secrets as _secrets
    token = _secrets.token_hex(20)
    db    = get_db()
    db.execute(
        "INSERT INTO server_state (key, value) VALUES ('share_token', %s) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (token,)
    )
    db.commit()
    return jsonify({"token": token})


@app.route("/admin/share-token", methods=["DELETE"])
@require_admin
def admin_delete_share_token():
    db = get_db()
    db.execute("DELETE FROM server_state WHERE key='share_token'")
    db.commit()
    return jsonify({"token": None})


@app.route("/share/<token>", methods=["GET"])
def public_share(token):
    """Public read-only card collection page — no auth required."""
    db  = get_db()
    row = db.execute("SELECT value FROM server_state WHERE key='share_token'").fetchone()
    if not row or row[0] != token:
        return "<h2>This collection link is invalid or has been revoked.</h2>", 404

    cards = db.execute(
        "SELECT qr_code, name, price, rarity, set_code, stock, image_url, "
        "language, condition, item_type, grade, tags "
        "FROM inventory WHERE stock>0 ORDER BY name"
    ).fetchall()

    rows_html = ""
    for c in cards:
        img = f'<img src="{c[6]}" style="height:40px;border-radius:4px">' if c[6] else "—"
        tags_html = "".join(f'<span style="background:#1e3a5f;border-radius:4px;padding:1px 6px;font-size:11px;margin:1px">{t}</span>'
                            for t in (c[11] or "").split(",") if t.strip())
        rows_html += (
            f"<tr><td>{img}</td><td>{c[1]}</td>"
            f"<td>{c[3] or '—'}</td><td>{c[4] or '—'}</td>"
            f"<td>{c[7]}</td><td>{c[8]}</td><td>{c[9]}</td>"
            f"<td>{'⭐ ' + c[10] if c[10] else '—'}</td>"
            f"<td>${float(c[2]):.2f}</td><td>{c[5]}</td>"
            f"<td>{tags_html}</td></tr>\n"
        )
    total_val = sum(float(c[2]) * int(c[5]) for c in cards)

    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Card Collection</title>
<style>
  body{{font-family:system-ui,sans-serif;background:#0a0f1e;color:#e2e8f0;margin:0;padding:16px}}
  h1{{color:#facc15;font-size:1.5rem;margin-bottom:4px}}
  .subtitle{{color:#94a3b8;font-size:.85rem;margin-bottom:16px}}
  table{{width:100%;border-collapse:collapse;font-size:.85rem}}
  th{{background:#1e293b;color:#94a3b8;padding:8px 10px;text-align:left;position:sticky;top:0}}
  td{{padding:6px 10px;border-bottom:1px solid #1e293b}}
  tr:hover td{{background:#0f172a}}
  .search{{background:#1e293b;border:1px solid #334155;color:#e2e8f0;
           padding:8px 14px;border-radius:8px;width:100%;max-width:340px;
           margin-bottom:14px;font-size:.9rem}}
  .stats{{display:flex;gap:24px;margin-bottom:16px;flex-wrap:wrap}}
  .stat{{background:#1e293b;border-radius:8px;padding:10px 18px}}
  .stat-val{{font-size:1.3rem;font-weight:700;color:#38bdf8}}
  .stat-lbl{{font-size:.75rem;color:#64748b}}
</style></head><body>
<h1>HanryxVault — Card Collection</h1>
<p class="subtitle">Public view · {len(cards)} cards listed</p>
<div class="stats">
  <div class="stat"><div class="stat-val">{len(cards)}</div><div class="stat-lbl">Cards</div></div>
  <div class="stat"><div class="stat-val">${total_val:,.2f}</div><div class="stat-lbl">Total Value</div></div>
</div>
<input class="search" id="srch" placeholder="Search by name, set, rarity…" oninput="filter()">
<table id="tbl">
<thead><tr><th>Image</th><th>Name</th><th>Rarity</th><th>Set</th>
<th>Lang</th><th>Cond</th><th>Type</th><th>Grade</th>
<th>Price</th><th>Stock</th><th>Tags</th></tr></thead>
<tbody id="tbody">{rows_html}</tbody>
</table>
<script>
function filter(){{
  const q=document.getElementById('srch').value.toLowerCase();
  document.querySelectorAll('#tbody tr').forEach(r=>{{
    r.style.display=r.textContent.toLowerCase().includes(q)?'':'none';
  }});
}}
</script></body></html>"""
    return html


# ---------------------------------------------------------------------------
# Price change alerts  (cards with >15% market price movement)
# ---------------------------------------------------------------------------

@app.route("/admin/price-alerts", methods=["GET"])
@require_admin
def admin_price_alerts():
    """Return cards whose market price moved >15% between first and latest reading."""
    threshold = float(request.args.get("threshold", 15))
    db = get_db()
    # Get min/max price per card_id from price_history
    rows = db.execute("""
        SELECT
            ph.card_id,
            ph.card_name,
            MIN(ph.market_price) AS price_low,
            MAX(ph.market_price) AS price_high,
            (SELECT market_price FROM price_history p2
             WHERE p2.card_id=ph.card_id ORDER BY p2.fetched_ms ASC LIMIT 1) AS first_price,
            (SELECT market_price FROM price_history p3
             WHERE p3.card_id=ph.card_id ORDER BY p3.fetched_ms DESC LIMIT 1) AS last_price,
            COUNT(*) AS readings
        FROM price_history ph
        GROUP BY ph.card_id, ph.card_name
        HAVING COUNT(*) >= 2
    """).fetchall()

    alerts = []
    for r in rows:
        first, last = float(r[4] or 0), float(r[5] or 0)
        if first <= 0:
            continue
        pct_change = ((last - first) / first) * 100
        if abs(pct_change) >= threshold:
            alerts.append({
                "cardId":     r[0],
                "cardName":   r[1],
                "firstPrice": round(first, 2),
                "lastPrice":  round(last, 2),
                "pctChange":  round(pct_change, 1),
                "direction":  "up" if pct_change > 0 else "down",
                "readings":   r[6],
            })
    alerts.sort(key=lambda a: abs(a["pctChange"]), reverse=True)
    return jsonify(alerts)


# ---------------------------------------------------------------------------
# Valuation report  (printable profit/loss table)
# ---------------------------------------------------------------------------

@app.route("/admin/valuation-report", methods=["GET"])
@require_admin
def admin_valuation_report():
    """Printable HTML valuation report with profit/loss per card."""
    db = get_db()
    cards = db.execute("""
        SELECT name, set_code, condition, language, stock, price,
               purchase_price, sale_price, rarity, item_type, grade
        FROM inventory
        WHERE stock > 0
        ORDER BY name
    """).fetchall()

    total_market = total_cost = total_revenue = total_profit = 0.0
    rows_html = ""
    for c in cards:
        name, set_code, cond, lang, qty, price = c[0], c[1], c[2], c[3], int(c[4]), float(c[5])
        purchase, sale = float(c[6]), float(c[7])
        rarity, item_type, grade = c[8], c[9], c[10]
        market_line = price * qty
        cost_line   = purchase * qty
        rev_line    = sale * qty if sale else 0
        profit_line = (sale - purchase) * qty if sale and purchase else 0
        total_market  += market_line
        total_cost    += cost_line
        total_revenue += rev_line
        total_profit  += profit_line
        profit_color = "#4ade80" if profit_line >= 0 else "#f87171"
        grade_str = f" ({grade})" if grade else ""
        rows_html += (
            f"<tr>"
            f"<td>{name}</td><td>{set_code or '—'}</td>"
            f"<td>{cond}</td><td>{lang}</td><td>{item_type}{grade_str}</td>"
            f"<td>{qty}</td>"
            f"<td>${price:.2f}</td><td>${market_line:.2f}</td>"
            f"<td>${purchase:.2f}</td><td>${cost_line:.2f}</td>"
            f"<td>{'$'+f'{sale:.2f}' if sale else '—'}</td>"
            f"<td style='color:{profit_color}'>"
            f"{'$'+f'{profit_line:+.2f}' if purchase else '—'}</td>"
            f"</tr>\n"
        )
    overall_profit_color = "#4ade80" if total_profit >= 0 else "#f87171"

    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<title>HanryxVault — Valuation Report</title>
<style>
  @media print{{
    .no-print{{display:none!important}}
    body{{background:#fff!important;color:#000!important}}
    th{{background:#f1f5f9!important;color:#0f172a!important}}
    td{{border-color:#e2e8f0!important}}
    .totals td{{background:#f8fafc!important}}
  }}
  body{{font-family:system-ui,sans-serif;background:#0a0f1e;color:#e2e8f0;
        padding:24px;margin:0;font-size:.82rem}}
  h1{{color:#facc15;margin-bottom:2px}}
  .meta{{color:#64748b;font-size:.78rem;margin-bottom:16px}}
  .no-print{{margin-bottom:14px}}
  button{{background:#1e3a5f;color:#e2e8f0;border:none;padding:7px 16px;
          border-radius:6px;cursor:pointer;font-size:.85rem}}
  table{{width:100%;border-collapse:collapse}}
  th{{background:#1e293b;color:#94a3b8;padding:7px 9px;text-align:left;
      position:sticky;top:0;white-space:nowrap}}
  td{{padding:5px 9px;border-bottom:1px solid #1e293b}}
  .totals td{{background:#1e293b;font-weight:700;color:#facc15}}
  .summary{{display:flex;gap:20px;margin-bottom:18px;flex-wrap:wrap}}
  .scard{{background:#1e293b;border-radius:8px;padding:10px 16px;min-width:140px}}
  .sval{{font-size:1.2rem;font-weight:700}}
  .slbl{{font-size:.72rem;color:#64748b;margin-top:2px}}
</style></head><body>
<h1>HanryxVault — Valuation Report</h1>
<p class="meta">Generated {__import__('datetime').datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC
· {len(cards)} SKUs in stock</p>
<div class="no-print"><button onclick="window.print()">🖨 Print / Save PDF</button></div>
<div class="summary">
  <div class="scard"><div class="sval">{len(cards)}</div><div class="slbl">SKUs</div></div>
  <div class="scard"><div class="sval">${total_market:,.2f}</div><div class="slbl">Market Value</div></div>
  <div class="scard"><div class="sval">${total_cost:,.2f}</div><div class="slbl">Cost Basis</div></div>
  <div class="scard"><div class="sval">${total_revenue:,.2f}</div><div class="slbl">Revenue (sold)</div></div>
  <div class="scard"><div class="sval" style="color:{overall_profit_color}">${total_profit:+,.2f}</div><div class="slbl">Profit/Loss</div></div>
</div>
<table>
<thead><tr>
  <th>Name</th><th>Set</th><th>Cond</th><th>Lang</th><th>Type</th><th>Qty</th>
  <th>Price ea.</th><th>Market Total</th>
  <th>Purchase ea.</th><th>Cost Total</th>
  <th>Sale Price</th><th>P/L</th>
</tr></thead>
<tbody>{rows_html}</tbody>
<tfoot class="totals"><tr>
  <td colspan="7">TOTALS</td>
  <td>${total_market:,.2f}</td>
  <td></td>
  <td>${total_cost:,.2f}</td>
  <td>${total_revenue:,.2f}</td>
  <td style="color:{overall_profit_color}">${total_profit:+,.2f}</td>
</tr></tfoot>
</table></body></html>"""
    return html


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "unknown").split(",")[0].strip()
    if request.method == "POST":
        # Brute-force gate
        allowed, mins_left = _check_login_rate(ip)
        if not allowed:
            return render_template_string(
                _ADMIN_LOGIN_HTML,
                error=f"Too many failed attempts. Try again in {mins_left} minute{'s' if mins_left != 1 else ''}.",
                next=request.form.get("next", "/admin"),
            )
        password = request.form.get("password", "")
        next_url  = request.form.get("next", "/admin")
        if hashlib.sha256(password.encode()).hexdigest() == \
           hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest():
            _clear_login_attempts(ip)
            log.info("[admin] login successful from %s", ip)
            # Check if TOTP 2FA is enabled
            try:
                db2 = _direct_db()
                totp_row = db2.execute(
                    "SELECT secret, enabled FROM totp_secrets WHERE username='admin' AND enabled=1"
                ).fetchone()
                db2.close()
            except Exception:
                totp_row = None
            if totp_row:
                # Partial auth — require TOTP next
                session["admin_2fa_pending"] = True
                session["admin_next_url"]    = next_url if next_url.startswith("/admin") else "/admin"
                return redirect("/admin/2fa/verify")
            session["admin_authenticated"] = True
            session["admin_user"]          = "admin"
            session.permanent = True
            _audit_write("admin.login", "admin_dashboard", f"ip={ip}")
            return redirect(next_url if next_url.startswith("/admin") else "/admin")
        _record_failed_login(ip)
        log.warning("[admin] failed login attempt from %s (attempt %s)",
                    ip, _login_attempts.get(ip, {}).get("count", "?"))
        remaining = _BF_MAX_ATTEMPTS - _login_attempts.get(ip, {}).get("count", 0)
        err_msg = "Incorrect password."
        if remaining <= 2 and remaining > 0:
            err_msg += f" ({remaining} attempt{'s' if remaining != 1 else ''} before lockout)"
        return render_template_string(
            _ADMIN_LOGIN_HTML, error=err_msg, next=next_url
        )
    next_url = request.args.get("next", "/admin")
    return render_template_string(_ADMIN_LOGIN_HTML, error=None, next=next_url)


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin_authenticated", None)
    return redirect("/admin/login")


# ---------------------------------------------------------------------------
# Admin dashboard (HTML)
# ---------------------------------------------------------------------------

@app.route("/admin", methods=["GET"])
@require_admin
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
        FROM sales WHERE timestamp_ms >= %s
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

    def _inv_row(r):
        qr      = e(r["qr_code"], quote=True)
        nm      = e(r["name"], quote=True)
        sell_ex = 'disabled title="Out of stock"' if r["stock"] <= 0 else ""
        return (
            f'<tr data-qr="{qr}" data-name="{nm}" data-price="{r["price"]}" '
            f'data-cat="{e(r["category"], quote=True)}" data-stock="{r["stock"]}" style="cursor:pointer">'
            f'<td>{e(r["name"])}</td><td><code style="color:#aaa">{e(r["qr_code"])}</code></td>'
            f'<td id="stock-{qr}">{r["stock"]}</td>'
            f'<td>${r["price"]:.2f}</td><td>{e(r["category"])}</td>'
            f'<td style="white-space:nowrap">'
            f'<button class="btn-sell" onclick="event.stopPropagation();sellOne(\'{qr}\',\'{nm}\',{r["price"]})" {sell_ex}>Sell 1</button> '
            f'<a href="/admin/qr/{urllib.parse.quote(r["qr_code"], safe="")}" target="_blank" '
            f'   onclick="event.stopPropagation()" title="View QR code" '
            f'   style="display:inline-block;padding:3px 8px;background:#1e3a5f;color:#7dd3fc;'
            f'          border-radius:4px;font-size:11px;text-decoration:none;border:1px solid #2563eb">QR</a> '
            f'<button class="btn-del" onclick="event.stopPropagation();deleteProduct(\'{qr}\')">DEL</button>'
            f'</td></tr>'
        )
    rows_inv = "".join(_inv_row(r) for r in inventory) or \
        "<tr><td colspan='7' style='color:#555;text-align:center;padding:20px'>No products yet</td></tr>"

    # 7-day revenue for sparkline (server-side)
    _spark_vals = []
    _spark_labels = []
    for _i in range(6, -1, -1):
        _d      = datetime.date.today() - datetime.timedelta(days=_i)
        _day_ms = int(datetime.datetime.combine(_d, datetime.time.min).timestamp() * 1000)
        _rev    = db.execute(
            "SELECT COALESCE(SUM(total_amount),0) FROM sales "
            "WHERE timestamp_ms >= %s AND timestamp_ms < %s",
            (_day_ms, _day_ms + 86_400_000)
        ).fetchone()[0]
        _spark_vals.append(float(_rev))
        _spark_labels.append(_d.strftime("%a"))
    sparkline_svg = _sparkline_svg(_spark_vals)
    spark_labels_html = "".join(
        f'<span style="flex:1;text-align:center;font-size:9px;color:#444">{lbl}</span>'
        for lbl in _spark_labels
    )
    inv_count = db.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
    low_stock_count = len(low_stock)

    # Goals with live progress
    total_inv_value = db.execute(
        "SELECT COALESCE(SUM(price*stock),0) FROM inventory WHERE stock>0"
    ).fetchone()[0]
    goal_rows = db.execute(
        "SELECT id, title, type, target_value, target_set, completed FROM goals ORDER BY created_at DESC"
    ).fetchall()
    goals_html = ""
    for gr in goal_rows:
        gid, gtitle, gtype, gtarget, gset, gcompleted = gr[0], gr[1], gr[2], gr[3], gr[4], gr[5]
        if gtype == "card_count":
            gcurrent = inv_count
        elif gtype == "value_target":
            gcurrent = float(total_inv_value)
        elif gtype == "set_completion" and gset:
            gcurrent = db.execute(
                "SELECT COUNT(*) FROM inventory WHERE set_code=%s AND stock>0", (gset,)
            ).fetchone()[0]
        else:
            gcurrent = 0
        pct = min(100, int((gcurrent / gtarget * 100) if gtarget else 0))
        pct_color = "#4caf50" if pct >= 100 else "#FFD700"
        strike = "text-decoration:line-through;opacity:.5;" if gcompleted else ""
        lbl = f"{gcurrent:.0f} / {gtarget}"
        goals_html += (
            f'<div style="margin-bottom:10px">'
            f'<div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:3px">'
            f'<span style="{strike}">{_html.escape(gtitle)}'
            f'<span style="color:#555;font-size:11px;margin-left:8px">[{gtype}]</span></span>'
            f'<span style="color:{pct_color}">{pct}% &nbsp; {lbl}</span></div>'
            f'<div style="background:#1a1a1a;border-radius:4px;height:6px;overflow:hidden">'
            f'<div style="background:{pct_color};width:{pct}%;height:100%;transition:width .4s"></div></div>'
            f'<div style="text-align:right;margin-top:3px">'
            f'<button onclick="toggleGoal({gid},{1 if not gcompleted else 0})" '
            f'style="background:{"#2d4a2d" if not gcompleted else "#333"};color:#aaa;border:none;'
            f'padding:2px 8px;border-radius:4px;font-size:11px;cursor:pointer">'
            f'{"✓ Mark Done" if not gcompleted else "↩ Reopen"}</button> '
            f'<button onclick="deleteGoal({gid})" '
            f'style="background:#2a1a1a;color:#f44;border:none;padding:2px 8px;border-radius:4px;'
            f'font-size:11px;cursor:pointer">DEL</button></div></div>'
        )
    if not goals_html:
        goals_html = '<p style="color:#555;font-size:13px">No goals set. Add your first collection goal below.</p>'

    # Share token status
    share_row = db.execute("SELECT value FROM server_state WHERE key='share_token'").fetchone()
    share_token = share_row[0] if share_row else None
    if share_token:
        share_ui = (
            f'<p style="color:#4caf50;font-size:13px">✓ Public link active</p>'
            f'<code style="background:#111;border:1px solid #2a2a2a;border-radius:4px;padding:4px 8px;'
            f'font-size:11px;word-break:break-all">/share/{share_token}</code>'
            f'<div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">'
            f'<button class="btn-gold" onclick="copyShareLink(\'/share/{share_token}\')" style="font-size:12px">Copy Link</button>'
            f'<button onclick="revokeShare()" style="background:#2a1a1a;color:#f44;border:1px solid #3a2020;'
            f'border-radius:6px;padding:6px 14px;font-size:12px;cursor:pointer">Revoke</button></div>'
        )
    else:
        share_ui = (
            '<p style="color:#888;font-size:13px">No public link active.</p>'
            '<button class="btn-gold" onclick="generateShare()">Generate Share Link</button>'
        )

    nav = _admin_nav("dashboard")
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HanryxVault POS — Dashboard</title>
<style>
{_ADMIN_BASE_CSS}
  .cards{{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:24px}}
  .card{{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:10px;padding:18px 20px;min-width:150px;flex:1}}
  .card label{{color:#777;font-size:10px;letter-spacing:1px;text-transform:uppercase;display:block;margin-bottom:4px}}
  .card .value{{color:#FFD700;font-size:28px;font-weight:900}}
  .card .value.green{{color:#4caf50}}
  .form-panel{{background:#111;border:1px solid #2a2a2a;border-radius:10px;padding:20px;margin-top:24px}}
  .form-panel h2{{margin-top:0;margin-bottom:14px;font-size:11px;color:#555;letter-spacing:1.5px;text-transform:uppercase}}
  .form-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px}}
  .form-grid input,.form-grid select{{width:100%;background:#1a1a1a;border:1px solid #333;border-radius:6px;color:#e0e0e0;padding:8px 10px;font-size:13px}}
  .form-grid input:focus,.form-grid select:focus{{outline:none;border-color:#FFD700}}
  .form-grid label{{display:block;color:#666;font-size:10px;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px}}
  .btn-del{{background:none;border:1px solid #c62828;color:#c62828;border-radius:4px;padding:3px 8px;font-size:11px;cursor:pointer}}
  .btn-del:hover{{background:#c62828;color:#fff}}
  .btn-sell{{background:none;border:1px solid #4caf50;color:#4caf50;border-radius:4px;padding:3px 9px;font-size:11px;cursor:pointer;margin-right:4px}}
  .btn-sell:hover{{background:#4caf50;color:#000}}
  .btn-sell:disabled{{border-color:#333;color:#444;cursor:default}}
  .sparkline-panel{{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:10px;padding:18px 20px;flex:2;min-width:220px}}
  .sparkline-panel label{{color:#777;font-size:10px;letter-spacing:1px;text-transform:uppercase;display:block;margin-bottom:8px}}
  #add-product{{scroll-margin-top:80px}}

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
{nav}
<div class="wrap">
<h1>🏠 Dashboard</h1>
<div class="subtitle">Raspberry Pi POS &nbsp;·&nbsp; <span id="clock"></span>
  &nbsp;·&nbsp; <a href="/admin/market">📈 Market Prices</a>
  &nbsp;·&nbsp; <a href="/admin/system">⚙️ System</a>
  &nbsp;·&nbsp; <a href="/admin/valuation-report" target="_blank" style="color:#facc15">📊 Valuation</a>
  &nbsp;·&nbsp; <a href="/download/apk" style="background:#FFD700;color:#000;padding:3px 8px;border-radius:4px;font-weight:bold;font-size:12px;">⬇ APK</a>
</div>

<div style="display:flex;gap:14px;flex-wrap:wrap;margin-bottom:24px;align-items:stretch">
  <div style="display:flex;flex-direction:column;gap:14px;flex:1;min-width:220px">
    <div class="cards" style="margin-bottom:0">
      <div class="card"><label>Today's Sales</label><div class="value green" id="stat-count">{today_sales['count']}</div></div>
      <div class="card"><label>Revenue Today</label><div class="value" id="stat-rev">${today_sales['revenue']:.2f}</div></div>
    </div>
    <div class="cards" style="margin-bottom:0">
      <div class="card"><label>Tax Collected</label><div class="value" id="stat-tax">${today_sales['tax']:.2f}</div></div>
      <div class="card"><label>Tips Today</label><div class="value" id="stat-tips">${today_sales['tips']:.2f}</div></div>
    </div>
    <div class="cards" style="margin-bottom:0">
      <div class="card"><label>Inventory Items</label><div class="value" id="stat-inv">{inv_count}</div></div>
      <div class="card"><label>Low Stock Items</label><div class="value{'red' if low_stock_count > 0 else ''}" id="stat-low">{low_stock_count}</div></div>
    </div>
  </div>
  <div class="sparkline-panel">
    <label>7-Day Revenue</label>
    <div id="sparkline-svg">{sparkline_svg}</div>
    <div style="display:flex;margin-top:4px">{spark_labels_html}</div>
    <div style="font-size:10px;color:#333;margin-top:6px" id="spark-last-refresh">Loaded at page render</div>
  </div>
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

<div class="form-panel" style="border-color:#2563eb;background:#00080f">
  <h2 style="color:#7dd3fc">📦 SYNC FROM INVENTORY SCANNER</h2>
  <p style="color:#aaa;font-size:13px;margin-bottom:12px">
    Pulls all scanned products from your <b style="color:#fff">Inventory-Scanner</b> GitHub repo
    (<code style="color:#7dd3fc">Ngansen/Inventory-Scanner</code>).
    Adds any new cards not yet in the POS — existing cards are updated by name/price/category.
    Set <code style="color:#7dd3fc">INVENTORY_SCANNER_URL</code> in your <code>.env</code> for fastest sync.
  </p>
  <button class="btn-gold" onclick="syncScanner(false)" style="margin-right:12px;background:#2563eb">☁ Sync Scanner</button>
  <button class="btn-gold" onclick="syncScanner(true)" style="background:#1e40af">🔄 Force Re-Sync Scanner</button>
  <div id="scanner-sync-result" style="margin-top:12px;font-size:13px;color:#aaa"></div>
</div>

<h2>Recent Sales</h2>
<table>
<thead><tr><th>Transaction</th><th>Total</th><th>Method</th><th>Employee</th><th>Time</th></tr></thead>
<tbody id="tbody-sales">{rows_recent or '<tr><td colspan="5" style="color:#555;text-align:center;padding:20px">No sales yet today</td></tr>'}</tbody>
</table>

<!-- ── Sale History (30-day per-item) ─────────────────────────────── -->
<div class="form-panel" style="border-color:#c084fc;background:#0d0417;margin-top:24px">
  <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px">
    <h2 style="color:#c084fc;margin:0">📋 SALE HISTORY (30 DAYS)</h2>
    <div style="display:flex;gap:8px;align-items:center">
      <input id="sh-search" placeholder="Filter by name…"
             style="background:#1a1a1a;border:1px solid #4a2060;border-radius:6px;color:#e0e0e0;
                    padding:6px 10px;font-size:12px;width:160px"
             oninput="loadSaleHistory()" />
      <button onclick="loadSaleHistory()"
              style="background:#4a2060;color:#c084fc;border:1px solid #6b31a0;
                     border-radius:6px;padding:6px 12px;font-size:12px;cursor:pointer">
        ↻ Refresh
      </button>
      <a href="/offline-search" target="_blank"
         style="background:#1a1a1a;color:#888;border:1px solid #333;border-radius:6px;
                padding:6px 12px;font-size:12px;text-decoration:none;white-space:nowrap">
        🔍 Offline Search
      </a>
    </div>
  </div>
  <div id="sh-summary" style="color:#888;font-size:12px;margin:10px 0"></div>
  <div style="overflow-x:auto">
    <table id="sh-table" style="min-width:500px">
    <thead>
      <tr>
        <th style="text-align:left">Card Name</th>
        <th>Qty</th>
        <th>Unit Price</th>
        <th>Line Total</th>
        <th>Sold At</th>
      </tr>
    </thead>
    <tbody id="sh-tbody">
      <tr><td colspan="5" style="color:#555;text-align:center;padding:20px">Loading…</td></tr>
    </tbody>
    </table>
  </div>
</div>

<h2>Low Stock (≤5)</h2>
<table>
<thead><tr><th>Product</th><th>QR Code</th><th>Stock</th><th>Price</th><th>Category</th></tr></thead>
<tbody>{rows_low}</tbody>
</table>

<!-- ── Collection Goals ─────────────────────────────────────────── -->
<div class="form-panel" style="border-color:#4caf50;background:#001208;margin-top:24px">
  <h2 style="color:#4caf50">🎯 COLLECTION GOALS</h2>
  <div id="goals-list">{goals_html}</div>
  <details style="margin-top:14px">
    <summary style="cursor:pointer;color:#888;font-size:12px;letter-spacing:1px">+ Add New Goal</summary>
    <div style="margin-top:12px;display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end">
      <div>
        <label style="display:block;color:#555;font-size:10px;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px">Title</label>
        <input id="goal-title" placeholder="e.g. Collect 500 cards" style="background:#1a1a1a;border:1px solid #333;border-radius:6px;color:#e0e0e0;padding:7px 10px;font-size:13px;width:200px">
      </div>
      <div>
        <label style="display:block;color:#555;font-size:10px;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px">Type</label>
        <select id="goal-type" style="background:#1a1a1a;border:1px solid #333;border-radius:6px;color:#e0e0e0;padding:7px 10px;font-size:13px">
          <option value="card_count">Card Count</option>
          <option value="value_target">Collection Value ($)</option>
          <option value="set_completion">Set Completion</option>
        </select>
      </div>
      <div>
        <label style="display:block;color:#555;font-size:10px;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px">Target</label>
        <input id="goal-target" type="number" placeholder="100" style="background:#1a1a1a;border:1px solid #333;border-radius:6px;color:#e0e0e0;padding:7px 10px;font-size:13px;width:100px">
      </div>
      <button class="btn-gold" onclick="addGoal()" style="background:#2d4a2d">Add Goal</button>
    </div>
  </details>
</div>

<!-- ── Share Collection ─────────────────────────────────────────── -->
<div class="form-panel" style="border-color:#6366f1;background:#06041a;margin-top:24px">
  <h2 style="color:#818cf8">🔗 SHARE COLLECTION</h2>
  <div id="share-ui">{share_ui}</div>
</div>

<!-- ── Price Change Alerts ──────────────────────────────────────── -->
<div class="form-panel" style="border-color:#f59e0b;background:#0f0900;margin-top:24px">
  <h2 style="color:#f59e0b">📉 PRICE CHANGE ALERTS <span style="font-weight:400;font-size:11px;color:#555">(>15% movement)</span></h2>
  <div id="price-alert-body" style="color:#888;font-size:13px">Loading…</div>
  <button onclick="loadPriceAlerts()" style="background:#1a1000;color:#f59e0b;border:1px solid #3a2500;border-radius:6px;padding:6px 14px;font-size:12px;cursor:pointer;margin-top:10px">Refresh Alerts</button>
</div>

<!-- ── TCG Database Update ──────────────────────────────────────── -->
<div class="form-panel" style="border-color:#10b981;background:#001a0e;margin-top:24px">
  <h2 style="color:#10b981">🗃 TCG DATABASE UPDATE</h2>
  <p style="color:#888;font-size:13px;margin-bottom:14px">
    Update market prices or download new card sets from the Pokémon TCG API into the local database.
    Requires <code>import_tcg_db.py</code> to be present in the same directory as <code>server.py</code>.
  </p>
  <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px">
    <button id="btn-update-prices" onclick="triggerTcgJob('update-prices')"
            style="background:#052e1a;color:#10b981;border:1px solid #10b981;border-radius:6px;
                   padding:8px 18px;font-size:13px;cursor:pointer">
      💰 Update Prices Only <span style="font-size:11px;color:#888">(fast)</span>
    </button>
    <button id="btn-update-db" onclick="triggerTcgJob('update-db')"
            style="background:#052e1a;color:#10b981;border:1px solid #10b981;border-radius:6px;
                   padding:8px 18px;font-size:13px;cursor:pointer">
      📥 Full DB Update <span style="font-size:11px;color:#888">(10+ min)</span>
    </button>
    <button onclick="pollTcgStatus()"
            style="background:#0a1a10;color:#888;border:1px solid #333;border-radius:6px;
                   padding:8px 14px;font-size:13px;cursor:pointer">
      🔄 Check Status
    </button>
    <button onclick="triggerCloudSync()"
            style="background:#052e1a;color:#60a5fa;border:1px solid #3b82f6;border-radius:6px;
                   padding:8px 18px;font-size:13px;cursor:pointer">
      ☁️ Sync from Cloud
    </button>
    <button onclick="triggerCloudSync(true)"
            style="background:#050a1a;color:#60a5fa;border:1px solid #1e3a8a;border-radius:6px;
                   padding:8px 14px;font-size:13px;cursor:pointer">
      🔄 Force Re-Sync
    </button>
  </div>
  <div id="tcg-status" style="font-size:12px;color:#888;background:#0a1a0f;border-radius:6px;
       padding:10px;min-height:32px;font-family:monospace;white-space:pre-wrap">
    Click a button above to start a job.
  </div>
</div>

<!-- ── Email Notifications ──────────────────────────────────────── -->
<div class="form-panel" style="border-color:#8b5cf6;background:#070012;margin-top:24px">
  <h2 style="color:#a78bfa">📧 EMAIL NOTIFICATIONS</h2>
  <div id="email-status" style="color:#888;font-size:13px;margin-bottom:10px">Loading…</div>
  <p style="color:#666;font-size:12px;margin-bottom:12px">
    Set <code>SMTP_USER</code> and <code>SMTP_APP_PASSWORD</code> environment variables to enable Gmail sale alerts.
    Use a <a href="https://myaccount.google.com/apppasswords" target="_blank" style="color:#a78bfa">Gmail App Password</a>, not your main password.
    Optionally set <code>NOTIFY_EMAIL</code> to send alerts to a different address.
  </p>
  <button onclick="sendTestEmail()"
          style="background:#0d0020;color:#a78bfa;border:1px solid #7c3aed;border-radius:6px;
                 padding:8px 18px;font-size:13px;cursor:pointer">
    🧪 Send Test Email
  </button>
  <button onclick="loadEmailStatus()"
          style="background:#0d0020;color:#888;border:1px solid #333;border-radius:6px;
                 padding:8px 14px;font-size:13px;cursor:pointer;margin-left:8px">
    🔄 Refresh
  </button>
  <div id="email-msg" style="margin-top:8px;font-size:12px;color:#a78bfa"></div>
</div>

<div class="form-panel" id="add-product">
  <h2>Add / Update Product</h2>
  <div style="display:flex;gap:10px;margin-bottom:14px;align-items:flex-end;flex-wrap:wrap">
    <div style="flex:1;min-width:180px">
      <label style="display:block;color:#666;font-size:10px;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px">QR Code / Set-Number *</label>
      <input id="f-qr" placeholder="SV1-1 or PRODUCT-001" style="width:100%;background:#1a1a1a;border:1px solid #333;border-radius:6px;color:#e0e0e0;padding:8px 10px;font-size:13px">
    </div>
    <button class="btn-gold" onclick="prefillFromTCG()" style="background:#6366f1;white-space:nowrap" title="Auto-fill name, rarity, set, image & market price from the TCG API">
      ⚡ Prefill from TCG API
    </button>
    <button class="btn-gold" onclick="openPhotoID()" style="background:#7c3aed;white-space:nowrap" title="Take a photo of the card — AI identifies it automatically">
      📷 Identify from Photo
    </button>
  </div>
  <div id="prefill-status" style="font-size:12px;color:#6366f1;margin-bottom:10px;display:none"></div>

  <!-- Photo-ID modal -->
  <div id="photo-id-modal" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:#000b;z-index:3000;align-items:center;justify-content:center">
    <div style="background:#111;border:1px solid #7c3aed;border-radius:10px;padding:24px;max-width:480px;width:92%;position:relative">
      <button onclick="closePhotoID()" style="position:absolute;top:10px;right:14px;background:none;border:none;color:#aaa;font-size:22px;cursor:pointer">✕</button>
      <h2 style="color:#c4b5fd;margin-top:0">📷 AI Card Identifier</h2>
      <p style="color:#888;font-size:13px;margin-bottom:12px">Take or upload a clear photo of the front of the card. GPT-4o Vision will identify it and auto-fill the form.</p>
      <div style="display:flex;gap:10px;margin-bottom:14px">
        <button class="btn-gold" onclick="document.getElementById('photo-file-input').click()" style="background:#7c3aed;flex:1">📁 Choose File</button>
        <button class="btn-gold" onclick="document.getElementById('photo-camera-input').click()" style="background:#6d28d9;flex:1">📸 Take Photo</button>
      </div>
      <input id="photo-file-input"   type="file" accept="image/*"          style="display:none" onchange="previewPhotoID(event)">
      <input id="photo-camera-input" type="file" accept="image/*" capture="environment" style="display:none" onchange="previewPhotoID(event)">
      <div id="photo-preview-wrap" style="display:none;margin-bottom:14px;text-align:center">
        <img id="photo-preview-img" style="max-width:100%;max-height:220px;border-radius:8px;border:1px solid #7c3aed">
      </div>
      <button id="photo-id-btn" class="btn-gold" onclick="runPhotoID()" style="background:#7c3aed;width:100%;display:none">🔍 Identify Card</button>
      <div id="photo-id-result" style="margin-top:12px;font-size:13px;color:#aaa"></div>
    </div>
  </div>
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

<div style="display:flex;align-items:center;gap:12px;margin-bottom:10px;flex-wrap:wrap">
  <h2 style="margin:0">Full Inventory ({len(inventory)} products)</h2>
  <a href="/admin/qr-sheet" target="_blank"
     style="background:#1e3a5f;color:#7dd3fc;border:1px solid #2563eb;padding:5px 14px;
            border-radius:6px;font-size:13px;font-weight:bold;text-decoration:none;white-space:nowrap">
    🖨 Print QR Labels
  </a>
  <a href="/admin/qr-sheet?zero=1" target="_blank"
     style="background:#111;color:#aaa;border:1px solid #444;padding:5px 14px;
            border-radius:6px;font-size:13px;text-decoration:none;white-space:nowrap">
    📋 All Items (incl. out-of-stock)
  </a>
</div>
<table>
<thead><tr><th>Name</th><th>QR Code</th><th>Stock</th><th>Price</th><th>Category</th><th>Actions</th></tr></thead>
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

/* ── Keyboard shortcuts ─────────────────────────────────────────────── */
document.addEventListener('keydown', e => {{
  if (e.key === 'Escape') pfDismiss();
}});

/* ── Sell 1 quick button ────────────────────────────────────────────── */
async function sellOne(qr, name, price) {{
  if (!confirm('Sell 1 × ' + name + ' @ $' + parseFloat(price).toFixed(2) + '?')) return;
  try {{
    const r = await fetch('/admin/sell-one/' + encodeURIComponent(qr), {{method:'POST'}});
    const d = await r.json();
    if (!r.ok) {{ toast(d.error || 'Sell failed', true); return; }}
    toast('✓ Sold 1 × ' + name + ' (stock now ' + d.new_stock + ')');
    const cell = document.getElementById('stock-' + qr);
    if (cell) cell.textContent = d.new_stock;
    const btn = cell && cell.closest('tr').querySelector('.btn-sell');
    if (btn && d.new_stock <= 0) {{ btn.disabled = true; btn.title = 'Out of stock'; }}
  }} catch(err) {{ toast('Network error: ' + err.message, true); }}
}}

/* ── Stats auto-refresh every 30 s ─────────────────────────────────── */
async function refreshStats() {{
  try {{
    const r = await fetch('/admin/stats-partial');
    if (!r.ok) return;
    const d = await r.json();
    document.getElementById('stat-count').textContent  = d.sales_count;
    document.getElementById('stat-rev').textContent    = '$' + d.revenue.toFixed(2);
    document.getElementById('stat-tax').textContent    = '$' + d.tax.toFixed(2);
    document.getElementById('stat-tips').textContent   = '$' + d.tips.toFixed(2);
    document.getElementById('stat-inv').textContent    = d.inv_count;
    document.getElementById('stat-low').textContent    = d.low_stock;
    // Refresh sparkline from the same response
    const vals = d.seven_days.map(x => x.revenue);
    const max  = Math.max(...vals, 1);
    const W=260, H=60, PAD=4;
    const step = (W - PAD*2) / Math.max(vals.length - 1, 1);
    const pts  = vals.map((v,i) => (PAD + i*step).toFixed(1) + ',' + (H - PAD - ((v/max)*(H-PAD*2))).toFixed(1)).join(' ');
    document.getElementById('sparkline-svg').innerHTML =
      `<svg viewBox="0 0 ${{W}} ${{H}}" width="100%" preserveAspectRatio="none" style="display:block">
        <polyline points="${{pts}}" fill="none" stroke="#FFD700" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
        ${{vals.map((v,i) => v > 0 ? `<circle cx="${{(PAD+i*step).toFixed(1)}}" cy="${{(H-PAD-((v/max)*(H-PAD*2))).toFixed(1)}}" r="3" fill="#FFD700"/>` : '').join('')}}
      </svg>`;
    document.getElementById('spark-last-refresh').textContent =
      'Refreshed ' + new Date().toLocaleTimeString();
  }} catch(e) {{ /* silent */ }}
}}
setInterval(refreshStats, 30000);

/* ── SSE scan stream → price flash (exponential back-off reconnect) ─── */
let _sseDelay = 1000;
function connectScanStream() {{
  const es = new EventSource('/scan/stream');
  es.onmessage = async (evt) => {{
    _sseDelay = 1000;   // successful message — reset back-off
    try {{
      const {{qrCode}} = JSON.parse(evt.data);
      if (!qrCode) return;
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
    setTimeout(connectScanStream, _sseDelay);
    _sseDelay = Math.min(_sseDelay * 2, 30000);  // back off up to 30 s
  }};
}}
connectScanStream();

/* ── Sale History panel ─────────────────────────────────────────────── */
async function loadSaleHistory() {{
  const name = (document.getElementById('sh-search') || {{}}).value || '';
  const tbody = document.getElementById('sh-tbody');
  const summary = document.getElementById('sh-summary');
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="5" style="color:#555;text-align:center;padding:16px">Loading…</td></tr>';
  try {{
    const r = await fetch('/admin/sale-history?' + new URLSearchParams({{limit:100, days:30, name}}));
    if (!r.ok) {{ tbody.innerHTML='<tr><td colspan="5" style="color:#f44336;text-align:center">Error</td></tr>'; return; }}
    const d = await r.json();
    if (summary) {{
      summary.textContent = d.count + ' line item' + (d.count!==1?'s':'') +
        ' in the last ' + d.days + ' days — $' + d.total_revenue.toFixed(2) + ' total revenue';
    }}
    if (!d.items.length) {{
      tbody.innerHTML='<tr><td colspan="5" style="color:#555;text-align:center;padding:20px">No sales recorded yet</td></tr>';
      return;
    }}
    tbody.innerHTML = d.items.map(it => {{
      const dt = it.sold_at > 1e12
        ? new Date(it.sold_at).toLocaleString()
        : new Date(it.sold_at * 1000).toLocaleString();
      const total = (it.price * it.quantity).toFixed(2);
      return '<tr>' +
        '<td style="text-align:left;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + it.name + '</td>' +
        '<td>' + it.quantity + '</td>' +
        '<td>$' + it.price.toFixed(2) + '</td>' +
        '<td style="color:#4caf50;font-weight:600">$' + total + '</td>' +
        '<td style="font-size:11px;color:#888">' + dt + '</td>' +
        '</tr>';
    }}).join('');
  }} catch(e) {{
    tbody.innerHTML = '<tr><td colspan="5" style="color:#f44336;text-align:center">'+e.message+'</td></tr>';
  }}
}}
loadSaleHistory();

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

// ── Photo ID via GPT-4o Vision ─────────────────────────────────────────────
let _photoIdBase64 = null;

function openPhotoID() {{
  document.getElementById('photo-id-modal').style.display = 'flex';
  document.getElementById('photo-id-result').textContent = '';
  document.getElementById('photo-preview-wrap').style.display = 'none';
  document.getElementById('photo-id-btn').style.display = 'none';
  _photoIdBase64 = null;
}}

function closePhotoID() {{
  document.getElementById('photo-id-modal').style.display = 'none';
}}

function previewPhotoID(evt) {{
  const file = evt.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = function(e) {{
    _photoIdBase64 = e.target.result; // data:image/...;base64,...
    const img = document.getElementById('photo-preview-img');
    img.src = _photoIdBase64;
    document.getElementById('photo-preview-wrap').style.display = 'block';
    document.getElementById('photo-id-btn').style.display = 'block';
    document.getElementById('photo-id-result').textContent = '';
  }};
  reader.readAsDataURL(file);
}}

async function runPhotoID() {{
  if (!_photoIdBase64) return;
  const resultEl = document.getElementById('photo-id-result');
  const btn      = document.getElementById('photo-id-btn');
  btn.disabled   = true;
  btn.textContent = '⏳ Identifying…';
  resultEl.textContent = '';
  try {{
    const r = await fetch('/card/identify-image', {{
      method: 'POST',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{image: _photoIdBase64}}),
    }});
    const d = await r.json();
    if (d.error) {{
      resultEl.textContent = '❌ ' + d.error;
      resultEl.style.color = '#f87171';
      btn.disabled = false; btn.textContent = '🔍 Identify Card';
      return;
    }}
    if (!d.identified) {{
      resultEl.textContent = '⚠️ Could not identify card — try a clearer photo.';
      resultEl.style.color = '#facc15';
      btn.disabled = false; btn.textContent = '🔍 Identify Card';
      return;
    }}
    // Fill form fields from GPT + enriched data
    const g  = d.gpt || {{}};
    const en = d.enriched || {{}};
    const t  = en.tcgData || {{}};
    if (d.qr_guess) document.getElementById('f-qr').value = d.qr_guess;
    const name = en.name || t.name || g.name;
    if (name)    document.getElementById('f-name').value = name;
    const rarity = t.rarity || g.rarity;
    if (rarity)  document.getElementById('f-rarity').value = rarity;
    const setCode = (t.set && t.set.ptcgoCode) || g.set_code;
    if (setCode) document.getElementById('f-set').value = setCode;
    const mkt = t.tcgplayer && t.tcgplayer.marketPrice;
    if (mkt && !document.getElementById('f-price').value)
      document.getElementById('f-price').value = mkt.toFixed(2);
    const img = en.imageUrl || (t.images && t.images.large);
    if (img) {{
      document.getElementById('f-imgurl').value = img;
      document.getElementById('f-img-preview').src = img;
      document.getElementById('prefill-img').style.display = 'block';
    }}
    resultEl.innerHTML = (
      `✅ Identified: <b style="color:#c4b5fd">${{name || g.name}}</b>` +
      (g.condition ? ` — Condition: <b style="color:#facc15">${{g.condition}}</b>` : '') +
      (mkt ? ` — Market: <b style="color:#4ade80">$${{mkt.toFixed(2)}}</b>` : '')
    );
    resultEl.style.color = '#4ade80';
    setTimeout(closePhotoID, 2000);
  }} catch(e) {{
    resultEl.textContent = '❌ Request failed: ' + e;
    resultEl.style.color = '#f87171';
  }}
  btn.disabled = false; btn.textContent = '🔍 Identify Card';
}}
// ──────────────────────────────────────────────────────────────────────────

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

async function syncScanner(force) {{
  const el = document.getElementById('scanner-sync-result');
  el.textContent = '⏳ Pulling from Inventory-Scanner…';
  el.style.color = '#aaa';
  try {{
    const r = await fetch('/admin/sync-scanner?force=' + (force?'1':'0'), {{method:'POST'}});
    const d = await r.json();
    if (d.error) {{
      el.textContent = '❌ ' + d.error;
      el.style.color = '#f87171';
    }} else if (d.skipped) {{
      el.textContent = `⏭ Skipped — already have ${{d.existing}} products. Use Force Re-Sync to refresh.`;
      el.style.color = '#facc15';
    }} else {{
      el.textContent = `✅ Done — ${{d.upserted}} product(s) imported/updated.`;
      el.style.color = '#4ade80';
      if (d.upserted > 0) setTimeout(() => location.reload(), 1500);
    }}
  }} catch(e) {{
    el.textContent = '❌ Request failed: ' + e;
    el.style.color = '#f87171';
  }}
}}

// ── Prefill from market page (sessionStorage handoff) ─────────────────────
(function applyPrefill() {{
  try {{
    const raw = sessionStorage.getItem('prefillData');
    if (!raw) return;
    sessionStorage.removeItem('prefillData');
    const p = JSON.parse(raw);
    if (p.qr)     document.getElementById('f-qr').value     = p.qr;
    if (p.name)   document.getElementById('f-name').value   = p.name;
    if (p.price)  document.getElementById('f-price').value  = Number(p.price).toFixed(2);
    if (p.rarity) document.getElementById('f-rarity').value = p.rarity;
    if (p.set)    document.getElementById('f-set').value    = p.set;
    if (p.imgurl) {{
      document.getElementById('f-imgurl').value = p.imgurl;
      document.getElementById('f-img-preview').src = p.imgurl;
      document.getElementById('prefill-img').style.display = 'block';
    }}
    if (p.tcgid) document.getElementById('f-tcgid').value = p.tcgid;
    const addEl = document.getElementById('add-product');
    if (addEl) {{ addEl.scrollIntoView({{behavior:'smooth'}}); }}
    toast('✓ Prefilled from Market Prices — review and save');
  }} catch(e) {{}}
}})();

// ── Goals ────────────────────────────────────────────────────────
async function addGoal() {{
  const title  = document.getElementById('goal-title').value.trim();
  const type   = document.getElementById('goal-type').value;
  const target = parseInt(document.getElementById('goal-target').value) || 1;
  if (!title) {{ toast('Title is required'); return; }}
  const r = await fetch('/admin/goals', {{
    method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{title, type, targetValue: target}})
  }});
  if (r.ok) {{ toast('✓ Goal added'); setTimeout(()=>location.reload(), 800); }}
  else {{ toast('Error adding goal'); }}
}}

async function toggleGoal(id, completed) {{
  await fetch(`/admin/goals/${{id}}`, {{
    method:'PATCH', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{completed}})
  }});
  setTimeout(()=>location.reload(), 300);
}}

async function deleteGoal(id) {{
  if (!confirm('Delete this goal?')) return;
  await fetch(`/admin/goals/${{id}}`, {{method:'DELETE'}});
  setTimeout(()=>location.reload(), 300);
}}

// ── Share Collection ─────────────────────────────────────────────
async function generateShare() {{
  const r = await fetch('/admin/share-token', {{method:'POST'}});
  const d = await r.json();
  toast('✓ Share link generated');
  setTimeout(()=>location.reload(), 800);
}}

async function revokeShare() {{
  if (!confirm('Revoke public share link?')) return;
  await fetch('/admin/share-token', {{method:'DELETE'}});
  toast('Link revoked');
  setTimeout(()=>location.reload(), 800);
}}

function copyShareLink(path) {{
  const url = `${{window.location.origin}}${{path}}`;
  navigator.clipboard.writeText(url).then(()=>toast('✓ Link copied!')).catch(()=>toast(url));
}}

// ── Price Alerts ─────────────────────────────────────────────────
async function loadPriceAlerts() {{
  const el = document.getElementById('price-alert-body');
  el.textContent = 'Loading…';
  try {{
    const r = await fetch('/admin/price-alerts');
    const alerts = await r.json();
    if (!alerts.length) {{
      el.innerHTML = '<span style="color:#4caf50">✓ No significant price movements detected.</span>';
      return;
    }}
    const rows = alerts.slice(0, 20).map(a => {{
      const dir   = a.direction === 'up' ? '▲' : '▼';
      const color = a.direction === 'up' ? '#4ade80' : '#f87171';
      return `<tr>
        <td>${{a.cardName}}</td>
        <td>$${{a.firstPrice.toFixed(2)}}</td>
        <td style="color:${{color}}">${{dir}} $${{a.lastPrice.toFixed(2)}}</td>
        <td style="color:${{color}};font-weight:700">${{a.pctChange > 0 ? '+' : ''}}${{a.pctChange}}%</td>
        <td style="color:#555">${{a.readings}} readings</td>
      </tr>`;
    }}).join('');
    el.innerHTML = `<table style="width:100%;font-size:12px">
      <thead><tr><th style="text-align:left;color:#555;padding:4px 8px">Card</th>
      <th style="text-align:left;color:#555;padding:4px 8px">Was</th>
      <th style="text-align:left;color:#555;padding:4px 8px">Now</th>
      <th style="text-align:left;color:#555;padding:4px 8px">Change</th>
      <th style="text-align:left;color:#555;padding:4px 8px">Data Pts</th></tr></thead>
      <tbody>${{rows}}</tbody></table>`;
  }} catch(e) {{ el.textContent = 'Error loading alerts'; }}
}}
loadPriceAlerts();

// ── TCG Database update helpers ─────────────────────────────────────────
async function triggerTcgJob(job) {{
  const el = document.getElementById('tcg-status');
  el.textContent = `Starting ${{job}}…`;
  try {{
    const r = await fetch(`/admin/${{job}}`, {{ method: 'POST' }});
    const d = await r.json();
    if (r.status === 409) {{
      el.textContent = '⚠ ' + d.message;
    }} else {{
      el.textContent = '✓ ' + d.message + '\nPolling for status…';
      setTimeout(pollTcgStatus, 3000);
    }}
  }} catch(e) {{ el.textContent = 'Error: ' + e.message; }}
}}

let _tcgPollTimer = null;
async function pollTcgStatus() {{
  const el = document.getElementById('tcg-status');
  try {{
    const r = await fetch('/admin/update-status');
    const d = await r.json();
    if (d.running) {{
      el.textContent = '⏳ Job running… (refresh to check progress)';
      clearTimeout(_tcgPollTimer);
      _tcgPollTimer = setTimeout(pollTcgStatus, 5000);
    }} else if (d.last_result) {{
      const res = d.last_result;
      const ok  = res.returncode === 0;
      el.textContent = (ok ? '✓ Completed' : '✗ Failed') +
        ` (exit ${{res.returncode}}) at ${{res.finished_at || ''}}\n` +
        (res.stdout ? 'STDOUT:\n' + res.stdout : '') +
        (res.stderr ? '\nSTDERR:\n' + res.stderr : '');
      el.style.color = ok ? '#10b981' : '#f87171';
    }} else {{
      el.textContent = 'No job running. No previous result.';
    }}
  }} catch(e) {{ el.textContent = 'Status check failed: ' + e.message; }}
}}

async function triggerCloudSync(force=false) {{
  const el = document.getElementById('tcg-status');
  el.textContent = force ? 'Force-syncing from cloud…' : 'Syncing from cloud…';
  try {{
    const r = await fetch('/admin/sync-from-cloud' + (force ? '?force=1' : ''), {{ method: 'POST' }});
    const d = await r.json();
    if (d.skipped) {{
      el.textContent = `⚠ Already have ${{d.existing}} products. Use Force Re-Sync to refresh.`;
      el.style.color = '#f59e0b';
    }} else {{
      el.textContent = `✓ Synced ${{d.upserted}} products from cloud (${{d.skipped_items || 0}} skipped).`;
      el.style.color = '#10b981';
      setTimeout(() => location.reload(), 2000);
    }}
  }} catch(e) {{ el.textContent = 'Sync failed: ' + e.message; el.style.color = '#f87171'; }}
}}

// ── Email status + test ─────────────────────────────────────────────────
async function loadEmailStatus() {{
  const el = document.getElementById('email-status');
  try {{
    const r = await fetch('/admin/email-config');
    const d = await r.json();
    if (d.enabled) {{
      el.innerHTML = `<span style="color:#10b981">✓ Enabled</span> — Alerts sent from <strong>${{d.smtp_user}}</strong> to <strong>${{d.notify_email}}</strong>`;
    }} else {{
      el.innerHTML = `<span style="color:#f87171">✗ Disabled</span> — Set <code>SMTP_USER</code> + <code>SMTP_APP_PASSWORD</code> env vars to activate.`;
    }}
  }} catch(e) {{ el.textContent = 'Could not load email config.'; }}
}}

async function sendTestEmail() {{
  const msg = document.getElementById('email-msg');
  msg.textContent = 'Sending…';
  try {{
    const r = await fetch('/admin/email-config/test', {{ method: 'POST' }});
    const d = await r.json();
    msg.textContent = d.ok ? '✓ ' + d.msg : '✗ ' + d.msg;
    msg.style.color = d.ok ? '#10b981' : '#f87171';
  }} catch(e) {{ msg.textContent = 'Error: ' + e.message; msg.style.color = '#f87171'; }}
}}

loadEmailStatus();
</script>
</div><!-- /wrap -->
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# Email notifications (SMTP/Gmail)
# ---------------------------------------------------------------------------

import smtplib as _smtplib
import email.mime.multipart as _mime_multi
import email.mime.text as _mime_text

def _get_email_cfg() -> dict:
    """Load SMTP config from env vars (SMTP_USER, SMTP_APP_PASSWORD, NOTIFY_EMAIL)."""
    return {
        "user":     os.environ.get("SMTP_USER", ""),
        "password": os.environ.get("SMTP_APP_PASSWORD", ""),
        "notify":   os.environ.get("NOTIFY_EMAIL", os.environ.get("SMTP_USER", "")),
        "enabled":  bool(os.environ.get("SMTP_USER") and os.environ.get("SMTP_APP_PASSWORD")),
    }


def _send_sale_email(sale_name: str, price: float, method: str = "", qty: int = 1):
    """Fire-and-forget sale alert email. Runs in a daemon thread."""
    def _do_send():
        cfg = _get_email_cfg()
        if not cfg["enabled"] or not cfg["notify"]:
            return
        try:
            msg = _mime_multi.MIMEMultipart("alternative")
            msg["Subject"] = f"Sale: {sale_name} — ${price:.2f}"
            msg["From"]    = f"HanryxVault <{cfg['user']}>"
            msg["To"]      = cfg["notify"]
            html = f"""
            <div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto;background:#1a1a1a;
                        color:#e0e0e0;border-radius:10px;overflow:hidden;">
              <div style="background:linear-gradient(135deg,#b8860b,#d4a843);padding:18px;text-align:center;">
                <h1 style="margin:0;color:#1a1a1a;font-size:20px;">HanryxVault — Sale Alert</h1>
              </div>
              <div style="padding:20px;">
                <table style="width:100%;border-collapse:collapse;background:#2a2a2a;border-radius:6px;">
                  <tr><td style="padding:8px 12px;font-weight:bold;color:#d4a843;">Card</td>
                      <td style="padding:8px 12px;">{sale_name}</td></tr>
                  <tr><td style="padding:8px 12px;font-weight:bold;color:#d4a843;">Price</td>
                      <td style="padding:8px 12px;">${price:.2f}</td></tr>
                  <tr><td style="padding:8px 12px;font-weight:bold;color:#d4a843;">Qty</td>
                      <td style="padding:8px 12px;">{qty}</td></tr>
                  <tr><td style="padding:8px 12px;font-weight:bold;color:#d4a843;">Method</td>
                      <td style="padding:8px 12px;">{method or 'POS'}</td></tr>
                  <tr><td style="padding:8px 12px;font-weight:bold;color:#d4a843;">Time</td>
                      <td style="padding:8px 12px;">{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</td></tr>
                </table>
              </div>
            </div>"""
            msg.attach(_mime_text.MIMEText(html, "html"))
            with _smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.login(cfg["user"], cfg["password"])
                server.sendmail(cfg["user"], cfg["notify"], msg.as_string())
            log.info("[email] Sale alert sent for: %s", sale_name)
        except Exception as _e:
            log.warning("[email] Failed to send sale alert: %s", _e)
    _bg(_do_send)


# ---------------------------------------------------------------------------
# Two-way stock sync — push sold quantities back to the storefront
# ---------------------------------------------------------------------------

def _push_stock_to_storefront(items: list):
    """
    After a sale, decrement stock on the HanRyx-Vault storefront so the public
    website never shows 'in stock' for a card that was just sold.

    items format: [{"qrCode": str, "name": str, "quantity": int}, ...]

    The storefront endpoint POST /api/inventory/sync accepts:
        { "items": [{"qrCode": str, "delta": int}] }
    where delta is negative for a sale (e.g. -1 for one copy sold).

    Runs in a daemon thread — fire-and-forget, never blocks the sale response.
    If STOREFRONT_URL is empty or the request fails, the error is logged and
    silently ignored (POS sale is already committed).
    """
    if not STOREFRONT_URL or not items:
        return

    def _do_push():
        try:
            payload = {
                "items": [
                    {"qrCode": it["qrCode"], "delta": -int(it.get("quantity", 1))}
                    for it in items
                    if it.get("qrCode")
                ]
            }
            if not payload["items"]:
                return
            resp = _requests.post(
                f"{STOREFRONT_URL}/api/inventory/sync",
                json=payload,
                headers={"Content-Type": "application/json", "User-Agent": "HanryxVaultPOS/2.0"},
                timeout=8,
            )
            if resp.ok:
                log.info("[storefront-sync] pushed %d stock delta(s) → %s",
                         len(payload["items"]), resp.status_code)
            else:
                log.warning("[storefront-sync] push returned %s: %s",
                            resp.status_code, resp.text[:200])
        except Exception as _e:
            log.warning("[storefront-sync] push failed (non-fatal): %s", _e)

    _bg(_do_push)


@app.route("/admin/email-config", methods=["GET"])
@require_admin
def admin_email_config_get():
    """Return current email config status (without exposing the password)."""
    cfg = _get_email_cfg()
    return jsonify({
        "enabled":  cfg["enabled"],
        "smtp_user": cfg["user"],
        "notify_email": cfg["notify"],
        "instructions": "Set SMTP_USER and SMTP_APP_PASSWORD env vars to enable. "
                        "Use a Gmail App Password (not your main password).",
    })


@app.route("/admin/email-config/test", methods=["POST"])
@require_admin
def admin_email_test():
    """Send a test email to verify SMTP config."""
    _send_sale_email("Test Card (Charizard ex)", 49.99, "Test")
    cfg = _get_email_cfg()
    if not cfg["enabled"]:
        return jsonify({"ok": False, "msg": "SMTP_USER / SMTP_APP_PASSWORD not set"}), 400
    return jsonify({"ok": True, "msg": f"Test email queued → {cfg['notify']}"})


# ---------------------------------------------------------------------------
# Brute-force login protection
# ---------------------------------------------------------------------------

_login_attempts: dict = {}   # ip -> {"count": int, "locked_until": float}
_BF_MAX_ATTEMPTS  = 5
_BF_LOCKOUT_SECS  = 900   # 15 minutes


def _check_login_rate(ip: str) -> tuple[bool, int]:
    """Return (allowed, minutes_left)."""
    now  = _time.time()
    info = _login_attempts.get(ip)
    if info and info["locked_until"] > now:
        return False, int((info["locked_until"] - now) / 60) + 1
    if info and info["locked_until"] <= now:
        del _login_attempts[ip]
    return True, 0


def _record_failed_login(ip: str):
    now  = _time.time()
    info = _login_attempts.setdefault(ip, {"count": 0, "locked_until": 0.0})
    info["count"] += 1
    if info["count"] >= _BF_MAX_ATTEMPTS:
        info["locked_until"] = now + _BF_LOCKOUT_SECS
        log.warning("[admin] IP %s locked out after %d failed login attempts", ip, info["count"])


def _clear_login_attempts(ip: str):
    _login_attempts.pop(ip, None)


# ---------------------------------------------------------------------------
# Background TCG DB update helpers
# ---------------------------------------------------------------------------

_update_lock   = threading.Lock()
_update_status: dict = {"running": False, "last_result": None}


def _run_import_script(args: list):
    """Run import_tcg_db.py in a background thread and store the result."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "import_tcg_db.py")
    cmd    = ["python3", script] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        _update_status["last_result"] = {
            "returncode": result.returncode,
            "stdout":     result.stdout[-3000:],
            "stderr":     result.stderr[-1000:],
            "finished_at": datetime.datetime.now().isoformat(),
        }
    except subprocess.TimeoutExpired:
        _update_status["last_result"] = {
            "returncode": -1, "stdout": "", "stderr": "Timed out after 600 s",
            "finished_at": datetime.datetime.now().isoformat(),
        }
    except Exception as exc:
        _update_status["last_result"] = {
            "returncode": -1, "stdout": "", "stderr": str(exc),
            "finished_at": datetime.datetime.now().isoformat(),
        }
    finally:
        _update_status["running"] = False
    log.info("[tcg-update] Job finished: returncode=%s",
             _update_status["last_result"]["returncode"])


@app.route("/admin/update-prices", methods=["POST"])
@require_admin
def admin_update_prices():
    """Trigger import_tcg_db.py --update-prices in the background.
    Refreshes market prices from TCG API without downloading new card sets.
    Poll /admin/update-status for progress."""
    with _update_lock:
        if _update_status["running"]:
            return jsonify({"status": "already_running",
                            "message": "An update is already in progress"}), 409
        _update_status["running"]     = True
        _update_status["last_result"] = None
    threading.Thread(
        target=_run_import_script, args=(["--update-prices"],), daemon=True
    ).start()
    return jsonify({"status": "started",
                    "message": "Price update running in background. Poll /admin/update-status."}), 202


@app.route("/admin/update-db", methods=["POST"])
@require_admin
def admin_update_db():
    """Trigger import_tcg_db.py --update to pull new sets + refresh all prices.
    WARNING: slow (10+ minutes). Poll /admin/update-status for progress."""
    with _update_lock:
        if _update_status["running"]:
            return jsonify({"status": "already_running",
                            "message": "An update is already in progress"}), 409
        _update_status["running"]     = True
        _update_status["last_result"] = None
    threading.Thread(
        target=_run_import_script, args=(["--update"],), daemon=True
    ).start()
    return jsonify({"status": "started",
                    "message": "Full DB update running in background. Poll /admin/update-status."}), 202


@app.route("/admin/update-status", methods=["GET"])
@require_admin
def admin_update_status():
    """Check whether a TCG DB update job is running and see the last result."""
    return jsonify({
        "running":     _update_status["running"],
        "last_result": _update_status["last_result"],
    }), 200


# ---------------------------------------------------------------------------
# Admin — sale history
# ---------------------------------------------------------------------------

@app.route("/admin/sale-history", methods=["GET"])
@require_admin
def admin_sale_history():
    """
    Return per-item sale history from the sale_history table.

    Query params:
      limit  — max rows (default 100, max 500)
      days   — look-back window in days (default 30)
      name   — optional substring filter on card name
    """
    try:
        limit = min(int(request.args.get("limit", 100)), 500)
        days  = max(int(request.args.get("days",   30)),  1)
        name  = request.args.get("name", "").strip()
    except ValueError:
        return jsonify({"error": "Invalid query param"}), 400

    db   = get_db()
    cutoff = _now_ms() - days * 86_400_000

    if name:
        rows = db.execute(
            "SELECT name, price, quantity, sold_at FROM sale_history "
            "WHERE sold_at >= %s AND name ILIKE %s "
            "ORDER BY sold_at DESC LIMIT %s",
            (cutoff, f"%{name}%", limit)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT name, price, quantity, sold_at FROM sale_history "
            "WHERE sold_at >= %s ORDER BY sold_at DESC LIMIT %s",
            (cutoff, limit)
        ).fetchall()

    items = [
        {
            "name":     r[0],
            "price":    float(r[1]),
            "quantity": int(r[2]),
            "sold_at":  r[3],
        }
        for r in rows
    ]

    total_revenue = sum(i["price"] * i["quantity"] for i in items)
    return jsonify({
        "items":         items,
        "count":         len(items),
        "days":          days,
        "total_revenue": round(total_revenue, 2),
    })


# ---------------------------------------------------------------------------
# Offline card search (no internet required)
# ---------------------------------------------------------------------------

@app.route("/offline-search", methods=["GET"])
def offline_search():
    """
    Standalone HTML page for searching the local card database and inventory
    without any internet connection.  Useful at trade shows when the TCG API
    is unavailable.  Calls /card/lookup (local DB only).
    """
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HanryxVault — Offline Card Search</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0d0d0d;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding:20px}}
  h1{{color:#FFD700;font-size:22px;letter-spacing:2px;text-transform:uppercase;margin-bottom:6px}}
  .sub{{color:#555;font-size:12px;letter-spacing:1px;margin-bottom:20px}}
  .search-bar{{display:flex;gap:10px;margin-bottom:20px}}
  input{{flex:1;background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#e0e0e0;
         padding:10px 14px;font-size:14px;outline:none}}
  input:focus{{border-color:#FFD700}}
  button{{background:#FFD700;color:#000;border:none;border-radius:8px;padding:10px 20px;
          font-size:14px;font-weight:700;cursor:pointer;letter-spacing:1px;white-space:nowrap}}
  button:hover{{background:#e5c100}}
  .results{{display:grid;gap:12px}}
  .card{{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:10px;padding:14px 16px;
         display:grid;grid-template-columns:1fr auto;gap:8px;align-items:start}}
  .card-name{{font-weight:600;font-size:15px;color:#fff;margin-bottom:4px}}
  .card-meta{{color:#888;font-size:12px;line-height:1.6}}
  .card-price{{text-align:right}}
  .price-val{{color:#4caf50;font-size:18px;font-weight:700}}
  .price-lbl{{color:#555;font-size:11px;letter-spacing:1px;text-transform:uppercase}}
  .badge{{display:inline-block;background:#2a2a2a;border:1px solid #333;border-radius:4px;
          padding:2px 7px;font-size:11px;color:#888;margin-right:4px;margin-top:2px}}
  .stock-ok{{color:#4caf50;font-weight:700}}
  .stock-low{{color:#ff9800;font-weight:700}}
  .stock-out{{color:#f44336;font-weight:700}}
  .empty{{text-align:center;color:#555;padding:40px;font-size:14px}}
  .status{{color:#888;font-size:12px;margin-bottom:12px;min-height:18px}}
  #back{{display:inline-block;color:#555;font-size:12px;text-decoration:none;
         border:1px solid #333;border-radius:6px;padding:5px 12px;margin-bottom:16px}}
  #back:hover{{color:#FFD700;border-color:#FFD700}}
</style>
</head>
<body>
<a href="/admin" id="back">← Admin Dashboard</a>
<h1>Offline Card Search</h1>
<p class="sub">Searches your local inventory + TCG database — no internet required</p>

<div class="search-bar">
  <input id="q" type="text" placeholder="Card name, set code, QR code…"
         autofocus autocomplete="off"
         onkeydown="if(event.key==='Enter')search()">
  <button onclick="search()">Search</button>
</div>
<div class="status" id="status"></div>
<div class="results" id="results"></div>

<script>
let _timer;
document.getElementById('q').addEventListener('input', () => {{
  clearTimeout(_timer);
  _timer = setTimeout(search, 350);
}});

async function search() {{
  const q = document.getElementById('q').value.trim();
  const status = document.getElementById('status');
  const results = document.getElementById('results');
  if (!q) {{ results.innerHTML=''; status.textContent=''; return; }}

  status.textContent = 'Searching…';
  try {{
    const r = await fetch('/card/lookup?' + new URLSearchParams({{q, limit: 40}}));
    const d = await r.json();
    const items = d.results || d.items || (Array.isArray(d) ? d : []);
    if (!items.length) {{
      results.innerHTML = '<div class="empty">No cards found for &ldquo;' + q + '&rdquo;</div>';
      status.textContent = '0 results';
      return;
    }}
    status.textContent = items.length + ' result' + (items.length!==1?'s':'') + ' (local DB)';
    results.innerHTML = items.map(c => {{
      const stock = c.stock ?? c.qty ?? null;
      const stockHtml = stock === null ? '' :
        stock === 0 ? '<span class="stock-out">OUT OF STOCK</span>' :
        stock <= 3  ? '<span class="stock-low">Low stock (' + stock + ')</span>' :
                      '<span class="stock-ok">In stock (' + stock + ')</span>';
      const price = c.price || c.store_price || c.market_price || 0;
      const set   = c.set_code || c.set || '';
      const num   = c.card_number || c.number || '';
      const cat   = c.category || c.rarity || '';
      const lang  = c.language || '';
      return '<div class="card">' +
        '<div>' +
          '<div class="card-name">' + (c.name||'Unknown') + '</div>' +
          '<div class="card-meta">' +
            (set  ? '<span class="badge">' + set + (num ? ' #' + num : '') + '</span>' : '') +
            (cat  ? '<span class="badge">' + cat  + '</span>' : '') +
            (lang ? '<span class="badge">' + lang + '</span>' : '') +
          '</div>' +
          '<div style="margin-top:6px">' + stockHtml + '</div>' +
        '</div>' +
        '<div class="card-price">' +
          (price > 0 ? '<div class="price-val">$' + price.toFixed(2) + '</div><div class="price-lbl">store price</div>' : '') +
        '</div>' +
        '</div>';
    }}).join('');
  }} catch(e) {{
    results.innerHTML = '<div class="empty">Error: ' + e.message + '</div>';
    status.textContent = '';
  }}
}}
</script>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


# ---------------------------------------------------------------------------
# /market/price — local TCG market price lookup
# ---------------------------------------------------------------------------

@app.route("/market/price", methods=["POST"])
def market_price():
    """
    Local market price lookup.  Priority:
      1) Your store's 30-day sale_history (highest trust)
      2) Local inventory DB (imported via import_tcg_db.py)
      3) store_price fallback (passed in by the caller)

    Body: { name, language, store_price, set_code, card_number }
    Returns weighted market average + confidence level.
    """
    import statistics as _stat
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

    # ── 1. Your store's 30-day sales history ─────────────────────────────
    if name:
        ago = int((_time.time() - 30 * 86400) * 1000)
        row = db.execute("""
            SELECT AVG(price) as avg_price, COUNT(*) as cnt
            FROM sale_history
            WHERE LOWER(name) LIKE %s AND sold_at >= %s
        """, (f"%{name[:30]}%", ago)).fetchone()
        if row and row["avg_price"]:
            local_avg       = round(float(row["avg_price"]), 2)
            local_sales_30d = int(row["cnt"])

    # ── 2. Local inventory / TCG card database ────────────────────────────
    try:
        if set_code and card_number:
            qr_key = f"{set_code}-{card_number}".upper()
            row = db.execute(
                "SELECT name, price, rarity, set_code FROM inventory "
                "WHERE qr_code=%s AND price > 0",
                (qr_key,)
            ).fetchone()
            if row:
                tcgdb_price  = round(float(row["price"]), 2)
                tcgdb_name   = row["name"]
                tcgdb_set    = row["set_code"]
                tcgdb_rarity = row["rarity"]

        if tcgdb_price == 0.0 and name:
            rows = db.execute("""
                SELECT name, price, rarity, set_code FROM inventory
                WHERE LOWER(name) LIKE %s AND price > 0
                ORDER BY price DESC LIMIT 10
            """, (f"%{name[:25]}%",)).fetchall()
            if rows:
                prices       = [float(r["price"]) for r in rows]
                tcgdb_price  = round(_stat.median(prices), 2)
                tcgdb_name   = rows[0]["name"]
                tcgdb_set    = rows[0]["set_code"]
                tcgdb_rarity = rows[0]["rarity"]
    except Exception as _mp_err:
        log.debug("[market/price] DB lookup error: %s", _mp_err)

    # ── 3. Weighted average ────────────────────────────────────────────────
    # sale_history (trust=3), inventory DB (trust=2), store_price (trust=1)
    weighted_sum = 0.0
    weight_total = 0
    if local_avg > 0:
        weighted_sum += local_avg * 3
        weight_total += 3
    if tcgdb_price > 0:
        weighted_sum += tcgdb_price * 2
        weight_total += 2
    if store_price > 0:
        weighted_sum += store_price * 1
        weight_total += 1

    if weight_total == 0:
        market = 0.0
        confidence = "none"
    else:
        market = round(weighted_sum / weight_total, 2)
        confidence = "high" if local_sales_30d >= 3 else ("medium" if weight_total >= 4 else "low")

    return jsonify({
        "marketPrice":    market,
        "confidence":     confidence,
        "localSalesAvg":  local_avg,
        "localSales30d":  local_sales_30d,
        "tcgdbPrice":     tcgdb_price,
        "tcgdbName":      tcgdb_name,
        "tcgdbSet":       tcgdb_set,
        "tcgdbRarity":    tcgdb_rarity,
        "storePrice":     store_price,
        "language":       lang,
    })


# ===========================================================================
# ENTERPRISE ENDPOINTS
# ===========================================================================

# ---------------------------------------------------------------------------
# 1. JWT token issuance — POST /api/v1/auth/token
# ---------------------------------------------------------------------------

@app.route("/api/v1/auth/token", methods=["POST"])
@require_admin
def api_issue_token():
    """
    Issue a signed JWT for the APK / Expo app.  Admin-only.
    Body: { "label": "my-tablet", "ttl_hours": 24, "scopes": "scan,sales" }
    Response: { "token": "...", "expires_at": <epoch_s>, "label": "..." }
    """
    if not _JWT_AVAILABLE:
        return jsonify({"error": "PyJWT not installed"}), 501
    body   = request.get_json(silent=True) or {}
    label  = (body.get("label") or "unnamed").strip()[:80]
    ttl_h  = min(int(body.get("ttl_hours") or _JWT_TTL_H), 8760)   # cap 1 year
    scopes = (body.get("scopes") or "scan,sales").strip()
    exp    = int(_time.time()) + ttl_h * 3600
    payload = {
        "sub":    "api_client",
        "label":  label,
        "scopes": scopes,
        "exp":    exp,
        "iat":    int(_time.time()),
    }
    token = _jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGO)
    # Store hash so we can revoke
    t_hash = hashlib.sha256(token.encode()).hexdigest()
    db = get_db()
    db.execute(
        "INSERT INTO api_tokens (label, token_hash, created_by, scopes, expires_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (label, t_hash, session.get("admin_user", "admin"), scopes, exp * 1000)
    )
    db.commit()
    _audit_write("api_token.issue", label, f"ttl_hours={ttl_h}")
    return jsonify({"token": token, "expires_at": exp, "label": label, "scopes": scopes})


@app.route("/api/v1/auth/tokens", methods=["GET"])
@require_admin
def api_list_tokens():
    db = get_db()
    rows = db.execute(
        "SELECT id, label, created_by, scopes, expires_at, revoked, created_at, last_used "
        "FROM api_tokens ORDER BY created_at DESC LIMIT 100"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/v1/auth/tokens/<int:token_id>", methods=["DELETE"])
@require_admin
def api_revoke_token(token_id):
    db = get_db()
    db.execute("UPDATE api_tokens SET revoked=1 WHERE id=%s", (token_id,))
    db.commit()
    _audit_write("api_token.revoke", str(token_id))
    return jsonify({"ok": True, "revoked": token_id})


# ---------------------------------------------------------------------------
# 2. TOTP 2FA — setup, verify, disable
# ---------------------------------------------------------------------------

_2FA_VERIFY_HTML = """<!doctype html>
<html><head><meta charset=utf-8><title>2FA — HanryxVault</title>
<style>body{font-family:Arial,sans-serif;background:#111;color:#e0e0e0;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0}
.card{background:#1e1e1e;border:1px solid #333;border-radius:12px;padding:32px;width:340px}
h2{color:#d4a843;margin:0 0 24px}input{width:100%;padding:10px;background:#2a2a2a;
border:1px solid #444;color:#fff;border-radius:6px;font-size:18px;text-align:center;
letter-spacing:4px;box-sizing:border-box;margin-bottom:12px}
button{width:100%;padding:12px;background:#d4a843;color:#111;font-size:16px;font-weight:bold;
border:none;border-radius:6px;cursor:pointer}.err{color:#f87171;font-size:14px;margin-bottom:12px}
</style></head><body><div class="card"><h2>🔐 Two-Factor Auth</h2>
{% if error %}<p class="err">{{ error }}</p>{% endif %}
<form method=POST><input name=code placeholder="000000" maxlength=6 autofocus autocomplete=off>
<button type=submit>Verify</button></form></div></body></html>"""

_2FA_SETUP_HTML = """<!doctype html>
<html><head><meta charset=utf-8><title>2FA Setup — HanryxVault</title>
<style>body{font-family:Arial,sans-serif;background:#111;color:#e0e0e0;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0}
.card{background:#1e1e1e;border:1px solid #333;border-radius:12px;padding:32px;width:380px}
h2{color:#d4a843;margin:0 0 16px}img{display:block;margin:0 auto 16px;border:4px solid #333;border-radius:8px}
.secret{font-family:monospace;font-size:13px;background:#2a2a2a;padding:8px 12px;border-radius:6px;
word-break:break-all;margin-bottom:16px}input{width:100%;padding:10px;background:#2a2a2a;
border:1px solid #444;color:#fff;border-radius:6px;font-size:18px;text-align:center;
letter-spacing:4px;box-sizing:border-box;margin-bottom:12px}
button{width:100%;padding:12px;background:#d4a843;color:#111;font-size:16px;font-weight:bold;
border:none;border-radius:6px;cursor:pointer}.err{color:#f87171}.ok{color:#34d399}
p{font-size:13px;color:#aaa}</style></head>
<body><div class="card"><h2>🔐 Set Up 2FA</h2>
{% if qr_url %}<img src="{{ qr_url }}" width=200 height=200>
<p>Scan with Google Authenticator / Authy, then enter the 6-digit code to enable.</p>
<p class=secret>Manual key: {{ secret }}</p>
{% endif %}
{% if error %}<p class="err">{{ error }}</p>{% endif %}
{% if success %}<p class="ok">{{ success }}</p>{% endif %}
{% if not enabled %}
<form method=POST><input name=code placeholder="000000" maxlength=6 autofocus autocomplete=off>
<button type=submit>Enable 2FA</button></form>
{% else %}
<p class=ok>✓ 2FA is currently enabled.</p>
<form method=POST action=/admin/2fa/disable>
<button style=background:#ef4444>Disable 2FA</button></form>
{% endif %}
</div></body></html>"""


@app.route("/admin/2fa/setup", methods=["GET", "POST"])
@require_admin
def admin_2fa_setup():
    if not _PYOTP_AVAILABLE:
        return jsonify({"error": "pyotp not installed"}), 501
    db = get_db()
    row = db.execute("SELECT secret, enabled FROM totp_secrets WHERE username='admin'").fetchone()
    secret = row["secret"] if row else _pyotp.random_base32()
    enabled = bool(row and row["enabled"])

    if not row:
        db.execute(
            "INSERT INTO totp_secrets (username, secret, enabled) VALUES ('admin', %s, 0)",
            (secret,)
        )
        db.commit()

    if request.method == "POST":
        code = request.form.get("code", "").strip().replace(" ", "")
        totp = _pyotp.TOTP(secret)
        if totp.verify(code, valid_window=1):
            db.execute("UPDATE totp_secrets SET enabled=1 WHERE username='admin'")
            db.commit()
            _audit_write("admin.2fa.enable", "admin")
            return render_template_string(_2FA_SETUP_HTML, qr_url=None, secret=secret,
                                          enabled=True, error=None, success="2FA enabled successfully!")
        return render_template_string(_2FA_SETUP_HTML, qr_url=_totp_qr_url(secret),
                                      secret=secret, enabled=False, error="Invalid code — try again", success=None)

    qr_url = _totp_qr_url(secret) if not enabled else None
    return render_template_string(_2FA_SETUP_HTML, qr_url=qr_url, secret=secret,
                                  enabled=enabled, error=None, success=None)


def _totp_qr_url(secret: str) -> str:
    """Return a data-URI PNG of the TOTP QR code."""
    try:
        import pyotp as _p
        uri = _p.TOTP(secret).provisioning_uri("admin", issuer_name="HanryxVault")
        import qrcode as _qrc
        img = _qrc.make(uri)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


@app.route("/admin/2fa/verify", methods=["GET", "POST"])
def admin_2fa_verify():
    if not session.get("admin_2fa_pending"):
        return redirect("/admin/login")
    if request.method == "POST":
        code = request.form.get("code", "").strip().replace(" ", "")
        try:
            db = _direct_db()
            row = db.execute(
                "SELECT secret FROM totp_secrets WHERE username='admin' AND enabled=1"
            ).fetchone()
            db.close()
        except Exception:
            row = None
        if row and _PYOTP_AVAILABLE:
            import pyotp as _p
            if _p.TOTP(row["secret"]).verify(code, valid_window=1):
                session.pop("admin_2fa_pending", None)
                session["admin_authenticated"] = True
                session["admin_user"]          = "admin"
                session.permanent = True
                _audit_write("admin.login.2fa", "admin_dashboard")
                next_url = session.pop("admin_next_url", "/admin")
                return redirect(next_url)
        return render_template_string(_2FA_VERIFY_HTML, error="Invalid code — try again")
    return render_template_string(_2FA_VERIFY_HTML, error=None)


@app.route("/admin/2fa/disable", methods=["POST"])
@require_admin
def admin_2fa_disable():
    db = get_db()
    db.execute("UPDATE totp_secrets SET enabled=0 WHERE username='admin'")
    db.commit()
    _audit_write("admin.2fa.disable", "admin")
    return redirect("/admin/2fa/setup")


# ---------------------------------------------------------------------------
# 3. Audit log viewer — GET /api/v1/audit-log
# ---------------------------------------------------------------------------

@app.route("/api/v1/audit-log", methods=["GET"])
@require_admin
def api_audit_log():
    limit  = min(int(request.args.get("limit", 200)), 1000)
    action = request.args.get("action", "").strip()
    actor  = request.args.get("actor", "").strip()
    db     = get_db()
    where, args = [], []
    if action:
        where.append("action ILIKE %s"); args.append(f"%{action}%")
    if actor:
        where.append("actor ILIKE %s"); args.append(f"%{actor}%")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    args.append(limit)
    rows = db.execute(
        f"SELECT id, ts_ms, actor, action, resource, detail, ip, request_id "
        f"FROM audit_log {clause} ORDER BY ts_ms DESC LIMIT %s",
        args
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# ---------------------------------------------------------------------------
# 4. Z-report (end-of-day) — GET /api/v1/reports/z-report[/pdf]
# ---------------------------------------------------------------------------

def _build_z_report(date_str: str, db) -> dict:
    """Compute Z-report data for the given YYYY-MM-DD date string."""
    # epoch bounds for the date (local midnight to midnight)
    try:
        day_start = int(datetime.datetime.strptime(date_str, "%Y-%m-%d").timestamp() * 1000)
    except ValueError:
        day_start = int(datetime.datetime.combine(datetime.date.today(),
                                                   datetime.time.min).timestamp() * 1000)
    day_end = day_start + 86_400_000

    rows = db.execute(
        "SELECT name, price, quantity, sold_at FROM sale_history "
        "WHERE sold_at >= %s AND sold_at < %s ORDER BY sold_at",
        (day_start, day_end)
    ).fetchall()

    total_revenue   = sum(r["price"] * r["quantity"] for r in rows)
    total_items     = sum(r["quantity"] for r in rows)
    transaction_cnt = len(rows)

    # Top 10 by revenue
    card_totals: dict = {}
    for r in rows:
        k = r["name"]
        card_totals[k] = card_totals.get(k, 0) + r["price"] * r["quantity"]
    top_cards = sorted(card_totals.items(), key=lambda x: x[1], reverse=True)[:10]

    # Payment method breakdown from sales table
    method_rows = db.execute(
        "SELECT payment_method, SUM(sale_price) as total, COUNT(*) as cnt "
        "FROM sales WHERE sold_at >= %s AND sold_at < %s "
        "GROUP BY payment_method",
        (day_start, day_end)
    ).fetchall()
    by_method = {r["payment_method"] or "unknown": {"total": r["total"], "count": r["cnt"]}
                 for r in method_rows}

    # Existing EOD reconciliation if saved
    rec = db.execute(
        "SELECT * FROM eod_reconciliations WHERE date_str=%s", (date_str,)
    ).fetchone()

    return {
        "date":              date_str,
        "total_revenue":     round(total_revenue, 2),
        "total_items_sold":  total_items,
        "transaction_count": transaction_cnt,
        "by_payment_method": by_method,
        "top_cards":         [{"name": n, "revenue": round(v, 2)} for n, v in top_cards],
        "reconciliation":    dict(rec) if rec else None,
        "generated_at":      datetime.datetime.now().isoformat(),
    }


@app.route("/api/v1/reports/z-report", methods=["GET"])
@require_admin
def api_z_report():
    date_str = request.args.get("date") or datetime.date.today().isoformat()
    db       = get_db()
    report   = _build_z_report(date_str, db)
    return jsonify(report)


@app.route("/api/v1/reports/z-report/pdf", methods=["GET"])
@require_admin
def api_z_report_pdf():
    """Generate and stream a PDF Z-report for the given date."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors as _rl_colors
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import cm
    except ImportError:
        return jsonify({"error": "reportlab not installed"}), 501

    date_str = request.args.get("date") or datetime.date.today().isoformat()
    db       = get_db()
    rpt      = _build_z_report(date_str, db)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=2*cm, leftMargin=2*cm,
                             topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    gold   = _rl_colors.HexColor("#d4a843")
    elems  = []

    def _h(text, size=14, bold=True):
        s = styles["Normal"].clone("h")
        s.fontSize = size; s.textColor = gold; s.spaceAfter = 4
        if bold: s.fontName = "Helvetica-Bold"
        return Paragraph(text, s)

    def _p(text, size=10):
        s = styles["Normal"].clone("p"); s.fontSize = size; s.spaceAfter = 2
        return Paragraph(text, s)

    elems += [
        _h("HanryxVault POS", 20),
        _h(f"Z-Report — {date_str}", 14),
        Spacer(1, 0.4*cm),
        _p(f"Generated: {rpt['generated_at']}"),
        Spacer(1, 0.5*cm),
    ]

    # Summary table
    summary_data = [
        ["Metric", "Value"],
        ["Total Revenue",     f"${rpt['total_revenue']:.2f}"],
        ["Items Sold",        str(rpt["total_items_sold"])],
        ["Transactions",      str(rpt["transaction_count"])],
    ]
    for method, d in rpt["by_payment_method"].items():
        summary_data.append([f"  {method.title()}", f"${d['total']:.2f} ({d['count']} txn)"])
    ts = TableStyle([
        ("BACKGROUND", (0,0), (-1,0), gold),
        ("TEXTCOLOR",  (0,0), (-1,0), _rl_colors.black),
        ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",   (0,0), (-1,-1), 10),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [_rl_colors.white, _rl_colors.HexColor("#f5f5f5")]),
        ("GRID",       (0,0), (-1,-1), 0.5, _rl_colors.grey),
        ("LEFTPADDING",  (0,0), (-1,-1), 8),
        ("RIGHTPADDING", (0,0), (-1,-1), 8),
        ("TOPPADDING",   (0,0), (-1,-1), 4),
        ("BOTTOMPADDING",(0,0), (-1,-1), 4),
    ])
    t = Table(summary_data, colWidths=[10*cm, 6*cm])
    t.setStyle(ts)
    elems += [t, Spacer(1, 0.6*cm)]

    if rpt["top_cards"]:
        elems.append(_h("Top Cards by Revenue", 12))
        card_data = [["Card Name", "Revenue"]] + [
            [c["name"][:50], f"${c['revenue']:.2f}"] for c in rpt["top_cards"]
        ]
        tc = Table(card_data, colWidths=[12*cm, 4*cm])
        tc.setStyle(ts)
        elems += [tc, Spacer(1, 0.4*cm)]

    rec = rpt.get("reconciliation")
    if rec:
        elems.append(_h("Cash Reconciliation", 12))
        rec_data = [
            ["Opening Float",  f"${rec.get('opening_float', 0):.2f}"],
            ["Expected Cash",  f"${rec.get('expected_cash', 0):.2f}"],
            ["Actual Cash",    f"${rec.get('actual_cash', 0):.2f}"],
            ["Discrepancy",    f"${rec.get('discrepancy', 0):.2f}"],
        ]
        tr = Table(rec_data, colWidths=[10*cm, 6*cm])
        tr.setStyle(ts)
        elems.append(tr)

    doc.build(elems)
    buf.seek(0)
    return send_file(buf, mimetype="application/pdf",
                     as_attachment=True, download_name=f"z-report-{date_str}.pdf")


# ---------------------------------------------------------------------------
# 5. Returns / Refunds — /api/v1/returns
# ---------------------------------------------------------------------------

@app.route("/api/v1/returns", methods=["GET"])
@require_admin
def api_list_returns():
    limit = min(int(request.args.get("limit", 50)), 500)
    db    = get_db()
    rows  = db.execute(
        "SELECT r.*, array_agg(row_to_json(ri)) AS items "
        "FROM returns r LEFT JOIN return_items ri ON ri.return_id=r.id "
        "GROUP BY r.id ORDER BY r.created_at DESC LIMIT %s", (limit,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/v1/returns", methods=["POST"])
@require_admin
def api_create_return():
    """
    Body: {
      "original_sale_id": 42,        (optional)
      "reason": "customer changed mind",
      "refund_amount": 12.50,
      "refund_method": "cash",
      "items": [{"qr_code": "SV1-1", "name": "Charizard", "quantity": 1,
                 "unit_price": 12.50, "restock": true}]
    }
    Restocked items are incremented back in inventory automatically.
    """
    body          = request.get_json(silent=True) or {}
    items         = body.get("items", [])
    reason        = (body.get("reason") or "").strip()[:500]
    refund_amount = float(body.get("refund_amount") or 0)
    refund_method = (body.get("refund_method") or "original").strip()
    orig_sale_id  = body.get("original_sale_id")
    reference     = f"RET-{_now_ms()}"

    db = get_db()
    db.execute(
        "INSERT INTO returns (reference, original_sale_id, reason, refund_amount, "
        "refund_method, status, created_by) VALUES (%s, %s, %s, %s, %s, 'completed', %s)",
        (reference, orig_sale_id, reason, refund_amount, refund_method,
         session.get("admin_user", "admin"))
    )
    return_id = db.execute("SELECT lastval()").fetchone()[0]

    restocked = []
    for item in items:
        qr    = (item.get("qr_code") or "").strip()
        name  = (item.get("name") or "").strip()
        qty   = int(item.get("quantity") or 1)
        price = float(item.get("unit_price") or 0)
        restock = bool(item.get("restock", True))
        db.execute(
            "INSERT INTO return_items (return_id, qr_code, name, quantity, unit_price, restock) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (return_id, qr, name, qty, price, int(restock))
        )
        if restock and qr:
            db.execute(
                "UPDATE inventory SET stock = stock + %s WHERE qr_code = %s", (qty, qr)
            )
            restocked.append(qr)
            _invalidate_inventory(qr)

    db.commit()
    _audit_write("return.create", reference, f"items={len(items)} refund=${refund_amount:.2f}")
    return jsonify({"ok": True, "reference": reference, "return_id": return_id,
                    "restocked": restocked}), 201


@app.route("/api/v1/returns/<int:return_id>", methods=["GET"])
@require_admin
def api_get_return(return_id):
    db  = get_db()
    row = db.execute("SELECT * FROM returns WHERE id=%s", (return_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    items = db.execute("SELECT * FROM return_items WHERE return_id=%s", (return_id,)).fetchall()
    result = dict(row)
    result["items"] = [dict(i) for i in items]
    return jsonify(result)


# ---------------------------------------------------------------------------
# 6. Suppliers — /api/v1/suppliers
# ---------------------------------------------------------------------------

@app.route("/api/v1/suppliers", methods=["GET"])
@require_admin
def api_list_suppliers():
    db   = get_db()
    rows = db.execute("SELECT * FROM suppliers ORDER BY name").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/v1/suppliers", methods=["POST"])
@require_admin
def api_create_supplier():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    db = get_db()
    try:
        db.execute(
            "INSERT INTO suppliers (name, contact, email, phone, address, notes) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (name, body.get("contact",""), body.get("email",""),
             body.get("phone",""), body.get("address",""), body.get("notes",""))
        )
        db.commit()
    except Exception as e:
        if "unique" in str(e).lower():
            return jsonify({"error": "Supplier with that name already exists"}), 409
        raise
    sid = db.execute("SELECT lastval()").fetchone()[0]
    _audit_write("supplier.create", name)
    return jsonify({"ok": True, "id": sid}), 201


@app.route("/api/v1/suppliers/<int:sid>", methods=["GET"])
@require_admin
def api_get_supplier(sid):
    db  = get_db()
    row = db.execute("SELECT * FROM suppliers WHERE id=%s", (sid,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    # attach purchase orders
    pos = db.execute(
        "SELECT id, reference, status, total_cost, created_at FROM purchase_orders "
        "WHERE supplier=(SELECT name FROM suppliers WHERE id=%s) ORDER BY created_at DESC LIMIT 20",
        (sid,)
    ).fetchall()
    result = dict(row)
    result["purchase_orders"] = [dict(p) for p in pos]
    return jsonify(result)


@app.route("/api/v1/suppliers/<int:sid>", methods=["PUT"])
@require_admin
def api_update_supplier(sid):
    body = request.get_json(silent=True) or {}
    db   = get_db()
    db.execute(
        "UPDATE suppliers SET name=%s, contact=%s, email=%s, phone=%s, address=%s, notes=%s "
        "WHERE id=%s",
        (body.get("name",""), body.get("contact",""), body.get("email",""),
         body.get("phone",""), body.get("address",""), body.get("notes",""), sid)
    )
    db.commit()
    _audit_write("supplier.update", str(sid))
    return jsonify({"ok": True})


@app.route("/api/v1/suppliers/<int:sid>", methods=["DELETE"])
@require_admin
def api_delete_supplier(sid):
    db = get_db()
    db.execute("DELETE FROM suppliers WHERE id=%s", (sid,))
    db.commit()
    _audit_write("supplier.delete", str(sid))
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 7. Low-stock alerts — /api/v1/stock-alerts
# ---------------------------------------------------------------------------

@app.route("/api/v1/stock-alerts", methods=["GET"])
@require_admin
def api_list_stock_alerts():
    """
    List low-stock alert configs.  Only sealed products appear here — singles are excluded.
    Query params: ?eligible=1 lists all sealed products in inventory (for adding alerts).
    """
    db = get_db()

    # ?eligible=1 → return all sealed products that could have an alert configured
    if request.args.get("eligible"):
        rows = db.execute(
            "SELECT i.qr_code, i.name, i.stock, i.item_type, "
            "   lsc.threshold, lsc.alerted "
            "FROM inventory i "
            "LEFT JOIN low_stock_config lsc ON lsc.qr_code=i.qr_code "
            "WHERE LOWER(i.item_type) NOT IN ('single', 'card', '') "
            "ORDER BY i.item_type, i.name"
        ).fetchall()
        return jsonify([dict(r) for r in rows])

    rows = db.execute(
        "SELECT lsc.qr_code, lsc.threshold, lsc.alerted, i.name, i.stock, i.item_type "
        "FROM low_stock_config lsc "
        "LEFT JOIN inventory i ON i.qr_code=lsc.qr_code "
        "WHERE LOWER(COALESCE(i.item_type,'')) NOT IN ('single', 'card') "
        "ORDER BY i.name"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/v1/stock-alerts", methods=["POST"])
@require_admin
def api_set_stock_alert():
    """
    Set a low-stock alert threshold for a product.
    Only valid for sealed products (booster packs, ETBs, boxes, etc.).
    Individual card singles are excluded — use reorder logic for those.
    """
    body      = request.get_json(silent=True) or {}
    qr_code   = (body.get("qr_code") or "").strip()
    threshold = max(1, int(body.get("threshold") or 1))
    if not qr_code:
        return jsonify({"error": "qr_code required"}), 400

    db = get_db()
    # Validate item_type — reject individual card singles
    item_row = db.execute(
        "SELECT name, item_type FROM inventory WHERE qr_code=%s", (qr_code,)
    ).fetchone()
    if item_row and item_row["item_type"] and \
            item_row["item_type"].lower() in ("single", "card"):
        return jsonify({
            "error": "Low-stock alerts are for sealed products only (booster packs, boxes, ETBs). "
                     f"'{item_row['name']}' is a {item_row['item_type']}."
        }), 422

    db.execute(
        "INSERT INTO low_stock_config (qr_code, threshold, alerted) VALUES (%s, %s, 0) "
        "ON CONFLICT (qr_code) DO UPDATE SET threshold=EXCLUDED.threshold, alerted=0",
        (qr_code, threshold)
    )
    db.commit()
    _audit_write("stock_alert.set", qr_code, f"threshold={threshold}")
    item_name = item_row["name"] if item_row else qr_code
    return jsonify({"ok": True, "qr_code": qr_code, "name": item_name, "threshold": threshold}), 201


@app.route("/api/v1/stock-alerts/<path:qr_code>", methods=["DELETE"])
@require_admin
def api_delete_stock_alert(qr_code):
    db = get_db()
    db.execute("DELETE FROM low_stock_config WHERE qr_code=%s", (qr_code,))
    db.commit()
    _audit_write("stock_alert.delete", qr_code)
    return jsonify({"ok": True})


def _run_low_stock_checker():
    """Background thread: check every 15 min, email when stock drops below threshold."""
    import time as _t
    _t.sleep(60)   # give the server 60 s to finish startup
    while True:
        try:
            db = _direct_db()
            # Only alert for sealed/product items — never individual card singles
            rows = db.execute(
                "SELECT lsc.qr_code, lsc.threshold, lsc.alerted, i.name, i.stock, i.item_type "
                "FROM low_stock_config lsc "
                "JOIN inventory i ON i.qr_code=lsc.qr_code "
                "WHERE i.stock <= lsc.threshold AND lsc.alerted=0 "
                "AND LOWER(i.item_type) NOT IN ('single', 'card')"
            ).fetchall()
            for r in rows:
                cfg = _get_email_cfg()
                if cfg["enabled"]:
                    def _send_alert(name=r["name"], stock=r["stock"], threshold=r["threshold"],
                                    qr=r["qr_code"]):
                        try:
                            import smtplib as _sm, email.mime.multipart as _mm, email.mime.text as _mt
                            c = _get_email_cfg()
                            msg = _mm.MIMEMultipart("alternative")
                            msg["Subject"] = f"⚠️ Low Stock: {name} ({stock} left)"
                            msg["From"]    = f"HanryxVault <{c['user']}>"
                            msg["To"]      = c["notify"]
                            html = (f"<h2>Low Stock Alert</h2>"
                                    f"<p><strong>{name}</strong> ({qr}) is at <strong>{stock}</strong> "
                                    f"unit(s) — threshold: {threshold}.</p>"
                                    f"<p>Please reorder stock.</p>")
                            msg.attach(_mt.MIMEText(html, "html"))
                            with _sm.SMTP("smtp.gmail.com", 587, timeout=15) as sv:
                                sv.ehlo(); sv.starttls()
                                sv.login(c["user"], c["password"])
                                sv.sendmail(c["user"], c["notify"], msg.as_string())
                            log.info("[low-stock] Alert sent for %s (stock=%s)", name, stock)
                        except Exception as _e:
                            log.warning("[low-stock] Email failed: %s", _e)
                    _bg(_send_alert)
                db.execute(
                    "UPDATE low_stock_config SET alerted=1 WHERE qr_code=%s", (r["qr_code"],)
                )
            db.commit()
            db.close()
        except Exception as _e:
            log.debug("[low-stock] checker error: %s", _e)
        import time as _t2; _t2.sleep(900)   # 15 min


# ---------------------------------------------------------------------------
# 8. PDF receipt for an individual sale — GET /api/v1/sales/<id>/receipt.pdf
# ---------------------------------------------------------------------------

@app.route("/api/v1/sales/<int:sale_id>/receipt.pdf", methods=["GET"])
@require_admin
def api_sale_receipt_pdf(sale_id):
    try:
        from reportlab.lib.pagesizes import A6
        from reportlab.lib import colors as _rlc
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import cm
    except ImportError:
        return jsonify({"error": "reportlab not installed"}), 501

    db  = get_db()
    row = db.execute("SELECT * FROM sales WHERE id=%s", (sale_id,)).fetchone()
    if not row:
        return jsonify({"error": "Sale not found"}), 404

    buf  = io.BytesIO()
    doc  = SimpleDocTemplate(buf, pagesize=A6, rightMargin=1*cm, leftMargin=1*cm,
                              topMargin=1*cm, bottomMargin=1*cm)
    ss   = getSampleStyleSheet()
    gold = _rlc.HexColor("#d4a843")

    def _lbl(text, size=9, bold=False):
        s = ss["Normal"].clone("l"); s.fontSize = size; s.spaceAfter = 2
        if bold: s.fontName = "Helvetica-Bold"
        return Paragraph(text, s)

    items_json = row.get("items") or "[]"
    try:
        items = json.loads(items_json) if isinstance(items_json, str) else items_json
    except Exception:
        items = []

    elems = [
        _lbl("HanryxVault", 14, bold=True),
        _lbl(f"Receipt #{sale_id}", 10),
        _lbl(datetime.datetime.fromtimestamp(row["sold_at"]/1000).strftime("%d %b %Y %H:%M"), 9),
        Spacer(1, 0.3*cm),
    ]

    if items:
        tdata = [["Item", "Qty", "Price"]] + [
            [i.get("name","")[:30], str(i.get("quantity",1)), f"${float(i.get('price',0)):.2f}"]
            for i in items
        ]
        t = Table(tdata, colWidths=[7*cm, 1.5*cm, 2.5*cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), gold),
            ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE",   (0,0), (-1,-1), 8),
            ("GRID",       (0,0), (-1,-1), 0.3, _rlc.grey),
            ("LEFTPADDING",  (0,0), (-1,-1), 4),
            ("RIGHTPADDING", (0,0), (-1,-1), 4),
        ]))
        elems.append(t)
        elems.append(Spacer(1, 0.3*cm))

    total = float(row.get("sale_price") or row.get("total_price") or 0)
    method = row.get("payment_method") or "POS"
    elems += [
        _lbl(f"<b>Total: ${total:.2f}</b>", 11, bold=True),
        _lbl(f"Payment: {method}", 9),
        Spacer(1, 0.3*cm),
        _lbl("Thank you for your purchase!", 8),
    ]

    doc.build(elems)
    buf.seek(0)
    return send_file(buf, mimetype="application/pdf",
                     as_attachment=True, download_name=f"receipt-{sale_id}.pdf")


# ---------------------------------------------------------------------------
# 9. Connection pool health — GET /api/v1/health/pool
# ---------------------------------------------------------------------------

@app.route("/api/v1/health/pool", methods=["GET"])
@require_admin
def api_health_pool():
    pool = _get_pool()
    try:
        used = len(pool._used) if hasattr(pool, "_used") else "?"
        free = len(pool._pool) if hasattr(pool, "_pool") else "?"
        mx   = pool.maxconn   if hasattr(pool, "maxconn") else "?"
    except Exception:
        used = free = mx = "?"
    return jsonify({
        "pool_used":  used,
        "pool_free":  free,
        "pool_max":   mx,
        "status":     "ok" if used != "?" and used < (mx or 999) else "unknown",
    })


# ---------------------------------------------------------------------------
# Intelligent pricing engine
# ---------------------------------------------------------------------------
# Ported from the Node.js implementation:
#   - Multi-language card input normalisation via Google Translate
#   - eBay sold-listing scraper (3 pages in parallel)
#   - Score-based listing filter (name, number, set matching)
#   - Outlier-robust price model (median / mean / confidence)
#   - PostgreSQL-backed translation + pricing cache
#   - Redis-backed short-TTL pricing cache (10 min)
# ---------------------------------------------------------------------------

import statistics as _stats_mod


def _translate_with_cache(text: str, target_lang: str) -> str:
    """Translate text to target_lang, using the DB cache to avoid repeat API calls."""
    if not text or not _TRANSLATE_OK:
        return text
    if target_lang == "en" and all(ord(c) < 128 for c in text):
        return text  # already ASCII English — skip API
    try:
        db = _direct_db()
        row = db.execute(
            "SELECT translated FROM translation_cache WHERE original=%s AND lang=%s",
            (text, target_lang),
        ).fetchone()
        if row:
            db.close()
            return row["translated"]
        translated = _GoogleTranslator(source="auto", target=target_lang).translate(text)
        db.execute(
            "INSERT INTO translation_cache (original, lang, translated) VALUES (%s,%s,%s) "
            "ON CONFLICT (original, lang) DO UPDATE SET translated=EXCLUDED.translated",
            (text, target_lang, translated),
        )
        db.commit()
        db.close()
        return translated
    except Exception as _e:
        log.debug("[translate] failed for %r → %s: %s", text[:30], target_lang, _e)
        return text


def _translate_card_input(card: dict, lang: str = "en") -> dict:
    """Normalise a card dict to English so eBay queries work correctly."""
    if lang == "en":
        return card
    return {
        "name":         _translate_with_cache(card.get("name", ""), "en"),
        "set":          _translate_with_cache(card.get("set", ""), "en") if card.get("set") else None,
        "number":       card.get("number"),
        "original_lang": lang,
    }


def _score_listing(title: str, card: dict) -> int:
    """
    Score an eBay listing against a card spec.
    Returns an integer; listings scoring < 6 are discarded.
    """
    title = title.lower()
    score = 0
    name = (card.get("name") or "").lower()
    if name and name in title:
        score += 5
    number = card.get("number") or ""
    if number and number in title:
        score += 5
    set_name = (card.get("set") or "").lower()
    if set_name and set_name in title:
        score += 3
    if "pokemon" in title or "pokémon" in title:
        score += 1
    if card.get("grade", "raw") == "raw" and "psa" in title:
        score -= 5
    return score


def _filter_and_score(items: list, card: dict) -> list:
    """Filter listings to score >= 6, attach score, sort descending."""
    scored = [
        {**item, "score": _score_listing(item.get("title", ""), card)}
        for item in items
    ]
    return sorted(
        (i for i in scored if i["score"] >= 6),
        key=lambda x: x["score"],
        reverse=True,
    )


def _remove_outliers(prices: list[float]) -> list[float]:
    """Remove prices further than 2 standard deviations from the mean."""
    if len(prices) < 3:
        return prices
    mean = sum(prices) / len(prices)
    variance = sum((p - mean) ** 2 for p in prices) / len(prices)
    std = variance ** 0.5
    return [p for p in prices if abs(p - mean) <= 2 * std] or prices


def _build_price_model(items: list) -> dict:
    """Build a price model from scored listings: median, avg, low, high, confidence."""
    prices = sorted(_remove_outliers([i["price"] for i in items if i.get("price")]))
    if not prices:
        return {"market": None, "average": None, "low": None, "high": None, "confidence": 0}
    n      = len(prices)
    median = prices[n // 2]
    avg    = sum(prices) / n
    return {
        "market":     round(median, 2),
        "average":    round(avg, 2),
        "low":        round(prices[0], 2),
        "high":       round(prices[-1], 2),
        "confidence": min(round(n / 20, 2), 1.0),
        "sample_size": n,
    }


def _fetch_ebay_page(query: str, page: int) -> str:
    """Fetch one page of eBay sold listings. Returns raw HTML or ''."""
    if not _BS4_OK:
        return ""
    url = (
        f"https://www.ebay.com/sch/i.html"
        f"?_nkw={urllib.parse.quote(query)}&_pgn={page}&LH_Sold=1&LH_Complete=1"
    )
    try:
        r = _requests.get(
            url,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
        )
        r.raise_for_status()
        return r.text
    except Exception as _e:
        log.debug("[ebay] page %d fetch failed: %s", page, _e)
        return ""


def _fetch_ebay_sales(card: dict) -> list[dict]:
    """Fetch eBay sold listings for a card across 3 pages in parallel."""
    if not _BS4_OK:
        return []
    name   = (card.get("name") or "").strip()
    number = (card.get("number") or "").strip()
    set_n  = (card.get("set") or "").strip()
    query  = " ".join(filter(None, [name, number, set_n, "pokemon"])).strip()

    with _TPE(max_workers=3) as pool:
        pages = list(pool.map(lambda p: _fetch_ebay_page(query, p), [1, 2, 3]))

    items = []
    for html in pages:
        if not html:
            continue
        soup = _BS4(html, "lxml")
        for el in soup.select(".s-item"):
            title_el = el.select_one(".s-item__title")
            price_el = el.select_one(".s-item__price")
            if not title_el or not price_el:
                continue
            title_text = title_el.get_text(strip=True)
            if title_text.lower() in ("shop on ebay", ""):
                continue
            price_str = re.sub(r"[^0-9.]", "", price_el.get_text().split("to")[0])
            try:
                price = float(price_str)
                if price > 0:
                    items.append({"title": title_text, "price": price})
            except (ValueError, TypeError):
                pass
    return items


def _get_pricing_cache(query: str):
    """Check Redis first, then PostgreSQL for a cached price model."""
    # L1: Redis (10 min TTL)
    rkey = f"hv:pricing:{hashlib.md5(query.encode()).hexdigest()}"
    cached = _rcache_get(rkey)
    if cached:
        return cached, True
    # L2: PostgreSQL (no TTL — manual invalidation via API)
    try:
        db = _direct_db()
        row = db.execute(
            "SELECT pricing FROM pricing_cache WHERE query=%s", (query,)
        ).fetchone()
        db.close()
        if row:
            val = row["pricing"] if isinstance(row["pricing"], dict) else json.loads(row["pricing"])
            _rcache_set(rkey, val, ttl=600)
            return val, True
    except Exception:
        pass
    return None, False


def _set_pricing_cache(query: str, pricing: dict):
    """Write a price model to Redis + PostgreSQL."""
    rkey = f"hv:pricing:{hashlib.md5(query.encode()).hexdigest()}"
    _rcache_set(rkey, pricing, ttl=600)
    try:
        db = _direct_db()
        db.execute(
            "INSERT INTO pricing_cache (query, pricing) VALUES (%s, %s::jsonb) "
            "ON CONFLICT (query) DO UPDATE SET pricing=EXCLUDED.pricing, created_at=%s",
            (query, json.dumps(pricing), _now_ms()),
        )
        db.commit()
        db.close()
    except Exception as _e:
        log.debug("[pricing-cache] write failed: %s", _e)


# ── GET /api/v1/pricing/intelligent ─────────────────────────────────────────

@app.route("/api/v1/pricing/intelligent", methods=["GET"])
@require_api_token
def api_pricing_intelligent():
    """
    Intelligent market-price lookup combining:
      1. Optional multi-language input normalisation (Google Translate)
      2. eBay sold-listing scraper (3 pages, parallel, cached)
      3. Score-based listing filter (name/set/number matching)
      4. Outlier-robust price model (median, avg, confidence)

    Query params:
      name     — card name  (required)
      set      — set name   (optional)
      number   — card number (optional)
      lang     — input language ISO code, e.g. "ja", "de" (default "en")
      refresh  — set "1" to bypass cache and force a fresh eBay fetch

    Returns:
      { card, query, pricing, top_matches }
    """
    name   = (request.args.get("name") or "").strip()
    set_n  = (request.args.get("set") or "").strip()
    number = (request.args.get("number") or "").strip()
    lang   = (request.args.get("lang") or "en").strip().lower()
    refresh = request.args.get("refresh", "0") == "1"

    if not name:
        return jsonify({"error": "name is required"}), 400

    # Normalise input to English
    card = _translate_card_input(
        {"name": name, "set": set_n or None, "number": number or None},
        lang,
    )
    query = " ".join(filter(None, [
        card.get("name"), card.get("number"), card.get("set")
    ])).strip()

    # Check cache
    pricing  = None
    matched  = []
    from_cache = False
    if not refresh:
        pricing, from_cache = _get_pricing_cache(query)

    if not pricing:
        sales   = _fetch_ebay_sales(card)
        matched = _filter_and_score(sales, card)
        pricing = _build_price_model(matched)
        _bg(_set_pricing_cache, query, pricing)

    return jsonify({
        "card":        card,
        "query":       query,
        "pricing":     pricing,
        "from_cache":  from_cache,
        "top_matches": matched[:5] if matched else [],
    })


@app.route("/api/v1/pricing/cache/<path:query_path>", methods=["DELETE"])
@require_admin
def api_pricing_cache_delete(query_path):
    """Delete a cached price model so the next request fetches fresh data."""
    query = urllib.parse.unquote(query_path)
    rkey  = f"hv:pricing:{hashlib.md5(query.encode()).hexdigest()}"
    _rcache_del(rkey)
    try:
        db = get_db()
        db.execute("DELETE FROM pricing_cache WHERE query=%s", (query,))
        db.commit()
    except Exception:
        pass
    return jsonify({"ok": True, "invalidated": query})


# ---------------------------------------------------------------------------
# 10. Storefront sync queue — GET/POST /api/v1/sync/pending
# ---------------------------------------------------------------------------

@app.route("/api/v1/sync/pending", methods=["GET"])
@require_api_token
def api_sync_pending():
    """
    Returns unsynced stock/price changes for the storefront to pull.
    Optional ?limit=N (default 100, max 500).
    """
    limit = min(int(request.args.get("limit", 100)), 500)
    db    = get_db()
    rows  = db.execute(
        "SELECT id, qr_code, change_type, delta, created_at "
        "FROM unsynced_changes WHERE synced = 0 ORDER BY id ASC LIMIT %s",
        (limit,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/v1/sync/ack", methods=["POST"])
@require_api_token
def api_sync_ack():
    """
    Mark one or more unsynced_changes rows as synced.
    Body: { "ids": [1, 2, 3] }
    """
    body = request.get_json(force=True, silent=True) or {}
    ids  = [int(i) for i in (body.get("ids") or []) if str(i).isdigit()]
    if not ids:
        return jsonify({"error": "ids array required"}), 400
    db = get_db()
    db.execute(
        "UPDATE unsynced_changes SET synced=1 WHERE id = ANY(%s::bigint[])",
        (ids,),
    )
    db.commit()
    return jsonify({"ok": True, "acked": len(ids)})


def _warmup_smart_scanner():
    """Pre-load the smart scanner index in the background so the first scan is fast."""
    try:
        db = _direct_db()
        _smart_scanner.smart_scan("__warmup__", db)
        db.close()
        log.info("[smart-scan] Index warmed up at startup")
    except Exception as e:
        log.warning("[smart-scan] Warm-up failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    _load_tokens_from_db()
    threading.Thread(target=sync_inventory_from_cloud,  daemon=True).start()
    threading.Thread(target=_warmup_smart_scanner,      daemon=True).start()
    threading.Thread(target=_run_low_stock_checker,     daemon=True).start()
    _cleanup_scan_queue()
    log.info("[server] Starting HanryxVault POS — Enterprise Edition — http://0.0.0.0:8080")
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
