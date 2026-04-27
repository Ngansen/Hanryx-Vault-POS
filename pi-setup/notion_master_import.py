#!/usr/bin/env python3
"""
notion_master_import.py — One-shot importer for the user's Notion-exported
"TCG Master Set" tracker into ref_set_mapping + cards_master.

Notion export structure (one folder per set, top-level MD per set):

    /mnt/cards/tcg-master-set/Pokemon Card Sets Database/
        TCG Sets <hash>.csv                           (set index — ignored, we
                                                       use the per-set MDs instead
                                                       since they're cleaner)
        TCG Sets/
            Unified Minds <hash>.md                   (h1=name, Release Date, etc)
            Unified Minds/
                Untitled <hash>.csv                   (Card Number, Name, Collected, Rarity)
                <card>.jpg / .webp                    (optional per-card art)

Per-set CSV columns:
    Card Number   "1/108"
    Name          "VenusaurEX"  (Notion concatenates CamelCase — we normalize)
    Collected     "Yes" / "No"
    Rarity        "Rare Holo ex  ☆"  (Notion symbols stripped on import)

Idempotency:
- ref_set_mapping keyed on set_id (slug); ON CONFLICT updates name/release.
- cards_master UNIQUE (set_id, card_number, variant_code); ON CONFLICT updates
  name_en, rarity, image_url, last_built. Re-running the importer after edits
  in Notion will sync the diff cleanly without dupes.

Usage (inside the pos container, where /mnt/cards is bind-mounted):

    docker compose exec pos python3 notion_master_import.py
    docker compose exec pos python3 notion_master_import.py --dry-run
    docker compose exec pos python3 notion_master_import.py --limit-sets 5
    docker compose exec pos python3 notion_master_import.py --root '/mnt/cards/tcg-master-set/Pokemon Card Sets Database'
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Iterator, Optional

# DB-heavy imports (server pulls psycopg2 which isn't on dev workstations)
# are deferred to run(). That lets the pure helpers below be imported and
# unit-tested without standing up the whole Postgres client stack.

log = logging.getLogger("notion_master_import")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

DEFAULT_ROOT = Path("/mnt/cards/tcg-master-set/Pokemon Card Sets Database")
SOURCE_TAG = "notion_master"
NOW_MS = lambda: int(time.time() * 1000)  # noqa: E731

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


# ─── helpers ──────────────────────────────────────────────────────────────


def slugify_set(name: str) -> str:
    """Turn 'XY Evolutions' → 'xy-evolutions'. Stable across re-imports."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s or "unknown"


# Rarity → variant code map. Notion uses free-text rarity; we collapse it
# down to one of the canonical variant codes from ref_variant_terms (STD,
# RH, MBH, PBH, 1ED, SAR). Anything we can't classify falls back to STD.
_VARIANT_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b1st\s*edition\b",        re.I), "1ED"),
    (re.compile(r"master\s*ball",            re.I), "MBH"),
    (re.compile(r"pok[eé]?\s*ball|monster\s*ball", re.I), "PBH"),
    (re.compile(r"special\s*art|\bsar\b",    re.I), "SAR"),
    (re.compile(r"reverse\s*holo|\brh\b",    re.I), "RH"),
]


def parse_rarity(raw: str) -> tuple[str, str]:
    """
    (rarity_clean, variant_code) — strips Notion symbols (☆⚫●◆) from rarity
    and infers variant from keywords. Default variant = STD.
    """
    if not raw:
        return ("", "STD")
    # Drop Notion's coloured-circle / star glyphs and collapse whitespace.
    cleaned = re.sub(r"[☆★⚫●◆◇■□▲▼♦♢]", "", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    variant = "STD"
    for rx, code in _VARIANT_RULES:
        if rx.search(cleaned):
            variant = code
            break
    return (cleaned, variant)


# Notion mashes English names into one CamelCase token: "MVenusaurEX",
# "PikachuVMAX", "CharizardGX", "SnorlaxV". We split before EX/GX/V/VMAX/VSTAR
# and lowercase the suffix to "ex" (modern TCG convention) where applicable.
_NAME_SUFFIX_RX = re.compile(
    r"^(.*?)(EX|GX|VMAX|VSTAR|V|BREAK|LEGEND|LV\.?\s*X)$"
)


def normalize_card_name(raw: str) -> str:
    if not raw:
        return ""
    name = raw.strip()
    # Mega prefix: "MVenusaurEX" → "M Venusaur EX"; we leave the M alone here
    # rather than expanding to "Mega" — keeps it 1:1 with how the modern UI
    # writes it. If that bothers anyone we can flip it later.
    if re.match(r"^M[A-Z][a-z]", name):
        name = "M " + name[1:]

    m = _NAME_SUFFIX_RX.match(name)
    if m:
        body, suffix = m.group(1), m.group(2)
        # 'EX' is the only suffix that's lowercase in modern Pokémon TCG
        # branding ("Pikachu ex"). Everything else stays uppercase.
        suffix_norm = "ex" if suffix.upper() == "EX" else suffix.upper()
        # Insert a space before suffix if the body doesn't end in one.
        if body and not body.endswith(" "):
            name = f"{body} {suffix_norm}"
        else:
            name = f"{body}{suffix_norm}"
    return name.strip()


def parse_release_year(text: str) -> str:
    """Extract a 4-digit year from 'August 2, 2019' → '2019'. Empty if none."""
    m = re.search(r"(19|20)\d{2}", text or "")
    return m.group(0) if m else ""


def find_card_image(set_folder: Path, card_number: str) -> str:
    """
    Look in the per-set folder for an image whose filename contains the
    card's collector number. Returns a file:// URL or '' if no match.

    The Notion images are named all over the map (e.g. '240 60.jpg',
    '275491.jpg', 'Psychic-Energy.HGSS.119.webp'), so this is best-effort.
    Only ~95 of the sets shipped images so we expect lots of misses; the
    POS will fall back to its existing network fetcher for the rest.
    """
    if not card_number:
        return ""
    # Card number arrives as e.g. "1/108"; we want just "1".
    num_only = card_number.split("/", 1)[0].strip().lstrip("0") or card_number
    # Try a couple of patterns: filename starts with the number, contains
    # ".<NUM>." (e.g. 'Psychic-Energy.HGSS.119.webp'), or ends with '<NUM>.ext'.
    candidates = []
    for f in set_folder.iterdir():
        if not f.is_file() or f.suffix.lower() not in IMAGE_EXTS:
            continue
        stem = f.stem
        if (
            stem == num_only
            or re.search(rf"(^|[^0-9]){re.escape(num_only)}([^0-9]|$)", stem)
        ):
            candidates.append(f)
    if not candidates:
        return ""
    # Prefer .webp > .jpg > .png (smaller / better quality on the modern Pi).
    candidates.sort(key=lambda p: (
        0 if p.suffix.lower() == ".webp" else 1 if p.suffix.lower() == ".jpg" else 2,
        len(p.name),
    ))
    return f"file://{candidates[0]}"


# ─── parsers ──────────────────────────────────────────────────────────────


def parse_set_md(md_path: Path) -> dict:
    """Pull h1 title + Release Date + linked CSV path from a Notion set MD."""
    text = md_path.read_text(encoding="utf-8", errors="replace")
    out = {"name_en": "", "release_date": "", "csv_path": None}

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# ") and not out["name_en"]:
            out["name_en"] = line[2:].strip()
        elif line.lower().startswith("release date:"):
            out["release_date"] = line.split(":", 1)[1].strip()
        elif line.startswith("[Untitled]("):
            # [Untitled](Unified%20Minds/Untitled%20<hash>.csv)
            m = re.match(r"\[Untitled\]\((.+?)\)", line)
            if m:
                from urllib.parse import unquote
                rel = unquote(m.group(1))
                out["csv_path"] = (md_path.parent / rel).resolve()

    # Fallback: if the MD didn't have a CSV link, look for an
    # "Untitled*.csv" file in a sibling folder named like the MD.
    if not out["csv_path"]:
        sibling = md_path.parent / md_path.stem.split(" ")[0]
        # md_path.stem looks like 'Unified Minds 29015188acc...' — drop the
        # 32-char trailing Notion hash to recover the human folder name.
        folder_name = re.sub(r"\s+[0-9a-f]{20,}$", "", md_path.stem)
        candidate_folder = md_path.parent / folder_name
        if candidate_folder.is_dir():
            csvs = list(candidate_folder.glob("Untitled*.csv"))
            if csvs:
                out["csv_path"] = csvs[0]
        del sibling  # keep linter quiet

    return out


def iter_set_mds(root: Path) -> Iterator[Path]:
    """All top-level set MDs under <root>/TCG Sets/."""
    sets_dir = root / "TCG Sets"
    if not sets_dir.is_dir():
        log.error("Expected directory not found: %s", sets_dir)
        return
    for md in sorted(sets_dir.glob("*.md")):
        yield md


def iter_card_rows(csv_path: Path) -> Iterator[dict]:
    # utf-8-sig strips the BOM that Notion writes at the start of every CSV.
    # Without it the first column's header becomes '\ufeffCard Number' and
    # every row.get('Card Number') returns None — silent 100% skip.
    with csv_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            yield row


# ─── DB upserts ───────────────────────────────────────────────────────────


def upsert_set_mapping(db, set_id: str, set_meta: dict) -> None:
    db.execute(
        """
        INSERT INTO ref_set_mapping
            (set_id, name_en, release_year, raw, imported_at)
        VALUES (%s, %s, %s, %s::jsonb, %s)
        ON CONFLICT (set_id) DO UPDATE SET
            name_en      = EXCLUDED.name_en,
            release_year = EXCLUDED.release_year,
            raw          = EXCLUDED.raw,
            imported_at  = EXCLUDED.imported_at
        """,
        (
            set_id,
            set_meta.get("name_en", ""),
            parse_release_year(set_meta.get("release_date", "")),
            json.dumps({"source": SOURCE_TAG, **set_meta}, default=str),
            NOW_MS(),
        ),
    )


def upsert_card(
    db,
    set_id: str,
    card_number: str,
    variant_code: str,
    name_en: str,
    rarity: str,
    image_url: str,
) -> None:
    now = NOW_MS()
    source_refs = json.dumps({
        "name_en": f"{SOURCE_TAG}:{set_id}",
        "rarity":  f"{SOURCE_TAG}:{set_id}",
    })
    db.execute(
        """
        INSERT INTO cards_master
            (set_id, card_number, variant_code,
             name_en, rarity, image_url,
             source_refs, first_seen, last_built)
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
        ON CONFLICT (set_id, card_number, variant_code) DO UPDATE SET
            name_en     = CASE WHEN EXCLUDED.name_en  <> '' THEN EXCLUDED.name_en  ELSE cards_master.name_en  END,
            rarity      = CASE WHEN EXCLUDED.rarity   <> '' THEN EXCLUDED.rarity   ELSE cards_master.rarity   END,
            image_url   = CASE WHEN EXCLUDED.image_url<> '' THEN EXCLUDED.image_url ELSE cards_master.image_url END,
            source_refs = cards_master.source_refs || EXCLUDED.source_refs,
            last_built  = EXCLUDED.last_built
        """,
        (
            set_id, card_number, variant_code,
            name_en, rarity, image_url,
            source_refs, now, now,
        ),
    )


# ─── main ─────────────────────────────────────────────────────────────────


def run(root: Path, dry_run: bool, limit_sets: Optional[int]) -> dict:
    if not root.is_dir():
        log.error("Root not found: %s", root)
        sys.exit(2)

    # Deferred imports — only needed when actually writing to the DB. Lets
    # the pure helpers above be imported in a non-Pi environment for tests.
    from server import _direct_db  # type: ignore[import-not-found]
    from unified.schema import init_unified_schema  # type: ignore[import-not-found]

    db = _direct_db()
    # Ensure tables exist (server.init_db now does this on startup, but the
    # importer is sometimes run against fresh databases out of band).
    init_unified_schema(db)

    stats = {
        "sets_seen":      0,
        "sets_upserted":  0,
        "sets_no_csv":    0,
        "cards_seen":     0,
        "cards_upserted": 0,
        "cards_skipped":  0,
        "images_matched": 0,
        "errors":         [],
    }

    for i, md_path in enumerate(iter_set_mds(root)):
        if limit_sets is not None and stats["sets_seen"] >= limit_sets:
            break
        stats["sets_seen"] += 1

        try:
            meta = parse_set_md(md_path)
        except Exception as e:
            log.warning("Failed to parse MD %s: %s", md_path.name, e)
            stats["errors"].append(f"md:{md_path.name}: {e}")
            continue

        set_name = meta["name_en"] or md_path.stem
        set_id = slugify_set(set_name)
        csv_path = meta["csv_path"]

        log.info("[set %3d] %s (id=%s) csv=%s",
                 i + 1, set_name, set_id,
                 csv_path.name if csv_path else "<MISSING>")

        if not dry_run:
            upsert_set_mapping(db, set_id, meta)
            stats["sets_upserted"] += 1

        if not csv_path or not csv_path.is_file():
            stats["sets_no_csv"] += 1
            log.warning("  → no per-set CSV found, skipping cards")
            if not dry_run:
                db.commit()
            continue

        set_folder = csv_path.parent
        set_seen = 0
        set_upserted = 0
        first_skipped_row: Optional[dict] = None
        for row in iter_card_rows(csv_path):
            stats["cards_seen"] += 1
            set_seen += 1
            try:
                card_number = (row.get("Card Number") or "").strip()
                raw_name    = (row.get("Name") or "").strip()
                raw_rarity  = (row.get("Rarity") or "").strip()
                if not card_number or not raw_name:
                    stats["cards_skipped"] += 1
                    if first_skipped_row is None:
                        first_skipped_row = dict(row)
                    continue

                name_en = normalize_card_name(raw_name)
                rarity_clean, variant_code = parse_rarity(raw_rarity)
                image_url = find_card_image(set_folder, card_number)
                if image_url:
                    stats["images_matched"] += 1

                if not dry_run:
                    upsert_card(
                        db, set_id, card_number, variant_code,
                        name_en, rarity_clean, image_url,
                    )
                stats["cards_upserted"] += 1
                set_upserted += 1
            except Exception as e:
                stats["cards_skipped"] += 1
                stats["errors"].append(f"{set_id}/{row}: {e}")
                if not dry_run:
                    try:
                        db.rollback()
                    except Exception:
                        pass

        # Loud warning if a set's CSV parsed but produced zero rows. This
        # almost always means the column headers don't match what we expect
        # (BOM, renamed columns, blank file). Surfaces silently-broken
        # imports immediately instead of after a full run.
        if set_seen > 0 and set_upserted == 0:
            log.warning(
                "  → 0/%d rows accepted for %s. CSV headers were: %s. "
                "First skipped row: %s",
                set_seen, set_id,
                list((first_skipped_row or {}).keys()),
                first_skipped_row,
            )

        if not dry_run:
            db.commit()

    db.close()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                    help=f"Root of the Notion export (default: {DEFAULT_ROOT})")
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse and report without writing to the DB.")
    ap.add_argument("--limit-sets", type=int, default=None,
                    help="Stop after N sets (debugging).")
    args = ap.parse_args()

    log.info("Importing from: %s  (dry_run=%s)", args.root, args.dry_run)
    t0 = time.time()
    stats = run(args.root, args.dry_run, args.limit_sets)
    dt = time.time() - t0

    log.info("─" * 60)
    log.info("DONE in %.1fs", dt)
    for k, v in stats.items():
        if k == "errors":
            log.info("  %-16s %d", k, len(v))
            for e in v[:10]:
                log.info("      %s", e)
            if len(v) > 10:
                log.info("      ... and %d more", len(v) - 10)
        else:
            log.info("  %-16s %s", k, v)


if __name__ == "__main__":
    main()
