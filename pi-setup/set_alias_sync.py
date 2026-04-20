"""
Auto-discover set aliases from pokemontcg.io.

The hand-curated table in `set_aliases.py` ships fine for today's sets,
but every new English release adds a row that someone has to type in.
This module pulls the canonical English set list directly from
pokemontcg.io's free `/v2/sets` endpoint and merges it with the
bundled Japanese/Korean code clusters so the operator gets every new
set automatically the moment pokemontcg.io publishes it.

What gets discovered
--------------------
For each English set the API returns we extract:
  - `name`         e.g. "Twilight Masquerade"
  - `id`           e.g. "sv6"          (matches Japanese parent code)
  - `ptcgoCode`    e.g. "TWM"          (TCGO 3-letter code)
  - `series`       e.g. "Scarlet & Violet"

A cluster is then built as:
  tokens = { name, id, ptcgoCode, id+"a", id+"pt5" }   ← JP siblings

The id-suffix expansion (`+a`, `+pt5`) is the well-known pattern by
which JP splits one English set into 2-3 smaller ones (sv6 → sv6 +
sv6a; sv8 → sv8 + sv8pt5). Adding the suffixed forms means a search
for the English name automatically matches the JP/KR rows our
import_jpn_cards / import_kr_cards pipelines stored under those
codes.

The result is written to:

    $HV/data/set_aliases_synced.json

…which `set_aliases.py` already loads (next to the operator-editable
`set_aliases.json`), so no further wiring is needed beyond running
`sync_now()`. A 24h cooldown prevents needless API hits.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import urllib.request
import urllib.error

log = logging.getLogger("set_alias_sync")

_API_URL = "https://api.pokemontcg.io/v2/sets?pageSize=250"
_COOLDOWN_SECONDS = 24 * 3600


def _data_dir() -> str:
    base = os.environ.get("HV") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    d = os.path.join(base, "data")
    os.makedirs(d, exist_ok=True)
    return d


def _synced_path() -> str:
    return os.path.join(_data_dir(), "set_aliases_synced.json")


def _state_path() -> str:
    return os.path.join(_data_dir(), ".set_alias_sync_state.json")


def _load_state() -> dict:
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(_state_path(), "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as exc:
        log.warning("[set_alias_sync] could not persist state: %s", exc)


def _fetch_sets() -> list[dict]:
    """Pull all English sets from pokemontcg.io. Auths with API key if set."""
    headers = {"User-Agent": "HanryxVault/1.0"}
    api_key = os.environ.get("POKEMONTCG_API_KEY", "").strip()
    if api_key:
        headers["X-Api-Key"] = api_key
    req = urllib.request.Request(_API_URL, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8")
    payload = json.loads(body)
    return list(payload.get("data") or [])


def _build_clusters(sets: list[dict]) -> list[dict]:
    """
    Turn raw /sets rows into our cluster format. Each English set
    becomes one cluster that also covers the Japanese sibling codes
    that follow the standard suffix pattern.
    """
    clusters: list[dict] = []
    for s in sets:
        name = (s.get("name") or "").strip()
        sid = (s.get("id") or "").strip()
        pcode = (s.get("ptcgoCode") or "").strip()
        if not name and not sid:
            continue
        tokens: list[str] = []
        if name:
            tokens.append(name)
        if sid:
            tokens.extend([sid, sid + "a", sid + "pt5"])
        if pcode:
            tokens.extend([pcode, pcode.lower()])
        # Dedupe preserving insertion order.
        tokens = list(dict.fromkeys(t for t in tokens if t))
        clusters.append({
            "name":   name or sid,
            "tokens": tokens,
            "_meta":  {
                "source":      "pokemontcg.io",
                "id":          sid,
                "ptcgoCode":   pcode,
                "series":      s.get("series") or "",
                "releaseDate": s.get("releaseDate") or "",
            },
        })
    return clusters


def _write_synced(clusters: list[dict]) -> str:
    path = _synced_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(clusters, f, indent=2, ensure_ascii=False)
    return path


def sync_now(force: bool = False) -> dict:
    """
    Pull the latest set list, derive clusters, persist them.

    Returns a small status dict the admin route surfaces to the operator:

        {"ok": True, "fetched": 168, "written": 168, "path": "...",
         "skipped": False, "next_eligible_in": 86400}
    """
    state = _load_state()
    last = float(state.get("last_sync_ts") or 0.0)
    now = time.time()
    if not force and (now - last) < _COOLDOWN_SECONDS:
        return {
            "ok": True,
            "skipped": True,
            "reason": "cooldown",
            "last_sync_ts": last,
            "next_eligible_in": int(_COOLDOWN_SECONDS - (now - last)),
        }

    try:
        raw = _fetch_sets()
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"HTTP {exc.code}: {exc.reason}"}
    except Exception as exc:
        return {"ok": False, "error": f"fetch failed: {exc}"}

    clusters = _build_clusters(raw)
    path = _write_synced(clusters)

    state.update({
        "last_sync_ts":   now,
        "last_count":     len(clusters),
        "last_path":      path,
    })
    _save_state(state)

    # Auto-backfill: any clusters that didn't exist in the previous synced
    # snapshot get a targeted import queued so we don't have to wait for a
    # full re-import to surface a brand-new set.
    try:
        import cluster_backfill
        bf = cluster_backfill.schedule_for_new(clusters)
        if bf.get("enqueued"):
            log.info("[set_alias_sync] queued %s backfill jobs (new clusters: %s)",
                     bf.get("enqueued"), bf.get("new_clusters"))
    except Exception as exc:
        log.info("[set_alias_sync] cluster_backfill skipped: %s", exc)

    # Invalidate the in-memory alias cache so the new entries are live
    # on the very next /admin/sets/cards request — no restart needed.
    try:
        import set_aliases as _sa
        with _sa._LOCK:
            _sa._CACHE = None
    except Exception:
        pass
    # Wipe the result cache too, otherwise old searches keep returning
    # the pre-sync union.
    try:
        import sets_browser as _sb
        _sb.invalidate_cache()
    except Exception:
        pass

    return {
        "ok":          True,
        "skipped":     False,
        "fetched":     len(raw),
        "written":     len(clusters),
        "path":        path,
        "last_sync_ts": now,
    }


def status() -> dict:
    """Quick status for the admin UI."""
    state = _load_state()
    last = float(state.get("last_sync_ts") or 0.0)
    age = time.time() - last if last else None
    return {
        "last_sync_ts":   last or None,
        "last_count":     state.get("last_count"),
        "synced_path":    _synced_path(),
        "synced_exists":  os.path.exists(_synced_path()),
        "age_seconds":    int(age) if age is not None else None,
        "next_eligible_in": (
            max(0, int(_COOLDOWN_SECONDS - age)) if age is not None else 0
        ),
    }


def maybe_sync_in_background() -> None:
    """Fire-and-forget; safe to call at server boot."""
    import threading

    def _runner():
        try:
            res = sync_now(force=False)
            if res.get("ok") and not res.get("skipped"):
                log.info("[set_alias_sync] synced %s clusters", res.get("written"))
        except Exception as exc:
            log.warning("[set_alias_sync] background sync failed: %s", exc)

    threading.Thread(target=_runner, daemon=True, name="set-alias-sync").start()
