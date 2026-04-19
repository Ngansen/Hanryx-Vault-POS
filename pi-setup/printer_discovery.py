"""
Bluetooth ESC/POS printer auto-discovery & rfcomm rebind.

If the existing printer device (/dev/rfcomm0 or similar) goes silent
because the printer was power-cycled, this module:
  1. Scans for known printer MACs via `bluetoothctl`
  2. Re-binds the rfcomm device with `rfcomm bind`
  3. Returns a structured report

Designed to be called periodically from a background thread or on
demand from /admin/printer/discover.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time

log = logging.getLogger("printer_discovery")


PRINTER_MAC_PREFIXES = (
    "00:01:90",  # MUNBYN / Gprinter common OEM
    "00:11:67",  # Bixolon
    "00:13:7B",  # Star Micronics
    "60:6E:41",  # Epson TM-m series
    "DC:0D:30",  # POS-58 / cheap thermal clones
    "00:05:1B",  # Generic ESC/POS
)


def _run(cmd: list[str], timeout: int = 8) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, check=False)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except Exception as exc:
        return 1, str(exc)


def is_printer_mac(mac: str) -> bool:
    return any(mac.upper().startswith(p) for p in PRINTER_MAC_PREFIXES)


def discover(*, scan_seconds: int = 6, rebind: bool = True) -> dict:
    """
    Scan BT, return found printers, optionally re-bind rfcomm0 to the
    first match.
    """
    out: dict = {"ts_ms": int(time.time() * 1000), "scanned": False,
                 "found": [], "bound": None, "errors": []}

    if not shutil.which("bluetoothctl"):
        out["errors"].append("bluetoothctl not installed")
        return out

    # Trigger a scan
    rc, log1 = _run(["bluetoothctl", "--timeout", str(scan_seconds), "scan", "on"])
    out["scanned"] = (rc == 0)
    if rc != 0:
        out["errors"].append(f"scan failed: {log1.strip()[:200]}")

    # Read paired + discovered devices
    rc, devices = _run(["bluetoothctl", "devices"])
    macs = []
    for line in devices.splitlines():
        # "Device AA:BB:CC:DD:EE:FF Printer-name"
        m = re.match(r"Device\s+([0-9A-Fa-f:]{17})\s+(.*)", line.strip())
        if not m:
            continue
        mac, name = m.group(1).upper(), m.group(2).strip()
        looks_like_printer = is_printer_mac(mac) or any(
            kw in name.lower()
            for kw in ("printer", "pos", "thermal", "escpos", "rongta",
                       "munbyn", "bixolon", "star tsp"))
        if looks_like_printer:
            out["found"].append({"mac": mac, "name": name})
            macs.append(mac)

    if not macs:
        return out

    if not rebind:
        return out

    # Re-bind rfcomm0 to the first printer found.  We release first to
    # clear any stale binding, then bind to channel 1 (ESC/POS standard).
    if not shutil.which("rfcomm"):
        out["errors"].append("rfcomm tool missing — cannot re-bind")
        return out

    target = macs[0]
    _run(["rfcomm", "release", "0"], timeout=3)  # ignore failure
    rc, log_b = _run(["rfcomm", "bind", "0", target, "1"], timeout=4)
    if rc == 0 and os.path.exists("/dev/rfcomm0"):
        out["bound"] = {"device": "/dev/rfcomm0", "mac": target}
    else:
        out["errors"].append(f"rfcomm bind failed: {log_b.strip()[:200]}")
    return out
