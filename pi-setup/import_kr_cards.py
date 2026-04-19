"""
Korean Pokémon TCG dataset importer
====================================

Pulls the Ngansen/ptcg-kr-db repository (parsed data from the official Korean
Pokémon Card site) into the local Postgres database so card lookups work for
Korean cards even when offline.

Source repo:  https://github.com/Ngansen/ptcg-kr-db
Dataset size: ~14 MB JSON (no images cloned — imageUrl fields are kept as
              external CDN references to pokemonkorea.co.kr).

The importer is idempotent:
  * On first call (table empty), it git-clones the repo and inserts every
    version of every card.
  * On subsequent calls it does nothing unless force=True is passed.

Schema (created by server.init_db, but also created here as a safety net):
    cards_kr (
      card_id        TEXT,        -- e.g. "bw4-001"  (primary key part 1)
      prod_code      TEXT,        -- e.g. "bw4"      (primary key part 2)
      card_number    TEXT,        -- e.g. "001"      (primary key part 3)
      set_name       TEXT,        -- "BW 확장팩 제4탄 «다크러시»"
      name_kr        TEXT,        -- "이상해씨"  (Hangul card name)
      pokedex_no     INTEGER,     -- e.g. 1   (NULL for trainers/energy)
      supertype      TEXT,        -- 포켓몬 / 트레이너즈 / 에너지
      subtype        TEXT,        -- 기본 / 1진화 / 서포트 / ...
      hp             INTEGER,
      type_kr        TEXT,        -- "(풀)" / "(불꽃)" / ...
      rarity         TEXT,        -- C / U / R / RR / SR / HR / UR / SAR
      artist         TEXT,
      prod_number    TEXT,
      image_url      TEXT,
      flavor_text    TEXT,
      raw_json       JSONB,
      imported_at    BIGINT,
      PRIMARY KEY (card_id, prod_code, card_number)
    )
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Iterable

log = logging.getLogger("import_kr_cards")

REPO_URL = "https://github.com/Ngansen/ptcg-kr-db.git"
CARD_SUBDIRS = ("pokemon", "trainers", "energy")


# ─── Public API ──────────────────────────────────────────────────────────────

def ensure_kr_cards_table(db_conn) -> None:
    """Create the cards_kr table if it doesn't exist (safe to call repeatedly)."""
    cur = db_conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cards_kr (
            card_id        TEXT NOT NULL,
            prod_code      TEXT NOT NULL DEFAULT '',
            card_number    TEXT NOT NULL DEFAULT '',
            set_name       TEXT NOT NULL DEFAULT '',
            name_kr        TEXT NOT NULL,
            pokedex_no     INTEGER,
            supertype      TEXT NOT NULL DEFAULT '',
            subtype        TEXT NOT NULL DEFAULT '',
            hp             INTEGER,
            type_kr        TEXT NOT NULL DEFAULT '',
            rarity         TEXT NOT NULL DEFAULT '',
            artist         TEXT NOT NULL DEFAULT '',
            prod_number    TEXT NOT NULL DEFAULT '',
            image_url      TEXT NOT NULL DEFAULT '',
            flavor_text    TEXT NOT NULL DEFAULT '',
            raw_json       JSONB,
            imported_at    BIGINT NOT NULL DEFAULT 0,
            PRIMARY KEY (card_id, prod_code, card_number)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cards_kr_name_trgm "
                "ON cards_kr USING gin (name_kr gin_trgm_ops)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cards_kr_pokedex "
                "ON cards_kr (pokedex_no)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cards_kr_setnum "
                "ON cards_kr (prod_code, card_number)")
    db_conn.commit()


def kr_cards_count(db_conn) -> int:
    cur = db_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM cards_kr")
    return int(cur.fetchone()[0])


def import_korean_cards(db_conn, *, force: bool = False) -> dict:
    """
    Import all Korean cards from ptcg-kr-db into Postgres.

    Returns: {"imported": N, "files_parsed": N, "errors": N, "skipped": bool}
    """
    ensure_kr_cards_table(db_conn)

    if not force and kr_cards_count(db_conn) > 0:
        log.info("[kr-import] cards_kr already populated — skipping (force=False)")
        return {"imported": 0, "files_parsed": 0, "errors": 0, "skipped": True}

    workdir = Path(tempfile.mkdtemp(prefix="ptcg-kr-"))
    try:
        log.info("[kr-import] cloning %s → %s", REPO_URL, workdir)
        subprocess.run(
            ["git", "clone", "--depth", "1", "--filter=blob:none",
             "--no-checkout", REPO_URL, str(workdir)],
            check=True, capture_output=True, timeout=120,
        )
        # Sparse-checkout only card_data/ — we don't want the 14 MB of images
        subprocess.run(
            ["git", "-C", str(workdir), "sparse-checkout", "set", "card_data"],
            check=True, capture_output=True, timeout=30,
        )
        subprocess.run(
            ["git", "-C", str(workdir), "checkout"],
            check=True, capture_output=True, timeout=60,
        )

        card_root = workdir / "card_data"
        if not card_root.is_dir():
            raise RuntimeError(f"card_data/ missing in cloned repo: {card_root}")

        if force:
            cur = db_conn.cursor()
            cur.execute("TRUNCATE cards_kr")
            db_conn.commit()
            log.info("[kr-import] TRUNCATEd cards_kr (force=True)")

        imported = files_parsed = errors = 0
        batch: list[tuple] = []
        BATCH_SIZE = 500

        for sub in CARD_SUBDIRS:
            sub_path = card_root / sub
            if not sub_path.exists():
                log.warning("[kr-import] missing subdir: %s", sub_path)
                continue
            for json_path in sub_path.rglob("*.json"):
                files_parsed += 1
                try:
                    rows = list(_parse_card_file(json_path))
                    batch.extend(rows)
                    imported += len(rows)
                    if len(batch) >= BATCH_SIZE:
                        _flush_batch(db_conn, batch)
                        batch.clear()
                except Exception as exc:
                    errors += 1
                    log.warning("[kr-import] parse error %s: %s", json_path.name, exc)

        if batch:
            _flush_batch(db_conn, batch)
        db_conn.commit()

        log.info("[kr-import] done — files=%d imported=%d errors=%d",
                 files_parsed, imported, errors)
        return {
            "imported": imported,
            "files_parsed": files_parsed,
            "errors": errors,
            "skipped": False,
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ─── Internal helpers ────────────────────────────────────────────────────────

def _parse_card_file(path: Path) -> Iterable[tuple]:
    """
    A single JSON file in ptcg-kr-db is an *array* of cards (different reprints
    sharing the same Korean name).  Each card has a `version_infos` list — we
    expand each version into its own row so set+number lookups work.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raw = [raw]

    now_ms = int(time.time() * 1000)

    for card in raw:
        if not isinstance(card, dict):
            continue
        card_id   = (card.get("id") or "").strip()
        name_kr   = (card.get("name") or "").strip()
        if not card_id or not name_kr:
            continue
        supertype = (card.get("supertype") or "").strip()
        subtype   = ", ".join(card.get("subtypes") or [])[:120]
        hp        = _safe_int(card.get("hp"))
        type_kr   = (card.get("type") or "").strip()
        flavor    = (card.get("flavorText") or "").strip()
        pokedex_no = None
        pokemons   = card.get("pokemons") or []
        if pokemons and isinstance(pokemons[0], dict):
            pokedex_no = _safe_int(pokemons[0].get("pokedexNumber"))

        versions = card.get("version_infos") or [{}]
        for v in versions:
            if not isinstance(v, dict):
                continue
            prod_code   = (v.get("prodCode")   or "").strip()
            number      = (v.get("number")     or "").strip().lstrip("0") or "0"
            prod_number = (v.get("prodNumber") or "").strip()
            set_name    = (v.get("prodName")   or "").strip()
            artist      = (v.get("artist")     or "").strip()
            rarity      = (v.get("rarity")     or "").strip()
            image_url   = (v.get("cardImgURL") or "").strip()

            yield (
                card_id, prod_code, number,
                set_name, name_kr, pokedex_no, supertype, subtype, hp,
                type_kr, rarity, artist, prod_number, image_url, flavor,
                json.dumps(card, ensure_ascii=False), now_ms,
            )


def _flush_batch(db_conn, batch: list[tuple]) -> None:
    if not batch:
        return
    cur = db_conn.cursor()
    cur.executemany("""
        INSERT INTO cards_kr (
            card_id, prod_code, card_number,
            set_name, name_kr, pokedex_no, supertype, subtype, hp,
            type_kr, rarity, artist, prod_number, image_url, flavor_text,
            raw_json, imported_at
        )
        VALUES (%s,%s,%s, %s,%s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s, %s::jsonb, %s)
        ON CONFLICT (card_id, prod_code, card_number) DO UPDATE SET
            set_name    = EXCLUDED.set_name,
            name_kr     = EXCLUDED.name_kr,
            pokedex_no  = EXCLUDED.pokedex_no,
            supertype   = EXCLUDED.supertype,
            subtype     = EXCLUDED.subtype,
            hp          = EXCLUDED.hp,
            type_kr     = EXCLUDED.type_kr,
            rarity      = EXCLUDED.rarity,
            artist      = EXCLUDED.artist,
            prod_number = EXCLUDED.prod_number,
            image_url   = EXCLUDED.image_url,
            flavor_text = EXCLUDED.flavor_text,
            raw_json    = EXCLUDED.raw_json,
            imported_at = EXCLUDED.imported_at
    """, batch)


def _safe_int(value) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


# ─── Korean lookup (used by server.py /card/lookup) ──────────────────────────

_HANGUL_RANGES = ((0xAC00, 0xD7A3), (0x1100, 0x11FF), (0x3130, 0x318F))


def has_hangul(s: str) -> bool:
    """Return True if the string contains any Hangul character."""
    if not s:
        return False
    for ch in s:
        cp = ord(ch)
        for lo, hi in _HANGUL_RANGES:
            if lo <= cp <= hi:
                return True
    return False


def lookup_kr(db_conn, *, name: str = "", set_code: str = "",
              card_num: str = "", pokedex_no: int | None = None,
              limit: int = 10) -> list[dict]:
    """
    Look up Korean cards by Hangul name, set+number, or Pokédex number.
    Returns a list of dicts shaped to match the rest of /card/lookup output.
    """
    cur = db_conn.cursor()
    where: list[str] = []
    params: list = []

    if name:
        where.append("name_kr ILIKE %s")
        params.append(f"%{name}%")
    if set_code:
        where.append("UPPER(prod_code) = %s")
        params.append(set_code.upper())
    if card_num:
        where.append("card_number = %s")
        params.append(card_num.lstrip("0") or "0")
    if pokedex_no is not None:
        where.append("pokedex_no = %s")
        params.append(int(pokedex_no))

    if not where:
        return []

    sql = (
        "SELECT card_id, prod_code, card_number, set_name, name_kr, "
        "       pokedex_no, supertype, rarity, artist, image_url, type_kr "
        "FROM cards_kr "
        "WHERE " + " AND ".join(where) + " "
        "ORDER BY prod_code DESC, card_number ASC "
        "LIMIT %s"
    )
    params.append(int(limit))
    cur.execute(sql, params)

    out: list[dict] = []
    for row in cur.fetchall():
        out.append({
            "id":          f"{row[1]}-{row[2]}" if row[1] else row[0],
            "tcg_id":      row[0],
            "setCode":     (row[1] or "").upper(),
            "cardNumber":  row[2],
            "setName":     row[3],
            "name":        row[4],
            "name_kr":     row[4],
            "pokedex_no":  row[5],
            "supertype":   row[6],
            "rarity":      row[7],
            "artist":      row[8],
            "imageUrl":    row[9],
            "image_url":   row[9],
            "type":        row[10],
            "language":    "Korean",
            "source":      "ptcg-kr-db",
        })
    return out


# ─── CLI entry point ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Import Korean Pokémon TCG cards")
    parser.add_argument("--force", action="store_true",
                        help="Truncate and re-import even if table is populated")
    args = parser.parse_args()

    import psycopg2
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL env var is required")
    conn = psycopg2.connect(dsn)
    try:
        result = import_korean_cards(conn, force=args.force)
        print(json.dumps(result, indent=2))
    finally:
        conn.close()
