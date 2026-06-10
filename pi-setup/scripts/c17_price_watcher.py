#!/usr/bin/env python3
"""C17: Smart background price watcher.

Detects cards whose price is moving fast (rotation announcements, tournament
results, banlists, viral hype) and re-scrapes them more frequently than the
30-day default of C16.  Three tiers:

    HOT   |Δ24h| > 20% AND price >= $5     re-scrape every 4h
    WARM  |Δ7d|  > 30% OR  price >= $50    re-scrape every 24h
    COLD  everything else                  handled by C16's 30-day backfill

Quota
-----
Shares eBay's 5000-call/day budget with C16 via a postgres counter
(`price_watch_calls` table).  Default reservation:
    --max-calls-per-day 1000     (C17's share)
    C16 should be run with --max-cards capping its share to ~4000/day.

Run
---
    docker compose exec -d pos sh -c \\
      'nohup python /app/scripts/c17_price_watcher.py > /tmp/c17.log 2>&1 < /dev/null &'

Loop
----
Every --tick-min minutes:
  1. Refresh tier rankings from price_history
  2. Pick the top --batch cards due for re-scrape
  3. Re-scrape via search_ebay_sold; write median row
  4. Sleep tick interval

AI curation (optional, off by default)
--------------------------------------
With --ai-curate, top-20 candidates per tick are sent to GPT-4o-mini with a
short prompt asking which look like real demand vs. noise.  Requires
OPENAI_API_KEY.  Saves quota at the cost of LLM tokens (~$0.01/day).

Flags
-----
    --tick-min 60              loop interval
    --batch 4                  cards per tick to re-scrape
    --rate-sec 17              seconds between API calls within a tick
    --max-calls-per-day 1000   shared quota cap
    --hot-pct 20               24h % move to qualify as HOT
    --warm-pct 30              7d % move to qualify as WARM
    --hot-min-usd 5            HOT requires price >= this
    --warm-min-usd 50          WARM requires price >= this
    --hot-cooldown-h 4         re-scrape interval for HOT
    --warm-cooldown-h 24       re-scrape interval for WARM
    --ai-curate                use GPT-4o-mini to filter candidates (default off)
    --once                     run one tick then exit (for cron / debugging)
"""
from __future__ import annotations
import argparse, json, os, signal, statistics, sys, time
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

sys.path.insert(0, "/app")
try:
    from ebay_sold import search_ebay_sold, EBAY_APP_ID
except ImportError as e:
    sys.exit(f"FATAL: could not import ebay_sold ({e}).")


_STOP = False
def _sigterm(*_):
    global _STOP
    _STOP = True
    print("[c17] SIGTERM — finishing current batch then exiting", flush=True)
signal.signal(signal.SIGTERM, _sigterm)
signal.signal(signal.SIGINT,  _sigterm)


# ---------- quota ----------

def _ensure_quota_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS price_watch_calls (
            day_utc DATE PRIMARY KEY,
            call_count INT NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


def _calls_today(cur) -> int:
    cur.execute("SELECT call_count FROM price_watch_calls WHERE day_utc = CURRENT_DATE")
    r = cur.fetchone()
    return int(r[0]) if r else 0


def _bump_quota(cur):
    cur.execute("""
        INSERT INTO price_watch_calls(day_utc, call_count) VALUES (CURRENT_DATE, 1)
        ON CONFLICT (day_utc)
        DO UPDATE SET call_count = price_watch_calls.call_count + 1, updated_at = NOW()
    """)


# ---------- tiering ----------

_TIER_SQL = """
WITH recent AS (
    SELECT card_id, observed_at, price_usd
    FROM price_history
    WHERE source IN ('ebay_sold','tcgplayer')
      AND observed_at > NOW() - INTERVAL '14 days'
      AND price_usd > 0
),
agg AS (
    SELECT
        card_id,
        AVG(price_usd) FILTER (WHERE observed_at > NOW() - INTERVAL '24 hours') AS p_24h,
        AVG(price_usd) FILTER (WHERE observed_at BETWEEN NOW() - INTERVAL '48 hours'
                                                     AND NOW() - INTERVAL '24 hours') AS p_24_48h,
        AVG(price_usd) FILTER (WHERE observed_at > NOW() - INTERVAL '7 days')   AS p_7d,
        AVG(price_usd) FILTER (WHERE observed_at BETWEEN NOW() - INTERVAL '14 days'
                                                     AND NOW() - INTERVAL '7 days') AS p_7_14d,
        MAX(price_usd) AS peak,
        MIN(price_usd) AS trough
    FROM recent
    GROUP BY card_id
),
last_ebay AS (
    SELECT card_id, MAX(observed_at) AS last_seen
    FROM price_history WHERE source='ebay_sold' GROUP BY card_id
)
SELECT a.card_id,
       a.p_24h, a.p_24_48h, a.p_7d, a.p_7_14d, a.peak, a.trough,
       le.last_seen,
       cm.name_en, cm.set_id, cm.card_number,
       CASE WHEN a.p_24_48h > 0 THEN ABS(a.p_24h - a.p_24_48h) / a.p_24_48h * 100 ELSE 0 END AS pct_24h,
       CASE WHEN a.p_7_14d  > 0 THEN ABS(a.p_7d  - a.p_7_14d)  / a.p_7_14d  * 100 ELSE 0 END AS pct_7d
FROM agg a
JOIN cards_master cm ON UPPER(cm.master_id::text) = a.card_id
LEFT JOIN last_ebay le ON le.card_id = a.card_id
WHERE a.p_24h IS NOT NULL OR a.p_7d IS NOT NULL
"""


def _classify(rows, args):
    hot, warm = [], []
    now = datetime.now(timezone.utc)
    hot_cool = args.hot_cooldown_h * 3600
    warm_cool = args.warm_cooldown_h * 3600
    for r in rows:
        last = r["last_seen"]
        age = (now - last).total_seconds() if last else 1e12
        peak = float(r["peak"] or 0)
        is_hot = (
            (r["pct_24h"] or 0) > args.hot_pct
            and (peak >= args.hot_min_usd)
            and age >= hot_cool
        )
        is_warm = (
            ((r["pct_7d"] or 0) > args.warm_pct or peak >= args.warm_min_usd)
            and age >= warm_cool
        )
        # rank score: USD weight × % move; HOT > WARM
        score = peak * ((r["pct_24h"] or 0) + 0.5 * (r["pct_7d"] or 0))
        item = {**dict(r), "score": float(score), "age_h": age / 3600}
        if is_hot:
            hot.append(item)
        elif is_warm:
            warm.append(item)
    hot.sort(key=lambda x: -x["score"])
    warm.sort(key=lambda x: -x["score"])
    return hot, warm


# ---------- scraping ----------

def _build_query(row) -> str:
    parts = []
    if row.get("name_en"):    parts.append(row["name_en"].strip())
    if row.get("set_id"):     parts.append(row["set_id"].strip().upper())
    if row.get("card_number"):parts.append(row["card_number"].strip().lstrip("0") or "0")
    return " ".join(parts)


def _median_usd(hits):
    usd = []
    for h in hits:
        if (h.get("currency") or "").upper() == "USD":
            try:
                p = float(h["price"])
                if p > 0: usd.append(p)
            except (TypeError, ValueError, KeyError):
                continue
    return (round(statistics.median(usd), 2), len(usd)) if usd else (0.0, 0)


# ---------- AI curation (optional) ----------

def _ai_curate(candidates, keep=5):
    """Ask GPT-4o-mini to pick the most-likely-to-keep-moving cards.
    Returns a sublist (preserves dict shape).  On any error, returns the
    statistical top-`keep` unchanged — never blocks the watcher."""
    try:
        import openai
        client = openai.OpenAI()
    except Exception:
        return candidates[:keep]

    snippet = [
        {"i": i,
         "name": c["name_en"], "set": c["set_id"], "num": c["card_number"],
         "price": round(float(c.get("p_24h") or c.get("p_7d") or 0), 2),
         "pct_24h": round(c["pct_24h"] or 0, 1),
         "pct_7d":  round(c["pct_7d"]  or 0, 1)}
        for i, c in enumerate(candidates[:20])
    ]
    prompt = (
        "You are a Pokemon TCG market analyst. Below are 20 cards whose USD "
        "price moved the most in the last 24h or 7d. Pick the {k} most likely "
        "to KEEP moving over the next 7 days (rotation, tournament demand, "
        "set spike) vs already-peaked pump-and-dumps. "
        "Reply with ONLY a JSON array of the indices you pick, e.g. [0,3,7,11,14]."
        "\n\n{data}"
    ).format(k=keep, data=json.dumps(snippet, ensure_ascii=False))
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            timeout=20,
        )
        txt = resp.choices[0].message.content.strip()
        # Robust extract — model sometimes wraps in ```json```
        l, r = txt.find("["), txt.rfind("]")
        idxs = json.loads(txt[l:r+1]) if l >= 0 else []
        picks = [candidates[i] for i in idxs if 0 <= i < len(candidates)][:keep]
        return picks or candidates[:keep]
    except Exception as e:
        print(f"[c17] AI curate failed ({e}) — falling back to statistical top-{keep}", flush=True)
        return candidates[:keep]


# ---------- main loop ----------

def _tick(conn, args):
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    _ensure_quota_table(cur); conn.commit()

    used = _calls_today(cur)
    remaining = max(0, args.max_calls_per_day - used)
    if remaining <= 0:
        print(f"[c17] daily quota exhausted ({used}/{args.max_calls_per_day}) — sleeping until next tick", flush=True)
        return

    cur.execute(_TIER_SQL)
    rows = cur.fetchall()
    hot, warm = _classify(rows, args)

    candidates = hot + warm
    if not candidates:
        print(f"[c17] no movers above thresholds (rows_seen={len(rows)})  used_today={used}/{args.max_calls_per_day}", flush=True)
        return

    if args.ai_curate:
        picks = _ai_curate(candidates, keep=args.batch)
    else:
        picks = candidates[:args.batch]

    picks = picks[:remaining]   # don't exceed daily cap

    print(f"[c17] tick: hot={len(hot)} warm={len(warm)} picked={len(picks)} "
          f"used_today={used}/{args.max_calls_per_day} ai_curate={args.ai_curate}",
          flush=True)

    for i, c in enumerate(picks, 1):
        if _STOP: break
        q = _build_query(c)
        try:
            hits = search_ebay_sold(q, limit=20)
        except Exception as e:
            print(f"[c17]   {c['card_id']}  API ERROR: {e}", flush=True)
            time.sleep(args.rate_sec)
            continue

        _bump_quota(cur); conn.commit()
        median, n = _median_usd(hits)

        tier = "HOT " if c in hot else "WARM"
        if n >= 3 and median > 0:
            cur.execute("""
                INSERT INTO price_history
                  (card_id, card_name, market_price, fetched_ms,
                   source, currency, price_usd, price_native, query_used, observed_at)
                VALUES (%s,%s,%s,%s,'ebay_sold','USD',%s,%s,%s, NOW())
            """, (c["card_id"].upper(), (c["name_en"] or c["card_id"])[:160],
                  median, int(time.time()*1000),
                  median, median, q[:200]))
            conn.commit()
            print(f"[c17]   {tier} {c['card_id']:14s} q={q!r}  hits={n}  ${median:.2f}  "
                  f"(24h={c['pct_24h']:.0f}% 7d={c['pct_7d']:.0f}% age={c['age_h']:.1f}h)",
                  flush=True)
        else:
            print(f"[c17]   {tier} {c['card_id']:14s} q={q!r}  hits={n}  -> below noise floor",
                  flush=True)

        if i < len(picks):
            time.sleep(args.rate_sec)

    cur.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tick-min",          type=int,   default=60)
    p.add_argument("--batch",             type=int,   default=4)
    p.add_argument("--rate-sec",          type=float, default=17.0)
    p.add_argument("--max-calls-per-day", type=int,   default=1000)
    p.add_argument("--hot-pct",           type=float, default=20.0)
    p.add_argument("--warm-pct",          type=float, default=30.0)
    p.add_argument("--hot-min-usd",       type=float, default=5.0)
    p.add_argument("--warm-min-usd",      type=float, default=50.0)
    p.add_argument("--hot-cooldown-h",    type=float, default=4.0)
    p.add_argument("--warm-cooldown-h",   type=float, default=24.0)
    p.add_argument("--ai-curate",         action="store_true")
    p.add_argument("--once",              action="store_true")
    args = p.parse_args()

    if not EBAY_APP_ID:
        sys.exit("FATAL: EBAY_APP_ID not set inside the pos container")

    db_url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not db_url:
        sys.exit("FATAL: DATABASE_URL not set")

    conn = psycopg2.connect(db_url); conn.autocommit = False
    print(f"[c17] watcher started  tick={args.tick_min}m  batch={args.batch}  "
          f"max_calls/day={args.max_calls_per_day}  ai_curate={args.ai_curate}",
          flush=True)

    while True:
        try:
            _tick(conn, args)
        except Exception as e:
            print(f"[c17] tick error: {e}", flush=True)
            try: conn.rollback()
            except Exception: pass
        if args.once or _STOP:
            break
        # Sleep in 30s chunks so SIGTERM is responsive
        end = time.time() + args.tick_min * 60
        while time.time() < end and not _STOP:
            time.sleep(30)

    conn.close()
    print("[c17] watcher exited cleanly", flush=True)


if __name__ == "__main__":
    main()
