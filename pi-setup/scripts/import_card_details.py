"""Backfill cards_master with abilities/attacks/text/variant from tcgdex.net.

Schema: one row per card, keyed by master_id. tcgdex card ID = "<set_id>-<card_number>".

    docker compose exec -T pos python /app/scripts/import_card_details.py
        [--limit N]            # cap rows; 0 = all (default 0)
        [--force]              # re-fetch even if abilities_jsonb already populated
        [--sleep 0.15]         # per-request politeness delay (s)
        [--langs en]           # comma-list of tcgdex locales (en,ja,de,fr,it,es,pt,ko,zh-tw)
                               # en is fetched first to get abilities/attacks/text;
                               # additional locales backfill their name_* columns only.

Idempotent. Safe to re-run. Skips cards whose abilities_jsonb is already populated unless --force.
"""
from __future__ import annotations
import argparse, os, sys, time, requests, psycopg2
from psycopg2.extras import Json

DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
if not DB_URL: sys.exit("ERROR: DATABASE_URL not set")
TCGDEX = "https://api.tcgdex.net/v2"

# Map tcgdex locale → cards_master name_<col> column
NAME_COLS = {
    "en":    "name_en",
    "ja":    "name_jp",
    "ko":    "name_kr",
    "fr":    "name_fr",
    "de":    "name_de",
    "it":    "name_it",
    "es":    "name_es",
    "zh-tw": "name_cht",
    "zh-cn": "name_chs",
}


def variant_from_rarity(rarity):
    if not rarity: return "normal"
    r = rarity.lower()
    if "secret" in r:                                            return "secret_rare"
    if "hyper" in r:                                             return "hyper_rare"
    if "special illustration" in r or "illustration rare" in r:  return "special_illust"
    if "full art" in r or "alternate art" in r:                  return "full_art"
    if "rainbow" in r:                                           return "rainbow_rare"
    if "shiny" in r:                                             return "shiny"
    if "promo" in r:                                             return "promo"
    if "reverse" in r:                                           return "reverse_holo"
    if "holo" in r:                                              return "holo"
    return "normal"


def fetch(lang, card_id, timeout=10):
    try:
        r = requests.get(f"{TCGDEX}/{lang}/cards/{card_id}", timeout=timeout)
        if r.status_code == 200: return r.json()
    except requests.RequestException:
        pass
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.15)
    ap.add_argument("--langs", default="en", help="comma list, e.g. en,ja,de,fr,it,es")
    args = ap.parse_args()

    langs = [l.strip().lower() for l in args.langs.split(",") if l.strip()]
    if "en" not in langs:
        langs = ["en"] + langs   # en is mandatory (drives abilities/attacks/text)

    conn = psycopg2.connect(DB_URL); conn.autocommit = False

    skip = "" if args.force else " AND (abilities_jsonb IS NULL OR card_text IS NULL OR card_text = '')"
    lim  = f" LIMIT {int(args.limit)}" if args.limit else ""
    sql  = (f"SELECT master_id, set_id, card_number FROM cards_master "
            f"WHERE set_id IS NOT NULL AND card_number IS NOT NULL{skip}{lim}")
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()

    print(f"[details] {len(rows):,} cards to enrich  langs={langs}  force={args.force}")
    if not rows:
        print("[details] nothing to do"); return

    n_ok = n_404 = n_err = 0
    t0 = time.time()
    batch = []
    BATCH_SIZE = 100

    for i, (mid, set_id, card_number) in enumerate(rows, 1):
        tid = f"{set_id}-{card_number}"
        en = fetch("en", tid)
        if not en:
            n_404 += 1
            time.sleep(args.sleep); continue
        # collect localised names
        loc_names = {}
        for lang in langs:
            if lang == "en":
                if en.get("name"): loc_names[NAME_COLS["en"]] = en["name"]
                continue
            if lang not in NAME_COLS: continue
            nd = fetch(lang, tid)
            if nd and nd.get("name"):
                loc_names[NAME_COLS[lang]] = nd["name"]
            time.sleep(args.sleep)
        batch.append({
            "master_id":       mid,
            "variant":         variant_from_rarity(en.get("rarity")),
            "abilities_jsonb": Json(en.get("abilities") or []),
            "attacks_jsonb":   Json(en.get("attacks") or []),
            "card_text":       en.get("description") or en.get("effect") or en.get("text") or "",
            "rarity_subtype":  en.get("rarity"),
            "names":           loc_names,
        })
        n_ok += 1
        time.sleep(args.sleep)

        if len(batch) >= BATCH_SIZE or i == len(rows):
            with conn.cursor() as cur:
                for row in batch:
                    sets_sql = ("variant=%s, abilities_jsonb=%s, attacks_jsonb=%s, "
                                "card_text=%s, rarity_subtype=%s")
                    params = [row["variant"], row["abilities_jsonb"], row["attacks_jsonb"],
                              row["card_text"], row["rarity_subtype"]]
                    for col, val in row["names"].items():
                        sets_sql += f", {col}=COALESCE({col},%s)"
                        params.append(val)
                    params.append(row["master_id"])
                    cur.execute(f"UPDATE cards_master SET {sets_sql} WHERE master_id=%s", params)
            conn.commit()
            print(f"  [{i:>6,}/{len(rows):,}] ok={n_ok:,}  404={n_404:,}  "
                  f"err={n_err:,}  elapsed={time.time()-t0:.0f}s  "
                  f"rate={i/(time.time()-t0):.1f}/s")
            batch = []

    print(f"[details] DONE: ok={n_ok:,} 404={n_404:,} err={n_err:,} elapsed={time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
