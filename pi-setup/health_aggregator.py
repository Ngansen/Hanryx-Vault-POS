"""
Cross-device health aggregator for the 3-Pi/tablet topology.

Pings:
  - Main Pi   (this process — read /health locally)
  - Satellite Pi (192.168.86.22, kiosk + nginx)
  - Tablets (last APK check-in timestamp from Redis)

Returns one consolidated JSON blob suitable for both the admin UI
and an external monitoring system.
"""
from __future__ import annotations

import json
import logging
import os
import time

import requests

log = logging.getLogger("health_aggregator")

SATELLITE_URL = os.environ.get("SATELLITE_HEALTH_URL",
                               "http://192.168.86.22/health")
TABLET_KEY    = "hv:tablet:lastseen"   # Redis hash {tablet_id: ts_ms}


def _redis():
    try:
        import server  # type: ignore
        return server._redis()
    except Exception:
        return None


def _ping_satellite() -> dict:
    t0 = time.time()
    try:
        r = requests.get(SATELLITE_URL, timeout=4,
                         headers={"User-Agent": "HanryxVault-Aggregator/1.0"})
        latency_ms = int((time.time() - t0) * 1000)
        body = {}
        try: body = r.json()
        except Exception: body = {"raw": r.text[:300]}
        return {"ok": r.status_code == 200, "status_code": r.status_code,
                "latency_ms": latency_ms, "body": body}
    except requests.RequestException as exc:
        return {"ok": False, "error": str(exc),
                "latency_ms": int((time.time() - t0) * 1000)}


def _tablet_status(stale_after_s: int = 300) -> dict:
    """Read tablet check-in timestamps from Redis."""
    r = _redis()
    out: dict = {"online": [], "stale": [], "raw": {}}
    if r is None:
        return out
    try:
        h = r.hgetall(TABLET_KEY) or {}
    except Exception as exc:
        log.debug("[health-agg] tablet hgetall failed: %s", exc)
        return out
    now = int(time.time() * 1000)
    for tid_b, ts_b in h.items():
        try:
            tid = tid_b.decode() if isinstance(tid_b, (bytes, bytearray)) else tid_b
            ts = int(ts_b)
            age_s = (now - ts) / 1000
            entry = {"tablet_id": tid, "last_seen_ms": ts,
                     "age_s": round(age_s, 1)}
            if age_s <= stale_after_s:
                out["online"].append(entry)
            else:
                out["stale"].append(entry)
            out["raw"][tid] = ts
        except Exception:
            continue
    return out


def aggregate(local_health: dict | None) -> dict:
    """Combine local /health output + satellite ping + tablet check-ins."""
    sat = _ping_satellite()
    tabs = _tablet_status()
    return {
        "ts_ms": int(time.time() * 1000),
        "main": local_health or {"ok": False, "error": "no local payload passed"},
        "satellite": sat,
        "tablets": {
            "online_count": len(tabs["online"]),
            "stale_count":  len(tabs["stale"]),
            "online":       tabs["online"],
            "stale":        tabs["stale"],
        },
        "overall_ok": bool((local_health or {}).get("status") == "ok"
                           and sat.get("ok")),
    }
