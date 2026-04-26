"""
unified/sources.py — Helpers for fetching source files from the
Ngansen/Card-Database GitHub repo.

The Card-Database repo holds your hand-curated Excel + text files
(Korean Master DB, Chinese Master DB, English Pokémon Cards xlsx,
JP master spreadsheets, EX serial codes, Korean_Cards.txt, etc.). The
importers in `pi-setup/import_*_xlsx.py` and `import_kr_promos.py`
all fetch from this single repo so there's exactly one place to point
at when files move.

We use raw.githubusercontent.com over a sparse git clone because:

  * The Excel files are small (<5 MB each) so a single HTTP GET is
    faster than spinning up a clone-and-checkout dance.
  * No git binary required inside the orchestrator container.
  * The GitHub raw CDN is heavily cached so trade-show offline-first
    isn't impacted (these are pulled at sync time, not at query time).

If GitHub rate-limits us, set GITHUB_TOKEN in the env — these helpers
honour it automatically.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

log = logging.getLogger("unified.sources")

CARD_DB_REPO_OWNER = "Ngansen"
CARD_DB_REPO_NAME = "Card-Database"
CARD_DB_BRANCH = os.environ.get("CARD_DB_BRANCH", "main")

# All known files in the Card-Database repo, normalised to a stable
# logical name. Each importer references entries here by logical name
# rather than hard-coding URLs, so renames in the upstream repo only
# need fixing in one place.
KNOWN_SOURCES: dict[str, str] = {
    "korean_master_db":   "Korean_Pokemon_Global_Master_Database.xlsx",
    "chinese_master_db":  "Chinese_Pokemon_Global_Master_Database.xlsx",
    "english_all_cards":  "ALL English Pokémon Cards.xlsx",
    "english_v3_2":       "Pokemon TCG Spreadsheet V3.2.xlsx",
    "english_checklist":  "Pokémon TCG Checklist.xlsx",
    "english_sets":       "Pokemon Sets Collection.xlsx",
    "english_ex_codes":   "Pokemon TCG ex Serial Codes.xlsx",
    "japanese_ex_codes":  "Pokemon TCG ex Serial Codes - Japanese.xlsx",
    "japanese_master_v1": "Japanese Pokemon Card Master List 1996 - May 2016.xlsx",
    "japanese_master_v2": "Japanese Pokemon Card Spreadsheet 2.0 1996-Dec 2017.xlsx",
    "korean_promos_txt":  "Korean_Cards.txt",
    "pokemon_name_list":  "Pokemon Name List.txt",
    "pocket_tracker_v2":  "Copy of Main Pokemon TCG Pocket Tracker v2.xlsx",
}


def _raw_url(path: str) -> str:
    """Build a raw.githubusercontent.com URL, properly URL-encoding
    spaces and unicode (like 'é' in 'Pokémon')."""
    return (
        f"https://raw.githubusercontent.com/"
        f"{CARD_DB_REPO_OWNER}/{CARD_DB_REPO_NAME}/{CARD_DB_BRANCH}/"
        f"{quote(path)}"
    )


def _attached_assets_lookup(filename: str) -> Optional[Path]:
    """Look for a file in the local attached_assets/ directory.

    The user uploads source files into Replit's attached_assets/ before
    pushing them to the Card-Database repo, so for development /
    one-off testing we prefer the local copy if it exists. The filename
    in attached_assets/ may have a `_<digits>` timestamp suffix appended
    by Replit — we accept any prefix match.
    """
    candidates = []
    for root in ["attached_assets", "../attached_assets"]:
        d = Path(root)
        if not d.is_dir():
            continue
        # exact match first
        exact = d / filename
        if exact.exists():
            return exact
        # then prefix match (Replit suffixes uploads with _<digits>)
        stem = Path(filename).stem
        suffix = Path(filename).suffix
        for p in d.glob(f"{stem}*{suffix}"):
            candidates.append(p)
        # Replit also normalises spaces and special chars — try a
        # space-stripped version
        stem_norm = stem.replace(" ", "_").replace("é", "e").replace("ó", "o")
        for p in d.glob(f"{stem_norm}*{suffix}"):
            candidates.append(p)
    if candidates:
        # prefer the one with the most recent mtime
        return max(candidates, key=lambda p: p.stat().st_mtime)
    return None


def fetch_source(logical_name: str, dest_path: Path, *,
                 timeout: int = 60, prefer_local: bool = True) -> Path:
    """Fetch a known source file to dest_path.

    Tries (in order):
      1. attached_assets/ local copy (if prefer_local)
      2. raw.githubusercontent.com under Ngansen/Card-Database

    Returns the path of the fetched/copied file (== dest_path on
    success, OR the local attached_assets path if we used that
    directly without copying — caller must accept either).
    """
    if logical_name not in KNOWN_SOURCES:
        raise KeyError(f"Unknown source name {logical_name!r}; "
                       f"known: {sorted(KNOWN_SOURCES)}")
    filename = KNOWN_SOURCES[logical_name]

    if prefer_local:
        local = _attached_assets_lookup(filename)
        if local is not None:
            log.info("[sources] using local copy: %s", local)
            return local

    url = _raw_url(filename)
    headers = {"User-Agent": "HanryxVault-POS/1.0"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"token {token}"

    log.info("[sources] downloading %s → %s", url, dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.time()
    with requests.get(url, headers=headers, timeout=timeout, stream=True) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    f.write(chunk)

    log.info("[sources] downloaded %s (%d bytes) in %.1fs",
             logical_name, dest_path.stat().st_size, time.time() - started)
    return dest_path
