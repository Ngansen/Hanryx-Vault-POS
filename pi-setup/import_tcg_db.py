#!/usr/bin/env python3
"""
PokeDex → Pi  ·  Database Importer
====================================
Run this on your Raspberry Pi to build a local SQLite database from the
JSON files you downloaded from PokeDex.

Supports two JSON sources:
  1. pokedex-export-YYYY-MM-DD.json      — your personal inventory (Export Full DB)
  2. pokemon-tcg-database-YYYY-MM-DD.json — full pokemontcg.io card dump (TCG Database)

Usage:
  python3 import_tcg_db.py --inventory  pokedex-export-2026-03-30.json
  python3 import_tcg_db.py --tcgdb      pokemon-tcg-database-2026-03-30.json
  python3 import_tcg_db.py --inventory  pokedex-export.json --tcgdb pokemon-tcg-database.json
  python3 import_tcg_db.py --search "Charizard"
  python3 import_tcg_db.py --stats
"""

import sqlite3
import json
import argparse
import sys
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "pokedex_local.db")


# ── Database setup ─────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    # Your personal inventory (from PokeDex)
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS inventory (
            id            INTEGER PRIMARY KEY,
            name          TEXT NOT NULL,
            set_name      TEXT,
            card_number   TEXT,
            rarity        TEXT,
            language      TEXT DEFAULT 'English',
            condition     TEXT DEFAULT 'Near Mint',
            item_type     TEXT DEFAULT 'Single',
            quantity      INTEGER DEFAULT 1,
            price         REAL DEFAULT 0,
            market_price  REAL DEFAULT 0,
            purchase_price REAL DEFAULT 0,
            sale_price    REAL DEFAULT 0,
            grading_company TEXT,
            grade         TEXT,
            cert_number   TEXT,
            barcode       TEXT,
            qr_code       TEXT,
            tags          TEXT,
            notes         TEXT,
            image_url     TEXT,
            sold          INTEGER DEFAULT 0,
            is_wishlist   INTEGER DEFAULT 0,
            created_at    TEXT,
            imported_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_inventory_name ON inventory(name);
        CREATE INDEX IF NOT EXISTS idx_inventory_barcode ON inventory(barcode);
        CREATE INDEX IF NOT EXISTS idx_inventory_set ON inventory(set_name);

        CREATE TABLE IF NOT EXISTS sets (
            id            TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            series        TEXT,
            printed_total INTEGER,
            total         INTEGER,
            ptcgo_code    TEXT,
            release_date  TEXT,
            updated_at    TEXT,
            symbol_url    TEXT,
            logo_url      TEXT,
            imported_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_sets_name ON sets(name);

        CREATE TABLE IF NOT EXISTS tcg_cards (
            id            TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            supertype     TEXT,
            subtypes      TEXT,
            hp            TEXT,
            types         TEXT,
            evolves_from  TEXT,
            rarity        TEXT,
            artist        TEXT,
            number        TEXT,
            national_dex  TEXT,
            set_id        TEXT,
            set_name      TEXT,
            set_series    TEXT,
            release_date  TEXT,
            image_small   TEXT,
            image_large   TEXT,
            price_normal  REAL,
            price_holo    REAL,
            price_reverse REAL,
            price_1st_ed  REAL,
            market_price  REAL,
            raw_prices    TEXT,
            imported_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_tcg_name ON tcg_cards(name);
        CREATE INDEX IF NOT EXISTS idx_tcg_set ON tcg_cards(set_id);
        CREATE INDEX IF NOT EXISTS idx_tcg_number ON tcg_cards(number);
    """)
    conn.commit()
    conn.close()
    print(f"[DB] Initialised: {DB_PATH}")


# ── Import: personal inventory ─────────────────────────────────────────────────

def import_inventory(json_path: str):
    print(f"[Inventory] Loading {json_path} …")
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    cards = data.get("cards", [])
    if not cards:
        print("[Inventory] No cards found in file.")
        return

    conn = get_db()
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    inserted = updated = 0

    for c in cards:
        def p(v):
            try: return float(str(v or "0").replace("$","").replace(",","")) or 0.0
            except: return 0.0

        row = (
            c.get("id"),
            c.get("name", ""),
            c.get("set", "") or c.get("setCode", ""),
            c.get("cardNumber", ""),
            c.get("rarity", ""),
            c.get("language", "English"),
            c.get("condition", "Near Mint"),
            c.get("itemType", "Single"),
            c.get("quantity", 1),
            p(c.get("price")),
            p(c.get("marketPrice")),
            p(c.get("purchasePrice")),
            p(c.get("salePrice")),
            c.get("gradingCompany", ""),
            c.get("grade", ""),
            c.get("certNumber", ""),
            c.get("barcode", ""),
            c.get("qrCode") or c.get("barcode") or str(c.get("id", "")),
            json.dumps(c.get("tags") or []),
            c.get("notes", ""),
            c.get("imageUrl", ""),
            1 if c.get("sold") else 0,
            1 if c.get("isWishlist") else 0,
            c.get("createdAt", ""),
            now,
        )
        existing = cur.execute("SELECT id FROM inventory WHERE id=?", (c.get("id"),)).fetchone()
        if existing:
            cur.execute("""
                UPDATE inventory SET
                  name=?, set_name=?, card_number=?, rarity=?, language=?,
                  condition=?, item_type=?, quantity=?, price=?, market_price=?,
                  purchase_price=?, sale_price=?, grading_company=?, grade=?,
                  cert_number=?, barcode=?, qr_code=?, tags=?, notes=?,
                  image_url=?, sold=?, is_wishlist=?, created_at=?, imported_at=?
                WHERE id=?
            """, row[1:] + (c.get("id"),))
            updated += 1
        else:
            cur.execute("""
                INSERT INTO inventory VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, row)
            inserted += 1

    conn.commit()
    conn.close()
    print(f"[Inventory] Done — {inserted} inserted, {updated} updated ({len(cards)} total cards)")


# ── Import: TCG database (pokemontcg.io full dump) ─────────────────────────────

def import_tcg_db(json_path: str):
    print(f"[TCG DB] Loading {json_path} … (this may take a moment for large files)")
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Import sets
    sets = data.get("sets", [])
    cards = data.get("cards", [])
    now = datetime.utcnow().isoformat()
    conn = get_db()
    cur = conn.cursor()

    print(f"[TCG DB] Importing {len(sets)} sets …")
    for s in sets:
        imgs = s.get("images", {})
        cur.execute("""
            INSERT OR REPLACE INTO sets VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            s.get("id",""),
            s.get("name",""),
            s.get("series",""),
            s.get("printedTotal"),
            s.get("total"),
            s.get("ptcgoCode",""),
            s.get("releaseDate",""),
            s.get("updatedAt",""),
            imgs.get("symbol",""),
            imgs.get("logo",""),
            now,
        ))
    conn.commit()
    print(f"[TCG DB] Sets done.")

    print(f"[TCG DB] Importing {len(cards):,} cards …")
    batch = []
    for i, c in enumerate(cards):
        imgs  = c.get("images", {})
        s     = c.get("set", {})
        tcgp  = (c.get("tcgplayer") or {}).get("prices", {})

        def best_price(tier):
            t = tcgp.get(tier, {})
            return t.get("market") or t.get("mid") or t.get("directLow") or None

        market = best_price("holofoil") or best_price("normal") or best_price("reverseHolofoil") or None

        batch.append((
            c.get("id",""),
            c.get("name",""),
            c.get("supertype",""),
            json.dumps(c.get("subtypes") or []),
            c.get("hp",""),
            json.dumps(c.get("types") or []),
            c.get("evolvesFrom",""),
            c.get("rarity",""),
            c.get("artist",""),
            c.get("number",""),
            json.dumps(c.get("nationalPokedexNumbers") or []),
            s.get("id",""),
            s.get("name",""),
            s.get("series",""),
            s.get("releaseDate",""),
            imgs.get("small",""),
            imgs.get("large",""),
            best_price("normal"),
            best_price("holofoil"),
            best_price("reverseHolofoil"),
            best_price("1stEditionHolofoil"),
            market,
            json.dumps(tcgp),
            now,
        ))

        if len(batch) >= 500:
            cur.executemany("INSERT OR REPLACE INTO tcg_cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
            conn.commit()
            batch = []
            pct = int((i + 1) / len(cards) * 100)
            print(f"  … {i+1:,}/{len(cards):,} cards ({pct}%)", end="\r", flush=True)

    if batch:
        cur.executemany("INSERT OR REPLACE INTO tcg_cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        conn.commit()

    conn.close()
    print(f"\n[TCG DB] Done — {len(cards):,} cards imported")


# ── Search (CLI) ──────────────────────────────────────────────────────────────

def search(query: str):
    conn = get_db()
    cur = conn.cursor()
    like = f"%{query}%"

    print(f"\n── Your Inventory ────────────────────────────────")
    rows = cur.execute(
        "SELECT name, set_name, card_number, language, condition, price, market_price, quantity, sold "
        "FROM inventory WHERE name LIKE ? ORDER BY name LIMIT 20", (like,)
    ).fetchall()
    if rows:
        for r in rows:
            status = "SOLD" if r["sold"] else f"qty:{r['quantity']}"
            print(f"  {r['name']:<35} {r['set_name'] or '—':<25} #{r['card_number'] or '—':<8} "
                  f"{r['language']:<10} {r['condition']:<10} ${r['price']:.2f}  [{status}]")
    else:
        print("  (no matches in your inventory)")

    print(f"\n── TCG Database ──────────────────────────────────")
    rows = cur.execute(
        "SELECT name, set_name, number, rarity, types, price_normal, price_holo, market_price "
        "FROM tcg_cards WHERE name LIKE ? ORDER BY release_date DESC, name LIMIT 20", (like,)
    ).fetchall()
    if rows:
        for r in rows:
            mp = r["market_price"] or r["price_holo"] or r["price_normal"] or 0
            print(f"  {r['name']:<35} {r['set_name'] or '—':<25} #{r['number'] or '—':<8} "
                  f"{r['rarity'] or '—':<18} market:${mp:.2f}")
    else:
        print("  (no matches in TCG database)")

    conn.close()


def stats():
    conn = get_db()
    cur = conn.cursor()
    inv_total = cur.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
    inv_active = cur.execute("SELECT COUNT(*) FROM inventory WHERE sold=0 AND is_wishlist=0").fetchone()[0]
    inv_value = cur.execute("SELECT SUM(price*quantity) FROM inventory WHERE sold=0 AND is_wishlist=0").fetchone()[0] or 0
    sets_total = cur.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
    tcg_total = cur.execute("SELECT COUNT(*) FROM tcg_cards").fetchone()[0]
    conn.close()
    print(f"\n── PokeDex Local Database Stats ─────────────────────")
    print(f"  Your inventory : {inv_total:,} total cards ({inv_active:,} active, ${inv_value:,.2f} value)")
    print(f"  TCG sets       : {sets_total:,}")
    print(f"  TCG cards      : {tcg_total:,}")
    print(f"  DB file        : {DB_PATH}")
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PokeDex Pi Database Importer")
    parser.add_argument("--inventory", metavar="FILE", help="Import your inventory JSON (pokedex-export-*.json)")
    parser.add_argument("--tcgdb",     metavar="FILE", help="Import TCG database JSON (pokemon-tcg-database-*.json)")
    parser.add_argument("--search",    metavar="QUERY", help="Search the local database")
    parser.add_argument("--stats",     action="store_true", help="Show database stats")
    args = parser.parse_args()

    if not any([args.inventory, args.tcgdb, args.search, args.stats]):
        parser.print_help()
        sys.exit(0)

    init_db()

    if args.inventory:
        if not os.path.exists(args.inventory):
            print(f"ERROR: File not found: {args.inventory}")
            sys.exit(1)
        import_inventory(args.inventory)

    if args.tcgdb:
        if not os.path.exists(args.tcgdb):
            print(f"ERROR: File not found: {args.tcgdb}")
            sys.exit(1)
        import_tcg_db(args.tcgdb)

    if args.search:
        search(args.search)

    if args.stats:
        stats()
