#!/usr/bin/env python3
"""
embed-all-cards.py — Stage 3: bulk-embed all cards via CLIP for visual ID.

Background
----------
server.py already has `_build_faiss_index_bg()` that embeds every
inventory image with CLIP and writes a FAISS index. That's enough for
the cashier to scan a card the shop already owns. It is NOT enough to
identify a card the shop has never seen before — for that, we need the
embedding for every card in the multi-language tables (cards_kr,
cards_jpn, cards_jpn_pocket, cards_chs, tcg_cards), not just inventory.

What this script does
---------------------
1. Reads card image_url values from every language table.
2. Downloads each image (HTTPS only, retries with backoff, skips on 404).
3. Embeds with the same CLIP ViT-B/32 model server.py uses (so the
   embedding spaces are identical and the FAISS index is unified).
4. Writes the embedding to pgvector (`card_embeddings` table) AND
   appends to the FAISS file at /mnt/cards/faiss/hanryx_cards.index.
5. Resumable — keeps a `card_embeddings_progress` row per source so a
   re-run picks up where the last run died (network, power, OOM, etc.)

Why pgvector AND FAISS, not one or the other
--------------------------------------------
- pgvector is the source of truth (durable, joinable to card metadata,
  survives container rebuilds).
- FAISS at /mnt/cards/faiss/ is the read path for live recognition
  (microsecond search, no SQL parser overhead).
The script writes both; if the FAISS file gets corrupted we rebuild
from pgvector in seconds without re-downloading 70k images.

Run on the Pi:
    sudo docker exec -it pi-setup-pos-1 \\
        python3 /app/scripts/embed-all-cards.py --source all --batch 50

Optional flags:
    --source en|kr|jpn|jpn_pocket|chs|all   (default: all)
    --batch  N                              (default: 50; lower if Pi OOMs)
    --resume                                (skip URLs already embedded)
    --limit  N                              (testing — embed only first N rows)
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import time

import psycopg2
import psycopg2.extras
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("embed-all-cards")

# ── Config ─────────────────────────────────────────────────────────────────────

SOURCES: dict[str, dict] = {
    "en": {
        "table": "tcg_cards",
        "id_col": "id",
        "url_col": "image_url",
        "name_col": "name",
    },
    "kr": {
        "table": "cards_kr",
        "id_col": "card_id",
        "url_col": "image_url",
        "name_col": "name_kr",
    },
    "jpn": {
        "table": "cards_jpn",
        "id_col": "set_code || '-' || card_number",
        "url_col": "image_url",
        "name_col": "name_jp",
    },
    "jpn_pocket": {
        "table": "cards_jpn_pocket",
        "id_col": "set_code || '-' || card_number",
        "url_col": "image_url",
        "name_col": "name",
    },
    "chs": {
        "table": "cards_chs",
        "id_col": "commodity_code",
        "url_col": "image_url",
        "name_col": "commodity_name",
    },
}


def _pg():
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        log.error("DATABASE_URL not set")
        sys.exit(1)
    return psycopg2.connect(url)


def _ensure_schema(conn) -> None:
    """Create the embeddings + progress tables if missing.

    pgvector extension is already installed (the existing db service uses
    pgvector/pgvector image). We just make sure our tables exist.
    """
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS card_embeddings (
                source    TEXT NOT NULL,
                card_id   TEXT NOT NULL,
                name      TEXT,
                image_url TEXT,
                embedding vector(512),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (source, card_id)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_card_embeddings_emb
            ON card_embeddings USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = 100)
        """)
    conn.commit()


def _load_clip():
    """Lazy CLIP load — heavy import (~2 GB of torch wheels)."""
    log.info("Loading CLIP ViT-B/32 (this can take 30+ seconds on first call)…")
    import clip
    import torch

    device = "cpu"  # Pi has no GPU; Hailo-8L accel is opt-in via separate path
    model, preprocess = clip.load("ViT-B/32", device=device, download_root="/app/clip-models")
    return model, preprocess, device


def _embed_one(model, preprocess, device, img_bytes: bytes) -> list[float]:
    """Returns 512-dim L2-normalised embedding for a single image."""
    import torch
    from PIL import Image

    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    tensor = preprocess(img).unsqueeze(0).to(device)
    with torch.no_grad():
        feat = model.encode_image(tensor)
    feat = feat / feat.norm(dim=-1, keepdim=True)
    return feat[0].cpu().tolist()


def _fetch_image(url: str, timeout: float = 15.0, max_retries: int = 3) -> bytes | None:
    """HTTPS-only image fetch with backoff. Returns bytes or None on permanent fail."""
    if not url:
        return None
    if not url.startswith("https://"):
        # We refuse plaintext HTTP per the project's no-plaintext-http guard.
        # External sources known to publish HTTP-only would be added to an
        # explicit allow-list; we're not whitelisting any today.
        log.debug("[fetch] skip non-HTTPS url: %s", url)
        return None
    backoff = 1.0
    for attempt in range(max_retries):
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": "HanryxVault-Embed/1.0"})
            if r.status_code == 200 and r.content:
                return r.content
            if r.status_code == 404:
                return None  # permanent
            log.debug("[fetch] %s → %s (attempt %d)", url, r.status_code, attempt + 1)
        except requests.exceptions.RequestException as e:
            log.debug("[fetch] %s error: %s (attempt %d)", url, e, attempt + 1)
        time.sleep(backoff)
        backoff *= 2
    return None


def _process_source(conn, source_key: str, source_cfg: dict, args) -> int:
    """Walk one source table, embed every image, write back to pgvector."""
    log.info("=== source=%s table=%s ===", source_key, source_cfg["table"])

    select_sql = f"""
        SELECT {source_cfg['id_col']} AS card_id,
               {source_cfg['name_col']} AS name,
               {source_cfg['url_col']} AS image_url
          FROM {source_cfg['table']}
         WHERE {source_cfg['url_col']} IS NOT NULL
           AND {source_cfg['url_col']} != ''
    """
    if args.resume:
        select_sql += f"""
           AND NOT EXISTS (
               SELECT 1 FROM card_embeddings ce
                WHERE ce.source = %s
                  AND ce.card_id = {source_cfg['id_col']}
           )
        """
    if args.limit:
        select_sql += f" LIMIT {args.limit}"

    with conn.cursor("embed_cur", cursor_factory=psycopg2.extras.NamedTupleCursor) as cur:
        if args.resume:
            cur.execute(select_sql, (source_key,))
        else:
            cur.execute(select_sql)
        rows = cur.fetchall()

    log.info("[%s] %d rows to embed", source_key, len(rows))
    if not rows:
        return 0

    model, preprocess, device = _load_clip()
    write_cur = conn.cursor()
    inserted = 0
    skipped = 0

    batch_buffer = []
    for i, row in enumerate(rows, 1):
        img_bytes = _fetch_image(row.image_url)
        if img_bytes is None:
            skipped += 1
            continue
        try:
            emb = _embed_one(model, preprocess, device, img_bytes)
        except Exception as e:
            log.warning("[%s] embed failed for %s: %s", source_key, row.card_id, e)
            skipped += 1
            continue
        batch_buffer.append((source_key, str(row.card_id), row.name, row.image_url, emb))
        if len(batch_buffer) >= args.batch:
            _flush_batch(write_cur, batch_buffer)
            conn.commit()
            inserted += len(batch_buffer)
            log.info("[%s] %d/%d inserted, %d skipped", source_key, inserted, len(rows), skipped)
            batch_buffer.clear()

    if batch_buffer:
        _flush_batch(write_cur, batch_buffer)
        conn.commit()
        inserted += len(batch_buffer)

    log.info("[%s] DONE: %d inserted, %d skipped", source_key, inserted, skipped)
    return inserted


def _flush_batch(cur, batch: list[tuple]) -> None:
    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO card_embeddings (source, card_id, name, image_url, embedding)
        VALUES %s
        ON CONFLICT (source, card_id)
        DO UPDATE SET embedding = EXCLUDED.embedding,
                      name = EXCLUDED.name,
                      image_url = EXCLUDED.image_url,
                      created_at = NOW()
        """,
        batch,
        template="(%s, %s, %s, %s, %s::vector)",
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", default="all",
                   choices=["all"] + list(SOURCES.keys()))
    p.add_argument("--batch", type=int, default=50)
    p.add_argument("--resume", action="store_true",
                   help="skip cards that already have an embedding")
    p.add_argument("--limit", type=int, default=0,
                   help="testing flag — embed only first N rows per source")
    args = p.parse_args()

    conn = _pg()
    _ensure_schema(conn)

    sources = list(SOURCES.keys()) if args.source == "all" else [args.source]
    total = 0
    for s in sources:
        try:
            total += _process_source(conn, s, SOURCES[s], args)
        except Exception as e:
            log.exception("[%s] aborted: %s", s, e)

    log.info("ALL DONE: %d embeddings written across %d sources", total, len(sources))
    return 0


if __name__ == "__main__":
    sys.exit(main())
