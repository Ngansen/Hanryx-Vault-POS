#!/usr/bin/env python3
"""
HanryxVault Barcode Daemon
Reads from USB and Bluetooth HID barcode scanners (they both appear as
/dev/input/event* keyboard devices once paired) and posts every scanned
code to the local POS server's /scan endpoint.

Your Expo app's camera scanner and this daemon both feed the same queue.
The tablet picks up whichever fires first via GET /scan/pending.

Bluetooth setup:
  sudo bluetoothctl
  power on
  scan on
  pair <MAC>
  trust <MAC>
  connect <MAC>
  quit
  Then restart this daemon — it will auto-grab the device.

USB: plug in the scanner — it appears immediately.

Supports multiple scanners simultaneously (one thread per device).
"""

import sys
import time
import threading
import urllib.request
import json
import re

try:
    import evdev
    from evdev import InputDevice, ecodes, categorize
except ImportError:
    print("[barcode] ERROR: evdev not installed — run: pip install evdev")
    sys.exit(1)

LOCAL_SERVER  = "http://127.0.0.1:8080"
SCAN_ENDPOINT = f"{LOCAL_SERVER}/scan"

# ── Key code → character (standard US layout) ────────────────────────────────
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
_KEY_ENTER      = 28
_KEY_LSHIFT     = 42
_KEY_RSHIFT     = 54
_SHIFT_KEYS     = {_KEY_LSHIFT, _KEY_RSHIFT}

# Names that are definitely NOT barcode scanners
_EXCLUDE_NAMES  = re.compile(
    r'(mouse|touchpad|touchscreen|trackpad|power|button|video|camera|audio)',
    re.IGNORECASE
)

_active_paths   = set()       # /dev/input/eventN currently grabbed
_lock           = threading.Lock()


def _post_scan(qr_code: str):
    body = json.dumps({"qrCode": qr_code}).encode()
    req  = urllib.request.Request(
        SCAN_ENDPOINT, data=body,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            result = json.loads(resp.read())
            print(f"[barcode] ✓ {qr_code}  →  {result}", flush=True)
    except Exception as e:
        print(f"[barcode] ✗ Failed to post '{qr_code}': {e}", flush=True)


def _is_scanner(device: InputDevice) -> bool:
    """Heuristic: has EV_KEY with lots of keys and is not a known non-scanner."""
    if _EXCLUDE_NAMES.search(device.name):
        return False
    caps = device.capabilities()
    if ecodes.EV_KEY not in caps:
        return False
    # Barcode scanners expose a full keyboard key set
    return len(caps[ecodes.EV_KEY]) >= 20


def _read_device(device: InputDevice):
    """Grab a device and translate key events into scan codes."""
    path = device.path
    print(f"[barcode] Grabbing: {device.name}  ({path})", flush=True)
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
                        _post_scan(text)
                elif code in _KEYMAP:
                    buffer.append(_KEYMAP[code][1 if shift_held else 0])
            elif ke.keystate == ke.key_up:
                if event.code in _SHIFT_KEYS:
                    shift_held = False
    except OSError as e:
        print(f"[barcode] Device lost ({path}): {e}", flush=True)
    finally:
        with _lock:
            _active_paths.discard(path)
        try:
            device.ungrab()
        except Exception:
            pass


def _spawn_if_new(path: str):
    with _lock:
        if path in _active_paths:
            return
    try:
        dev = InputDevice(path)
        if not _is_scanner(dev):
            return
        with _lock:
            _active_paths.add(path)
        t = threading.Thread(target=_read_device, args=(dev,), daemon=True)
        t.start()
    except Exception as e:
        print(f"[barcode] Could not open {path}: {e}", flush=True)


def main():
    print("[barcode] HanryxVault Barcode Daemon — watching for USB/BT scanners",
          flush=True)
    print(f"[barcode] Posting scans to {SCAN_ENDPOINT}", flush=True)

    while True:
        try:
            for path in evdev.list_devices():
                _spawn_if_new(path)
        except Exception as e:
            print(f"[barcode] Device list error: {e}", flush=True)
        time.sleep(3)   # poll every 3 s — picks up reconnecting BT scanners


if __name__ == "__main__":
    main()
