#!/usr/bin/env python3
"""
HanryxVault QR Scan Hub
========================
Reads from USB and Bluetooth HID QR scanners (they appear as keyboard-style
/dev/input/event* devices once paired) and makes every scan available to ANY
app running on the Pi — not just the POS server.

HOW IT WORKS
  The daemon runs a tiny HTTP hub on port 8765.  Any project subscribes to:
    GET  http://localhost:8765/scan/stream   ← SSE stream (instant push)
    GET  http://localhost:8765/scan/pending  ← polling fallback
    POST http://localhost:8765/scan/ack/<id> ← mark scan handled
    GET  http://localhost:8765/health        ← status + connected devices

  It ALSO forwards scans to every URL listed in /opt/hanryxvault/scan_endpoints.conf
  so your existing POS server receives scans exactly as before.

SCANNER SUPPORT
  • USB QR scanners:   plug in — detected within 3 seconds
  • Bluetooth QR:      pair once with bluetoothctl (see below), then auto-reconnects
  • Multiple scanners: all run simultaneously in separate threads
  • Duplicate filter:  ignores identical scan within 1.5 s (scanner double-fire)

BLUETOOTH PAIRING (one-time setup)
  sudo bluetoothctl
  power on → scan on → pair <MAC> → trust <MAC> → connect <MAC> → quit

ADDING A NEW APP
  Add one line to /opt/hanryxvault/scan_endpoints.conf:
    http://localhost:8081/scan
  That app now receives every scan automatically.
"""

import sys
import time
import threading
import urllib.request
import json
import re
import os
import queue as _queue_mod
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

try:
    import evdev
    from evdev import InputDevice, ecodes, categorize
except ImportError:
    print("[scan-hub] ERROR: evdev not installed — run: pip install evdev")
    sys.exit(1)

# ── Configuration ─────────────────────────────────────────────────────────────
HUB_PORT         = int(os.environ.get("SCAN_HUB_PORT", 8765))
CONF_DIR         = os.environ.get("HANRYX_DIR", "/opt/hanryxvault")
ENDPOINTS_CONF   = os.path.join(CONF_DIR, "scan_endpoints.conf")
MIN_SCAN_LEN     = 3        # ignore codes shorter than this (noise)
MAX_SCAN_LEN     = 512      # ignore absurdly long events (key logging protection)
DEDUP_WINDOW_S   = 1.5      # suppress identical scan within this many seconds
DEVICE_POLL_S    = 3        # how often to check for new scanners
FORWARD_TIMEOUT  = 3        # seconds to wait for each webhook endpoint

# ── Shared state ──────────────────────────────────────────────────────────────
_scan_lock        = threading.Lock()
_scan_queue       = []        # list of {"id": int, "qrCode": str, "processed": bool}
_scan_id_counter  = 0

_sse_lock         = threading.Lock()
_sse_subscribers  = []        # list of queue.Queue — one per SSE client

_device_lock      = threading.Lock()
_active_paths     = set()     # /dev/input/eventN currently grabbed

_last_scan_time   = {}        # qr_code → timestamp  (dedup)
_dedup_lock       = threading.Lock()

_stats = {
    "total_scans":    0,
    "devices_seen":   0,
    "forward_errors": 0,
}

# ── Key code → character ──────────────────────────────────────────────────────
_KEYMAP = {
     2:('1','!'),  3:('2','@'),  4:('3','#'),  5:('4','$'),  6:('5','%'),
     7:('6','^'),  8:('7','&'),  9:('8','*'), 10:('9','('), 11:('0',')'),
    12:('-','_'), 13:('=','+'), 16:('q','Q'), 17:('w','W'), 18:('e','E'),
    19:('r','R'), 20:('t','T'), 21:('y','Y'), 22:('u','U'), 23:('i','I'),
    24:('o','O'), 25:('p','P'), 26:('[','{'), 27:(']','}'), 30:('a','A'),
    31:('s','S'), 32:('d','D'), 33:('f','F'), 34:('g','G'), 35:('h','H'),
    36:('j','J'), 37:('k','K'), 38:('l','L'), 39:(';',':'), 40:("'",'"'),
    44:('z','Z'), 45:('x','X'), 46:('c','C'), 47:('v','V'), 48:('b','B'),
    49:('n','N'), 50:('m','M'), 51:(',','<'), 52:('.','>'), 53:('/','?'),
    57:(' ',' '),
}
_KEY_ENTER   = 28
_KEY_LSHIFT  = 42
_KEY_RSHIFT  = 54
_SHIFT_KEYS  = {_KEY_LSHIFT, _KEY_RSHIFT}

_EXCLUDE_NAMES = re.compile(
    r'(mouse|touchpad|touchscreen|trackpad|power|button|video|camera|audio|'
    r'keyboard|accel|gyro|lid|switch)',
    re.IGNORECASE
)


# ── Scan processing ───────────────────────────────────────────────────────────

def _is_duplicate(qr_code: str) -> bool:
    now = time.monotonic()
    with _dedup_lock:
        last = _last_scan_time.get(qr_code, 0)
        if now - last < DEDUP_WINDOW_S:
            return True
        _last_scan_time[qr_code] = now
        # prune old entries to avoid unbounded growth
        if len(_last_scan_time) > 500:
            cutoff = now - DEDUP_WINDOW_S * 10
            for k in [k for k, v in _last_scan_time.items() if v < cutoff]:
                del _last_scan_time[k]
    return False


def _enqueue_scan(qr_code: str):
    """Accept a scan, add it to the local queue, push to SSE subscribers, forward to webhooks."""
    global _scan_id_counter

    if not MIN_SCAN_LEN <= len(qr_code) <= MAX_SCAN_LEN:
        return
    if _is_duplicate(qr_code):
        print(f"[scan-hub] Dedup suppressed: {qr_code[:40]}", flush=True)
        return

    with _scan_lock:
        _scan_id_counter += 1
        entry = {"id": _scan_id_counter, "qrCode": qr_code, "processed": False}
        _scan_queue.append(entry)
        # keep queue bounded — discard processed items beyond 200
        if len(_scan_queue) > 200:
            _scan_queue[:] = [s for s in _scan_queue if not s["processed"]][-200:]

    _stats["total_scans"] += 1
    print(f"[scan-hub] ✓ {qr_code[:60]}", flush=True)

    # Push instantly to SSE subscribers
    event = json.dumps({"id": _scan_id_counter, "qrCode": qr_code})
    with _sse_lock:
        dead = []
        for q in _sse_subscribers:
            try:
                q.put_nowait(event)
            except _queue_mod.Full:
                dead.append(q)
        for q in dead:
            _sse_subscribers.remove(q)

    # Forward to webhook endpoints in the background
    endpoints = _load_endpoints()
    if endpoints:
        threading.Thread(
            target=_forward_to_endpoints,
            args=(qr_code, endpoints),
            daemon=True,
        ).start()


def _forward_to_endpoints(qr_code: str, endpoints: list):
    body = json.dumps({"qrCode": qr_code}).encode()
    for url in endpoints:
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=FORWARD_TIMEOUT):
                pass
        except Exception as e:
            _stats["forward_errors"] += 1
            print(f"[scan-hub] Forward failed → {url}: {e}", flush=True)


def _load_endpoints() -> list:
    """Read scan_endpoints.conf and return list of URLs, skipping blank/comment lines."""
    if not os.path.exists(ENDPOINTS_CONF):
        return []
    urls = []
    try:
        with open(ENDPOINTS_CONF) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
    except Exception:
        pass
    return urls


# ── Device reading ────────────────────────────────────────────────────────────

def _is_qr_scanner(device: InputDevice) -> bool:
    """
    QR/barcode scanners appear as HID keyboards.  They have a full key set but
    we exclude known non-scanner devices by name.
    """
    if _EXCLUDE_NAMES.search(device.name):
        return False
    caps = device.capabilities()
    if ecodes.EV_KEY not in caps:
        return False
    keys = caps[ecodes.EV_KEY]
    # Must have at least 20 keys including digit and letter keys
    return len(keys) >= 20


def _read_device(device: InputDevice):
    path       = device.path
    print(f"[scan-hub] Grabbing scanner: {device.name}  ({path})", flush=True)
    _stats["devices_seen"] += 1
    buffer     = []
    shift_held = False
    try:
        device.grab()
        for event in device.read_loop():
            if event.type != ecodes.EV_KEY:
                continue
            ke = categorize(event)
            if ke.keystate == ke.key_down:
                code = event.code
                if code in _SHIFT_KEYS:
                    shift_held = True
                elif code == _KEY_ENTER:
                    text = "".join(buffer).strip()
                    buffer.clear()
                    if text:
                        _enqueue_scan(text)
                elif code in _KEYMAP:
                    buffer.append(_KEYMAP[code][1 if shift_held else 0])
                else:
                    # Unknown key — clear buffer to avoid garbage accumulation
                    if len(buffer) > 100:
                        buffer.clear()
            elif ke.keystate == ke.key_up:
                if event.code in _SHIFT_KEYS:
                    shift_held = False
    except OSError as e:
        print(f"[scan-hub] Scanner disconnected ({path}): {e}", flush=True)
    finally:
        with _device_lock:
            _active_paths.discard(path)
        try:
            device.ungrab()
        except Exception:
            pass
        print(f"[scan-hub] Released: {path}", flush=True)


def _device_watcher():
    """Poll /dev/input every DEVICE_POLL_S seconds and spawn threads for new scanners."""
    print("[scan-hub] Device watcher started", flush=True)
    while True:
        try:
            for path in evdev.list_devices():
                with _device_lock:
                    if path in _active_paths:
                        continue
                try:
                    dev = InputDevice(path)
                    if not _is_qr_scanner(dev):
                        continue
                    with _device_lock:
                        _active_paths.add(path)
                    t = threading.Thread(target=_read_device, args=(dev,), daemon=True)
                    t.start()
                except Exception as e:
                    print(f"[scan-hub] Could not open {path}: {e}", flush=True)
        except Exception as e:
            print(f"[scan-hub] Device list error: {e}", flush=True)
        time.sleep(DEVICE_POLL_S)


# ── HTTP Hub ──────────────────────────────────────────────────────────────────

class ScanHubHandler(BaseHTTPRequestHandler):
    """Tiny HTTP server so any project can subscribe to scans."""

    def log_message(self, fmt, *args):
        pass  # silence default access log

    # ── GET ───────────────────────────────────────────────────────────────────

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/health":
            self._json({
                "status":           "ok",
                "hub_port":         HUB_PORT,
                "total_scans":      _stats["total_scans"],
                "devices_seen":     _stats["devices_seen"],
                "forward_errors":   _stats["forward_errors"],
                "active_scanners":  len(_active_paths),
                "sse_subscribers":  len(_sse_subscribers),
                "endpoints_file":   ENDPOINTS_CONF,
                "endpoints":        _load_endpoints(),
            })

        elif path == "/scan/pending":
            with _scan_lock:
                pending = next((s for s in _scan_queue if not s["processed"]), None)
            if pending:
                self._json({"id": pending["id"], "qrCode": pending["qrCode"]})
            else:
                self._json({"id": 0, "qrCode": ""})

        elif path == "/scan/stream":
            self._sse_stream()

        else:
            self._err(404, "not found")

    # ── POST ──────────────────────────────────────────────────────────────────

    def do_POST(self):
        path = urlparse(self.path).path

        # Accept scans posted directly (e.g. from a camera app or another device)
        if path == "/scan":
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length) or b"{}")
            qr     = (body.get("qrCode") or body.get("qr_code") or "").strip()
            if not qr:
                self._err(400, "qrCode required")
                return
            _enqueue_scan(qr)
            self._json({"ok": True, "queued": qr}, 201)

        elif path.startswith("/scan/ack/"):
            try:
                scan_id = int(path.split("/")[-1])
            except ValueError:
                self._err(400, "invalid id")
                return
            with _scan_lock:
                for s in _scan_queue:
                    if s["id"] == scan_id:
                        s["processed"] = True
                        break
            self._json({"ok": True, "acked": scan_id})

        else:
            self._err(404, "not found")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    def _sse_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")   # nginx: disable proxy buffer
        self.end_headers()

        q = _queue_mod.Queue(maxsize=50)
        with _sse_lock:
            _sse_subscribers.append(q)

        try:
            while True:
                try:
                    event = q.get(timeout=15)
                    line  = f"data: {event}\n\n".encode()
                    self.wfile.write(line)
                    self.wfile.flush()
                except _queue_mod.Empty:
                    # heartbeat — keeps nginx and TCP stacks from timing out
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with _sse_lock:
                try:
                    _sse_subscribers.remove(q)
                except ValueError:
                    pass


def _run_hub():
    server = HTTPServer(("127.0.0.1", HUB_PORT), ScanHubHandler)
    print(f"[scan-hub] HTTP hub listening on port {HUB_PORT}", flush=True)
    print(f"[scan-hub]   SSE stream : http://localhost:{HUB_PORT}/scan/stream", flush=True)
    print(f"[scan-hub]   Polling    : http://localhost:{HUB_PORT}/scan/pending", flush=True)
    print(f"[scan-hub]   Health     : http://localhost:{HUB_PORT}/health", flush=True)
    server.serve_forever()


# ── Startup ───────────────────────────────────────────────────────────────────

def _write_default_endpoints_conf():
    """Create scan_endpoints.conf with sensible defaults if it doesn't exist."""
    if os.path.exists(ENDPOINTS_CONF):
        return
    try:
        os.makedirs(CONF_DIR, exist_ok=True)
        with open(ENDPOINTS_CONF, "w") as f:
            f.write(
                "# HanryxVault Scan Endpoints\n"
                "# One URL per line — every QR scan is forwarded to all of them.\n"
                "# Blank lines and lines starting with # are ignored.\n"
                "#\n"
                "# POS server (always included):\n"
                "http://localhost:8080/scan\n"
                "#\n"
                "# Add your other apps here, e.g.:\n"
                "#   http://localhost:8081/scan   ← Pokémon lookup app\n"
                "#   http://localhost:8082/scan   ← another project\n"
            )
        print(f"[scan-hub] Created default {ENDPOINTS_CONF}", flush=True)
    except Exception as e:
        print(f"[scan-hub] Could not write {ENDPOINTS_CONF}: {e}", flush=True)


def main():
    print("=" * 60, flush=True)
    print(" HanryxVault QR Scan Hub", flush=True)
    print(f" Scan hub port : {HUB_PORT}", flush=True)
    print(f" Endpoints conf: {ENDPOINTS_CONF}", flush=True)
    print("=" * 60, flush=True)

    _write_default_endpoints_conf()

    endpoints = _load_endpoints()
    if endpoints:
        print(f"[scan-hub] Forwarding scans to {len(endpoints)} endpoint(s):", flush=True)
        for ep in endpoints:
            print(f"  → {ep}", flush=True)
    else:
        print("[scan-hub] No forward endpoints configured — hub-only mode", flush=True)

    # Start HTTP hub in a daemon thread
    hub_thread = threading.Thread(target=_run_hub, daemon=True)
    hub_thread.start()

    # Start device watcher (blocks forever in the main thread)
    _device_watcher()


if __name__ == "__main__":
    main()
