"""
import_kr_promos.py — Korean_Cards.txt parser (U6)

Parses the 718-line `Korean_Cards.txt` file from your Card-Database
repo into `ref_promo_provenance`. The file groups Korean promo cards
by acquisition source (Movie Promos / Theme Decks / Tournament &
League / Purchase Bonuses / Promo Packs / Event Participation /
Misc), which is gold for pricing decisions — a Korean Pikachu from a
movie premiere is worth more than the same card from a regular
theme deck.

File structure (best-effort parse — the file is human-curated text):

    === Movie Promos ===
    XY-P, 001/XY-P, 피카츄, Pikachu, Movie Premiere 2014
    XY-P, 002/XY-P, 라이추, Raichu, Movie Premiere 2014
    ...

    === Theme Decks ===
    ...

The parser is forgiving: each non-blank, non-header, non-comment line
is split on commas and the first 4-5 fields are mapped to set_label,
card_number, name_kr, name_en, notes. If a line doesn't fit this
shape, it's stored verbatim in the `raw` JSONB and `notes` field.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import psycopg2
import psycopg2.extras

from unified.schema import init_unified_schema
from unified.sources import fetch_source

log = logging.getLogger("import_kr_promos")

# Header lines look like:  === Movie Promos ===     or    ## Theme Decks
HEADER_RE = re.compile(r"^\s*[#=\-\*]+\s*(?P<cat>[^=#\-\*]+?)\s*[=#\-\*]*\s*$")
COMMENT_RE = re.compile(r"^\s*(//|#|;).*$")


def _looks_like_header(line: str) -> str | None:
    m = HEADER_RE.match(line)
    if not m:
        return None
    cat = m.group("cat").strip()
    # Skip pure separator lines like '====='
    if not cat:
        return None
    # Skip very long lines that are probably not headers
    if len(cat) > 60:
        return None
    return cat


def _parse_line(line: str) -> tuple[str, str, str, str, str]:
    """Return (set_label, card_number, name_kr, name_en, notes)."""
    parts = [p.strip() for p in line.split(",")]
    while len(parts) < 5:
        parts.append("")
    set_label, card_number, name_kr, name_en, *rest = parts
    notes = ", ".join(rest).strip()
    return set_label, card_number, name_kr, name_en, notes


def _ingest(db_conn, txt_path: Path) -> dict:
    cur = db_conn.cursor()
    cur.execute("DELETE FROM ref_promo_provenance")
    now = int(time.time())

    current_category = "Uncategorised"
    inserted = 0
    skipped = 0
    batch: list[tuple] = []
    BATCH_SIZE = 200
    insert_sql = """
        INSERT INTO ref_promo_provenance
          (source_category, set_label, card_number, name_kr, name_en,
           notes, raw, imported_at)
        VALUES %s
    """

    def flush():
        nonlocal inserted
        if not batch: return
        psycopg2.extras.execute_values(cur, insert_sql, batch, page_size=200)
        inserted += len(batch)
        batch.clear()

    with open(txt_path, "r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n").rstrip("\r")
            if not line.strip():
                continue
            if COMMENT_RE.match(line):
                continue
            header = _looks_like_header(line)
            if header is not None:
                current_category = header
                continue
            try:
                set_label, card_number, name_kr, name_en, notes = _parse_line(line)
            except Exception as e:
                log.debug("[kr-promos] skip line %r: %s", line, e)
                skipped += 1
                continue
            # Reject obviously-bad rows: need at least one of
            # name_kr or name_en or set_label to be non-empty.
            if not (name_kr or name_en or set_label):
                skipped += 1
                continue
            batch.append((
                current_category,
                set_label,
                card_number,
                name_kr,
                name_en,
                notes,
                json.dumps({"raw_line": line}, ensure_ascii=False),
                now,
            ))
            if len(batch) >= BATCH_SIZE: flush()
    flush()
    db_conn.commit()
    return {"inserted": inserted, "skipped": skipped}


def import_kr_promos(db_conn, *, force: bool = False) -> dict:
    init_unified_schema(db_conn)
    cur = db_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM ref_promo_provenance")
    pre = int(cur.fetchone()[0])
    if pre > 0 and not force:
        return {"skipped": True, "pre_count": pre}

    workdir = Path(tempfile.mkdtemp(prefix="kr-promos-"))
    try:
        path = fetch_source("korean_promos_txt", workdir / "korean_cards.txt")
        return _ingest(db_conn, path)
    finally:
        f = workdir / "korean_cards.txt"
        if f.exists():
            try: f.unlink()
            except Exception: pass
        try: workdir.rmdir()
        except Exception: pass


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
        print(json.dumps(import_kr_promos(conn, force=args.force), indent=2,
                         ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
