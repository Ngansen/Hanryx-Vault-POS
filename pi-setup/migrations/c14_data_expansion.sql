-- C14: data expansion
--   #2 variant differentiation on cards_master
--   #7 card_text + abilities_jsonb + attacks_jsonb on cards_master
--   #1 sealed_products + sealed_price_history
--   #3 buyback_rules (condition-tiered, with sensible defaults)
--   #4 price_trends_daily materialised view (7d/30d/90d % change)
--   #5 ebay_sold support (no schema change — uses price_history with source='ebay_sold')
-- Idempotent: safe to re-run. ADD COLUMN IF NOT EXISTS / CREATE IF NOT EXISTS throughout.
BEGIN;

-- ─── #2 + #7: cards_master enrichment ───
ALTER TABLE cards_master
  ADD COLUMN IF NOT EXISTS variant         TEXT DEFAULT 'normal',
  ADD COLUMN IF NOT EXISTS abilities_jsonb JSONB,
  ADD COLUMN IF NOT EXISTS attacks_jsonb   JSONB,
  ADD COLUMN IF NOT EXISTS card_text       TEXT,
  ADD COLUMN IF NOT EXISTS rarity_subtype  TEXT;

COMMENT ON COLUMN cards_master.variant IS
  'normal | reverse_holo | holo | full_art | special_illust | hyper_rare | secret_rare | rainbow_rare | shiny | promo | first_edition | shadowless | unlimited';

CREATE INDEX IF NOT EXISTS idx_cards_master_variant ON cards_master(variant);
CREATE INDEX IF NOT EXISTS idx_cards_master_card_text_trgm
  ON cards_master USING gin (card_text gin_trgm_ops)
  WHERE card_text IS NOT NULL;

-- ─── #1: sealed_products ───
CREATE TABLE IF NOT EXISTS sealed_products (
  id               SERIAL PRIMARY KEY,
  set_id           TEXT,
  set_name         TEXT,
  name             TEXT NOT NULL,
  product_type     TEXT,
  language         TEXT DEFAULT 'en',
  image_url        TEXT,
  upc              TEXT,
  msrp_usd         NUMERIC(10,2),
  market_price_usd NUMERIC(10,2),
  market_price_native NUMERIC(12,2),
  native_currency  TEXT,
  source           TEXT,
  source_url       TEXT,
  qty_on_hand      INTEGER DEFAULT 0,
  cost_basis_usd   NUMERIC(10,2),
  bin_location     TEXT,
  notes            TEXT,
  created_at       TIMESTAMPTZ DEFAULT NOW(),
  updated_at       TIMESTAMPTZ DEFAULT NOW(),
  CONSTRAINT uq_sealed_natkey UNIQUE (set_id, name, product_type, language)
);
COMMENT ON COLUMN sealed_products.product_type IS
  'booster_box | etb | blister | premium_collection | theme_deck | tin | bundle | single_pack | other';
CREATE INDEX IF NOT EXISTS idx_sealed_set  ON sealed_products(set_id);
CREATE INDEX IF NOT EXISTS idx_sealed_type ON sealed_products(product_type);

CREATE TABLE IF NOT EXISTS sealed_price_history (
  id          BIGSERIAL PRIMARY KEY,
  product_id  INTEGER REFERENCES sealed_products(id) ON DELETE CASCADE,
  source      TEXT,
  price       NUMERIC(12,2),
  currency    TEXT,
  price_usd   NUMERIC(12,2),
  observed_at TIMESTAMPTZ DEFAULT NOW(),
  query_used  TEXT
);
CREATE INDEX IF NOT EXISTS idx_sealed_hist_observed  ON sealed_price_history(observed_at);
CREATE INDEX IF NOT EXISTS idx_sealed_hist_prod_src  ON sealed_price_history(product_id, source);

-- ─── #3: buyback_rules ───
CREATE TABLE IF NOT EXISTS buyback_rules (
  id            SERIAL PRIMARY KEY,
  rule_name     TEXT NOT NULL,
  set_id        TEXT,
  rarity        TEXT,
  game_code     TEXT DEFAULT 'pokemon',
  condition     TEXT NOT NULL CHECK (condition IN ('NM','LP','MP','HP','DMG')),
  ratio         NUMERIC(5,4) NOT NULL CHECK (ratio >= 0 AND ratio <= 2),
  min_price_usd NUMERIC(10,2) DEFAULT 0,
  active        BOOLEAN DEFAULT TRUE,
  priority      INTEGER DEFAULT 100,
  notes         TEXT,
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  updated_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_buyback_lookup
  ON buyback_rules(active, priority DESC, condition);
CREATE INDEX IF NOT EXISTS idx_buyback_scope
  ON buyback_rules(game_code, COALESCE(set_id,''), COALESCE(rarity,''), condition);

-- Seed defaults only on a fresh table
INSERT INTO buyback_rules (rule_name, condition, ratio, priority)
SELECT * FROM (VALUES
  ('default-NM',  'NM',  0.80::NUMERIC, 10),
  ('default-LP',  'LP',  0.70::NUMERIC, 10),
  ('default-MP',  'MP',  0.55::NUMERIC, 10),
  ('default-HP',  'HP',  0.40::NUMERIC, 10),
  ('default-DMG', 'DMG', 0.20::NUMERIC, 10)
) AS v(rule_name, condition, ratio, priority)
WHERE NOT EXISTS (SELECT 1 FROM buyback_rules);

-- ─── #4: price_trends_daily materialised view ───
DROP MATERIALIZED VIEW IF EXISTS price_trends_daily;
CREATE MATERIALIZED VIEW price_trends_daily AS
WITH latest AS (
  SELECT DISTINCT ON (card_id, source)
    card_id, source, price_usd, observed_at
  FROM price_history
  WHERE price_usd IS NOT NULL
  ORDER BY card_id, source, observed_at DESC
),
ago_7d AS (
  SELECT DISTINCT ON (card_id, source)
    card_id, source, price_usd
  FROM price_history
  WHERE price_usd IS NOT NULL AND observed_at <= NOW() - INTERVAL '7 days'
  ORDER BY card_id, source, observed_at DESC
),
ago_30d AS (
  SELECT DISTINCT ON (card_id, source)
    card_id, source, price_usd
  FROM price_history
  WHERE price_usd IS NOT NULL AND observed_at <= NOW() - INTERVAL '30 days'
  ORDER BY card_id, source, observed_at DESC
),
ago_90d AS (
  SELECT DISTINCT ON (card_id, source)
    card_id, source, price_usd
  FROM price_history
  WHERE price_usd IS NOT NULL AND observed_at <= NOW() - INTERVAL '90 days'
  ORDER BY card_id, source, observed_at DESC
)
SELECT
  l.card_id,
  l.source,
  l.price_usd                             AS price_now,
  a7.price_usd                            AS price_7d_ago,
  a30.price_usd                           AS price_30d_ago,
  a90.price_usd                           AS price_90d_ago,
  CASE WHEN a7.price_usd  > 0 THEN ROUND(((l.price_usd - a7.price_usd)  / a7.price_usd  * 100)::numeric, 2) END AS pct_7d,
  CASE WHEN a30.price_usd > 0 THEN ROUND(((l.price_usd - a30.price_usd) / a30.price_usd * 100)::numeric, 2) END AS pct_30d,
  CASE WHEN a90.price_usd > 0 THEN ROUND(((l.price_usd - a90.price_usd) / a90.price_usd * 100)::numeric, 2) END AS pct_90d,
  l.observed_at                           AS last_seen
FROM latest l
LEFT JOIN ago_7d  a7  ON a7.card_id  = l.card_id AND a7.source  = l.source
LEFT JOIN ago_30d a30 ON a30.card_id = l.card_id AND a30.source = l.source
LEFT JOIN ago_90d a90 ON a90.card_id = l.card_id AND a90.source = l.source;

CREATE UNIQUE INDEX IF NOT EXISTS idx_price_trends_pk    ON price_trends_daily(card_id, source);
CREATE INDEX        IF NOT EXISTS idx_price_trends_pct7  ON price_trends_daily(pct_7d);
CREATE INDEX        IF NOT EXISTS idx_price_trends_pct30 ON price_trends_daily(pct_30d);

-- ─── #5: helper index for ebay_sold filtering ───
CREATE INDEX IF NOT EXISTS idx_ph_source_observed ON price_history(source, observed_at);

COMMIT;
