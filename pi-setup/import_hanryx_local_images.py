#!/usr/bin/env python3
"""
import_hanryx_local_images.py — link /mnt/cards/HanryxVault local images to cards_master.

Scans three sub-collections copied from the operator's Windows machine:

  images/<SET>/<NUM>.jpg               — Pocket + numbered vintage sets
  Pokemon_Cards_Korean_Global/         — KR_<SET>_<NUM>.png  (Korean SV-era)
  JP_Varient/<name>.<SET>.<N>.<id>.thumb.png — Japanese vintage thumbnails

For each image, appends a hanryx_local candidate to cards_master.image_url_alt
so the /card/image endpoint can serve it directly from disk without any CDN
dependency. The server re-checks local paths on every request, so newly
transferred files become available immediately after this script runs.

Usage (from pi-setup/):
  docker compose exec pos python3 /app/import_hanryx_local_images.py [--dry-run]
  docker compose exec pos python3 /app/import_hanryx_local_images.py --root /mnt/cards/HanryxVault
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras

log = logging.getLogger("import_hanryx_local_images")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)

_USB_ROOT    = Path(os.environ.get("USB_CARDS_ROOT", "/mnt/cards"))
DEFAULT_ROOT = _USB_ROOT / "HanryxVault"
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://vaultpos:vaultpos@db:5432/vaultpos",
)

SRC_TAG = "hanryx_local"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


# ── Filename parsers ──────────────────────────────────────────────────────────

def scan_pocket_images(root: Path) -> list[tuple[str, str, str, str]]:
    """
    images/<SET>/<NUM>.jpg  →  (set_id, raw_card_num, local_path, lang)

    Folder name IS the set_id (A1, A1a, A2, base1, Ancient Mew, …).
    File stem IS the card number as recorded on the card face (001, 002, …).
    These are Pocket / vintage numbered sets where the art language is JP.
    """
    results: list[tuple[str, str, str, str]] = []
    img_root = root / "images"
    if not img_root.is_dir():
        log.warning("images/ not found under %s — skipping pocket scan", root)
        return results
    for set_dir in sorted(img_root.iterdir()):
        if not set_dir.is_dir():
            continue
        set_id = set_dir.name
        for f in sorted(set_dir.iterdir()):
            if not f.is_file() or f.suffix.lower() not in IMAGE_EXTS:
                continue
            results.append((set_id, f.stem, str(f), "jp"))
    log.info("[pocket] %d images across %d set folders in images/",
             len(results),
             sum(1 for d in img_root.iterdir() if d.is_dir()))
    return results


# KR_<SET>_<NUM>(opt_suffix).ext  — e.g. KR_SV1_001.png, KR_SV2a_012.png
_KR_RX = re.compile(r"^KR_([A-Za-z0-9]+)_(\d+[A-Za-z]?)\.", re.IGNORECASE)


def scan_kr_images(root: Path) -> list[tuple[str, str, str, str]]:
    """
    Pokemon_Cards_Korean_Global/KR_<SET>_<NUM>.png  →  (set_id, raw_num, path, "kr")
    """
    results: list[tuple[str, str, str, str]] = []
    kr_root = root / "Pokemon_Cards_Korean_Global"
    if not kr_root.is_dir():
        log.warning("Pokemon_Cards_Korean_Global/ not found under %s — skipping KR scan", root)
        return results
    for f in sorted(kr_root.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in IMAGE_EXTS:
            continue
        m = _KR_RX.match(f.name)
        if not m:
            log.debug("[kr] unrecognised filename: %s", f.name)
            continue
        set_id, card_num = m.group(1), m.group(2)
        results.append((set_id, card_num, str(f), "kr"))
    log.info("[kr] %d images in Pokemon_Cards_Korean_Global/", len(results))
    return results


# <name-with-dots-and-dashes>.<SET>.<NUM>.<tcgplayer_id>.thumb.(png|jpg)
# Anchored to the LAST 4 dot-segments before the extension so the name
# part can itself contain dots (e.g. "Bills-PC.VEN3.48.53092.thumb.png").
_JP_THUMB_RX = re.compile(
    r"\.([A-Za-z0-9]+)\.(\d+)\.(\d+)\.thumb\.(png|jpg|jpeg)$",
    re.IGNORECASE,
)


def scan_jp_varient(root: Path) -> list[tuple[str, str, str, str]]:
    """
    JP_Varient/<name>.<SET>.<NUM>.<id>.thumb.png  →  (set_id, raw_num, path, "jp")
    """
    results: list[tuple[str, str, str, str]] = []
    jp_root = root / "JP_Varient"
    if not jp_root.is_dir():
        log.warning("JP_Varient/ not found under %s — skipping JP variant scan", root)
        return results
    for f in sorted(jp_root.rglob("*")):
        if not f.is_file():
            continue
        m = _JP_THUMB_RX.search(f.name)
        if not m:
            log.debug("[jp_varient] unrecognised filename: %s", f.name)
            continue
        set_id, card_num = m.group(1), m.group(2)
        results.append((set_id, card_num, str(f), "jp"))
    log.info("[jp_varient] %d images in JP_Varient/", len(results))
    return results


# ── DB helpers ────────────────────────────────────────────────────────────────

def _num_variants(raw: str) -> set[str]:
    """Generate card_number forms to try: raw, int-stripped, zero-padded 3-digit."""
    variants = {raw, raw.strip()}
    try:
        n = int(raw)
        variants.add(str(n))        # '001' → '1'
        variants.add(f"{n:03d}")    # '1'   → '001'
    except ValueError:
        pass
    variants.discard("")
    return variants


def find_card_id(cur, set_id: str, raw_num: str) -> Optional[tuple]:
    """Return (set_id, card_number) composite key for the cards_master row,
    trying multiple normalised forms of raw_num. Returns None if no match."""
    for num in _num_variants(raw_num):
        cur.execute(
            "SELECT set_id, card_number FROM cards_master"
            " WHERE set_id=%s AND card_number=%s LIMIT 1",
            (set_id, num),
        )
        row = cur.fetchone()
        if row:
            return (row[0], row[1])
    return None


def append_candidate(
    cur, card_id: tuple, local_path: str, lang: str, dry_run: bool
) -> bool:
    """Append a {src, url, local, lang} entry to cards_master.image_url_alt
    if this exact local_path is not already listed. Returns True if the row
    was (or would be, in dry-run) updated.
    card_id is a (set_id, card_number) tuple."""
    sid, cnum = card_id
    cur.execute(
        "SELECT image_url_alt FROM cards_master WHERE set_id=%s AND card_number=%s",
        (sid, cnum),
    )
    row = cur.fetchone()
    if not row:
        return False

    raw_alt = row[0]
    if isinstance(raw_alt, str):
        try:
            alt: list = json.loads(raw_alt) if raw_alt else []
        except Exception:
            alt = []
    elif isinstance(raw_alt, list):
        alt = raw_alt
    else:
        alt = []

    # Deduplicate on local path
    for c in alt:
        if isinstance(c, dict) and c.get("local") == local_path:
            return False

    candidate = {"src": SRC_TAG, "url": "", "local": local_path, "lang": lang}
    alt.append(candidate)

    if not dry_run:
        cur.execute(
            "UPDATE cards_master SET image_url_alt=%s::jsonb"
            " WHERE set_id=%s AND card_number=%s",
            (json.dumps(alt), sid, cnum),
        )
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--root", default=str(DEFAULT_ROOT),
        help=f"Root of the HanryxVault image tree (default: {DEFAULT_ROOT})",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Report matches without writing to the database",
    )
    ap.add_argument("--skip-pocket", action="store_true", help="Skip images/ scan")
    ap.add_argument("--skip-kr",     action="store_true", help="Skip Pokemon_Cards_Korean_Global/ scan")
    ap.add_argument("--skip-jp",     action="store_true", help="Skip JP_Varient/ scan")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        log.error(
            "Root %s does not exist. Run the scp/rsync transfer first, then retry.", root
        )
        return 1

    # Collect
    candidates: list[tuple[str, str, str, str]] = []
    if not args.skip_pocket:
        candidates.extend(scan_pocket_images(root))
    if not args.skip_kr:
        candidates.extend(scan_kr_images(root))
    if not args.skip_jp:
        candidates.extend(scan_jp_varient(root))

    log.info("Total candidate images: %d", len(candidates))
    if not candidates:
        log.warning("No images found — check --root and directory structure.")
        return 0

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    linked   = 0
    already  = 0
    unmatched = 0

    for set_id, raw_num, local_path, lang in candidates:
        card_id = find_card_id(cur, set_id, raw_num)
        if card_id is None:
            log.debug("[no match] set_id=%-6s num=%-5s %s", set_id, raw_num, local_path)
            unmatched += 1
            continue
        updated = append_candidate(cur, card_id, local_path, lang, args.dry_run)
        if updated:
            linked += 1
        else:
            already += 1

    if not args.dry_run:
        conn.commit()
        log.info(
            "Committed. linked=%d  already_present=%d  unmatched=%d",
            linked, already, unmatched,
        )
    else:
        log.info(
            "Dry run complete. would_link=%d  already_present=%d  unmatched=%d",
            linked, already, unmatched,
        )

    conn.close()

    if unmatched:
        pct = 100 * unmatched // max(1, len(candidates))
        log.info(
            "%d/%d (%.0f%%) images had no cards_master match — "
            "these sets may not be imported yet (run the relevant import_*.py first).",
            unmatched, len(candidates), pct,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
