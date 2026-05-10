"""Backfill cards_master with variant + card_text + abilities + attacks from tcgdex.net.

    docker compose exec -T pos python /app/scripts/import_card_details.py
        [--limit N]      # cap for smoke tests; 0 = all
        [--lang en|ja]   # source locale (defaults to en)
        [--force]        # re-fetch even if abilities_jsonb already populated
        [--sleep 0.15]   # per-request politeness delay (s)

Idempotent. Skips cards whose abilities_jsonb is already populated unless --force.
Auto-detects whether cards_master has a `tcgdex_id` column or just `set_id+number`.
"""
from __future__ import annotations
import argparse, os, sys, time, requests, psycopg2
from psycopg2.extras import Json

DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
if not DB_URL: sys.exit("ERROR: DATABASE_URL not set")
TCGDEX = "https://api.tcgdex.net/v2"


def variant_from_rarity(rarity):
    if not rarity: return "normal"
    r = rarity.lower()
    if "secret" in r:                                       return "secret_rare"
    if "hyper" in r:                                        return "hyper_rare"
    if "special illustration" in r or "illustration rare" in r: return "special_illust"
    if "full art" in r or "alternate art" in r:             return "full_art"
    if "rainbow" in r:                                      return "rainbow_rare"
    if "shiny" in r:                                        return "shiny"
    if "promo" in r:                                        return "promo"
    if "reverse" in r:                                      return "reverse_holo"
    if "holo" in r:                                         return "holo"
    return "normal"


def fetch_card(lang, card_id, timeout=10):
    try:
        r = requests.get(f"{TCGDEX}/{lang}/cards/{card_id}", timeout=timeout)
        if r.status_code == 200: return r.json()
    except requests.RequestException:
        pass
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--lang",  default="en")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.15)
    args = ap.parse_args()

    conn = psycopg2.connect(DB_URL); conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute("""SELECT column_name FROM information_schema.columns
                       WHERE table_name='cards_master'""")
        cols = {r[0] for r in cur.fetchall()}

    if "tcgdex_id" in cols:
        id_expr = "tcgdex_id"
    elif {"set_id","number"} <= cols:
        id_expr = "(set_id || '-' || number)"
    else:
        sys.exit(f"ERROR: cards_master missing tcgdex_id and (set_id+number). Have: {sorted(cols)}")

    skip = "" if args.force else " AND (abilities_jsonb IS NULL OR card_text IS NULL)"
    lim  = f" LIMIT {int(args.limit)}" if args.limit else ""
    lang_clause = ""
    lang_arg = ()
    if "language" in cols:
        lang_clause = " AND language = %s"
        lang_arg = (args.lang,)

    sql = (f"SELECT card_id, {id_expr} AS tid FROM cards_master "
           f"WHERE {id_expr} IS NOT NULL{lang_clause}{skip}{lim}")
    with conn.cursor() as cur:
        cur.execute(sql, lang_arg)
        rows = cur.fetchall()

    print(f"[details] {len(rows)} cards to enrich (lang={args.lang}, force={args.force})")
    n_ok = n_skip = 0; t0 = time.time(); upd = []
    for i, (card_id, tid) in enumerate(rows, 1):
        if not tid: n_skip += 1; continue
        card = fetch_card(args.lang, tid)
        if not card:
            n_skip += 1
        else:
            upd.append((
                variant_from_rarity(card.get("rarity")),
                Json(card.get("abilities") or []),
                Json(card.get("attacks") or []),
                card.get("description") or card.get("text") or None,
                card.get("rarity"),
                card_id,
            ))
            n_ok += 1
        if len(upd) >= 100 or i == len(rows):
            with conn.cursor() as cur:
                cur.executemany(
                    "UPDATE cards_master "
                    "SET variant=%s, abilities_jsonb=%s, attacks_jsonb=%s, "
                    "    card_text=%s, rarity_subtype=%s "
                    "WHERE card_id=%s",
                    upd)
            conn.commit()
            print(f"  [{i}/{len(rows)}] ok={n_ok} skip={n_skip} elapsed={time.time()-t0:.0f}s")
            upd = []
        time.sleep(args.sleep)
    print(f"[details] done: ok={n_ok} skip={n_skip} elapsed={time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
