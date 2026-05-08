#!/usr/bin/env python3
"""
build_species_names.py — generate species_names.py from PokéAPI.

Pulls every Pokémon species from https://pokeapi.co/api/v2/pokemon-species/<id>
and writes a stable, sorted, hand-readable Python data module that the live
POS imports at startup. The generated file is committed to git so the runtime
NEVER hits PokéAPI — that fetch happens only when this script is re-run.

WHY A GENERATED FILE (vs. a runtime fetch / DB table):
  - Trade-show kiosks need to work offline (no network at venue).
  - Lookup is a tight inner loop on every /card/price call; in-memory dict
    is sub-microsecond, a DB lookup or HTTP call is 1-100ms.
  - The data only changes when GameFreak releases a new Pokémon (~once per
    year for a mainline game, occasionally a DLC drops more). Polling the
    API on every boot is wasteful.

WHEN TO RE-RUN:
  - When a new mainline Pokémon game / DLC ships and PokéAPI's contributor
    community has ingested the new species (usually 1-7 days after release).
  - On demand if you suspect upstream name corrections.

USAGE:
  python3 pi-setup/build_species_names.py            # fetch & write
  python3 pi-setup/build_species_names.py --dry-run  # fetch, show diff, no write
  python3 pi-setup/build_species_names.py --use-cache /tmp/species_raw.json
                                                     # skip the fetch, useful
                                                     # while iterating on the
                                                     # output format

OUTPUT:
  pi-setup/species_names.py  (overwritten in place)

After running, commit the diff:
  git add pi-setup/species_names.py
  git commit -m "refresh species_names from PokéAPI (N species)"

POLITENESS:
  8 concurrent connections → ~14s for 1025 species. PokéAPI explicitly says
  "be reasonable" with no hard rate limit; 8 in flight is well below their
  published guidance.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

API_BASE = "https://pokeapi.co/api/v2/pokemon-species"
USER_AGENT = "HanryxVault-POS/1.0 (species-name-builder; +https://github.com/Ngansen/Hanryx-Vault-POS)"

# PokéAPI language codes → our internal field names. We keep ja AND ja_kana
# as separate fields because for some species ja is kanji-only (rare) and
# the explicit hiragana/katakana form lives in ja-Hrkt. snkrdunk specifically
# wants ja_kana (Pokémon names on snkrdunk are katakana). zh-Hant and zh-Hans
# are kept for future Chinese marketplace integrations.
LANG_MAP = {
    "en":      "en",
    "ja":      "ja",
    "ja-hrkt": "ja_kana",
    "ja-roma": "ja_roma",
    "ko":      "ko",
    "zh-hant": "zh_hant",
    "zh-hans": "zh_hans",
    "fr":      "fr",
    "de":      "de",
}

# Order of fields in the generated dicts — determines column order in the
# output file. Putting the high-value fields first (en, ja_kana, ko) keeps
# the most-used translations visible when a human eyeballs the file.
FIELD_ORDER = ["en", "ja", "ja_kana", "ja_roma", "ko", "zh_hant", "zh_hans", "fr", "de"]

OUT_PATH = Path(__file__).parent / "species_names.py"


def fetch_one(dex: int) -> dict:
    """Fetch one species from PokéAPI with retry. Returns flat dict of names."""
    url = f"{API_BASE}/{dex}"
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            data = json.loads(urllib.request.urlopen(req, timeout=20).read())
            out: dict = {}
            for n in data.get("names", []):
                lang = n["language"]["name"]
                if lang in LANG_MAP:
                    out[LANG_MAP[lang]] = n["name"]
            # English fallback from slug if the en row is missing (defensive —
            # PokéAPI always returns en in practice, but a corrupted ingest
            # could miss it).
            if "en" not in out:
                out["en"] = data["name"].title()
            return out
        except Exception as e:
            last_err = e
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"fetch #{dex} failed after 3 attempts: {last_err}")


def fetch_all(total: int = 1025, workers: int = 8) -> dict[int, dict]:
    """Parallel-fetch every species. Returns {dex: {field: name, ...}}."""
    records: dict[int, dict] = {}
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_one, d): d for d in range(1, total + 1)}
        done = 0
        errors = []
        for fut in as_completed(futs):
            dex = futs[fut]
            try:
                records[dex] = fut.result()
            except Exception as e:
                errors.append((dex, str(e)))
                continue
            done += 1
            if done % 100 == 0:
                print(f"  fetched {done}/{total} ({time.monotonic()-t0:.1f}s)",
                      file=sys.stderr)
    print(f"\n  total fetched: {len(records)}/{total} in {time.monotonic()-t0:.1f}s",
          file=sys.stderr)
    if errors:
        print(f"  errors: {len(errors)} — sample: {errors[:5]}", file=sys.stderr)
    return records


def discover_total() -> int:
    """Ask PokéAPI how many species exist right now (so we adapt to new gens)."""
    req = urllib.request.Request(f"{API_BASE}?limit=1",
                                 headers={"User-Agent": USER_AGENT})
    data = json.loads(urllib.request.urlopen(req, timeout=10).read())
    return int(data["count"])


def write_module(records: dict[int, dict], out_path: Path) -> None:
    """Write a stable, sorted, hand-readable Python module."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    n = len(records)

    lines: list[str] = [
        '"""',
        f"species_names.py — multilingual Pokémon species name table.",
        "",
        "GENERATED FILE — DO NOT EDIT MANUALLY.",
        f"Run `python3 pi-setup/build_species_names.py` to regenerate from PokéAPI.",
        "",
        f"Snapshot: {n} species, fetched {timestamp}",
        "",
        "Used by:",
        "  - price_scrapers.search_all() to translate English card-name queries",
        "    into katakana (snkrdunk) and hangul (tcgkorea) before scraping,",
        "    because those marketplaces only index in their native language.",
        "  - card_enrich, ai_assistant, fuzzy search — anywhere we need to",
        "    map between localised names of the same species.",
        '"""',
        "from __future__ import annotations",
        "",
        "import re",
        "from typing import Optional",
        "",
        "# {pokedex_number: {language_field: localized_name}}",
        "# Field meaning:",
        "#   en       — English",
        "#   ja       — Japanese (default form, usually katakana)",
        "#   ja_kana  — Japanese explicit hiragana/katakana (= what snkrdunk uses)",
        "#   ja_roma  — Japanese romaji (latin transliteration)",
        "#   ko       — Korean (hangul, = what tcgkorea uses)",
        "#   zh_hant  — Chinese traditional",
        "#   zh_hans  — Chinese simplified",
        "#   fr, de   — French, German",
        "SPECIES_NAMES: dict[int, dict[str, str]] = {",
    ]

    for dex in sorted(records):
        r = records[dex]
        # Build dict literal in stable field order, omit empty values
        parts = []
        for field in FIELD_ORDER:
            v = r.get(field)
            if v:
                # Use repr so we get safe escaping; force unicode escapes off
                # by calling str() — Python will keep CJK chars verbatim under
                # a `# -*- coding: utf-8 -*-` (default in Py3 source files).
                parts.append(f'"{field}": {json.dumps(v, ensure_ascii=False)}')
        lines.append(f"    {dex}: {{{', '.join(parts)}}},")

    lines.extend([
        "}",
        "",
        "",
        "# ─────────────────────────────────────────────────────────────────────",
        "# Lookup helpers",
        "# ─────────────────────────────────────────────────────────────────────",
        "",
        "# English-name-keyed reverse index, lowercased for case-insensitive lookup.",
        "# Built once at import time.",
        "_BY_LOWER_EN: dict[str, int] = {",
        "    r['en'].lower(): dex for dex, r in SPECIES_NAMES.items() if r.get('en')",
        "}",
        "",
        "# Card-name suffixes/decorators we strip before species lookup.",
        "# Order matters: longer / more-specific tokens come first so the regex",
        "# engine doesn't bail early on a substring match.",
        "_CARD_SUFFIX_RE = re.compile(",
        "    r'\\s+(?:'",
        "    r'VMAX|VSTAR|V-UNION|TAG TEAM|BREAK|LEGEND|LV\\.?\\s*X|PRIME|'",
        "    r'GX|EX|ex|V|☆|\\*|δ|Star|Prism Star|Radiant|Shining|Crystal'",
        "    r')\\b.*$',",
        "    re.IGNORECASE,",
        ")",
        "",
        "# Parenthetical set context, e.g. 'Pikachu (Celebrations)'",
        "_PAREN_RE = re.compile(r'\\s*\\([^)]*\\)\\s*$')",
        "",
        "",
        "def _stem(query: str) -> str:",
        "    \"\"\"Reduce a card-name query to its bare species stem.\"\"\"",
        "    s = (query or '').strip()",
        "    s = _PAREN_RE.sub('', s)",
        "    s = _CARD_SUFFIX_RE.sub('', s)",
        "    return s.strip()",
        "",
        "",
        "def lookup(query: str) -> Optional[int]:",
        "    \"\"\"Resolve a query string to a pokedex number, or None.\"\"\"",
        "    if not query:",
        "        return None",
        "    s = _stem(query).lower()",
        "    if not s:",
        "        return None",
        "    # Exact match first",
        "    if s in _BY_LOWER_EN:",
        "        return _BY_LOWER_EN[s]",
        "    # Try progressively dropping trailing words ('Mr Mime gx' → 'mr mime')",
        "    parts = s.split()",
        "    while len(parts) > 1:",
        "        parts.pop()",
        "        candidate = ' '.join(parts)",
        "        if candidate in _BY_LOWER_EN:",
        "            return _BY_LOWER_EN[candidate]",
        "    return None",
        "",
        "",
        "def translate(query: str, target_lang: str) -> Optional[str]:",
        "    \"\"\"Translate a card-name query to the target language.",
        "",
        "    Strips card suffixes (V, VMAX, ex, GX, ...) and parenthetical set",
        "    context before resolving to a species. Returns the localised name",
        "    or None if no species was matched / no translation available.",
        "",
        "    Examples:",
        "        translate('Pikachu',          'ja_kana') -> 'ピカチュウ'",
        "        translate('Pikachu V',        'ja_kana') -> 'ピカチュウ'",
        "        translate('Pikachu VMAX (Celebrations)', 'ko') -> '피카츄'",
        "        translate('Charizard ex',     'zh_hant') -> '噴火龍'",
        "        translate('NOT_A_POKEMON',    'ja_kana') -> None",
        "    \"\"\"",
        "    dex = lookup(query)",
        "    if dex is None:",
        "        return None",
        "    return SPECIES_NAMES.get(dex, {}).get(target_lang) or None",
        "",
        "",
        "__all__ = ['SPECIES_NAMES', 'lookup', 'translate']",
        "",
    ])

    out_path.write_text("\n".join(lines), encoding="utf-8")
    size_kb = out_path.stat().st_size / 1024
    print(f"\nwrote {out_path} ({n} species, {size_kb:.1f} KB)", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch but don't write the output file")
    parser.add_argument("--use-cache", metavar="PATH",
                        help="load records from a previous fetch JSON instead "
                             "of hitting PokéAPI (used during development)")
    parser.add_argument("--workers", type=int, default=8,
                        help="parallel HTTP workers (default 8)")
    args = parser.parse_args()

    if args.use_cache:
        print(f"loading cached records from {args.use_cache}", file=sys.stderr)
        with open(args.use_cache, encoding="utf-8") as f:
            raw = json.load(f)
        # JSON keys are strings; convert back to ints
        records = {int(k): {kk: vv for kk, vv in v.items() if kk != "_dex"}
                   for k, v in raw.items()}
    else:
        total = discover_total()
        print(f"PokéAPI reports {total} species; fetching all", file=sys.stderr)
        records = fetch_all(total=total, workers=args.workers)

    if not records:
        print("no records fetched, aborting", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"\n--dry-run: would write {len(records)} species to {OUT_PATH}",
              file=sys.stderr)
        return 0

    write_module(records, OUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
