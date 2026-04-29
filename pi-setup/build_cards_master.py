"""
build_cards_master.py — Unified consolidator (U9)

Builds the `cards_master` table by joining every Layer-1 source table
with the Layer-2 reference tables, applying the priority rules from
`unified/priority.py` to pick the best value for each field.

Strategy
========
We treat TCGdex's `src_tcgdex_multi` as the SPINE: every row there
becomes one cards_master row keyed by (set_id, card_number, 'STD').
For each spine row we pull enrichment from every other source by
trying multiple match keys in priority order:

  1. exact match on (set_id, card_number)
  2. exact match on TCGdex global id (e.g. 'sv8-025')
  3. trimmed/normalised card_number match within set
  4. (no fuzzy name match — too risky for a POS)

Cards present in source tables but NOT covered by TCGdex are added
as orphan rows under a synthetic set_id derived from the source's
set_name (or '_orphan' if no set_name). This means the consolidator
NEVER drops a card on the floor — every source row appears in
cards_master at least once.

Idempotency: the consolidator uses BEGIN + DELETE FROM cards_master
+ bulk INSERT inside one transaction, so readers either see the old
fully-populated table or the new fully-populated table.

Each cards_master row carries `source_refs` JSONB recording which
source contributed each field. This is critical for debugging
("why does this Korean card have an English name from the wrong
set?") and for the /admin/db-coverage endpoint to compute per-source
contribution stats.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from typing import Any

import psycopg2
import psycopg2.extras

from unified.local_images import SOURCE_LANG, local_path_for
from unified.priority import AGGREGATES, PRIORITY
from unified.schema import init_unified_schema

log = logging.getLogger("build_cards_master")


# ─── Source readers ───────────────────────────────────────────────────────

def _safe_str(v) -> str:
    if v is None: return ""
    return str(v).strip()


def _normalise_card_number(n: str) -> str:
    """Drop the '/total' suffix so '025/106' matches '025' and '25'.
    Also strips leading zeros for a secondary match key."""
    if not n: return ""
    s = str(n).strip()
    if "/" in s:
        s = s.split("/", 1)[0]
    return s.strip()


def _alt_keys_for_number(n: str) -> set[str]:
    """Yield every form a card number might take across sources."""
    s = _normalise_card_number(n)
    if not s: return set()
    out = {s}
    out.add(s.lstrip("0") or "0")
    # Pad to 3 digits if numeric
    try:
        out.add(f"{int(s):03d}")
    except ValueError:
        pass
    return out


def _read_tcgdex(cur) -> dict[tuple, dict]:
    """Returns {(set_id, normalised_local_id): row_dict}."""
    cur.execute("""
        SELECT src_id, set_id, card_local_id, card_global_id,
               image_base, names, raw
          FROM src_tcgdex_multi
    """)
    out: dict[tuple, dict] = {}
    for row in cur.fetchall():
        sid = _safe_str(row["set_id"])
        lid = _normalise_card_number(row["card_local_id"])
        if not sid or not lid:
            continue
        names = row["names"] or {}
        if isinstance(names, str):
            names = json.loads(names)
        raw = row["raw"] or {}
        if isinstance(raw, str):
            raw = json.loads(raw)
        out[(sid, lid)] = {
            "src_id": row["src_id"],
            "set_id": sid,
            "card_number": lid,
            "global_id": _safe_str(row["card_global_id"]),
            "image_base": _safe_str(row["image_base"]),
            "names": names,
            "raw": raw,
        }
    return out


def _read_simple_table(cur, sql: str, key_fn) -> dict[Any, list[dict]]:
    """Run sql, build a dict keyed by key_fn(row) → [rows]."""
    cur.execute(sql)
    out: dict[Any, list[dict]] = defaultdict(list)
    for row in cur.fetchall():
        d = dict(row)
        k = key_fn(d)
        if k is None:
            continue
        out[k].append(d)
    return out


# Each loader returns: dict mapping (set_id_or_label, card_number) → row_dict.
# When set_id isn't known the key uses the source's set name verbatim
# and the consolidator tries to alias it to the canonical set_id via
# ref_set_mapping fuzzy match.

def _read_tcg_api(cur) -> dict[tuple, dict]:
    cur.execute("SELECT to_regclass('cards') IS NOT NULL AS exists")
    if not cur.fetchone()["exists"]:
        return {}
    cur.execute("""
        SELECT card_id, set_id, set_name, name, supertype, subtype,
               types, hp, artist, rarity, image_url, number, national_pokedex_numbers
          FROM cards
        LIMIT 100000
    """ if False else """
        SELECT * FROM cards LIMIT 100000
    """)
    rows = cur.fetchall()
    out: dict[tuple, dict] = {}
    for row in rows:
        d = dict(row)
        # Be tolerant of schema variations between deployments
        sid = _safe_str(d.get("set_id") or d.get("set", ""))
        num = _safe_str(d.get("number") or d.get("card_number", ""))
        if not sid or not num:
            continue
        out[(sid, _normalise_card_number(num))] = d
    return out


def _read_kr_official(cur) -> dict[tuple, dict]:
    cur.execute("SELECT to_regclass('cards_kr') IS NOT NULL AS exists")
    if not cur.fetchone()["exists"]:
        return {}
    cur.execute("""
        SELECT card_id, prod_code, card_number, set_name, name_kr,
               pokedex_no, supertype, subtype, hp, type_kr,
               rarity, artist, image_url
          FROM cards_kr
    """)
    out: dict[tuple, dict] = {}
    for row in cur.fetchall():
        d = dict(row)
        sid = _safe_str(d.get("prod_code"))
        num = _normalise_card_number(d.get("card_number") or "")
        if not sid or not num: continue
        out[(sid, num)] = d
    return out


def _read_jp_pokell(cur) -> dict[tuple, dict]:
    cur.execute("SELECT to_regclass('cards_jpn') IS NOT NULL AS exists")
    if not cur.fetchone()["exists"]:
        return {}
    cur.execute("""
        SELECT set_code, set_name, series, card_number, name_en,
               name_jp, rarity, card_type, image_url
          FROM cards_jpn
    """)
    out: dict[tuple, dict] = {}
    for row in cur.fetchall():
        d = dict(row)
        sid = _safe_str(d.get("set_code"))
        num = _normalise_card_number(d.get("card_number") or "")
        if not sid or not num: continue
        out[(sid, num)] = d
    return out


def _read_chs_official(cur) -> dict[tuple, dict]:
    cur.execute("SELECT to_regclass('cards_chs') IS NOT NULL AS exists")
    if not cur.fetchone()["exists"]:
        return {}
    cur.execute("""
        SELECT card_id, commodity_code, collection_number, commodity_name,
               name_chs, card_type, rarity, hp, image_url
          FROM cards_chs
    """)
    out: dict[tuple, dict] = {}
    for row in cur.fetchall():
        d = dict(row)
        # collection_number looks like '008/207' — derive set from
        # commodity_code prefix or punt to the Chinese-master mapping.
        cn = _normalise_card_number(d.get("collection_number") or "")
        cc = _safe_str(d.get("commodity_code"))
        # Use first letters of commodity_code as set proxy (best-effort)
        sid = re.match(r"^[A-Za-z]+\d*", cc).group(0).lower() if cc else ""
        if not sid or not cn: continue
        out[(sid, cn)] = d
    return out


def _read_pocket_official(cur) -> dict[tuple, dict]:
    cur.execute("SELECT to_regclass('cards_jpn_pocket') IS NOT NULL AS exists")
    if not cur.fetchone()["exists"]:
        return {}
    cur.execute("""
        SELECT set_code, card_number, name, rarity, image_url
          FROM cards_jpn_pocket
    """)
    out: dict[tuple, dict] = {}
    for row in cur.fetchall():
        d = dict(row)
        sid = _safe_str(d.get("set_code"))
        num = _normalise_card_number(str(d.get("card_number") or ""))
        if not sid or not num: continue
        out[(sid, num)] = d
    return out


def _read_pocket_lt(cur) -> dict[tuple, dict]:
    cur.execute("""
        SELECT expansion_id, card_number, name, rarity, card_type,
               pack, image_url
          FROM src_pocket_limitless
    """)
    out: dict[tuple, dict] = {}
    for row in cur.fetchall():
        d = dict(row)
        sid = _safe_str(d.get("expansion_id"))
        num = _normalise_card_number(d.get("card_number") or "")
        if not sid or not num: continue
        out[(sid, num)] = d
    return out


def _read_eng_xlsx(cur) -> dict[tuple, dict]:
    cur.execute("""
        SELECT row_id, set_name, card_number, pokedex_id, card_name,
               card_type, rarity_variant, other_pokemon, ex_serial_numbers
          FROM src_eng_xlsx
    """)
    out: dict[tuple, dict] = {}
    for row in cur.fetchall():
        d = dict(row)
        sid = _safe_str(d.get("set_name"))   # set_NAME, not id; aliased later
        num = _normalise_card_number(d.get("card_number") or "")
        if not sid or not num: continue
        out[(sid, num)] = d
    return out


def _read_jp_xlsx(cur) -> dict[tuple, dict]:
    cur.execute("""
        SELECT card_name, era, card_type, rarity, special_rarity,
               release_date, set_name_eng, set_name_jpn, set_number,
               promo_number
          FROM src_jp_xlsx
    """)
    out: dict[tuple, dict] = {}
    for row in cur.fetchall():
        d = dict(row)
        sid = _safe_str(d.get("set_name_eng"))
        num = _normalise_card_number(d.get("set_number") or d.get("promo_number") or "")
        if not sid or not num: continue
        out[(sid, num)] = d
    return out


def _read_jp_pcc(cur) -> dict[tuple, dict]:
    cur.execute("""
        SELECT card_id, set_code, set_name, card_number, name_jp,
               rarity, card_type, image_url
          FROM src_jp_pokemoncardcom
    """)
    out: dict[tuple, dict] = {}
    for row in cur.fetchall():
        d = dict(row)
        sid = _safe_str(d.get("set_code"))
        num = _normalise_card_number(d.get("card_number") or "")
        if not sid or not num: continue
        out[(sid, num)] = d
    return out


def _read_eng_ex(cur) -> dict[tuple, list[dict]]:
    cur.execute("""
        SELECT set_name, card_name, card_number, rarity,
               code_1, code_2, code_3, rh_code
          FROM src_eng_ex_codes
    """)
    out: dict[tuple, list[dict]] = defaultdict(list)
    for row in cur.fetchall():
        d = dict(row)
        sid = _safe_str(d.get("set_name"))
        num = _normalise_card_number(d.get("card_number") or "")
        if not sid or not num: continue
        out[(sid, num)].append(d)
    return out


def _read_jp_ex(cur) -> dict[tuple, list[dict]]:
    cur.execute("""
        SELECT set_name, card_name_jp, card_name_en, card_number,
               rarity, code_1, code_2, code_3, rh_code
          FROM src_jp_ex_codes
    """)
    out: dict[tuple, list[dict]] = defaultdict(list)
    for row in cur.fetchall():
        d = dict(row)
        sid = _safe_str(d.get("set_name"))
        num = _normalise_card_number(d.get("card_number") or "")
        if not sid or not num: continue
        out[(sid, num)].append(d)
    return out


def _read_ref_dex(cur) -> dict[int, dict]:
    cur.execute("SELECT * FROM ref_pokedex_species")
    out: dict[int, dict] = {}
    for row in cur.fetchall():
        d = dict(row)
        try:
            out[int(d["pokedex_no"])] = d
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _read_ref_set_mapping(cur) -> dict[str, dict]:
    cur.execute("SELECT * FROM ref_set_mapping")
    out: dict[str, dict] = {}
    for row in cur.fetchall():
        d = dict(row)
        sid = _safe_str(d.get("set_id"))
        if sid: out[sid] = d
    return out


def _read_ref_promo(cur) -> dict[tuple, list[dict]]:
    cur.execute("SELECT * FROM ref_promo_provenance")
    out: dict[tuple, list[dict]] = defaultdict(list)
    for row in cur.fetchall():
        d = dict(row)
        k = (_safe_str(d.get("set_label")),
             _normalise_card_number(d.get("card_number") or ""))
        out[k].append(d)
    return out


# ─── Per-source field extractors ──────────────────────────────────────────

def _extract(source_id: str, src_row: dict, field: str) -> str | int | None:
    """Pull `field` from a source row of source `source_id`. Returns
    None when the source doesn't carry that field at all (so the
    consolidator falls through to the next source)."""
    if src_row is None:
        return None

    if source_id == "tcgdex":
        names = src_row.get("names") or {}
        m = {
            "name_en":  names.get("en", ""),
            "name_kr":  names.get("ko", ""),
            "name_jp":  names.get("ja", ""),
            "name_chs": names.get("zh-cn", "") or names.get("zh-Hans", ""),
            "name_cht": names.get("zh-tw", "") or names.get("zh-Hant", ""),
            "name_fr":  names.get("fr", ""),
            "name_de":  names.get("de", ""),
            "name_it":  names.get("it", ""),
            "name_es":  names.get("es", ""),
            "image_url": src_row.get("image_base", ""),
        }
        return m.get(field)

    if source_id == "tcg_api":
        m = {
            "name_en":  src_row.get("name", ""),
            "card_type": src_row.get("supertype", ""),
            "energy_type": (src_row.get("types") or [""])[0] if isinstance(src_row.get("types"), list) else "",
            "subtype": (src_row.get("subtypes") or [""])[0] if isinstance(src_row.get("subtypes"), list) else "",
            "stage": "",
            "rarity": src_row.get("rarity", ""),
            "rarity_code": "",
            "hp": _safe_int_or_none(src_row.get("hp")),
            "artist": src_row.get("artist", ""),
            "image_url": src_row.get("image_url", ""),
            "pokedex_id": _first_int(src_row.get("national_pokedex_numbers")),
        }
        return m.get(field)

    if source_id == "kr_official":
        m = {
            "name_kr":  src_row.get("name_kr", ""),
            "card_type": src_row.get("supertype", ""),
            "energy_type": src_row.get("type_kr", ""),
            "subtype": src_row.get("subtype", ""),
            "rarity": src_row.get("rarity", ""),
            "rarity_code": src_row.get("rarity", ""),
            "hp": _safe_int_or_none(src_row.get("hp")),
            "artist": src_row.get("artist", ""),
            "image_url": src_row.get("image_url", ""),
            "pokedex_id": _safe_int_or_none(src_row.get("pokedex_no")),
        }
        return m.get(field)

    if source_id == "jp_pokell":
        m = {
            "name_jp":  src_row.get("name_jp", ""),
            "name_en":  src_row.get("name_en", ""),
            "card_type": src_row.get("card_type", ""),
            "rarity": src_row.get("rarity", ""),
            "image_url": src_row.get("image_url", ""),
        }
        return m.get(field)

    if source_id == "chs_official":
        m = {
            "name_chs": src_row.get("name_chs", "") or src_row.get("commodity_name", ""),
            "card_type": src_row.get("card_type", ""),
            "rarity": src_row.get("rarity", ""),
            "rarity_code": src_row.get("rarity", ""),
            "hp": _safe_int_or_none(src_row.get("hp")),
            "image_url": src_row.get("image_url", ""),
        }
        return m.get(field)

    if source_id == "pocket_off":
        m = {
            "name_en":  src_row.get("name", ""),
            "rarity":   src_row.get("rarity", ""),
            "image_url": src_row.get("image_url", ""),
        }
        return m.get(field)

    if source_id == "pocket_lt":
        m = {
            "name_en":  src_row.get("name", ""),
            "rarity":   src_row.get("rarity", ""),
            "card_type": src_row.get("card_type", ""),
            "image_url": src_row.get("image_url", ""),
        }
        return m.get(field)

    if source_id == "eng_xlsx":
        m = {
            "name_en":  src_row.get("card_name", ""),
            "card_type": src_row.get("card_type", ""),
            "rarity":  src_row.get("rarity_variant", ""),
            "rarity_code": src_row.get("rarity_variant", ""),
            "pokedex_id": _safe_int_or_none(src_row.get("pokedex_id")),
            "other_pokemon": src_row.get("other_pokemon", ""),
        }
        return m.get(field)

    if source_id == "jp_xlsx":
        m = {
            "name_jp": src_row.get("card_name", ""),
            "card_type": src_row.get("card_type", ""),
            "rarity": src_row.get("rarity", ""),
        }
        return m.get(field)

    if source_id == "jp_pcc":
        m = {
            "name_jp":  src_row.get("name_jp", ""),
            "card_type": src_row.get("card_type", ""),
            "energy_type": src_row.get("card_type", ""),
            "image_url": src_row.get("image_url", ""),
            "rarity": src_row.get("rarity", ""),
        }
        return m.get(field)

    if source_id == "ref_dex":
        m = {
            "name_en":  src_row.get("name_en", ""),
            "name_jp":  src_row.get("name_jp", ""),
            "name_kr":  src_row.get("name_kr", ""),
            "name_chs": src_row.get("name_chs", ""),
            "name_cht": src_row.get("name_cht", ""),
            "name_fr":  src_row.get("name_fr", ""),
            "name_de":  src_row.get("name_de", ""),
        }
        return m.get(field)

    return None


def _safe_int_or_none(v):
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _first_int(v):
    if isinstance(v, list) and v:
        try: return int(v[0])
        except (TypeError, ValueError): return None
    return _safe_int_or_none(v)


def _read_jp_cards_json(cur) -> dict:
    """Returns {(EDITION_UPPER, numero): [list of records]} from
    src_jp_cards_json. Edition is uppercased for case-insensitive joins
    against arbitrary spine set_ids.

    Multiple records per key are common (different scrapes of the same
    physical card across reprints with the same JP set + dex#). The
    backfill takes the first match — this source is name-only fallback,
    not a primary join, so collisions are tolerable.

    Returns {} if the table doesn't exist yet (importer hasn't run) so
    the consolidator stays runnable on a fresh DB.
    """
    out: dict[tuple, list[dict]] = {}
    try:
        cur.execute("""
            SELECT card_id, name, edition, description, element, health, numero
              FROM src_jp_cards_json
             WHERE name <> '' AND edition <> '' AND numero IS NOT NULL
        """)
    except psycopg2.errors.UndefinedTable:
        return out
    except psycopg2.Error as e:
        log.warning("[consolidator] _read_jp_cards_json failed: %s", e)
        return out
    for row in cur.fetchall():
        key = ((row["edition"] or "").strip().upper(), row["numero"])
        out.setdefault(key, []).append(dict(row))
    return out


def _read_set_alias_map(cur) -> dict:
    """Returns {CODE_UPPER: set(CODE_UPPER, …)} — every set code mapped to
    every other regional code for the same set. Used by the JP backfill
    so a spine row whose set_id is the EN code (e.g. 'sv2') can still
    find JP records keyed by 'SV2'. Empty if ref_set_alias is missing.

    Symmetric: every code is in its own bucket, so callers can do a
    single dict lookup without worrying about direction.
    """
    out: dict[str, set[str]] = {}
    try:
        cur.execute("""
            SELECT code_en, code_jp, code_kr, code_chs
              FROM ref_set_alias
        """)
    except psycopg2.errors.UndefinedTable:
        return out
    except psycopg2.Error as e:
        log.warning("[consolidator] _read_set_alias_map failed: %s", e)
        return out
    for row in cur.fetchall():
        codes = {(row[k] or "").strip().upper()
                 for k in ("code_en", "code_jp", "code_kr", "code_chs")}
        codes.discard("")
        if len(codes) < 2:
            continue
        for c in codes:
            out.setdefault(c, set()).update(codes)
    return out


def _read_set_canonicaliser_map(cur) -> dict:
    """Returns {ANY_CODE_UPPER: canonical_set_id_lowercase}.

    Canonical = the EN abbrev (lowercased) when the row has one, else
    first non-empty in (JP, KR, CHS) priority. Lowercase matches the
    convention used by tcg_api / tcgdex (the dominant high-priority
    EN sources) so existing spine rows from those sources stay stable.

    Codes that appear in ref_set_alias rows with CONFLICTING canonicals
    (e.g. JP 'LS' splits into both EN 'G1' and EN 'G2') are dropped from
    the map — those are genuinely ambiguous at the set level and need
    card_number discrimination, not blind set-code rewriting. Such
    codes pass through canonicalisation unchanged, and the JP backfill's
    alias-walk catches them as defence-in-depth.

    Empty dict when ref_set_alias is missing → canonicaliser becomes
    a no-op identity, preserving current behaviour on a fresh DB.
    """
    out: dict[str, str] = {}
    conflicts: set[str] = set()
    try:
        cur.execute("""
            SELECT code_en, code_jp, code_kr, code_chs FROM ref_set_alias
        """)
    except psycopg2.errors.UndefinedTable:
        return out
    except psycopg2.Error as e:
        log.warning("[consolidator] _read_set_canonicaliser_map failed: %s", e)
        return out
    for row in cur.fetchall():
        en  = (row["code_en"]  or "").strip()
        jp  = (row["code_jp"]  or "").strip()
        kr  = (row["code_kr"]  or "").strip()
        chs = (row["code_chs"] or "").strip()
        canonical = (en or jp or kr or chs).lower()
        if not canonical:
            continue
        for c in (en, jp, kr, chs):
            if not c:
                continue
            cu = c.upper()
            existing = out.get(cu)
            if existing and existing != canonical:
                conflicts.add(cu)
            else:
                out[cu] = canonical
    for cu in conflicts:
        out.pop(cu, None)
    if conflicts:
        log.info("[consolidator] canonicaliser: %d ambiguous codes left "
                 "unmapped (need card_number discrimination): %s",
                 len(conflicts), ", ".join(sorted(conflicts))[:200])
    return out


def _build_canonicaliser(canon_map: dict):
    """Returns a function (set_id) → canonical_set_id.

    Returns the input unchanged (case-preserved) when the code isn't in
    the canonicaliser map — which covers JP-only sets (like SV2a
    'Pokémon Card 151'), unknown codes, and ambiguous codes the loader
    deliberately dropped. This means the canonicaliser is always SAFE:
    the worst case is no-op behaviour, never a wrong rewrite.
    """
    def canonicalise(set_id):
        if not set_id:
            return set_id
        s = str(set_id).strip()
        if not s:
            return set_id
        return canon_map.get(s.upper(), s)
    return canonicalise


def _lookup_one(src_dict: dict, candidates: list, alt_keys_fn) -> tuple:
    """Walk candidate (orig_set_id, orig_card_num) keys against a single
    -row source dict. Tries each candidate's number-variant alts. Returns
    (row, matched_key) or (None, None). The matched_key is needed by the
    image-collection loop so it can pass the ORIGINAL set_id to
    local_path_for() — preserving on-disk image lookup paths that were
    built before canonicalisation existed.
    """
    for cand_sid, cand_num in candidates:
        row = src_dict.get((cand_sid, cand_num))
        if row is not None:
            return row, (cand_sid, cand_num)
        for alt_num in alt_keys_fn(cand_num):
            row = src_dict.get((cand_sid, alt_num))
            if row is not None:
                return row, (cand_sid, alt_num)
    return None, None


def _lookup_multi(src_dict: dict, candidates: list) -> list:
    """Multi-row variant for sources like eng_ex / jp_ex / ref_promo.
    Concatenates across every candidate that hits — e.g. a spine row
    canonicalised from BOTH a JP and EN source can pull EX-set codes
    from both sides for a complete ex_serial_codes aggregate.
    """
    out: list = []
    for cand_key in candidates:
        out.extend(src_dict.get(cand_key, []))
    return out


def _src_id_for(source_id: str, src_row: dict) -> str:
    """Stringify the source row's PK for source_refs auditability."""
    if source_id == "tcgdex":
        return f"src_tcgdex_multi:{src_row.get('src_id', '?')}"
    if source_id == "tcg_api":
        return f"cards:{src_row.get('card_id', '?')}"
    if source_id == "kr_official":
        return f"cards_kr:{src_row.get('card_id', '?')}"
    if source_id == "jp_pokell":
        return f"cards_jpn:{src_row.get('set_code', '?')}-{src_row.get('card_number', '?')}"
    if source_id == "chs_official":
        return f"cards_chs:{src_row.get('card_id', '?')}"
    if source_id == "pocket_off":
        return f"cards_jpn_pocket:{src_row.get('set_code', '?')}-{src_row.get('card_number', '?')}"
    if source_id == "pocket_lt":
        return f"src_pocket_limitless:{src_row.get('expansion_id', '?')}-{src_row.get('card_number', '?')}"
    if source_id == "eng_xlsx":
        return f"src_eng_xlsx:{src_row.get('row_id', '?')}"
    if source_id == "jp_xlsx":
        return f"src_jp_xlsx:{src_row.get('set_name_eng','?')}-{src_row.get('set_number','?')}"
    if source_id == "jp_pcc":
        return f"src_jp_pokemoncardcom:{src_row.get('card_id', '?')}"
    if source_id == "ref_dex":
        return f"ref_pokedex_species:{src_row.get('pokedex_no', '?')}"
    return source_id


# ─── Main consolidation loop ──────────────────────────────────────────────

def build_cards_master(db_conn) -> dict:
    """Rebuild cards_master from every source. Returns counts dict."""
    init_unified_schema(db_conn)
    cur = db_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    log.info("[consolidator] reading source tables…")
    sources: dict[str, dict] = {
        "tcgdex":       _read_tcgdex(cur),
        "tcg_api":      _read_tcg_api(cur),
        "kr_official":  _read_kr_official(cur),
        "jp_pokell":    _read_jp_pokell(cur),
        "chs_official": _read_chs_official(cur),
        "pocket_off":   _read_pocket_official(cur),
        "pocket_lt":    _read_pocket_lt(cur),
        "eng_xlsx":     _read_eng_xlsx(cur),
        "jp_xlsx":      _read_jp_xlsx(cur),
        "jp_pcc":       _read_jp_pcc(cur),
    }
    eng_ex = _read_eng_ex(cur)
    jp_ex = _read_jp_ex(cur)
    ref_dex = _read_ref_dex(cur)
    ref_promo = _read_ref_promo(cur)
    jp_cards_json = _read_jp_cards_json(cur)
    set_alias_map = _read_set_alias_map(cur)
    canon_map     = _read_set_canonicaliser_map(cur)
    canonicalise  = _build_canonicaliser(canon_map)

    # Re-key jp_cards_json from upper-edition to canonical so its keys
    # align with the spine's canonical (set_id, card_number) form. The
    # JP backfill below relies on this — a spine row at ('sv2', 47)
    # must be able to hit jp_cards_json keyed by ('sv2', 47) on the
    # direct path, with the alias-walk reserved for ambiguous codes
    # the canonicaliser deliberately left alone.
    jp_recanon: dict[tuple, list[dict]] = defaultdict(list)
    for (ed_upper, numero), recs in jp_cards_json.items():
        canon_ed = canonicalise(ed_upper)
        jp_recanon[(canon_ed, numero)].extend(recs)
    jp_cards_json = dict(jp_recanon)

    for name, d in sources.items():
        log.info("[consolidator] %s: %d rows", name, len(d))
    log.info("[consolidator] jp_cards_json: %d keys, set_alias_map: %d codes, "
             "canon_map: %d codes",
             len(jp_cards_json), len(set_alias_map), len(canon_map))
    log.info("[consolidator] eng_ex: %d, jp_ex: %d, ref_dex: %d, ref_promo: %d",
             len(eng_ex), len(jp_ex), len(ref_dex), len(ref_promo))

    # ── Spine: one row per CANONICAL (set_id, card_number) ──
    # Source dicts keep their ORIGINAL set_ids (never rewritten) so the
    # KR image-path lookup at local_path_for() still finds files on disk
    # under the codes they were downloaded with. The spine collapses
    # cross-region duplicates by canonicalising every original set_id
    # through ref_set_alias and grouping the original keys behind the
    # canonical form. Per-card lookups walk that candidate list — see
    # _lookup_one / _lookup_multi above.
    candidates_by_canon: dict[tuple, list[tuple]] = defaultdict(list)
    for src_dict in sources.values():
        for orig_key in src_dict.keys():
            orig_sid, orig_num = orig_key
            canon_key = (canonicalise(orig_sid), orig_num)
            candidates_by_canon[canon_key].append(orig_key)
    # Also expose jp_cards_json's (edition, numero) keys to the canonical
    # spine — but jp_cards_json is dex-id-keyed, not card_number-keyed,
    # so we don't add it to the spine here. The JP backfill block below
    # handles its own canonical lookup against jp_cards_json directly.

    pre_spine = sum(len(s) for s in sources.values())
    spine_keys = list(candidates_by_canon.keys())
    log.info("[consolidator] spine has %d canonical (set_id, card_number) keys "
             "(collapsed from %d source rows; %d duplicates merged)",
             len(spine_keys), pre_spine, pre_spine - len(spine_keys))

    # ── Build rows ──
    now = int(time.time())
    rows: list[tuple] = []
    for set_id, card_number in spine_keys:
        candidates = candidates_by_canon[(set_id, card_number)]
        src_refs: dict[str, str] = {}
        out: dict[str, Any] = {
            "set_id": set_id,                 # canonical (lowercase EN where known)
            "card_number": card_number,
            "variant_code": "STD",            # variants TBD — see plan U2/U6 ref data
        }

        # First-wins fields — walk every canonical candidate against each
        # source dict in PRIORITY order. The first non-empty extracted
        # value wins; source_refs records the original set_id when it
        # differs from canonical, for full audit traceability.
        for field, source_order in PRIORITY.items():
            for sid in source_order:
                src_dict = sources.get(sid, {})
                src_row, matched_key = _lookup_one(
                    src_dict, candidates, _alt_keys_for_number)
                if src_row is None:
                    continue
                v = _extract(sid, src_row, field)
                if v is None or v == "":
                    continue
                out[field] = v
                ref = _src_id_for(sid, src_row)
                if matched_key and matched_key[0] != set_id:
                    ref = f"{ref} (orig {matched_key[0]})"
                src_refs[field] = ref
                break

        # ref_dex fallback for missing language names — needs pokedex_id
        dex_id = out.get("pokedex_id")
        if dex_id and dex_id in ref_dex:
            dex_row = ref_dex[dex_id]
            for field in ("name_en", "name_kr", "name_jp", "name_chs",
                           "name_cht", "name_fr", "name_de"):
                if not out.get(field):
                    v = dex_row.get(field, "")
                    if v:
                        out[field] = v
                        src_refs[field] = _src_id_for("ref_dex", dex_row)

        # JP cards.json backfill — fills name_jp / hp / energy_type when
        # every higher-priority source missed this card. After Slice 7
        # (spine canonicalisation) jp_cards_json is also re-keyed by
        # canonical so the direct lookup hits in the common case. The
        # alias-walk stays as defence-in-depth for the few ambiguous
        # codes the canonicaliser deliberately left alone (e.g. JP 'LS'
        # which splits into EN 'G1'/'G2'). See unified/priority.py
        # docstring for why this lives outside PRIORITY (cards.json
        # has no card_number to join on).
        if dex_id and jp_cards_json:
            try_codes: list[str] = [set_id] if set_id else []
            spine_upper = (set_id or "").upper()
            for alt in set_alias_map.get(spine_upper, ()):
                alt_canon = canonicalise(alt)
                if alt_canon and alt_canon not in try_codes:
                    try_codes.append(alt_canon)
            jp_recs: list[dict] = []
            matched_via = ""
            for code in try_codes:
                jp_recs = jp_cards_json.get((code, dex_id), [])
                if jp_recs:
                    matched_via = code
                    break
            if jp_recs:
                rec = jp_recs[0]
                via = "" if matched_via == set_id else f" via {matched_via}"
                ref = f"src_jp_cards_json:{rec.get('card_id', '?')}{via}"
                if not out.get("name_jp") and rec.get("name"):
                    out["name_jp"] = rec["name"]
                    src_refs["name_jp"] = ref
                if out.get("hp") in (None, 0) and rec.get("health"):
                    out["hp"] = rec["health"]
                    src_refs["hp"] = ref
                if not out.get("energy_type") and rec.get("element"):
                    out["energy_type"] = rec["element"]
                    src_refs["energy_type"] = ref

        # Aggregate fields (collect every non-empty value across every
        # candidate — a card now collects EX codes from BOTH its EN and
        # JP source rows when both contributed to the canonical spine).
        ex_codes: list[dict] = []
        for src_row in _lookup_multi(eng_ex, candidates):
            for k in ("code_1", "code_2", "code_3", "rh_code"):
                code = _safe_str(src_row.get(k))
                if code and code != "-":
                    ex_codes.append({"code": code, "kind": k, "lang": "en",
                                      "set": _safe_str(src_row.get("set_name"))})
        for src_row in _lookup_multi(jp_ex, candidates):
            for k in ("code_1", "code_2", "code_3", "rh_code"):
                code = _safe_str(src_row.get(k))
                if code and code != "-":
                    ex_codes.append({"code": code, "kind": k, "lang": "jp",
                                      "set": _safe_str(src_row.get("set_name"))})
        if ex_codes:
            out["ex_serial_codes"] = ex_codes
            src_refs["ex_serial_codes"] = f"src_eng_ex_codes/src_jp_ex_codes:{len(ex_codes)} entries"

        # Promo source — also candidates-aware now
        promos = _lookup_multi(ref_promo, candidates)
        if promos:
            out["promo_source"] = promos[0].get("source_category", "")
            src_refs["promo_source"] = _src_id_for("ref_dex", promos[0]) \
                .replace("ref_pokedex_species", "ref_promo_provenance")

        # Image candidates: walk every source's image_url. Critically,
        # local_path_for() receives the ORIGINAL source-dict set_id (from
        # matched_key) — NOT the canonical — because KR images are
        # stored on disk at ptcg-kr-db/card_img/<original_set_id>/...
        # and renaming would require re-mirroring 100k+ files. The
        # canonical spine is purely a query-time grouping.
        img_candidates: list[dict] = []
        seen_urls: set[str] = set()
        for sid in AGGREGATES.get("image_url_alt", []):
            src_dict = sources.get(sid, {})
            src_row, matched_key = _lookup_one(
                src_dict, candidates, _alt_keys_for_number)
            if src_row is None:
                continue
            url = _safe_str(_extract(sid, src_row, "image_url"))
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            orig_sid = matched_key[0] if matched_key else set_id
            local = local_path_for(sid, url, set_id=orig_sid)
            img_candidates.append({
                "src":   sid,
                "lang":  SOURCE_LANG.get(sid, ""),
                "url":   url,
                "local": local,
            })

        rows.append((
            out["set_id"],
            out["card_number"],
            out.get("variant_code", "STD"),
            _safe_int_or_none(out.get("pokedex_id")),
            _safe_str(out.get("name_en")),
            _safe_str(out.get("name_kr")),
            _safe_str(out.get("name_jp")),
            _safe_str(out.get("name_chs")),
            _safe_str(out.get("name_cht")),
            _safe_str(out.get("name_fr")),
            _safe_str(out.get("name_de")),
            _safe_str(out.get("name_it")),
            _safe_str(out.get("name_es")),
            _safe_str(out.get("card_type")),
            _safe_str(out.get("energy_type")),
            _safe_str(out.get("subtype")),
            _safe_str(out.get("stage")),
            _safe_str(out.get("rarity")),
            _safe_str(out.get("rarity_code")),
            _safe_int_or_none(out.get("hp")),
            _safe_str(out.get("artist")),
            json.dumps(out.get("ex_serial_codes", []), ensure_ascii=False),
            _safe_str(out.get("other_pokemon")),
            _safe_str(out.get("promo_source")),
            _safe_str(out.get("image_url")),
            json.dumps(img_candidates, ensure_ascii=False),
            json.dumps(src_refs, ensure_ascii=False),
            now,
            now,
        ))

    # ── Bulk write ──
    cur.execute("BEGIN")
    cur.execute("DELETE FROM cards_master")
    insert_sql = """
        INSERT INTO cards_master
          (set_id, card_number, variant_code, pokedex_id,
           name_en, name_kr, name_jp, name_chs, name_cht,
           name_fr, name_de, name_it, name_es,
           card_type, energy_type, subtype, stage,
           rarity, rarity_code, hp, artist,
           ex_serial_codes, other_pokemon, promo_source,
           image_url, image_url_alt, source_refs,
           first_seen, last_built)
        VALUES %s
        ON CONFLICT (set_id, card_number, variant_code) DO NOTHING
    """
    psycopg2.extras.execute_values(cur, insert_sql, rows, page_size=500)
    db_conn.commit()

    # ── Stats ──
    cur.execute("SELECT COUNT(*) AS n FROM cards_master")
    total = int(cur.fetchone()["n"])
    cur.execute("""
        SELECT
          SUM(CASE WHEN name_en  <> '' THEN 1 ELSE 0 END) AS with_en,
          SUM(CASE WHEN name_kr  <> '' THEN 1 ELSE 0 END) AS with_kr,
          SUM(CASE WHEN name_jp  <> '' THEN 1 ELSE 0 END) AS with_jp,
          SUM(CASE WHEN name_chs <> '' THEN 1 ELSE 0 END) AS with_chs,
          SUM(CASE WHEN ex_serial_codes <> '[]'::jsonb THEN 1 ELSE 0 END) AS with_codes,
          SUM(CASE WHEN promo_source <> '' THEN 1 ELSE 0 END) AS with_promo
        FROM cards_master
    """)
    stats = dict(cur.fetchone())

    return {"total": total, "rows_written": len(rows), **stats}


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    args = ap.parse_args()
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr); return 1
    with psycopg2.connect(url) as conn:
        result = build_cards_master(conn)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
