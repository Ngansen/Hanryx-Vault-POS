"""
unified/schema.py — CREATE TABLE statements for the unified card DB.

All new tables introduced by the unified layer (src_*, ref_*, cards_master)
live here. Existing per-language tables (cards, cards_kr, cards_jpn,
cards_jpn_pocket, cards_chs) are NOT touched — their schemas stay in the
respective import_* modules. We deliberately did not rename them because
server.py has hundreds of references to the old names; renaming would
have been a large blast-radius change for purely cosmetic benefit.

The consolidator (`build_cards_master.py`) reads from a mix of the old
tables and these new src_* tables and produces `cards_master`.

All schemas are idempotent (`CREATE TABLE IF NOT EXISTS`). Calling
`init_unified_schema(db_conn)` from any importer is safe and cheap.
"""
from __future__ import annotations

import logging

log = logging.getLogger("unified.schema")


# ─── Layer 1: raw source tables (NEW only — old ones stay put) ────────────

# Each src_* table is the raw projection of one upstream source file.
# Nothing is normalised here; that happens in the consolidator. Each row
# carries enough source-of-truth metadata (source filename, raw_row JSONB)
# that we can audit any consolidated value back to its origin.

DDL_SRC_ENG_XLSX = """
CREATE TABLE IF NOT EXISTS src_eng_xlsx (
    src_id            BIGSERIAL PRIMARY KEY,
    source_file       TEXT NOT NULL DEFAULT '',
    sheet_name        TEXT NOT NULL DEFAULT '',
    row_id            TEXT NOT NULL DEFAULT '',     -- 'ID' column from xlsx
    set_name          TEXT NOT NULL DEFAULT '',
    card_number       TEXT NOT NULL DEFAULT '',
    pokedex_id        INTEGER,
    card_name         TEXT NOT NULL DEFAULT '',
    card_type         TEXT NOT NULL DEFAULT '',
    rarity_variant    TEXT NOT NULL DEFAULT '',
    other_pokemon     TEXT NOT NULL DEFAULT '',     -- 'Other Pokémon in Artwork'
    ex_serial_numbers TEXT NOT NULL DEFAULT '',
    raw_row           JSONB,
    imported_at       BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_src_eng_xlsx_name     ON src_eng_xlsx (card_name);
CREATE INDEX IF NOT EXISTS idx_src_eng_xlsx_setnum   ON src_eng_xlsx (set_name, card_number);
CREATE INDEX IF NOT EXISTS idx_src_eng_xlsx_pokedex  ON src_eng_xlsx (pokedex_id);
"""

DDL_SRC_ENG_EX_CODES = """
CREATE TABLE IF NOT EXISTS src_eng_ex_codes (
    src_id        BIGSERIAL PRIMARY KEY,
    source_file   TEXT NOT NULL DEFAULT '',
    set_name      TEXT NOT NULL DEFAULT '',
    card_name     TEXT NOT NULL DEFAULT '',
    card_type     TEXT NOT NULL DEFAULT '',
    hp            INTEGER,
    stage         TEXT NOT NULL DEFAULT '',
    card_number   TEXT NOT NULL DEFAULT '',
    rarity        TEXT NOT NULL DEFAULT '',
    code_1        TEXT NOT NULL DEFAULT '',
    code_2        TEXT NOT NULL DEFAULT '',
    code_3        TEXT NOT NULL DEFAULT '',
    rh_code       TEXT NOT NULL DEFAULT '',         -- reverse-holo code
    raw_row       JSONB,
    imported_at   BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_src_eng_ex_setname  ON src_eng_ex_codes (set_name);
CREATE INDEX IF NOT EXISTS idx_src_eng_ex_name     ON src_eng_ex_codes (card_name);
CREATE INDEX IF NOT EXISTS idx_src_eng_ex_code1    ON src_eng_ex_codes (code_1);
"""

DDL_SRC_JP_EX_CODES = """
CREATE TABLE IF NOT EXISTS src_jp_ex_codes (
    src_id        BIGSERIAL PRIMARY KEY,
    source_file   TEXT NOT NULL DEFAULT '',
    set_name      TEXT NOT NULL DEFAULT '',
    card_name_jp  TEXT NOT NULL DEFAULT '',
    card_name_en  TEXT NOT NULL DEFAULT '',
    card_type     TEXT NOT NULL DEFAULT '',
    hp            INTEGER,
    stage         TEXT NOT NULL DEFAULT '',
    card_number   TEXT NOT NULL DEFAULT '',
    rarity        TEXT NOT NULL DEFAULT '',
    code_1        TEXT NOT NULL DEFAULT '',
    code_2        TEXT NOT NULL DEFAULT '',
    code_3        TEXT NOT NULL DEFAULT '',
    rh_code       TEXT NOT NULL DEFAULT '',
    raw_row       JSONB,
    imported_at   BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_src_jp_ex_setname   ON src_jp_ex_codes (set_name);
CREATE INDEX IF NOT EXISTS idx_src_jp_ex_namejp    ON src_jp_ex_codes (card_name_jp);
CREATE INDEX IF NOT EXISTS idx_src_jp_ex_code1     ON src_jp_ex_codes (code_1);
"""

DDL_SRC_JP_XLSX = """
CREATE TABLE IF NOT EXISTS src_jp_xlsx (
    src_id          BIGSERIAL PRIMARY KEY,
    source_file     TEXT NOT NULL DEFAULT '',
    card_name       TEXT NOT NULL DEFAULT '',
    era             TEXT NOT NULL DEFAULT '',
    card_type       TEXT NOT NULL DEFAULT '',
    rarity          TEXT NOT NULL DEFAULT '',
    special_rarity  TEXT NOT NULL DEFAULT '',
    release_date    TEXT NOT NULL DEFAULT '',
    set_name_eng    TEXT NOT NULL DEFAULT '',
    set_name_jpn    TEXT NOT NULL DEFAULT '',
    set_number      TEXT NOT NULL DEFAULT '',
    promo_number    TEXT NOT NULL DEFAULT '',
    raw_row         JSONB,
    imported_at     BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_src_jp_xlsx_name    ON src_jp_xlsx (card_name);
CREATE INDEX IF NOT EXISTS idx_src_jp_xlsx_setjp   ON src_jp_xlsx (set_name_jpn);
CREATE INDEX IF NOT EXISTS idx_src_jp_xlsx_seteng  ON src_jp_xlsx (set_name_eng);
"""

DDL_SRC_JP_PCC = """
CREATE TABLE IF NOT EXISTS src_jp_pokemoncardcom (
    src_id        BIGSERIAL PRIMARY KEY,
    card_id       TEXT NOT NULL DEFAULT '',        -- pokemon-card.com numeric ID
    set_code      TEXT NOT NULL DEFAULT '',
    set_name      TEXT NOT NULL DEFAULT '',
    card_number   TEXT NOT NULL DEFAULT '',
    name_jp       TEXT NOT NULL DEFAULT '',
    rarity        TEXT NOT NULL DEFAULT '',
    card_type     TEXT NOT NULL DEFAULT '',
    image_url     TEXT NOT NULL DEFAULT '',
    raw_row       JSONB,
    imported_at   BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_src_jp_pcc_namejp  ON src_jp_pokemoncardcom (name_jp);
CREATE INDEX IF NOT EXISTS idx_src_jp_pcc_setnum  ON src_jp_pokemoncardcom (set_code, card_number);
"""

DDL_SRC_JP_CARDS_JSON = """
CREATE TABLE IF NOT EXISTS src_jp_cards_json (
    card_id     TEXT PRIMARY KEY,                  -- pokemon-card-jp-database internal numeric id
    name        TEXT NOT NULL DEFAULT '',          -- JP card name (kana/kanji)
    edition     TEXT NOT NULL DEFAULT '',          -- JP set code (e.g. SV2a, M1L, SVG)
    dimension   TEXT NOT NULL DEFAULT '',          -- height/weight blurb (Pokémon only)
    description TEXT NOT NULL DEFAULT '',          -- flavour text (JP)
    element     TEXT NOT NULL DEFAULT '',          -- energy_type
    health      INTEGER,                           -- HP (Pokémon only)
    numero      INTEGER,                           -- national pokedex number
    attacks     JSONB NOT NULL DEFAULT '[]'::jsonb,
    raw         JSONB NOT NULL DEFAULT '{}'::jsonb,
    imported_at BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_src_jp_cards_json_edition_numero
    ON src_jp_cards_json (edition, numero);
CREATE INDEX IF NOT EXISTS idx_src_jp_cards_json_name
    ON src_jp_cards_json (name);
"""

DDL_SRC_POCKET_LIMITLESS = """
CREATE TABLE IF NOT EXISTS src_pocket_limitless (
    src_id        BIGSERIAL PRIMARY KEY,
    expansion_id  TEXT NOT NULL DEFAULT '',
    card_number   TEXT NOT NULL DEFAULT '',
    name          TEXT NOT NULL DEFAULT '',
    rarity        TEXT NOT NULL DEFAULT '',
    card_type     TEXT NOT NULL DEFAULT '',
    pack          TEXT NOT NULL DEFAULT '',
    image_url     TEXT NOT NULL DEFAULT '',
    raw_row       JSONB,
    imported_at   BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_src_pocket_lt_setnum  ON src_pocket_limitless (expansion_id, card_number);
CREATE INDEX IF NOT EXISTS idx_src_pocket_lt_name    ON src_pocket_limitless (name);
"""

DDL_SRC_TCGDEX_MULTI = """
CREATE TABLE IF NOT EXISTS src_tcgdex_multi (
    src_id        BIGSERIAL PRIMARY KEY,
    set_id        TEXT NOT NULL DEFAULT '',
    card_local_id TEXT NOT NULL DEFAULT '',         -- 'localId' field
    card_global_id TEXT NOT NULL DEFAULT '',        -- 'id' field (set_id + '-' + localId)
    card_type     TEXT NOT NULL DEFAULT '',         -- Pokemon/Trainer/Energy
    rarity        TEXT NOT NULL DEFAULT '',
    hp            INTEGER,
    stage         TEXT NOT NULL DEFAULT '',
    illustrator   TEXT NOT NULL DEFAULT '',
    image_base    TEXT NOT NULL DEFAULT '',
    region        TEXT NOT NULL DEFAULT '',         -- 'data' (intl) or 'data-asia'
    pokedex_ids   JSONB DEFAULT '[]',
    names         JSONB DEFAULT '{}',               -- {"en": "...", "ja": "...", "ko": "...", "zh-cn": "..."}
    raw           JSONB,
    imported_at   BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_src_tcgdex_setid     ON src_tcgdex_multi (set_id);
CREATE INDEX IF NOT EXISTS idx_src_tcgdex_globalid  ON src_tcgdex_multi (card_global_id);
CREATE INDEX IF NOT EXISTS idx_src_tcgdex_local     ON src_tcgdex_multi (set_id, card_local_id);
"""


# ─── Layer 2: reference / mapping tables ──────────────────────────────────

DDL_REF_SET_MAPPING = """
CREATE TABLE IF NOT EXISTS ref_set_mapping (
    set_id          TEXT PRIMARY KEY,                -- canonical TCGdex set ID
    era             TEXT NOT NULL DEFAULT '',
    name_en         TEXT NOT NULL DEFAULT '',
    name_kr         TEXT NOT NULL DEFAULT '',
    name_jp         TEXT NOT NULL DEFAULT '',
    name_chs        TEXT NOT NULL DEFAULT '',
    name_cht        TEXT NOT NULL DEFAULT '',
    release_year    TEXT NOT NULL DEFAULT '',
    region          TEXT NOT NULL DEFAULT '',        -- 'Simplified' / 'Traditional' / etc
    aliases         JSONB DEFAULT '[]',              -- alternate set IDs / abbreviations
    raw             JSONB,
    imported_at     BIGINT NOT NULL DEFAULT 0
);
"""

DDL_REF_VARIANT_TERMS = """
CREATE TABLE IF NOT EXISTS ref_variant_terms (
    variant_code    TEXT PRIMARY KEY,                -- 'MBH', 'PBH', '1ED', 'STD', 'SAR', 'RH', ...
    en_term         TEXT NOT NULL DEFAULT '',
    kr_term         TEXT NOT NULL DEFAULT '',
    jp_term         TEXT NOT NULL DEFAULT '',
    cht_term        TEXT NOT NULL DEFAULT '',
    chs_term        TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    imported_at     BIGINT NOT NULL DEFAULT 0
);
"""

DDL_REF_PROMO_PROVENANCE = """
CREATE TABLE IF NOT EXISTS ref_promo_provenance (
    promo_id        BIGSERIAL PRIMARY KEY,
    source_category TEXT NOT NULL DEFAULT '',        -- 'Movie Promos', 'Theme Decks', etc
    set_label       TEXT NOT NULL DEFAULT '',        -- e.g. 'XY-P', 'SM-P'
    card_number     TEXT NOT NULL DEFAULT '',
    name_kr         TEXT NOT NULL DEFAULT '',
    name_en         TEXT NOT NULL DEFAULT '',
    notes           TEXT NOT NULL DEFAULT '',
    raw             JSONB,
    imported_at     BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ref_promo_namekr   ON ref_promo_provenance (name_kr);
CREATE INDEX IF NOT EXISTS idx_ref_promo_nameen   ON ref_promo_provenance (name_en);
CREATE INDEX IF NOT EXISTS idx_ref_promo_setnum   ON ref_promo_provenance (set_label, card_number);
"""

DDL_REF_POKEDEX_SPECIES = """
CREATE TABLE IF NOT EXISTS ref_pokedex_species (
    pokedex_no    INTEGER PRIMARY KEY,
    name_en       TEXT NOT NULL DEFAULT '',
    name_jp       TEXT NOT NULL DEFAULT '',
    name_jp_kana  TEXT NOT NULL DEFAULT '',
    name_kr       TEXT NOT NULL DEFAULT '',
    name_chs      TEXT NOT NULL DEFAULT '',
    name_cht      TEXT NOT NULL DEFAULT '',
    name_fr       TEXT NOT NULL DEFAULT '',
    name_de       TEXT NOT NULL DEFAULT '',
    generation    INTEGER,
    raw           JSONB,
    imported_at   BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ref_dex_name_en   ON ref_pokedex_species (name_en);
CREATE INDEX IF NOT EXISTS idx_ref_dex_name_jp   ON ref_pokedex_species (name_jp);
CREATE INDEX IF NOT EXISTS idx_ref_dex_name_kr   ON ref_pokedex_species (name_kr);
CREATE INDEX IF NOT EXISTS idx_ref_dex_name_chs  ON ref_pokedex_species (name_chs);
"""


# ─── Layer 3: cards_master (the unified denormalised view) ────────────────

# One row per LOGICAL card (set_id, card_number, variant_code). All
# language names + serial codes joined into a single row. Built by
# build_cards_master.py from the consolidator priority rules.
#
# We DON'T mark this as a PostgreSQL VIEW because rebuilds would be
# expensive on every read; instead it's a materialised projection
# rebuilt by the consolidator (DROP+CREATE+INSERT inside one txn).

DDL_CARDS_MASTER = """
CREATE TABLE IF NOT EXISTS cards_master (
    master_id        BIGSERIAL PRIMARY KEY,
    set_id           TEXT NOT NULL,
    card_number      TEXT NOT NULL,
    variant_code     TEXT NOT NULL DEFAULT 'STD',
    pokedex_id       INTEGER,
    name_en          TEXT NOT NULL DEFAULT '',
    name_kr          TEXT NOT NULL DEFAULT '',
    name_jp          TEXT NOT NULL DEFAULT '',
    name_chs         TEXT NOT NULL DEFAULT '',
    name_cht         TEXT NOT NULL DEFAULT '',
    name_fr          TEXT NOT NULL DEFAULT '',
    name_de          TEXT NOT NULL DEFAULT '',
    name_it          TEXT NOT NULL DEFAULT '',
    name_es          TEXT NOT NULL DEFAULT '',
    card_type        TEXT NOT NULL DEFAULT '',         -- Pokemon/Trainer/Energy
    energy_type      TEXT NOT NULL DEFAULT '',         -- Lightning/Water/etc
    subtype          TEXT NOT NULL DEFAULT '',
    stage            TEXT NOT NULL DEFAULT '',
    rarity           TEXT NOT NULL DEFAULT '',
    rarity_code      TEXT NOT NULL DEFAULT '',
    hp               INTEGER,
    artist           TEXT NOT NULL DEFAULT '',
    ex_serial_codes  JSONB DEFAULT '[]',
    other_pokemon    TEXT NOT NULL DEFAULT '',
    promo_source     TEXT NOT NULL DEFAULT '',
    image_url        TEXT NOT NULL DEFAULT '',
    image_url_alt    JSONB DEFAULT '[]',
    source_refs      JSONB NOT NULL DEFAULT '{}',      -- {field_name: 'src_table:src_id'}
    first_seen       BIGINT NOT NULL DEFAULT 0,
    last_built       BIGINT NOT NULL DEFAULT 0,
    UNIQUE (set_id, card_number, variant_code)
);
CREATE INDEX IF NOT EXISTS idx_cards_master_name_en   ON cards_master (name_en);
CREATE INDEX IF NOT EXISTS idx_cards_master_name_kr   ON cards_master (name_kr);
CREATE INDEX IF NOT EXISTS idx_cards_master_name_jp   ON cards_master (name_jp);
CREATE INDEX IF NOT EXISTS idx_cards_master_name_chs  ON cards_master (name_chs);
CREATE INDEX IF NOT EXISTS idx_cards_master_setnum    ON cards_master (set_id, card_number);
CREATE INDEX IF NOT EXISTS idx_cards_master_pokedex   ON cards_master (pokedex_id);
"""

# Trigram indexes for fuzzy search. Only created if pg_trgm is available
# (it should be — server.init_db creates the extension at startup).
DDL_CARDS_MASTER_TRGM = """
CREATE INDEX IF NOT EXISTS idx_cards_master_name_en_trgm
    ON cards_master USING gin (name_en gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_cards_master_name_kr_trgm
    ON cards_master USING gin (name_kr gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_cards_master_name_jp_trgm
    ON cards_master USING gin (name_jp gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_cards_master_name_chs_trgm
    ON cards_master USING gin (name_chs gin_trgm_ops);
"""


# ─── Continuous Discovery v1 (D1) ─────────────────────────────────────────
# Queue of sets/cards/operator-reports awaiting auto-import; written by the
# probes (discover_new_sets.py) and the /admin/search "Report missing" button,
# drained by discovery_dispatch.py. Decoupling probe from dispatch lets the
# operator UI surface in-flight work without blocking on subprocess runs.
DDL_DISCOVERY_QUEUE = """
CREATE TABLE IF NOT EXISTS discovery_queue (
    id              BIGSERIAL PRIMARY KEY,
    kind            TEXT NOT NULL,                 -- 'set' | 'card' | 'report'
    payload         JSONB NOT NULL DEFAULT '{}',   -- {set_id, query, ...}
    source          TEXT NOT NULL DEFAULT '',      -- 'tcgdex' | 'operator' | ...
    reporter        TEXT NOT NULL DEFAULT 'worker',-- 'worker' | 'operator'
    status          TEXT NOT NULL DEFAULT 'pending',
        -- 'pending' | 'running' | 'resolved' | 'failed' | 'noop'
    discovered_at   BIGINT NOT NULL DEFAULT 0,     -- epoch ms
    resolved_at     BIGINT NOT NULL DEFAULT 0,
    next_attempt_at BIGINT NOT NULL DEFAULT 0,     -- backoff target (epoch ms)
    attempts        INT    NOT NULL DEFAULT 0,
    last_error      TEXT   NOT NULL DEFAULT '',
    resolved_master_id BIGINT
);
CREATE INDEX IF NOT EXISTS idx_discovery_queue_status_kind
    ON discovery_queue (status, kind);
CREATE INDEX IF NOT EXISTS idx_discovery_queue_discovered_at
    ON discovery_queue (discovered_at DESC);
-- Dedup for set-discovery: only one open row per set_id
CREATE UNIQUE INDEX IF NOT EXISTS uq_discovery_queue_pending_set
    ON discovery_queue ((payload->>'set_id'))
 WHERE kind = 'set' AND status IN ('pending','running');
-- Dedup for operator reports: only one open row per query string. Closes the
-- SELECT-then-INSERT race in /admin/discovery/report when two tablets POST
-- the same missing-card query within milliseconds of each other.
CREATE UNIQUE INDEX IF NOT EXISTS uq_discovery_queue_pending_report
    ON discovery_queue ((payload->>'query'))
 WHERE kind = 'report' AND status = 'pending';
"""

DDL_DISCOVERY_LOG = """
CREATE TABLE IF NOT EXISTS discovery_log (
    log_id          BIGSERIAL PRIMARY KEY,
    queue_id        BIGINT REFERENCES discovery_queue(id) ON DELETE CASCADE,
    attempted_at    BIGINT NOT NULL DEFAULT 0,
    duration_ms     INT    NOT NULL DEFAULT 0,
    source_tried    TEXT   NOT NULL DEFAULT '',  -- 'tcgdex','pokemon-card.com',...
    outcome         TEXT   NOT NULL DEFAULT '',  -- 'resolved' | 'noop' | 'error'
    cards_added     INT    NOT NULL DEFAULT 0,
    note            TEXT   NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_discovery_log_queue
    ON discovery_log (queue_id);
CREATE INDEX IF NOT EXISTS idx_discovery_log_attempted_at
    ON discovery_log (attempted_at DESC);
"""


# ─── Public entry points ──────────────────────────────────────────────────

_ALL_DDL = [
    ("src_eng_xlsx",            DDL_SRC_ENG_XLSX),
    ("src_eng_ex_codes",        DDL_SRC_ENG_EX_CODES),
    ("src_jp_ex_codes",         DDL_SRC_JP_EX_CODES),
    ("src_jp_xlsx",             DDL_SRC_JP_XLSX),
    ("src_jp_pokemoncardcom",   DDL_SRC_JP_PCC),
    ("src_jp_cards_json",       DDL_SRC_JP_CARDS_JSON),
    ("src_pocket_limitless",    DDL_SRC_POCKET_LIMITLESS),
    ("src_tcgdex_multi",        DDL_SRC_TCGDEX_MULTI),
    ("ref_set_mapping",         DDL_REF_SET_MAPPING),
    ("ref_variant_terms",       DDL_REF_VARIANT_TERMS),
    ("ref_promo_provenance",    DDL_REF_PROMO_PROVENANCE),
    ("ref_pokedex_species",     DDL_REF_POKEDEX_SPECIES),
    ("cards_master",            DDL_CARDS_MASTER),
    ("discovery_queue",         DDL_DISCOVERY_QUEUE),
    ("discovery_log",           DDL_DISCOVERY_LOG),
]


def init_unified_schema(db_conn) -> dict:
    """Create every table the unified layer needs. Idempotent.

    Returns: {"tables_ensured": [name, ...], "trigram_ok": bool}
    """
    cur = db_conn.cursor()
    ensured: list[str] = []
    for name, ddl in _ALL_DDL:
        cur.execute(ddl)
        ensured.append(name)
        log.debug("[unified.schema] ensured %s", name)
    db_conn.commit()

    # Trigram indexes are best-effort. server.init_db creates pg_trgm at
    # startup; if for some reason it isn't there we don't want to crash
    # the importer — fuzzy-search will just be slower.
    trgm_ok = False
    try:
        cur.execute(DDL_CARDS_MASTER_TRGM)
        db_conn.commit()
        trgm_ok = True
    except Exception as e:
        db_conn.rollback()
        log.warning("[unified.schema] trigram indexes skipped (pg_trgm missing?): %s", e)

    log.info("[unified.schema] init complete (%d tables, trgm=%s)", len(ensured), trgm_ok)
    return {"tables_ensured": ensured, "trigram_ok": trgm_ok}


if __name__ == "__main__":
    # CLI entry point so `python3 unified/schema.py` runs against DATABASE_URL
    # for a quick sanity check during development.
    import os
    import sys

    import psycopg2

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        sys.exit(1)

    with psycopg2.connect(url) as conn:
        result = init_unified_schema(conn)
    print(result)
