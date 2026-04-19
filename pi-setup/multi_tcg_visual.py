"""
Visual card identification for the multi-TCG catalog.

Re-uses the same CLIP ViT-B/32 model already loaded by server.py for the
Pokémon catalog, but builds a SEPARATE FAISS index per game so MTG art
isn't matched against a One Piece query.

Index files live at:
    /tmp/hanryx_multi_<game>.index
    /tmp/hanryx_multi_<game>.ids.json

Indexes are built lazily in a background thread the first time the game
is queried (or on demand via /admin/multi/<game>/visual/rebuild).

Public API
----------
    build_index(db, game, *, force=False) → dict
    search(db, game, image_pil, *, top_k=5) → list[card-dict]
    status(game) → dict
"""
from __future__ import annotations

import io
import json
import logging
import os
import threading
import time

import requests

log = logging.getLogger("multi_tcg_visual")

_INDEX_DIR = "/tmp"
_locks: dict[str, threading.Lock] = {}
_indexes: dict[str, object] = {}     # game → faiss.Index
_ids_cache: dict[str, list] = {}     # game → [card_id, …]


def _idx_path(game: str) -> str:
    return os.path.join(_INDEX_DIR, f"hanryx_multi_{game}.index")


def _ids_path(game: str) -> str:
    return os.path.join(_INDEX_DIR, f"hanryx_multi_{game}.ids.json")


def _lock_for(game: str) -> threading.Lock:
    if game not in _locks:
        _locks[game] = threading.Lock()
    return _locks[game]


def _embed(img):
    """Borrow the CLIP encoder from server.py — it's already loaded."""
    try:
        import server  # type: ignore
        return server._clip_embed(img)
    except Exception as exc:
        log.warning("[multi-visual] clip embed unavailable: %s", exc)
        return None


def status(game: str) -> dict:
    p = _idx_path(game); ip = _ids_path(game)
    out = {"game": game, "built": os.path.exists(p) and os.path.exists(ip)}
    if out["built"]:
        try:
            out["card_count"] = len(json.load(open(ip)))
            out["index_size_mb"] = round(os.path.getsize(p) / (1024 * 1024), 2)
            out["built_at"] = int(os.path.getmtime(p))
        except Exception:
            pass
    return out


def _load_index(game: str):
    """Open the on-disk FAISS index for a game (memoised)."""
    if game in _indexes:
        return _indexes[game], _ids_cache[game]
    p = _idx_path(game); ip = _ids_path(game)
    if not (os.path.exists(p) and os.path.exists(ip)):
        return None, None
    try:
        import faiss
        idx = faiss.read_index(p)
        ids = json.load(open(ip))
        _indexes[game] = idx; _ids_cache[game] = ids
        return idx, ids
    except Exception as exc:
        log.warning("[multi-visual:%s] failed to load index: %s", game, exc)
        return None, None


def build_index(db, game: str, *, force: bool = False, max_cards: int = 5000) -> dict:
    """Embed every card image for `game` into a FAISS index.  Idempotent."""
    game = (game or "").lower().strip()
    if game not in ("mtg", "onepiece", "lorcana", "dbs"):
        return {"error": f"unknown game: {game}"}

    with _lock_for(game):
        if not force and os.path.exists(_idx_path(game)):
            return {"game": game, "skipped": True, **status(game)}

        try:
            import numpy as np
            import faiss
        except ImportError as exc:
            return {"error": f"faiss/numpy missing: {exc}"}

        rows = db.execute(
            "SELECT card_id, image_url FROM cards_multi "
            "WHERE game = %s AND image_url <> '' "
            "ORDER BY imported_at DESC LIMIT %s",
            (game, max_cards),
        ).fetchall()
        if not rows:
            return {"game": game, "error": "no cards with images yet"}

        log.info("[multi-visual:%s] embedding %d card images …", game, len(rows))
        vecs: list = []; ids: list = []
        from PIL import Image
        t0 = time.time()
        for cid, url in rows:
            try:
                r = requests.get(url, timeout=8,
                                 headers={"User-Agent": "HanryxVault-POS/1.0"})
                if r.status_code != 200:
                    continue
                img = Image.open(io.BytesIO(r.content)).convert("RGB")
                v = _embed(img)
                if v is None:
                    continue
                vecs.append(v[0]); ids.append(cid)
            except Exception as e:
                log.debug("[multi-visual:%s] skip %s: %s", game, cid, e)

        if not vecs:
            return {"game": game, "error": "no embeddings produced"}

        mat = np.stack(vecs).astype("float32")
        idx = faiss.IndexFlatIP(mat.shape[1])
        idx.add(mat)
        faiss.write_index(idx, _idx_path(game))
        with open(_ids_path(game), "w") as f:
            json.dump(ids, f)
        _indexes[game] = idx; _ids_cache[game] = ids
        elapsed = round(time.time() - t0, 1)
        log.info("[multi-visual:%s] built index dim=%d cards=%d in %ss",
                 game, mat.shape[1], len(ids), elapsed)
        return {"game": game, "built": True, "card_count": len(ids),
                "elapsed_s": elapsed}


def search(db, game: str, img, *, top_k: int = 5) -> list[dict]:
    """Reverse-image-search cards for a given game.  Returns full card rows."""
    game = (game or "").lower().strip()
    idx, ids = _load_index(game)
    if idx is None:
        return []
    v = _embed(img)
    if v is None:
        return []
    try:
        scores, indexes = idx.search(v.astype("float32"), top_k)
    except Exception as exc:
        log.warning("[multi-visual:%s] search failed: %s", game, exc)
        return []

    matches = []
    for rank, (i, s) in enumerate(zip(indexes[0], scores[0])):
        if i < 0 or i >= len(ids):
            continue
        cid = ids[i]
        row = db.execute(
            "SELECT card_id, name, set_code, set_name, card_number, "
            "rarity, image_url, language, price_usd "
            "FROM cards_multi WHERE game=%s AND card_id=%s",
            (game, cid),
        ).fetchone()
        if not row:
            continue
        matches.append({
            "id": row[0], "name": row[1],
            "setCode": row[2], "setName": row[3],
            "cardNumber": row[4], "rarity": row[5],
            "imageUrl": row[6], "language": row[7],
            "price_usd": float(row[8]) if row[8] is not None else None,
            "game": game,
            "score": round(float(s), 4),
            "rank": rank + 1,
        })
    return matches
