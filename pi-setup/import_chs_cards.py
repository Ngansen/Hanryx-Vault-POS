"""
Simplified-Chinese Pokémon TCG dataset importer
================================================

Pulls Ngansen/PTCG-CHS-Datasets — a single ~21 MB JSON file (`ptcg_chs_infos.json`)
plus 5.7 GB of card images. We sparse-checkout *only* the JSON; image URLs are
kept as raw.githubusercontent.com references so the Pi never has to host the
images itself.

Source repo:  https://github.com/Ngansen/PTCG-CHS-Datasets
Schema notes (from inspecting the JSON):
  * Top level has a "dict" object (enums) and one or more card-bearing arrays.
  * The importer is defensive: it walks the JSON tree and treats any object
    matching {"id": <int>, "name": <str>, "details": <obj>, "image": <str>}
    as a card record.
  * Each card has rich `details` with cardName, collectionNumber ("008/207"),
    rarityText, hp, abilityItemList, illustratorName, commodityList, etc.

Table cards_chs columns mirror cards_kr where possible.
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

log = logging.getLogger("import_chs_cards")

REPO_URL  = "https://github.com/Ngansen/PTCG-CHS-Datasets.git"
JSON_PATH = "ptcg_chs_infos.json"
IMG_RAW_BASE = "https://raw.githubusercontent.com/Ngansen/PTCG-CHS-Datasets/main"


# ─── Public API ──────────────────────────────────────────────────────────────

def ensure_chs_cards_table(db_conn) -> None:
    cur = db_conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cards_chs (
            card_id            BIGINT NOT NULL,
            commodity_code     TEXT NOT NULL DEFAULT '',
            collection_number  TEXT NOT NULL DEFAULT '',
            commodity_name     TEXT NOT NULL DEFAULT '',
            name_chs           TEXT NOT NULL,
            yoren_code         TEXT NOT NULL DEFAULT '',
            card_type          TEXT NOT NULL DEFAULT '',
            card_type_text     TEXT NOT NULL DEFAULT '',
            rarity             TEXT NOT NULL DEFAULT '',
            rarity_text        TEXT NOT NULL DEFAULT '',
            regulation_mark    TEXT NOT NULL DEFAULT '',
            hp                 INTEGER,
            attribute          TEXT NOT NULL DEFAULT '',
            evolve_text        TEXT NOT NULL DEFAULT '',
            pokedex_code       TEXT NOT NULL DEFAULT '',
            pokedex_text       TEXT NOT NULL DEFAULT '',
            illustrators       TEXT NOT NULL DEFAULT '',
            image_url          TEXT NOT NULL DEFAULT '',
            hash               TEXT NOT NULL DEFAULT '',
            raw_json           JSONB,
            imported_at        BIGINT NOT NULL DEFAULT 0,
            PRIMARY KEY (card_id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cards_chs_name        ON cards_chs (name_chs)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cards_chs_commodity   ON cards_chs (commodity_code)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cards_chs_collection  ON cards_chs (collection_number)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cards_chs_yoren       ON cards_chs (yoren_code)")
    db_conn.commit()


def chs_cards_count(db_conn) -> int:
    cur = db_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM cards_chs")
    return int(cur.fetchone()[0])


def import_chinese_cards(db_conn, *, force: bool = False) -> dict:
    """
    Returns: {"imported": N, "found": N, "errors": N, "skipped": bool}
    """
    ensure_chs_cards_table(db_conn)

    if not force and chs_cards_count(db_conn) > 0:
        log.info("[chs-import] cards_chs already populated — skipping")
        return {"imported": 0, "found": 0, "errors": 0, "skipped": True}

    workdir = Path(tempfile.mkdtemp(prefix="ptcg-chs-"))
    try:
        log.info("[chs-import] cloning %s (sparse, JSON only) → %s", REPO_URL, workdir)
        subprocess.run(
            ["git", "clone", "--depth", "1", "--filter=blob:none",
             "--no-checkout", REPO_URL, str(workdir)],
            check=True, capture_output=True, timeout=180,
        )
        # Sparse-checkout *only* the JSON (skips the 5.7 GB of images)
        subprocess.run(
            ["git", "-C", str(workdir), "sparse-checkout", "set", JSON_PATH],
            check=True, capture_output=True, timeout=30,
        )
        subprocess.run(
            ["git", "-C", str(workdir), "checkout"],
            check=True, capture_output=True, timeout=120,
        )

        json_file = workdir / JSON_PATH
        if not json_file.exists():
            raise RuntimeError(f"{JSON_PATH} missing after sparse checkout")

        log.info("[chs-import] parsing %s (%.1f MB)",
                 json_file.name, json_file.stat().st_size / 1e6)
        data = json.loads(json_file.read_text(encoding="utf-8"))

        if force:
            cur = db_conn.cursor()
            cur.execute("TRUNCATE cards_chs")
            db_conn.commit()
            log.info("[chs-import] TRUNCATEd cards_chs (force=True)")

        imported = found = errors = 0
        batch: list[tuple] = []
        BATCH_SIZE = 500
        seen_ids: set[int] = set()

        for record in _walk_cards(data):
            found += 1
            try:
                row = _build_row(record)
                if row is None:
                    continue
                if row[0] in seen_ids:
                    continue
                seen_ids.add(row[0])
                batch.append(row)
                imported += 1
                if len(batch) >= BATCH_SIZE:
                    _flush_batch(db_conn, batch)
                    batch.clear()
            except Exception as exc:
                errors += 1
                if errors < 10:
                    log.warning("[chs-import] row error: %s", exc)

        if batch:
            _flush_batch(db_conn, batch)
        db_conn.commit()

        log.info("[chs-import] done — found=%d imported=%d errors=%d",
                 found, imported, errors)
        return {"imported": imported, "found": found, "errors": errors, "skipped": False}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ─── Internal helpers ────────────────────────────────────────────────────────

def _walk_cards(node) -> Iterable[dict]:
    """Recursively yield any dict that looks like a card record."""
    if isinstance(node, dict):
        if _looks_like_card(node):
            yield node
            return  # cards don't contain other cards
        for v in node.values():
            yield from _walk_cards(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_cards(v)


def _looks_like_card(d: dict) -> bool:
    return (
        isinstance(d.get("id"), int)
        and isinstance(d.get("details"), dict)
        and ("name" in d or "cardName" in d.get("details", {}))
    )


def _build_row(record: dict) -> tuple | None:
    details = record.get("details") or {}
    card_id = record.get("id")
    if not isinstance(card_id, int):
        return None

    name = (details.get("cardName") or record.get("name") or "").strip()
    if not name:
        return None

    commodity_code = (record.get("commodityCode")
                      or details.get("commodityCode") or "").strip()
    image = (record.get("image") or "").strip()
    image_url = (f"{IMG_RAW_BASE}/{image}" if image and not image.startswith("http")
                 else image)

    commodity_list = details.get("commodityList") or []
    commodity_name = ""
    if commodity_list and isinstance(commodity_list[0], dict):
        commodity_name = (commodity_list[0].get("commodityName") or "").strip()

    illustrators = details.get("illustratorName") or []
    illu_str = ", ".join(i for i in illustrators if isinstance(i, str))[:200]

    return (
        card_id,
        commodity_code,
        (details.get("collectionNumber") or "").strip(),
        commodity_name,
        name,
        (details.get("yorenCode") or record.get("yorenCode") or "").strip(),
        (details.get("cardType") or "").strip(),
        (details.get("cardTypeText") or "").strip(),
        (details.get("rarity") or "").strip(),
        (details.get("rarityText") or "").strip(),
        (details.get("regulationMarkText") or "").strip(),
        _safe_int(details.get("hp")),
        (details.get("attribute") or "").strip(),
        (details.get("evolveText") or "").strip(),
        (details.get("pokedexCode") or "").strip(),
        (details.get("pokedexText") or "").strip()[:600],
        illu_str,
        image_url,
        (record.get("hash") or "").strip(),
        json.dumps(record, ensure_ascii=False),
        int(time.time() * 1000),
    )


def _flush_batch(db_conn, batch: list[tuple]) -> None:
    if not batch:
        return
    cur = db_conn.cursor()
    cur.executemany("""
        INSERT INTO cards_chs (
            card_id, commodity_code, collection_number, commodity_name,
            name_chs, yoren_code, card_type, card_type_text,
            rarity, rarity_text, regulation_mark, hp, attribute,
            evolve_text, pokedex_code, pokedex_text, illustrators,
            image_url, hash, raw_json, imported_at
        )
        VALUES (
            %s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s,%s,
            %s,%s,%s,%s, %s,%s, %s::jsonb, %s
        )
        ON CONFLICT (card_id) DO UPDATE SET
            commodity_code    = EXCLUDED.commodity_code,
            collection_number = EXCLUDED.collection_number,
            commodity_name    = EXCLUDED.commodity_name,
            name_chs          = EXCLUDED.name_chs,
            yoren_code        = EXCLUDED.yoren_code,
            card_type         = EXCLUDED.card_type,
            card_type_text    = EXCLUDED.card_type_text,
            rarity            = EXCLUDED.rarity,
            rarity_text       = EXCLUDED.rarity_text,
            regulation_mark   = EXCLUDED.regulation_mark,
            hp                = EXCLUDED.hp,
            attribute         = EXCLUDED.attribute,
            evolve_text       = EXCLUDED.evolve_text,
            pokedex_code      = EXCLUDED.pokedex_code,
            pokedex_text      = EXCLUDED.pokedex_text,
            illustrators      = EXCLUDED.illustrators,
            image_url         = EXCLUDED.image_url,
            hash              = EXCLUDED.hash,
            raw_json          = EXCLUDED.raw_json,
            imported_at       = EXCLUDED.imported_at
    """, batch)


def _safe_int(value) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


# ─── CLI entry point ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Import Simplified-Chinese Pokémon TCG cards")
    parser.add_argument("--force", action="store_true",
                        help="Truncate and re-import even if table is populated")
    args = parser.parse_args()

    import psycopg2
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL env var is required")
    conn = psycopg2.connect(dsn)
    try:
        result = import_chinese_cards(conn, force=args.force)
        print(json.dumps(result, indent=2))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Targeted backfill hook for cluster_backfill.
# ---------------------------------------------------------------------------
def backfill_codes(db_conn, set_codes: list[str]) -> dict:
    """Re-run the Chinese import (UPSERT-safe) and record it in source_runs."""
    try:
        import source_state
        run_id = source_state.begin_run(
            db_conn, source="chs_cards",
            notes=("backfill: " + ",".join(set_codes[:10])
                   + ("…" if len(set_codes) > 10 else "")),
        )
    except Exception:
        run_id = None
    try:
        before = chs_cards_count(db_conn)
        import_chinese_cards(db_conn, force=False)
        after = chs_cards_count(db_conn)
        added = max(0, after - before)
        if run_id is not None:
            source_state.end_run(db_conn, run_id, ok=True,
                                 rows_seen=after, rows_inserted=added)
        return {"ok": True, "added": added, "total": after,
                "set_codes": set_codes}
    except Exception as exc:
        if run_id is not None:
            try: source_state.end_run(db_conn, run_id, ok=False,
                                      errors=1, notes=str(exc)[:300])
            except Exception: pass
        return {"ok": False, "error": str(exc)}
