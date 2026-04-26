#!/usr/bin/env python3
"""
PokeDex · TCG Database Sync
============================
Continuously keeps your local Pokemon TCG database up to date by checking
pokemontcg.io for new or updated sets and only downloading what's changed.

Run manually:
  python3 sync_tcg_db.py

Run once and exit (no looping):
  python3 sync_tcg_db.py --once

Force a full re-sync of all sets:
  python3 sync_tcg_db.py --full

Set a custom check interval (default: 6 hours):
  python3 sync_tcg_db.py --interval 12

Show what's currently in the database:
  python3 sync_tcg_db.py --stats

Add to crontab for automatic daily sync at 3am:
  0 3 * * * /usr/bin/python3 /home/pi/pokedex/raspberry-pi/sync_tcg_db.py --once >> /var/log/tcg_sync.log 2>&1
"""

import sqlite3
import json
import argparse
import sys
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────

# Resolve through cards_db_path so syncs land on /mnt/cards/pokedex_local.db
# when HANRYX_LOCAL_DB_DIR is set (production on Pi), and fall back to the
# in-package path on a dev box. See cards_db_path.py for rationale.
from cards_db_path import local_db_path as _resolve_db_path
DB_PATH      = _resolve_db_path()
API_BASE     = "https://api.pokemontcg.io/v2"
API_KEY      = os.environ.get("POKEMON_TCG_API_KEY", "")   # optional — set in env for higher rate limits
CHECK_HOURS  = 6       # default: check every 6 hours when running in loop mode
PAGE_SIZE    = 250     # max allowed by the API


# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def api_get(path: str) -> dict:
    url = f"{API_BASE}/{path}"
    req = urllib.request.Request(url)
    if API_KEY:
        req.add_header("X-Api-Key", API_KEY)
    req.add_header("User-Agent", "PokeDex-Pi-Sync/1.0")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} from {url}: {e.reason}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e.reason}")


def get_db() -> sqlite3.Connection:
    if not os.path.exists(DB_PATH):
        log(f"ERROR: Database not found at {DB_PATH}")
        log("Run import_tcg_db.py --tcgdb <file> first to create it.")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_sync_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_log (
            set_id       TEXT PRIMARY KEY,
            last_synced  TEXT,
            card_count   INTEGER DEFAULT 0,
            api_updated  TEXT
        )
    """)
    conn.commit()


# ── Core sync logic ────────────────────────────────────────────────────────────

def fetch_all_sets() -> list[dict]:
    log("Fetching set list from pokemontcg.io …")
    data = api_get(f"sets?pageSize={PAGE_SIZE}&orderBy=-releaseDate")
    sets = data.get("data", [])
    log(f"Found {len(sets)} sets in the API")
    return sets


def get_synced_sets(conn: sqlite3.Connection) -> dict[str, str]:
    """Returns {set_id: api_updated_at} for all sets already in sync_log."""
    rows = conn.execute("SELECT set_id, api_updated FROM sync_log").fetchall()
    return {r["set_id"]: r["api_updated"] for r in rows}


def sets_needing_sync(all_sets: list[dict], synced: dict[str, str], full: bool) -> list[dict]:
    """Filter to sets that are new or have been updated since last sync."""
    todo = []
    for s in all_sets:
        sid = s.get("id", "")
        api_updated = s.get("updatedAt", "")
        if full or sid not in synced or synced[sid] != api_updated:
            todo.append(s)
    return todo


def fetch_cards_for_set(set_id: str) -> list[dict]:
    """Fetch all cards for a single set (may be multiple pages for large sets)."""
    all_cards = []
    page = 1
    while True:
        query = f"q=set.id:{set_id}&pageSize={PAGE_SIZE}&page={page}"
        data = api_get(f"cards?{query}")
        cards = data.get("data", [])
        all_cards.extend(cards)
        total = data.get("totalCount", 0)
        if len(all_cards) >= total or not cards:
            break
        page += 1
    return all_cards


def upsert_set(conn: sqlite3.Connection, s: dict):
    imgs = s.get("images", {})
    conn.execute("""
        INSERT OR REPLACE INTO sets VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))
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
    ))


def upsert_cards(conn: sqlite3.Connection, cards: list[dict]):
    now = datetime.utcnow().isoformat()
    batch = []
    for c in cards:
        imgs  = c.get("images", {})
        s     = c.get("set", {})
        tcgp  = (c.get("tcgplayer") or {}).get("prices", {})

        def best_price(tier):
            t = tcgp.get(tier, {})
            return t.get("market") or t.get("mid") or t.get("directLow") or None

        market = (best_price("holofoil") or best_price("normal")
                  or best_price("reverseHolofoil") or best_price("1stEditionHolofoil"))

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

    if batch:
        conn.executemany("""
            INSERT OR REPLACE INTO tcg_cards VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, batch)


def record_sync(conn: sqlite3.Connection, set_id: str, api_updated: str, card_count: int):
    conn.execute("""
        INSERT OR REPLACE INTO sync_log VALUES (?, datetime('now'), ?, ?)
    """, (set_id, card_count, api_updated))


def run_sync(full: bool = False):
    conn = get_db()
    ensure_sync_table(conn)

    try:
        all_sets = fetch_all_sets()
    except RuntimeError as e:
        log(f"ERROR fetching sets: {e}")
        conn.close()
        return False

    synced = get_synced_sets(conn)
    todo   = sets_needing_sync(all_sets, synced, full)

    if not todo:
        log("Already up to date — no new or changed sets found.")
        conn.close()
        return True

    mode = "full re-sync" if full else "incremental sync"
    log(f"Starting {mode}: {len(todo)} set(s) to update")

    total_cards = 0
    errors = 0
    for i, s in enumerate(todo, 1):
        set_id   = s.get("id","")
        set_name = s.get("name","")
        api_upd  = s.get("updatedAt","")
        try:
            log(f"  [{i}/{len(todo)}] {set_name} ({set_id}) …")
            cards = fetch_cards_for_set(set_id)
            upsert_set(conn, s)
            upsert_cards(conn, cards)
            record_sync(conn, set_id, api_upd, len(cards))
            conn.commit()
            log(f"    ✓ {len(cards)} cards synced")
            total_cards += len(cards)
            # Small delay to be polite to the API (free tier: 1000 req/day)
            time.sleep(0.5)
        except RuntimeError as e:
            log(f"    ✗ ERROR: {e}")
            errors += 1
            time.sleep(2)  # back off a bit on errors

    # Print summary
    total_in_db = conn.execute("SELECT COUNT(*) FROM tcg_cards").fetchone()[0]
    conn.close()
    log(f"Sync complete — {total_cards:,} cards updated, {errors} error(s). "
        f"Total in DB: {total_in_db:,} cards")
    return errors == 0


def show_stats():
    conn = get_db()
    ensure_sync_table(conn)
    total_sets   = conn.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
    total_cards  = conn.execute("SELECT COUNT(*) FROM tcg_cards").fetchone()[0]
    synced_sets  = conn.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0]
    last_sync    = conn.execute("SELECT MAX(last_synced) FROM sync_log").fetchone()[0]
    newest_set   = conn.execute("SELECT name, release_date FROM sets ORDER BY release_date DESC LIMIT 1").fetchone()
    conn.close()

    print(f"\n── TCG Sync Database Stats ────────────────────────")
    print(f"  Sets in DB      : {total_sets}")
    print(f"  Sets synced     : {synced_sets} / {total_sets}")
    print(f"  Cards in DB     : {total_cards:,}")
    print(f"  Last sync       : {last_sync or 'Never'}")
    if newest_set:
        print(f"  Newest set      : {newest_set['name']} ({newest_set['release_date']})")
    print(f"  DB path         : {DB_PATH}")
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PokeDex TCG Database Sync")
    parser.add_argument("--once",     action="store_true", help="Run sync once and exit (good for cron)")
    parser.add_argument("--full",     action="store_true", help="Force re-sync of all sets (ignores cache)")
    parser.add_argument("--interval", type=float, default=CHECK_HOURS,
                        help=f"Hours between checks in loop mode (default: {CHECK_HOURS})")
    parser.add_argument("--stats",    action="store_true", help="Show database stats and exit")
    args = parser.parse_args()

    if args.stats:
        show_stats()
        sys.exit(0)

    if args.once:
        log("Running one-time sync …")
        success = run_sync(full=args.full)
        sys.exit(0 if success else 1)

    # ── Loop mode ──────────────────────────────────────────────────────────────
    interval_secs = int(args.interval * 3600)
    log(f"Starting continuous sync — checking every {args.interval:.0f} hour(s). Press Ctrl+C to stop.")
    while True:
        run_sync(full=args.full)
        args.full = False  # only do full on first run if requested
        next_check = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log(f"Sleeping {args.interval:.0f}h — next check around "
            f"{datetime.fromtimestamp(time.time() + interval_secs).strftime('%Y-%m-%d %H:%M:%S')}")
        try:
            time.sleep(interval_secs)
        except KeyboardInterrupt:
            log("Stopped.")
            sys.exit(0)
