#!/usr/bin/env python3
"""
HanryxVault QR Scan Hub
========================
Reads from USB and Bluetooth HID QR scanners (they appear as keyboard-style
/dev/input/event* devices once paired) and makes every scan available to ANY
app running on — or connected to — the Pi network.

HOW IT WORKS
  The daemon runs a tiny HTTP hub on port 8765, bound to ALL network interfaces
  (0.0.0.0) so your Expo app, tablet, or any device on the same WiFi can reach it.

  HTTP API:
    GET  http://<PI_IP>:8765/scan/stream   ← SSE stream (instant push — best for Expo)
    GET  http://<PI_IP>:8765/scan/pending  ← one-scan polling (lightweight)
    POST http://<PI_IP>:8765/scan          ← submit a scan (Expo camera, web app, etc.)
    POST http://<PI_IP>:8765/scan/ack/<id> ← mark scan as handled
    GET  http://<PI_IP>:8765/health        ← hub status + connected devices
    GET  http://<PI_IP>:8765/devices       ← list currently active scanners

SCANNER SOURCES
  • USB QR scanners:    plug in — detected within 3 seconds automatically
  • Bluetooth QR:       pair once with bluetoothctl (see below), then auto-reconnects
  • Expo app (camera):  POST { "qrCode": "..." } to http://<PI_IP>:8765/scan
  • Web / other apps:   same POST endpoint, CORS is fully open
  • Multiple scanners:  all run simultaneously in separate threads
  • Dedup filter:       ignores identical scan within 1.5 s (scanner double-fire)
  • Source tagging:     every scan record includes "source": "usb" | "bluetooth" | "network"

EXPO APP INTEGRATION (receive scans in your app)
  import EventSource from 'react-native-sse';          // or use polling

  const es = new EventSource('http://192.168.1.50:8765/scan/stream');
  es.addEventListener('message', (e) => {
    const { id, qrCode, source } = JSON.parse(e.data);
    // qrCode is the scanned value — route it to your lookup/POS flow
    fetch(`http://192.168.1.50:8765/scan/ack/${id}`, { method: 'POST' });
  });

  Or, if using the Expo camera to scan, POST results back to the hub so
  USB/BT scanner users and camera users share the same event stream:
    fetch('http://192.168.1.50:8765/scan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ qrCode: data.data, source: 'expo' }),
    });

BLUETOOTH PAIRING (one-time setup per scanner)
  sudo bluetoothctl
  power on → agent on → scan on → pair <MAC> → trust <MAC> → connect <MAC> → quit
  The daemon auto-reconnects on every boot once the scanner is trusted.

USB SCANNERS
  Just plug in — no config needed.  The hub auto-detects and grabs the device.
  Multiple USB scanners can be plugged in simultaneously.

ADDING A NEW APP (webhook push)
  Add one URL per line to /opt/hanryxvault/scan_endpoints.conf:
    http://localhost:8081/scan
  Restart the hub after editing:  sudo systemctl restart hanryxvault-scan-hub
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
HUB_HOST         = os.environ.get("SCAN_HUB_HOST", "0.0.0.0")   # 0.0.0.0 = all interfaces (Expo accessible)
CONF_DIR         = os.environ.get("HANRYX_DIR", "/opt/hanryxvault")
ENDPOINTS_CONF   = os.path.join(CONF_DIR, "scan_endpoints.conf")
MIN_SCAN_LEN     = 3        # ignore codes shorter than this (noise)
MAX_SCAN_LEN     = 512      # ignore absurdly long events (key logging protection)
DEDUP_WINDOW_S   = 1.5      # suppress identical scan within this many seconds
DEVICE_POLL_S    = 3        # how often to check for new scanners
FORWARD_TIMEOUT  = 3        # seconds to wait for each webhook endpoint

# ── Shared state ──────────────────────────────────────────────────────────────
_scan_lock        = threading.Lock()
_scan_queue       = []        # list of {"id": int, "qrCode": str, "source": str, "processed": bool}
_scan_id_counter  = 0

_sse_lock         = threading.Lock()
_sse_subscribers  = []        # list of queue.Queue — one per SSE client

_device_lock      = threading.Lock()
_active_paths     = set()     # /dev/input/eventN currently grabbed
_active_devices   = {}        # path → {"name": str, "source": "usb"|"bluetooth"}

_last_scan_time   = {}        # qr_code → timestamp  (dedup)
_dedup_lock       = threading.Lock()

_stats = {
    "total_scans":    0,
    "usb_scans":      0,
    "bluetooth_scans": 0,
    "network_scans":  0,
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

# Patterns that match non-scanner input devices — excluded from auto-grab
_EXCLUDE_NAMES = re.compile(
    r'(mouse|touchpad|touchscreen|trackpad|power|button|video|camera|audio|'
    r'accel|gyro|lid|switch)',
    re.IGNORECASE
)


# ── Bluetooth vs USB detection ────────────────────────────────────────────────

def _detect_source(device: InputDevice) -> str:
    """
    Return 'bluetooth' if this device is a paired BT HID scanner, else 'usb'.
    Bluetooth HID devices have a phys like 'AA:BB:CC:DD:EE:FF/hci0/...'
    USB HID devices have a phys like 'usb-0000:01:00.0-1.x/input0'
    """
    phys = (getattr(device, 'phys', None) or "").lower()
    if re.match(r'^([0-9a-f]{2}:){5}[0-9a-f]{2}', phys):
        return "bluetooth"
    return "usb"


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


def _enqueue_scan(qr_code: str, source: str = "network"):
    """
    Accept a scan from any source (USB HID, Bluetooth HID, Expo app, network POST).
    Adds it to the local queue, pushes to SSE subscribers, forwards to webhooks.

    source values: "usb" | "bluetooth" | "expo" | "network"
    """
    global _scan_id_counter

    if not MIN_SCAN_LEN <= len(qr_code) <= MAX_SCAN_LEN:
        return
    if _is_duplicate(qr_code):
        print(f"[scan-hub] Dedup suppressed ({source}): {qr_code[:40]}", flush=True)
        return

    with _scan_lock:
        _scan_id_counter += 1
        entry = {
            "id":        _scan_id_counter,
            "qrCode":    qr_code,
            "source":    source,
            "processed": False,
            "ts":        int(time.time() * 1000),
        }
        _scan_queue.append(entry)
        # keep queue bounded — discard processed items beyond 200
        if len(_scan_queue) > 200:
            _scan_queue[:] = [s for s in _scan_queue if not s["processed"]][-200:]

    _stats["total_scans"] += 1
    if source == "usb":
        _stats["usb_scans"] += 1
    elif source == "bluetooth":
        _stats["bluetooth_scans"] += 1
    else:
        _stats["network_scans"] += 1

    print(f"[scan-hub] ✓ [{source}] {qr_code[:60]}", flush=True)

    # Push instantly to all SSE subscribers (Expo app, web dashboards, etc.)
    event = json.dumps({
        "id":     _scan_id_counter,
        "qrCode": qr_code,
        "source": source,
        "ts":     entry["ts"],
    })
    with _sse_lock:
        dead = []
        for q in _sse_subscribers:
            try:
                q.put_nowait(event)
            except _queue_mod.Full:
                dead.append(q)
        for q in dead:
            _sse_subscribers.remove(q)

    # Forward to webhook endpoints in the background (doesn't block the scanner)
    endpoints = _load_endpoints()
    if endpoints:
        threading.Thread(
            target=_forward_to_endpoints,
            args=(qr_code, source, endpoints),
            daemon=True,
        ).start()


def _forward_to_endpoints(qr_code: str, source: str, endpoints: list):
    body = json.dumps({"qrCode": qr_code, "source": source}).encode()
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
    QR/barcode scanners appear as HID keyboards.  They present a full key set but
    we exclude known non-scanner devices by name.

    Note: we intentionally do NOT exclude devices named "keyboard" generically
    because many BT barcode scanners self-identify as "Bluetooth Keyboard".
    """
    if _EXCLUDE_NAMES.search(device.name):
        return False
    caps = device.capabilities()
    if ecodes.EV_KEY not in caps:
        return False
    keys = caps[ecodes.EV_KEY]
    return len(keys) >= 20


def _read_device(device: InputDevice):
    path       = device.path
    source     = _detect_source(device)
    print(f"[scan-hub] Grabbing {source} scanner: {device.name}  ({path})", flush=True)
    _stats["devices_seen"] += 1
    buffer     = []
    shift_held = False

    with _device_lock:
        _active_devices[path] = {"name": device.name, "source": source, "phys": device.phys or ""}

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
                        _enqueue_scan(text, source=source)
                elif code in _KEYMAP:
                    buffer.append(_KEYMAP[code][1 if shift_held else 0])
                else:
                    # Unknown key — clear buffer if it's grown too large (noise protection)
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
            _active_devices.pop(path, None)
        try:
            device.ungrab()
        except Exception:
            pass
        print(f"[scan-hub] Released {source} scanner: {path}", flush=True)


def _device_watcher():
    """Poll /dev/input every DEVICE_POLL_S seconds and spawn threads for new scanners."""
    print("[scan-hub] Device watcher started (USB + Bluetooth HID)", flush=True)
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
    """
    Tiny HTTP server accessible from any device on the network.
    Bound to 0.0.0.0 so your Expo app, tablet, or web dashboard can connect.
    Full CORS support — no browser / Expo restrictions.
    """

    def log_message(self, fmt, *args):
        pass  # silence default access log noise

    # ── OPTIONS (CORS preflight — Expo / browser fetch needs this) ─────────────

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ── GET ───────────────────────────────────────────────────────────────────

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/health":
            with _device_lock:
                devices = list(_active_devices.values())
            self._json({
                "status":            "ok",
                "hub_host":          HUB_HOST,
                "hub_port":          HUB_PORT,
                "total_scans":       _stats["total_scans"],
                "usb_scans":         _stats["usb_scans"],
                "bluetooth_scans":   _stats["bluetooth_scans"],
                "network_scans":     _stats["network_scans"],
                "devices_seen":      _stats["devices_seen"],
                "forward_errors":    _stats["forward_errors"],
                "active_scanners":   len(_active_paths),
                "sse_subscribers":   len(_sse_subscribers),
                "endpoints_file":    ENDPOINTS_CONF,
                "endpoints":         _load_endpoints(),
                "devices":           devices,
            })

        elif path == "/devices":
            with _device_lock:
                devices = list(_active_devices.values())
            self._json({"devices": devices, "count": len(devices)})

        elif path == "/scan/pending":
            with _scan_lock:
                pending = next((s for s in _scan_queue if not s["processed"]), None)
            if pending:
                self._json({
                    "id":     pending["id"],
                    "qrCode": pending["qrCode"],
                    "source": pending.get("source", "unknown"),
                    "ts":     pending.get("ts", 0),
                })
            else:
                self._json({"id": 0, "qrCode": "", "source": "", "ts": 0})

        elif path == "/scan/stream":
            self._sse_stream()

        else:
            self._err(404, "not found")

    # ── POST ──────────────────────────────────────────────────────────────────

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/scan":
            # Accept scans from Expo camera, web app, another device on LAN, etc.
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, Exception):
                self._err(400, "invalid JSON")
                return
            qr     = (body.get("qrCode") or body.get("qr_code") or "").strip()
            source = (body.get("source") or "network").strip().lower()
            # Only allow safe source labels from external callers
            if source not in ("expo", "network", "web", "apk", "tablet"):
                source = "network"
            if not qr:
                self._err(400, "qrCode required")
                return
            _enqueue_scan(qr, source=source)
            self._json({"ok": True, "queued": qr, "source": source}, 201)

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

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    def _sse_stream(self):
        """
        Server-Sent Events stream.  Expo app subscribes here to receive scans
        from USB, Bluetooth, and other network sources in real time.

        In React Native (Expo):
          import EventSource from 'react-native-sse';
          const es = new EventSource('http://<PI_IP>:8765/scan/stream');
          es.addEventListener('message', e => console.log(JSON.parse(e.data)));
        """
        self.send_response(200)
        self.send_header("Content-Type",  "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")   # nginx: disable proxy buffer
        self._cors_headers()
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
                    # Heartbeat keeps TCP alive and stops nginx / phone OS from killing the stream
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
    server = HTTPServer((HUB_HOST, HUB_PORT), ScanHubHandler)
    print(f"[scan-hub] HTTP hub listening on {HUB_HOST}:{HUB_PORT}", flush=True)
    print(f"[scan-hub]   SSE stream (Expo): http://<PI_IP>:{HUB_PORT}/scan/stream", flush=True)
    print(f"[scan-hub]   Post a scan:        http://<PI_IP>:{HUB_PORT}/scan", flush=True)
    print(f"[scan-hub]   Poll latest:        http://<PI_IP>:{HUB_PORT}/scan/pending", flush=True)
    print(f"[scan-hub]   Health / devices:   http://<PI_IP>:{HUB_PORT}/health", flush=True)
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
                "# One URL per line — every scan (USB, Bluetooth, Expo camera) is forwarded to ALL.\n"
                "# Blank lines and lines starting with # are ignored.\n"
                "# Restart the service after editing:\n"
                "#   sudo systemctl restart hanryxvault-scan-hub\n"
                "#\n"
                "# POS server (always include this):\n"
                "http://localhost:8080/scan\n"
                "#\n"
                "# To receive scans in your Expo app:\n"
                "#   Subscribe to the SSE stream instead — much more responsive:\n"
                "#   http://<PI_IP>:8765/scan/stream\n"
                "#\n"
                "# Other webhook endpoints:\n"
                "#   http://localhost:8081/scan   ← Pokémon lookup app\n"
                "#   http://localhost:8082/scan   ← another project\n"
            )
        print(f"[scan-hub] Created default {ENDPOINTS_CONF}", flush=True)
    except Exception as e:
        print(f"[scan-hub] Could not write {ENDPOINTS_CONF}: {e}", flush=True)


def main():
    print("=" * 60, flush=True)
    print(" HanryxVault QR Scan Hub", flush=True)
    print(f" Hub address   : {HUB_HOST}:{HUB_PORT}  (all network interfaces)", flush=True)
    print(f" Endpoints conf: {ENDPOINTS_CONF}", flush=True)
    print(" Sources       : USB HID | Bluetooth HID | Expo camera | Network POST", flush=True)
    print("=" * 60, flush=True)

    _write_default_endpoints_conf()

    endpoints = _load_endpoints()
    if endpoints:
        print(f"[scan-hub] Forwarding scans to {len(endpoints)} webhook endpoint(s):", flush=True)
        for ep in endpoints:
            print(f"  → {ep}", flush=True)
    else:
        print("[scan-hub] No webhook endpoints configured — hub-only / SSE mode", flush=True)

    # Start HTTP hub in a daemon thread
    hub_thread = threading.Thread(target=_run_hub, daemon=True)
    hub_thread.start()

    # Start device watcher — blocks forever, spawning threads for each scanner found
    _device_watcher()


if __name__ == "__main__":
    main()
