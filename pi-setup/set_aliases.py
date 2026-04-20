"""
Set-alias expansion.

Pokémon TCG sets ship under different names and codes in every region:

    EN "Twilight Masquerade"   ←→   JP sv6  + JP sv6a   ←→   KR sv6 + sv6a

The Sets Browser needs to treat all of those tokens as one logical set
so a single search returns Korean / Chinese / Japanese / English rows
side-by-side. This module owns that mapping.

A curated table covers the modern Scarlet & Violet block plus key
Sword & Shield sets. The map is intentionally token-based (not row-
based): each entry is a *cluster* of search strings that all refer to
the same logical release. Expanding any one token returns the whole
cluster, which the browser then ORs into its WHERE clauses.

Operators can add their own aliases without editing code by dropping a
JSON file at  $HV/data/set_aliases.json  with the same shape:

    [
      {
        "name": "Twilight Masquerade",
        "tokens": ["Twilight Masquerade", "sv6", "sv6a",
                   "Mask of Change", "Night Wanderer"]
      },
      ...
    ]

The runtime merges the on-disk file on top of the bundled defaults so
upgrades never clobber operator additions.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Iterable

log = logging.getLogger("set_aliases")

# ── curated bundled aliases ─────────────────────────────────────────────────
# Each cluster: human label + every token (English name, JP set code, KR
# code, alternative names) that should be treated as the same set.
# Codes follow the pokemontcg.io / Bulbapedia conventions.
_BUNDLED: list[dict] = [
    # ── Scarlet & Violet block ─────────────────────────────────────────────
    {"name": "Scarlet & Violet Base",
     "tokens": ["Scarlet & Violet", "Scarlet and Violet", "sv1",
                "Scarlet ex", "Violet ex", "sv1S", "sv1V"]},
    {"name": "Paldea Evolved",
     "tokens": ["Paldea Evolved", "sv2", "Triplet Beat", "sv1a",
                "Snow Hazard", "Clay Burst", "sv2P", "sv2D"]},
    {"name": "Obsidian Flames",
     "tokens": ["Obsidian Flames", "sv3", "Ruler of the Black Flame", "sv3a"]},
    {"name": "151",
     "tokens": ["151", "Pokemon 151", "Pokémon 151", "sv3pt5",
                "Scarlet & Violet 151", "sv2a"]},
    {"name": "Paradox Rift",
     "tokens": ["Paradox Rift", "sv4", "Ancient Roar", "Future Flash",
                "sv4K", "sv4M"]},
    {"name": "Paldean Fates",
     "tokens": ["Paldean Fates", "sv4pt5", "Shiny Treasure ex", "sv4a"]},
    {"name": "Temporal Forces",
     "tokens": ["Temporal Forces", "sv5", "Wild Force", "Cyber Judge",
                "sv5K", "sv5M"]},
    {"name": "Twilight Masquerade",
     "tokens": ["Twilight Masquerade", "sv6", "Mask of Change",
                "Night Wanderer", "sv6a"]},
    {"name": "Shrouded Fable",
     "tokens": ["Shrouded Fable", "sv6pt5", "Crimson Haze", "sv6a"]},
    {"name": "Stellar Crown",
     "tokens": ["Stellar Crown", "sv7", "Stellar Miracle", "sv7a"]},
    {"name": "Surging Sparks",
     "tokens": ["Surging Sparks", "sv8", "Super Electric Breaker",
                "Paradigm Trigger Returns", "sv7K", "sv7M"]},
    {"name": "Prismatic Evolutions",
     "tokens": ["Prismatic Evolutions", "sv8pt5", "Terastal Festival",
                "sv8a"]},
    {"name": "Journey Together",
     "tokens": ["Journey Together", "sv9", "Battle Partners", "sv9a"]},
    {"name": "Destined Rivals",
     "tokens": ["Destined Rivals", "sv10", "Heat Wave Arena",
                "Glory of Team Rocket", "sv10K", "sv10M"]},
    # SV special / promo
    {"name": "SV Black Star Promos",
     "tokens": ["SV Black Star Promos", "svp", "S-P", "SV-P"]},

    # ── Sword & Shield block (still high-volume in singles market) ────────
    {"name": "Sword & Shield Base",
     "tokens": ["Sword & Shield", "swsh1", "s1W", "s1H",
                "Sword", "Shield"]},
    {"name": "Rebel Clash",
     "tokens": ["Rebel Clash", "swsh2", "s2", "Explosive Walker",
                "Rebellion Crash", "s1a"]},
    {"name": "Darkness Ablaze",
     "tokens": ["Darkness Ablaze", "swsh3", "s3", "Infinity Zone",
                "Legendary Heartbeat", "s3a"]},
    {"name": "Vivid Voltage",
     "tokens": ["Vivid Voltage", "swsh4", "s4", "Amazing Volt Tackle",
                "Shocking Volt Tackle", "s4a"]},
    {"name": "Shining Fates",
     "tokens": ["Shining Fates", "swsh45", "Shiny Star V", "s4a"]},
    {"name": "Battle Styles",
     "tokens": ["Battle Styles", "swsh5", "s5R", "s5I",
                "Single Strike Master", "Rapid Strike Master"]},
    {"name": "Chilling Reign",
     "tokens": ["Chilling Reign", "swsh6", "s6", "Silver Lance",
                "Jet-Black Spirit", "Jet-Black Geist", "s6H", "s6K"]},
    {"name": "Evolving Skies",
     "tokens": ["Evolving Skies", "swsh7", "s7", "Eevee Heroes",
                "Skyscraping Perfect", "Towering Perfection",
                "Blue Sky Stream", "s7D", "s7R"]},
    {"name": "Celebrations",
     "tokens": ["Celebrations", "cel25", "25th Anniversary Collection",
                "s8a"]},
    {"name": "Fusion Strike",
     "tokens": ["Fusion Strike", "swsh8", "s8", "Fusion Arts"]},
    {"name": "Brilliant Stars",
     "tokens": ["Brilliant Stars", "swsh9", "s9", "Star Birth"]},
    {"name": "Astral Radiance",
     "tokens": ["Astral Radiance", "swsh10", "s10", "Time Gazer",
                "Space Juggler", "s10P", "s10D"]},
    {"name": "Pokemon GO",
     "tokens": ["Pokemon GO", "Pokémon GO", "pgo", "s10b"]},
    {"name": "Lost Origin",
     "tokens": ["Lost Origin", "swsh11", "s11", "Lost Abyss",
                "Incandescent Arcana", "s11a"]},
    {"name": "Silver Tempest",
     "tokens": ["Silver Tempest", "swsh12", "s12", "Paradigm Trigger"]},
    {"name": "Crown Zenith",
     "tokens": ["Crown Zenith", "swsh12pt5", "VSTAR Universe", "s12a"]},
]


_LOCK = threading.RLock()
_CACHE: dict | None = None
_CACHE_LOADED_AT: float = 0.0
_RELOAD_INTERVAL = 60.0  # re-scan disk every minute, cheap


def _data_dir() -> str:
    base = os.environ.get("HV") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "data")


def _alias_file_path() -> str:
    """Operator-editable override file. $HV/data/set_aliases.json."""
    return os.path.join(_data_dir(), "set_aliases.json")


def _synced_file_path() -> str:
    """Auto-discovered file from set_alias_sync.py. $HV/data/set_aliases_synced.json."""
    return os.path.join(_data_dir(), "set_aliases_synced.json")


def _load_json_clusters(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            log.warning("[set_aliases] %s is not a JSON list, ignoring", path)
            return []
        clean = []
        for entry in data:
            tokens = entry.get("tokens") or []
            if isinstance(tokens, list) and tokens:
                clean.append({
                    "name":   str(entry.get("name") or tokens[0]),
                    "tokens": [str(t) for t in tokens if t],
                })
        return clean
    except Exception as exc:
        log.warning("[set_aliases] failed to load %s: %s", path, exc)
        return []


def _load_disk_overrides() -> list[dict]:
    """Operator-edited file."""
    return _load_json_clusters(_alias_file_path())


def _load_synced() -> list[dict]:
    """Auto-discovered file (pokemontcg.io)."""
    return _load_json_clusters(_synced_file_path())


def _build_index() -> dict:
    """
    Returns
    -------
    {
      "clusters": [ {name, tokens:[...]}, ... ],
      "lookup":   { lowercase_token: cluster_index, ... }
    }

    Precedence (earlier wins on token-lookup ties): bundled curated >
    operator override > auto-synced. This keeps hand-tuned JP/KR
    cross-language linkage authoritative even after a sync rewrites
    English-side metadata.
    """
    clusters = list(_BUNDLED) + _load_disk_overrides() + _load_synced()
    lookup: dict[str, int] = {}
    for i, cl in enumerate(clusters):
        for t in cl.get("tokens", []):
            key = t.strip().lower()
            if key and key not in lookup:
                lookup[key] = i
    return {"clusters": clusters, "lookup": lookup}


def _index() -> dict:
    """Cached, auto-reloads every _RELOAD_INTERVAL so JSON edits show up."""
    global _CACHE, _CACHE_LOADED_AT
    with _LOCK:
        now = time.time()
        if _CACHE is None or (now - _CACHE_LOADED_AT) > _RELOAD_INTERVAL:
            _CACHE = _build_index()
            _CACHE_LOADED_AT = now
        return _CACHE


def expand(query: str) -> tuple[list[str], str | None]:
    """
    Given a raw search string, return (tokens, cluster_label).

    If the query (or any whitespace-separated piece of it) maps to a known
    cluster, every token in that cluster is returned and the cluster's
    canonical name is returned alongside. Otherwise the original string is
    returned as the only token and cluster_label is None.

    Token matching is case-insensitive; substring matches against the
    bundled tokens are also tried so that "twilight" still finds the
    "Twilight Masquerade" cluster.
    """
    q = (query or "").strip()
    if not q:
        return [], None
    idx = _index()
    lookup = idx["lookup"]
    clusters = idx["clusters"]

    # 1) exact (case-insensitive) match on the whole query
    hit = lookup.get(q.lower())
    if hit is not None:
        cl = clusters[hit]
        return list(dict.fromkeys(cl["tokens"])), cl["name"]

    # 2) substring against any cluster token (for partial typing)
    ql = q.lower()
    for i, cl in enumerate(clusters):
        for t in cl["tokens"]:
            tl = t.lower()
            if ql in tl or tl in ql:
                # only treat as a real alias if the overlap is meaningful
                if len(ql) >= 3 and (ql == tl or ql in tl):
                    return list(dict.fromkeys(cl["tokens"])), cl["name"]

    # 3) no alias; pass through verbatim
    return [q], None


def all_clusters() -> list[dict]:
    """Used by an admin endpoint that lists every known cluster."""
    return list(_index()["clusters"])
