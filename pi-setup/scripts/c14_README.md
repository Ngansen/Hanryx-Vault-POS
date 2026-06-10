# C14 — data expansion

Bundles six data improvements into one schema migration + helper scripts.

| # | Feature | Touched |
|---|---------|---------|
| 1 | Sealed-product catalog + price history | `sealed_products`, `sealed_price_history`, `import_sealed_csv.py` |
| 2 | Variant / printing distinction          | `cards_master.variant` |
| 3 | Condition-tiered buyback ratios         | `buyback_rules` (seeded NM/LP/MP/HP/DMG @ 80/70/55/40/20%) |
| 4 | Price trend deltas (7d/30d/90d)         | matview `price_trends_daily`, `refresh_price_trends.py` |
| 5 | eBay sold-listings as price source      | `ebay_sold.py` (no schema change — uses `price_history.source='ebay_sold'`) |
| 7 | Card text + abilities + attacks         | `cards_master.{abilities_jsonb,attacks_jsonb,card_text,rarity_subtype}`, `import_card_details.py` |

## Deploy

```bash
cd ~/Hanryx-Vault-POS/pi-setup
git pull

# Make migrations + scripts visible inside the pos container.
# Easiest: bind-mount via compose.yml, but a one-shot copy works:
docker cp migrations pi-setup-pos-1:/app/migrations
docker cp scripts    pi-setup-pos-1:/app/scripts
docker cp ebay_sold.py pi-setup-pos-1:/app/

# 1. Apply schema (idempotent)
docker compose exec -T pos python /app/scripts/c14_apply.py

# 2. Backfill card text/abilities/attacks/variant from tcgdex (smoke test first)
docker compose exec -T pos python /app/scripts/import_card_details.py --limit 50
docker compose exec -T pos python /app/scripts/import_card_details.py            # full run

# 3. Build the trend view (first run — empty until refresh_market_prices has accumulated history)
docker compose exec -T pos python /app/scripts/refresh_price_trends.py

# 4. (Optional) load sealed-product inventory CSV
docker compose exec -T pos python /app/scripts/import_sealed_csv.py /app/data/sealed.csv
```

## Wire into existing scrapers

Add to `pi-setup/price_scrapers.py`:

```python
from ebay_sold import search_ebay_sold
SCRAPERS["ebay_sold"] = search_ebay_sold
# Optional: only call ebay for cards with market >= $50
_TRANSLATE_LANG["ebay_sold"] = "en"   # eBay catalog is English-first
```

Add to `pi-setup/.env`:

```
EBAY_APP_ID=<your-production-app-id>
EBAY_GLOBAL_ID=EBAY-US      # or EBAY-GB, EBAY-DE, etc.
```

Without `EBAY_APP_ID` the scraper no-ops returning `[]` — won't break anything.

## Cron the trend view refresh

The `sync` container is the natural home. Add to `pi-setup/sync_loop.py` (or wherever
the 6-min tick lives):

```python
if minute_counter % 60 == 0:   # once an hour
    subprocess.run(["python", "/app/scripts/refresh_price_trends.py"], check=False)
```

## Buyback ratios — usage from `ai_assistant.py`

```python
def quote_buyback(card_id, market_usd, condition):
    rule = db.fetchone("""
      SELECT ratio FROM buyback_rules
       WHERE active = TRUE AND condition = %s
         AND (set_id IS NULL OR set_id = (SELECT set_id FROM cards_master WHERE card_id=%s))
         AND %s >= min_price_usd
       ORDER BY priority DESC, set_id NULLS LAST
       LIMIT 1
    """, (condition, card_id, market_usd))
    return market_usd * float(rule[0]) if rule else None
```

## Verifying the migration after apply

```sql
-- Variants distribution
SELECT variant, count(*) FROM cards_master GROUP BY variant ORDER BY 2 DESC;

-- Buyback rules
SELECT rule_name, condition, ratio FROM buyback_rules ORDER BY priority DESC, condition;

-- Trends — top 20 weekly gainers
SELECT card_id, source, price_now, pct_7d FROM price_trends_daily
 WHERE pct_7d > 0 ORDER BY pct_7d DESC LIMIT 20;

-- Sealed coverage
SELECT product_type, count(*) FROM sealed_products GROUP BY product_type;
```
