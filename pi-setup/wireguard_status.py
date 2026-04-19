"""
Parse `wg show` output into structured JSON.

Used by /admin/wireguard/status.  Requires the `wg` binary on the host
and that the container has either NET_ADMIN cap or runs the parser
on the Pi side via SSH.  Safe to call when WireGuard isn't installed —
returns a clear error instead of crashing.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time

log = logging.getLogger("wg_status")


def _run(cmd: list[str], timeout: int = 4) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, check=False)
        return p.returncode, p.stdout or "", p.stderr or ""
    except FileNotFoundError:
        return 127, "", f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as exc:
        return 1, "", str(exc)


def status() -> dict:
    if not shutil.which("wg"):
        return {"installed": False,
                "error": "`wg` binary not found in container PATH"}

    rc, out, err = _run(["wg", "show"])
    if rc != 0:
        return {"installed": True, "ok": False, "error": err.strip()
                or f"wg show exited {rc}"}

    interfaces: dict = {}
    current_iface = None
    current_peer  = None

    for line in out.splitlines():
        if line.startswith("interface:"):
            current_iface = line.split(":", 1)[1].strip()
            interfaces[current_iface] = {"name": current_iface, "peers": []}
            current_peer = None
            continue
        if line.startswith("peer:"):
            current_peer = {"public_key": line.split(":", 1)[1].strip()}
            interfaces[current_iface]["peers"].append(current_peer)
            continue
        if not current_iface:
            continue

        if ":" in line:
            k, _, v = line.strip().partition(":")
            k = k.strip(); v = v.strip()
            target = current_peer if current_peer else interfaces[current_iface]
            if k == "latest handshake":
                target["latest_handshake"] = v
                target["handshake_age_s"]  = _parse_age(v)
            elif k == "transfer":
                target["transfer"] = v
                rx, tx = _parse_transfer(v)
                target["rx_bytes"] = rx
                target["tx_bytes"] = tx
            elif k in ("endpoint", "allowed ips", "listening port",
                       "persistent keepalive", "preshared key"):
                target[k.replace(" ", "_")] = v

    healthy_peers = sum(1 for ifc in interfaces.values() for p in ifc["peers"]
                        if p.get("handshake_age_s") is not None
                        and p["handshake_age_s"] < 180)
    total_peers = sum(len(ifc["peers"]) for ifc in interfaces.values())

    return {
        "installed": True,
        "ok": True,
        "ts_ms": int(time.time() * 1000),
        "interfaces": list(interfaces.values()),
        "summary": {
            "interface_count": len(interfaces),
            "peer_count": total_peers,
            "healthy_peers": healthy_peers,
        },
    }


def _parse_age(human: str) -> int | None:
    """Convert 'N seconds/minutes/hours/days ago' → integer seconds."""
    m = re.match(r"(\d+)\s+(second|minute|hour|day)s?\s+ago", human, re.I)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    return n * {"second": 1, "minute": 60, "hour": 3600, "day": 86400}[unit]


def _parse_transfer(human: str) -> tuple[int, int]:
    """Parse 'X.YY MiB received, Z.AA MiB sent' → (rx_bytes, tx_bytes)."""
    units = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
    rx = tx = 0
    for amt, unit, kind in re.findall(
            r"([\d.]+)\s+(B|KiB|MiB|GiB|TiB)\s+(received|sent)", human):
        b = int(float(amt) * units.get(unit, 1))
        if kind == "received": rx = b
        else: tx = b
    return rx, tx
