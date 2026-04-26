"""
import_pokeapi_species.py — PokéAPI species name loader (U8c)

Loads the multilingual Pokémon species name table from PokéAPI's
upstream CSV fixtures (the ones used to seed the public PokéAPI). We
deliberately do NOT clone the entire 53 MB pokeapi/pokeapi repo —
just the two CSVs we need:

  pokemon_species.csv          species_id, identifier, generation_id, ...
  pokemon_species_names.csv    pokemon_species_id, local_language_id,
                               name, genus

The result is loaded into `ref_pokedex_species`, indexed on every
language column for fast typo-tolerant cross-language lookup. The
consolidator joins the cards_master.pokedex_id to this table to fill
in the per-language species name when a card-level source is missing
the translation.

Why this matters: a customer types 'Pikachu' but only the Korean
data has '피카츄'. The consolidator can fill in name_kr from
ref_pokedex_species so the trade-show kiosk finds the card either
way. Since we want fuzzy search across languages, a normalised
species table is the foundation.

Languages we promote:
  9  → en        11 → ja
  1  → ja_kana   3  → ko
  12 → zh-cn     4  → zh-tw
  5  → fr        6  → de
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import sys
import time

import psycopg2
import psycopg2.extras
import requests

from unified.schema import init_unified_schema

log = logging.getLogger("import_pokeapi_species")

# Official PokeAPI fixtures repository — the upstream of the public
# PokéAPI. Hosted at github.com/PokeAPI/pokeapi (NOT under Ngansen's
# fork, since that fork is just for code, not data).
_PRIMARY_BASE = ("https://raw.githubusercontent.com/PokeAPI/pokeapi/"
                 "master/data/v2/csv")
_FALLBACK_BASE = ("https://raw.githubusercontent.com/Ngansen/pokeapi/"
                  "master/data/v2/csv")
SPECIES_CSV = "pokemon_species.csv"
NAMES_CSV = "pokemon_species_names.csv"

LANG_MAP: dict[int, str] = {
    9:  "name_en",
    1:  "name_jp_kana",
    11: "name_jp",
    3:  "name_kr",
    12: "name_chs",
    4:  "name_cht",
    5:  "name_fr",
    6:  "name_de",
}


def _fetch_csv(filename: str, timeout: int = 60) -> list[dict]:
    headers = {"User-Agent": "HanryxVault-POS/1.0"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"token {token}"

    last_err: Exception | None = None
    for base in (_PRIMARY_BASE, _FALLBACK_BASE):
        url = f"{base}/{filename}"
        try:
            log.info("[pokeapi] downloading %s", url)
            r = requests.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            return list(csv.DictReader(io.StringIO(r.text)))
        except Exception as e:
            last_err = e
            log.warning("[pokeapi] %s failed: %s", url, e)
    raise RuntimeError(f"could not fetch {filename}: {last_err}")


def _ingest(db_conn, species_rows: list[dict], names_rows: list[dict]) -> int:
    """Build the species master and bulk-insert."""
    # species_id → {field_name: value}
    by_dex: dict[int, dict] = {}
    for s in species_rows:
        try:
            sid = int(s["id"])
        except (KeyError, ValueError):
            continue
        gen = None
        try:
            gen = int(s.get("generation_id") or 0) or None
        except ValueError:
            pass
        by_dex[sid] = {
            "pokedex_no": sid,
            "name_en": s.get("identifier", "").replace("-", " ").title(),
            "generation": gen,
            "raw": {"identifier": s.get("identifier")},
        }

    for n in names_rows:
        try:
            sid = int(n["pokemon_species_id"])
            lid = int(n["local_language_id"])
        except (KeyError, ValueError):
            continue
        if sid not in by_dex:
            continue
        col = LANG_MAP.get(lid)
        if not col:
            continue
        # The English row in names.csv has both `name` and `genus`;
        # we prefer the names-table English over the identifier for
        # display ('Mr. Mime' vs 'Mr Mime').
        by_dex[sid][col] = n.get("name", "").strip()

    cur = db_conn.cursor()
    cur.execute("DELETE FROM ref_pokedex_species")
    now = int(time.time())

    insert_sql = """
        INSERT INTO ref_pokedex_species
          (pokedex_no, name_en, name_jp, name_jp_kana, name_kr,
           name_chs, name_cht, name_fr, name_de, generation,
           raw, imported_at)
        VALUES %s
    """
    batch: list[tuple] = []
    BATCH = 200
    inserted = 0

    def flush():
        nonlocal inserted
        if not batch: return
        psycopg2.extras.execute_values(cur, insert_sql, batch, page_size=200)
        inserted += len(batch)
        batch.clear()

    for sid, slot in sorted(by_dex.items()):
        batch.append((
            sid,
            slot.get("name_en", ""),
            slot.get("name_jp", ""),
            slot.get("name_jp_kana", ""),
            slot.get("name_kr", ""),
            slot.get("name_chs", ""),
            slot.get("name_cht", ""),
            slot.get("name_fr", ""),
            slot.get("name_de", ""),
            slot.get("generation"),
            json.dumps(slot.get("raw", {}), ensure_ascii=False, default=str),
            now,
        ))
        if len(batch) >= BATCH: flush()
    flush()
    db_conn.commit()
    return inserted


def import_pokeapi_species(db_conn, *, force: bool = False) -> dict:
    init_unified_schema(db_conn)
    cur = db_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM ref_pokedex_species")
    pre = int(cur.fetchone()[0])
    if pre > 0 and not force:
        return {"skipped": True, "pre_count": pre}

    species = _fetch_csv(SPECIES_CSV)
    names = _fetch_csv(NAMES_CSV)
    inserted = _ingest(db_conn, species, names)
    return {"inserted": inserted, "species_rows": len(species),
            "name_rows": len(names)}


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr); return 1
    with psycopg2.connect(url) as conn:
        print(json.dumps(import_pokeapi_species(conn, force=args.force),
                         indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
