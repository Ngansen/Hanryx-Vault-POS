#!/usr/bin/env python3
"""C16: Backfill ebay_sold price_history rows for every card in cards_master.

Why
---
The C14 price_trends_daily matview gives 7d/30d/90d % change per (card_id, source).
Without ebay_sold data the chip only ever shows tcgplayer trends — which is
inflated TCGplayer LIST prices, not realised sold prices.  This script fills
the gap by recording one ebay_sold median per card per run.

Cadence
-------
eBay's Finding API free tier = 5000 calls/day.  Default sleep is 17s/card,
which gives 5,082 calls/day — fits the quota with a small margin.

Run
---
    docker compose exec -d pos sh -c \\
      'nohup python /app/scripts/c16_ebay_backfill.py > /tmp/c16.log 2>&1 < /dev/null &'

Monitor
-------
    docker compose exec -T pos tail -f /tmp/c16.log
    # progress query
    docker compose exec -T pos python -c "import os,psycopg2; c=psycopg2.connect(os.environ['DATABASE_URL']).cursor(); c.execute(\\\"SELECT count(DISTINCT card_id) FROM price_history WHERE source='ebay_sold'\\\"); print(c.fetchone()[0], 'cards have ebay_sold data')"

Resume
------
Killed/restarted runs auto-skip any card with an ebay_sold row within the
last 30 days.  Safe to re-run anytime — it picks up where it left off.

Flags
-----
    --rate-sec 17        seconds between calls (default 17 = 5082/day)
    --limit 20           how many sold listings to fetch per card
    --min-usd 0          skip cards whose tcgplayer market < this (default 0)
    --max-cards N        process at most N cards this run (default = all)
    --min-hits 3         drop cards where eBay returned <N matches (noise floor)
    --refresh-days 30    re-scrape if last ebay row older than this
    --dry-run            don't actually call eBay or write rows; print plan
"""
from __future__ import annotations
import argparse, os, sys, time, signal, statistics
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

# Repo helper — same dir layout as other C-prefixed scripts
sys.path.insert(0, "/app")
try:
    from ebay_sold import search_ebay_sold, EBAY_APP_ID
except ImportError as e:
    sys.exit(f"FATAL: could not import ebay_sold ({e}). "
             f"Mount the repo at /app or run from the pos container.")


_STOP = False
def _sigterm(*_):
    global _STOP
    _STOP = True
    print("[c16] SIGTERM received — finishing current card then exiting", flush=True)
signal.signal(signal.SIGTERM, _sigterm)
signal.signal(signal.SIGINT,  _sigterm)


def _build_query(row: dict) -> str:
    """eBay-style search string from a cards_master row."""
    parts = []
    if row.get("name_en"):
        parts.append(row["name_en"].strip())
    if row.get("set_id"):
        parts.append(row["set_id"].strip().upper())
    if row.get("card_number"):
        parts.append(row["card_number"].strip().lstrip("0") or "0")
    return " ".join(parts)


def _median_usd(hits: list[dict]) -> tuple[float, int]:
    """Median USD price across hits.  Drops non-USD rows (cheap and reliable —
    eBay-US returns mostly USD anyway)."""
    usd = []
    for h in hits:
        if (h.get("currency") or "").upper() == "USD":
            try:
                p = float(h["price"])
                if p > 0:
                    usd.append(p)
            except (KeyError, TypeError, ValueError):
                continue
    return (round(statistics.median(usd), 2), len(usd)) if usd else (0.0, 0)


def _parse_sold_date(raw):
    """eBay returns sold_at as ISO8601 like '2026-05-08T12:34:56.000Z'.
    Extract the date portion; return None on failure (column is nullable)."""
    if not raw:
        return None
    try:
        s = str(raw).strip()
        return s[:10] if len(s) >= 10 and s[4] == "-" and s[7] == "-" else None
    except Exception:
        return None


def _persist_ebay_history(cur, query, hits):
    """Mirror server.py:19898 pattern — bulk-insert each hit into
    ebay_sold_history with NOT EXISTS dedupe so price_trends.trends()
    sees the same data as the per-card /card/price flow."""
    written = 0
    for h in hits:
        try:
            title = (h.get("title") or "")[:500]
            price = float(h.get("price") or 0)
            if not title or price <= 0:
                continue
            sold_date = _parse_sold_date(h.get("sold_at"))
            cur.execute("""
                INSERT INTO ebay_sold_history (query, title, price, sold_date, score, scraped_at)
                SELECT %s, %s, %s, %s, 0, (EXTRACT(epoch FROM now())*1000)::bigint
                WHERE NOT EXISTS (
                    SELECT 1 FROM ebay_sold_history
                    WHERE query=%s AND title=%s AND price=%s
                      AND sold_date IS NOT DISTINCT FROM %s
                )
            """, (query, title, price, sold_date,
                  query, title, price, sold_date))
            written += cur.rowcount or 0
        except Exception:
            continue
    return written


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--rate-sec",      type=float, default=17.0)
    p.add_argument("--limit",         type=int,   default=20)
    p.add_argument("--min-usd",       type=float, default=0.0)
    p.add_argument("--max-cards",     type=int,   default=0)
    p.add_argument("--min-hits",      type=int,   default=3)
    p.add_argument("--refresh-days",  type=int,   default=30)
    p.add_argument("--dry-run",       action="store_true")
    args = p.parse_args()

    if not EBAY_APP_ID and not args.dry_run:
        sys.exit("FATAL: EBAY_APP_ID env var not set inside the pos container. "
                 "Add to pi-setup/.env then `docker compose up -d --force-recreate pos`.")

    db_url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not db_url:
        sys.exit("FATAL: DATABASE_URL not set")

    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    cur  = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # Build the work-list.  Uses master_id::text as the canonical card_id
    # (price_history.card_id is text; existing scrapers use opaque PKM-ML*
    # hashes that don't link to cards_master, so eBay rows we insert use the
    # master_id key for clean joins).  Filters out cards with non-numeric
    # card_number (Unown promos like "!", "%3F") which give useless eBay hits.
    cutoff_ts = f"NOW() - INTERVAL '{int(args.refresh_days)} days'"
    sql = f"""
    WITH last_ebay AS (
        SELECT card_id, MAX(observed_at) AS last_seen
        FROM price_history
        WHERE source = 'ebay_sold'
        GROUP BY card_id
    ),
    latest_tcg AS (
        SELECT DISTINCT ON (card_id) card_id, price_usd
        FROM price_history
        WHERE source = 'tcgplayer' AND price_usd > 0
        ORDER BY card_id, observed_at DESC
    )
    SELECT cm.master_id,
           UPPER(cm.master_id::text) AS card_id,
           cm.set_id, cm.card_number, cm.name_en,
           COALESCE(lt.price_usd, 0)::float AS tcg_usd
    FROM cards_master cm
    LEFT JOIN last_ebay  le ON le.card_id = UPPER(cm.master_id::text)
    LEFT JOIN latest_tcg lt ON lt.card_id = UPPER(cm.master_id::text)
    WHERE cm.name_en IS NOT NULL AND cm.name_en <> ''
      AND cm.set_id  IS NOT NULL AND cm.card_number IS NOT NULL
      AND cm.card_number ~ '[0-9]'
      AND cm.variant_code = 'STD'
      AND (le.last_seen IS NULL OR le.last_seen < {cutoff_ts})
      AND (%(min_usd)s = 0 OR lt.price_usd >= %(min_usd)s)
    ORDER BY COALESCE(lt.price_usd, 0) DESC, cm.master_id
    """
    cur.execute(sql, {"min_usd": args.min_usd})
    work = cur.fetchall()
    total = len(work)

    if args.max_cards > 0:
        work = work[:args.max_cards]

    if total == 0:
        print("[c16] nothing to do — all cards already have fresh ebay_sold data", flush=True)
        return

    eta_sec = len(work) * args.rate_sec
    eta_h, eta_m = divmod(int(eta_sec / 60), 60)
    eta_d, eta_h = divmod(eta_h, 24)
    print(f"[c16] {len(work):,} cards to process "
          f"(of {total:,} total candidates)  "
          f"rate={args.rate_sec}s/card  "
          f"ETA={eta_d}d{eta_h}h{eta_m}m  "
          f"min_usd={args.min_usd}  min_hits={args.min_hits}  "
          f"dry_run={args.dry_run}",
          flush=True)
    if args.dry_run:
        for r in work[:5]:
            print(f"  would process: {r['card_id']:>10s} ({r['set_id']}-{r['card_number']}) "
                  f"q={_build_query(dict(r))!r}", flush=True)
        print("  ... (--dry-run, exiting)", flush=True)
        return

    t0 = time.time()
    written = 0
    skipped_no_hits = 0
    api_errors = 0
    ebay_hist_rows = 0

    for i, r in enumerate(work, 1):
        if _STOP:
            break
        row     = dict(r)
        card_id = row["card_id"]                  # UPPER(master_id::text)
        label   = f"{card_id} ({row['set_id']}-{row['card_number']})"
        query   = _build_query(row)

        try:
            hits = search_ebay_sold(query, limit=args.limit)
        except Exception as e:
            api_errors += 1
            print(f"[c16] [{i}/{len(work)}] {label}  API ERROR: {e}", flush=True)
            time.sleep(args.rate_sec)
            continue

        median, n_usd = _median_usd(hits)

        # Always persist raw hits to ebay_sold_history (feeds price_trends.trends()
        # which the admin marketplace renders).  Done even when below min_hits so
        # the trend module can decide its own confidence threshold.
        try:
            new_rows = _persist_ebay_history(cur, query, hits)
            conn.commit()
            ebay_hist_rows += new_rows
        except Exception as e:
            conn.rollback()
            print(f"[c16] [{i}/{len(work)}] {label}  ebay_sold_history ERROR: {e}", flush=True)

        if n_usd < args.min_hits or median <= 0:
            skipped_no_hits += 1
            if i % 50 == 0:
                print(f"[c16] [{i}/{len(work)}] {label}  q={query!r}  hits={len(hits)} usd={n_usd} -> skip (below min_hits={args.min_hits})", flush=True)
        else:
            try:
                cur.execute("""
                    INSERT INTO price_history
                      (card_id, card_name, market_price, fetched_ms,
                       source, currency, price_usd, price_native, query_used,
                       observed_at)
                    VALUES (%s,%s,%s,%s,'ebay_sold','USD',%s,%s,%s, NOW())
                """, (card_id, (row["name_en"] or card_id)[:160],
                      median, int(time.time()*1000),
                      median, median, query[:200]))
                conn.commit()
                written += 1
            except Exception as e:
                conn.rollback()
                print(f"[c16] [{i}/{len(work)}] {label}  price_history DB ERROR: {e}", flush=True)

        if i % 100 == 0 or i == 1:
            elapsed = time.time() - t0
            rate    = i / max(elapsed, 1)
            remain  = (len(work) - i) / max(rate, 0.001)
            eh, em  = divmod(int(remain/60), 60)
            ed, eh  = divmod(eh, 24)
            print(f"[c16] [{i}/{len(work)}] {label}  q={query!r}  "
                  f"hits={len(hits)} usd={n_usd} median=${median:.2f}  "
                  f"written={written} skipped={skipped_no_hits} api_err={api_errors} "
                  f"hist_rows={ebay_hist_rows}  "
                  f"elapsed={int(elapsed/60)}m  ETA={ed}d{eh}h{em}m",
                  flush=True)

        time.sleep(args.rate_sec)

    print(f"[c16] DONE  processed={i} written={written} "
          f"skipped_no_hits={skipped_no_hits} api_err={api_errors} "
          f"hist_rows={ebay_hist_rows} "
          f"elapsed={int((time.time()-t0)/60)}m", flush=True)
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
