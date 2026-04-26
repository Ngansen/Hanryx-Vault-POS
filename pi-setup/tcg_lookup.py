"""
tcg_lookup.py  —  Local Pokemon TCG card lookup for HanryxVault
================================================================
Drop this file into your HanryxVault directory, then add this to your
Flask app (app.py or wherever you register blueprints):

    from tcg_lookup import tcg_bp
    app.register_blueprint(tcg_bp)

This exposes three API endpoints your POS can call locally:

  GET /tcg/search?q=Charizard          — search by name
  GET /tcg/card/<pokemontcg_id>        — get one card by pokemontcg.io ID
  GET /tcg/inventory/<barcode_or_id>   — look up your personal inventory card

It also exposes Python functions you can import directly:

    from tcg_lookup import search_tcg, get_inventory_card, find_price
"""

import sqlite3
import json
import os
from flask import Blueprint, jsonify, request

# Path to the SQLite database created by import_tcg_db.py.
# Resolved through cards_db_path so the same SQLite file is shared with
# server.py, sync_tcg_db.py, import_tcg_db.py, and the new sync orchestrator.
# Set HANRYX_LOCAL_DB_DIR=/mnt/cards in docker-compose to put the DB on USB;
# unset = falls back to pi-setup/pokedex_local.db (legacy in-package path).
from cards_db_path import local_db_path as _resolve_db_path


def _DB_PATH() -> str:  # noqa: N802 — kept for grep-friendliness with the old name
    return _resolve_db_path()

tcg_bp = Blueprint("tcg", __name__, url_prefix="/tcg")


def _get_db():
    path = _DB_PATH()
    if not os.path.exists(path):
        return None
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_dict(row):
    if row is None:
        return None
    d = dict(row)
    for k in ("tags", "subtypes", "types", "national_dex", "raw_prices"):
        if k in d and d[k]:
            try:
                d[k] = json.loads(d[k])
            except Exception:
                pass
    return d


# ── Python API (import these directly in your Flask views) ────────────────────

def search_tcg(query: str, limit: int = 20) -> list[dict]:
    """Search the local TCG card database by name. Returns list of card dicts."""
    conn = _get_db()
    if not conn:
        return []
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT * FROM tcg_cards WHERE name LIKE ? ORDER BY release_date DESC, name LIMIT ?",
        (f"%{query}%", limit)
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def search_inventory(query: str, limit: int = 20) -> list[dict]:
    """Search your personal inventory by name."""
    conn = _get_db()
    if not conn:
        return []
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT * FROM inventory WHERE name LIKE ? AND sold=0 ORDER BY name LIMIT ?",
        (f"%{query}%", limit)
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_inventory_card(barcode_or_id) -> dict | None:
    """Look up a single inventory card by barcode or PokeDex ID."""
    conn = _get_db()
    if not conn:
        return None
    cur = conn.cursor()
    row = cur.execute(
        "SELECT * FROM inventory WHERE barcode=? OR qr_code=? OR id=? LIMIT 1",
        (str(barcode_or_id), str(barcode_or_id), str(barcode_or_id))
    ).fetchone()
    conn.close()
    return _row_to_dict(row)


def get_tcg_card(tcg_id: str) -> dict | None:
    """Get a single TCG card by its pokemontcg.io ID (e.g. 'xy1-1')."""
    conn = _get_db()
    if not conn:
        return None
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM tcg_cards WHERE id=?", (tcg_id,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def find_price(name: str, set_name: str = "", number: str = "") -> float | None:
    """
    Best-effort market price lookup for a card.
    Returns the market price in USD, or None if not found.
    Tries to match by name + set + number first, then falls back to name only.
    """
    conn = _get_db()
    if not conn:
        return None
    cur = conn.cursor()

    if set_name and number:
        row = cur.execute(
            "SELECT market_price FROM tcg_cards WHERE name=? AND set_name=? AND number=? LIMIT 1",
            (name, set_name, number)
        ).fetchone()
        if row and row[0]:
            conn.close()
            return float(row[0])

    if set_name:
        row = cur.execute(
            "SELECT market_price FROM tcg_cards WHERE name=? AND set_name=? LIMIT 1",
            (name, set_name)
        ).fetchone()
        if row and row[0]:
            conn.close()
            return float(row[0])

    row = cur.execute(
        "SELECT market_price FROM tcg_cards WHERE name=? ORDER BY release_date DESC LIMIT 1",
        (name,)
    ).fetchone()
    conn.close()
    return float(row[0]) if (row and row[0]) else None


def db_stats() -> dict:
    """Return stats about the local database."""
    conn = _get_db()
    if not conn:
        return {"error": "Database not found. Run import_tcg_db.py first."}
    cur = conn.cursor()
    return {
        "inventory_cards": cur.execute("SELECT COUNT(*) FROM inventory").fetchone()[0],
        "inventory_active": cur.execute("SELECT COUNT(*) FROM inventory WHERE sold=0 AND is_wishlist=0").fetchone()[0],
        "tcg_sets": cur.execute("SELECT COUNT(*) FROM sets").fetchone()[0],
        "tcg_cards": cur.execute("SELECT COUNT(*) FROM tcg_cards").fetchone()[0],
        "db_path": _DB_PATH(),
    }


# ── Flask Blueprint routes ─────────────────────────────────────────────────────

@tcg_bp.route("/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "Provide ?q=<search term>"}), 400
    limit = min(int(request.args.get("limit", 20)), 100)
    scope = request.args.get("scope", "both")  # both | tcg | inventory

    result = {}
    if scope in ("both", "inventory"):
        result["inventory"] = search_inventory(q, limit)
    if scope in ("both", "tcg"):
        result["tcg"] = search_tcg(q, limit)
    return jsonify(result)


@tcg_bp.route("/card/<tcg_id>")
def api_tcg_card(tcg_id: str):
    card = get_tcg_card(tcg_id)
    if not card:
        return jsonify({"error": "Not found"}), 404
    return jsonify(card)


@tcg_bp.route("/inventory/<identifier>")
def api_inventory_card(identifier: str):
    card = get_inventory_card(identifier)
    if not card:
        return jsonify({"error": "Not found"}), 404
    return jsonify(card)


@tcg_bp.route("/stats")
def api_stats():
    return jsonify(db_stats())
