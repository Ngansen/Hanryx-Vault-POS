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

DDL_REF_SET_ALIAS = """
CREATE TABLE IF NOT EXISTS ref_set_alias (
    alias_id      BIGSERIAL PRIMARY KEY,
    name_en       TEXT NOT NULL DEFAULT '',
    name_jp       TEXT NOT NULL DEFAULT '',
    name_kr       TEXT NOT NULL DEFAULT '',
    name_chs      TEXT NOT NULL DEFAULT '',
    code_en       TEXT NOT NULL DEFAULT '',     -- e.g. SV1, SWSH1, BS, HF
    code_jp       TEXT NOT NULL DEFAULT '',     -- e.g. SV1, SM8.5, EXP
    code_kr       TEXT NOT NULL DEFAULT '',
    code_chs      TEXT NOT NULL DEFAULT '',
    relationship  TEXT NOT NULL DEFAULT '',     -- '=' '~' '+' 'EN only'
    era           TEXT NOT NULL DEFAULT '',     -- WOTC / EX / DP / BW / XY / SM / SWSH / SV
    imported_at   BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ref_set_alias_en   ON ref_set_alias (UPPER(code_en));
CREATE INDEX IF NOT EXISTS idx_ref_set_alias_jp   ON ref_set_alias (UPPER(code_jp));
CREATE INDEX IF NOT EXISTS idx_ref_set_alias_kr   ON ref_set_alias (UPPER(code_kr));
CREATE INDEX IF NOT EXISTS idx_ref_set_alias_chs  ON ref_set_alias (UPPER(code_chs));
CREATE INDEX IF NOT EXISTS idx_ref_set_alias_era  ON ref_set_alias (era);
"""

DDL_CARD_IMAGE_EMBEDDING = """
CREATE TABLE IF NOT EXISTS card_image_embedding (
    set_id        TEXT NOT NULL,
    card_number   TEXT NOT NULL,
    -- Which image was embedded — recorded for debug & re-embed detection
    -- when the image file changes (e.g. mirror downloaded a sharper copy).
    image_path    TEXT NOT NULL DEFAULT '',
    image_src     TEXT NOT NULL DEFAULT '',     -- 'tcgo|en' style src tag from image_url_alt
    -- Model fingerprint so multiple embedding generations can co-exist
    -- (e.g. ViT-B/32 today, ViT-L/14 next quarter) and the recognizer
    -- can pick the model it was built against.
    model_id      TEXT NOT NULL,                -- 'clip-vit-b32-onnx-1.0'
    -- Embedding stored as REAL[] (native PG array). No pgvector dep —
    -- the recognizer service fetches rows for its model_id and builds
    -- an in-memory index at startup; cosine search runs in Python.
    -- We can switch to pgvector later without changing the schema's
    -- value semantics, just by adding a generated `embedding_v vector`
    -- column populated by trigger.
    embedding     REAL[] NOT NULL DEFAULT '{}'::REAL[],
    embedding_dim INTEGER NOT NULL DEFAULT 0,
    norm_before   REAL NOT NULL DEFAULT 0,      -- L2 norm before normalisation (debug)
    failure       TEXT NOT NULL DEFAULT '',     -- '' = OK; else 'NO_MODEL'|'NO_LIB'|'BAD_IMAGE'|'ORT_ERROR:<...>'
    created_at    BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (set_id, card_number, model_id)
);
CREATE INDEX IF NOT EXISTS idx_cie_model    ON card_image_embedding (model_id);
-- Partial index makes it cheap to find every card that needs a re-try
-- after a model install / data rescue without scanning the table.
CREATE INDEX IF NOT EXISTS idx_cie_failure  ON card_image_embedding (failure)
    WHERE failure <> '';
"""

DDL_CARD_OCR = """
CREATE TABLE IF NOT EXISTS card_ocr (
    set_id        TEXT NOT NULL,
    card_number   TEXT NOT NULL,
    -- Language hint passed to the OCR engine ('japan', 'korean', 'ch',
    -- 'en'); recorded so multi-language cards can co-exist (a foil JP
    -- card with English Pokémon-name overlay benefits from BOTH passes).
    lang_hint     TEXT NOT NULL,
    model_id      TEXT NOT NULL,                -- 'paddleocr-ppocrv4-1.0'
    image_path    TEXT NOT NULL DEFAULT '',
    full_text     TEXT NOT NULL DEFAULT '',     -- joined text for trigram fuzzy search
    lines         JSONB NOT NULL DEFAULT '[]'::jsonb,  -- [{text, conf, bbox:[x1,y1,x2,y2,x3,y3,x4,y4]}, ...]
    line_count    INTEGER NOT NULL DEFAULT 0,
    avg_conf      REAL NOT NULL DEFAULT 0,
    failure       TEXT NOT NULL DEFAULT '',
    created_at    BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (set_id, card_number, lang_hint, model_id)
);
CREATE INDEX IF NOT EXISTS idx_card_ocr_lang     ON card_ocr (lang_hint);
CREATE INDEX IF NOT EXISTS idx_card_ocr_failure  ON card_ocr (failure)
    WHERE failure <> '';
"""

DDL_CARD_LANGUAGE_EXTRA = """
CREATE TABLE IF NOT EXISTS card_language_extra (
    set_id          TEXT NOT NULL,
    card_number     TEXT NOT NULL,
    -- Romanisations (ASCII representation of CJK pronunciations)
    romaji_jp       TEXT NOT NULL DEFAULT '',     -- pykakasi (Japanese → Latin)
    romaji_jp_status TEXT NOT NULL DEFAULT '',    -- 'OK' | 'EMPTY_INPUT' | 'JP_LIB_MISSING' | 'ERROR:<...>'
    pinyin_chs      TEXT NOT NULL DEFAULT '',     -- pypinyin (Simplified Chinese → Pinyin with tone numbers)
    pinyin_chs_status TEXT NOT NULL DEFAULT '',
    hangul_roman    TEXT NOT NULL DEFAULT '',     -- Revised Romanization (Korean → Latin) — built-in pure Python
    hangul_roman_status TEXT NOT NULL DEFAULT '',
    -- Cross-language backfills derived during this run, recorded so
    -- admin can see which name came from where without diffing.
    backfilled_fields JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Library-version stamps so re-running with newer libs is detectable
    library_versions JSONB NOT NULL DEFAULT '{}'::jsonb,
    enriched_at     BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (set_id, card_number)
);
CREATE INDEX IF NOT EXISTS idx_card_lang_extra_romaji  ON card_language_extra (romaji_jp);
CREATE INDEX IF NOT EXISTS idx_card_lang_extra_pinyin  ON card_language_extra (pinyin_chs);
CREATE INDEX IF NOT EXISTS idx_card_lang_extra_hangul  ON card_language_extra (hangul_roman);
"""

DDL_DATA_ANALYSIS_REPORT = """
CREATE TABLE IF NOT EXISTS data_analysis_report (
    report_id      BIGSERIAL PRIMARY KEY,
    report_kind    TEXT NOT NULL,         -- 'completeness' | 'language_coverage' | 'image_coverage'
                                          -- | 'top_gap_sets' | 'rarity_distribution' | 'duplicates'
    payload        JSONB NOT NULL,
    rows_examined  BIGINT NOT NULL DEFAULT 0,
    notes          TEXT NOT NULL DEFAULT '',
    generated_at   BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_data_analysis_kind  ON data_analysis_report (report_kind, generated_at DESC);
-- Convenience view: latest snapshot of each report kind, what the
-- admin dashboard would render. CREATE OR REPLACE makes it cheap to
-- evolve the projection in future slices.
CREATE OR REPLACE VIEW data_analysis_latest AS
SELECT DISTINCT ON (report_kind) *
  FROM data_analysis_report
 ORDER BY report_kind, generated_at DESC;
"""

DDL_BG_TASK_QUEUE = """
CREATE TABLE IF NOT EXISTS bg_task_queue (
    task_id        BIGSERIAL PRIMARY KEY,
    task_type      TEXT NOT NULL,                  -- 'image_health' | 'clip_embed' | 'ocr_index' | 'image_mirror'
    task_key       TEXT NOT NULL DEFAULT '',       -- caller-defined unique identity per (type, key) — e.g. 'sv2/47'
    payload        JSONB NOT NULL DEFAULT '{}',    -- worker-specific input
    priority       INTEGER NOT NULL DEFAULT 100,   -- lower = sooner
    status         TEXT NOT NULL DEFAULT 'PENDING',-- PENDING / CLAIMED / DONE / FAILED
    attempts       INTEGER NOT NULL DEFAULT 0,
    max_attempts   INTEGER NOT NULL DEFAULT 3,
    claimed_at     BIGINT,
    claimed_by     TEXT NOT NULL DEFAULT '',       -- '<hostname>:<pid>'
    completed_at   BIGINT,
    last_error     TEXT NOT NULL DEFAULT '',
    created_at     BIGINT NOT NULL DEFAULT 0,
    UNIQUE (task_type, task_key)
);
-- Hot path: claim next batch of pending tasks of a given type.
CREATE INDEX IF NOT EXISTS idx_bg_task_pending
    ON bg_task_queue (task_type, priority, created_at)
    WHERE status = 'PENDING';
-- Reaper: find tasks stuck in CLAIMED past their timeout.
CREATE INDEX IF NOT EXISTS idx_bg_task_claimed
    ON bg_task_queue (status, claimed_at)
    WHERE status = 'CLAIMED';
-- Admin: review failures.
CREATE INDEX IF NOT EXISTS idx_bg_task_failed
    ON bg_task_queue (task_type, completed_at DESC)
    WHERE status = 'FAILED';
"""

DDL_BG_WORKER_RUN = """
CREATE TABLE IF NOT EXISTS bg_worker_run (
    run_id          BIGSERIAL PRIMARY KEY,
    worker_type     TEXT NOT NULL,
    worker_id       TEXT NOT NULL DEFAULT '',     -- '<hostname>:<pid>'
    started_at      BIGINT NOT NULL,
    ended_at        BIGINT,
    items_claimed   INTEGER NOT NULL DEFAULT 0,
    items_ok        INTEGER NOT NULL DEFAULT 0,
    items_failed    INTEGER NOT NULL DEFAULT 0,
    notes           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_bg_worker_run_type ON bg_worker_run (worker_type, started_at DESC);
"""

DDL_IMAGE_HEALTH_CHECK = """
CREATE TABLE IF NOT EXISTS image_health_check (
    set_id         TEXT NOT NULL,
    card_number    TEXT NOT NULL,
    status         TEXT NOT NULL,                 -- 'OK' | 'PARTIAL' | 'NO_PATHS' | 'ALL_MISSING' | 'ALL_EMPTY' | 'ALL_CORRUPT' | 'MISSING_CARD'
    paths_checked  INTEGER NOT NULL DEFAULT 0,
    paths_ok       INTEGER NOT NULL DEFAULT 0,
    details        JSONB NOT NULL DEFAULT '[]'::jsonb,  -- per-path [{path, status, size_bytes}]
    checked_at     BIGINT NOT NULL,
    PRIMARY KEY (set_id, card_number, checked_at)       -- keep history so admin can see when an image went missing
);
CREATE INDEX IF NOT EXISTS idx_health_card    ON image_health_check (set_id, card_number, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_health_status  ON image_health_check (status, checked_at DESC);
"""

DDL_REF_PROMO_CLASS = """
CREATE TABLE IF NOT EXISTS ref_promo_class (
    class_id        TEXT PRIMARY KEY,           -- 'P-001', 'P-002', …
    promo_name      TEXT NOT NULL DEFAULT '',   -- 'Pikachu Promo'
    promo_category  TEXT NOT NULL DEFAULT '',   -- 'Movie Promo' / 'Anniversary Promo' / …
    variant_en      TEXT NOT NULL DEFAULT '',
    variant_jp      TEXT NOT NULL DEFAULT '',
    variant_kr      TEXT NOT NULL DEFAULT '',
    variant_chs     TEXT NOT NULL DEFAULT '',
    code_en         TEXT NOT NULL DEFAULT '',   -- 'SM234', 'SM-P', 'PR', 'WCS', 'STAFF', …
    code_jp         TEXT NOT NULL DEFAULT '',   -- 'プロモ' (catch-all for most), 'コロコロ', etc.
    code_kr         TEXT NOT NULL DEFAULT '',   -- 'SM-P', 'S-P', 'SV-P', 'KR-MC', …
    code_chs        TEXT NOT NULL DEFAULT '',
    lang_coverage   TEXT NOT NULL DEFAULT '',   -- 'EN/JP/KR/CN' or partial
    notes           TEXT NOT NULL DEFAULT '',
    imported_at     BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_promo_class_en   ON ref_promo_class (UPPER(code_en));
CREATE INDEX IF NOT EXISTS idx_promo_class_jp   ON ref_promo_class (UPPER(code_jp));
CREATE INDEX IF NOT EXISTS idx_promo_class_kr   ON ref_promo_class (UPPER(code_kr));
CREATE INDEX IF NOT EXISTS idx_promo_class_chs  ON ref_promo_class (UPPER(code_chs));
CREATE INDEX IF NOT EXISTS idx_promo_class_cat  ON ref_promo_class (promo_category);
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

# OCR full_text needs trigram for fuzzy "I scanned a card and the
# name reads almost-but-not-quite right" lookups. Same best-effort
# pattern as cards_master — pg_trgm should be present at server
# startup but we don't want OCR-table creation to fail without it.
DDL_CARD_OCR_TRGM = """
CREATE INDEX IF NOT EXISTS idx_card_ocr_full_text_trgm
    ON card_ocr USING gin (full_text gin_trgm_ops);
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

DDL_MIRROR_FETCH_FAILURE = """
CREATE TABLE IF NOT EXISTS mirror_fetch_failure (
    -- One row per UNIQUE source URL that sync_card_mirror has ever
    -- attempted. Acts as both the failure log AND the success
    -- ledger for those URLs (resolved_at flips when a later attempt
    -- finally succeeds). Operators triage with:
    --
    --   SELECT * FROM mirror_fetch_failure
    --    WHERE resolved_at IS NULL
    --    ORDER BY attempt_count DESC, last_attempt_at DESC;
    --
    -- And historical "what URLs have rotted at least once" with:
    --
    --   SELECT * FROM mirror_fetch_failure
    --    WHERE attempt_count >= 3
    --    ORDER BY attempt_count DESC;
    url             TEXT PRIMARY KEY,
    src             TEXT,             -- 'kr_cardimg', 'jp_pcc', 'tcgo', etc.
    dest_path       TEXT,             -- final on-disk destination
    last_status     TEXT NOT NULL,    -- 'http-404', 'too-small', 'err-URLError', 'ok', 'skip-exists'
    attempt_count   INTEGER NOT NULL DEFAULT 1,
    first_seen_at   BIGINT NOT NULL,
    last_attempt_at BIGINT NOT NULL,
    resolved_at     BIGINT            -- NULL while still broken
);
-- Partial index keeps the unresolved triage query O(broken) instead
-- of O(everything-ever-attempted) — Phase C alone has ~120k URLs.
CREATE INDEX IF NOT EXISTS idx_mirror_fetch_failure_unresolved
    ON mirror_fetch_failure (last_attempt_at DESC, attempt_count DESC)
 WHERE resolved_at IS NULL;
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


# ─── KR set completeness audit ────────────────────────────────────────────
#
# The kr_set_audit worker walks the cloned ptcg-kr-db repo and compares
# its canonical (set_id, card_number) inventory against the cards_master
# rows that landed for those same set_ids. Per-set diff is materialised
# here so the operator can see, at a glance, which sets are short and
# which numbers are missing — the difference between "Iono SAR is gone"
# and "32 numbers are gone" matters when triaging a bad import run.
#
# missing_numbers[] = canonical - cards_master  (we have less than upstream)
# extra_numbers[]   = cards_master - canonical  (set_id alias drift)
#
# extra_numbers being non-empty is interesting: it usually means a
# ref_set_alias rule sent the wrong source set into this set_id. We don't
# auto-correct — just expose it.
DDL_KR_SET_GAP = """
CREATE TABLE IF NOT EXISTS kr_set_gap (
    set_id           TEXT PRIMARY KEY,
    expected_count   INTEGER NOT NULL DEFAULT 0,
    actual_count     INTEGER NOT NULL DEFAULT 0,
    missing_numbers  JSONB   NOT NULL DEFAULT '[]'::jsonb,
    extra_numbers    JSONB   NOT NULL DEFAULT '[]'::jsonb,
    audited_at       BIGINT  NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kr_set_gap_audited
    ON kr_set_gap (audited_at DESC);
"""


# ─── Cross-region card alias map (TC ↔ SC ↔ KR ↔ JP ↔ EN) ─────────────────
#
# Single canonical row per physical card across every region we sell at
# the booth. The canonical_key is a stable string derived from the JP
# coordinates — `f"jp:{jp_set_id}:{jp_card_num}"`. JP is the spine
# because every Pokémon TCG release ships in JP first and the JP set
# codes are the most stable (KR sometimes renames, TC rebrands every
# couple of years, SC has the Tencent rename of 2023). One spine, many
# mouths.
#
# Per-region columns are nullable strings — most cards are not printed
# in every region, and a NOT NULL constraint would block partial linkage
# (which is the common case during a set's first week, when JP exists
# but TC/KR don't yet).
#
# match_method records WHICH heuristic produced the link:
#   'manual'      — operator override at /mnt/cards/manual_aliases.json.
#                   Always wins; aliaser refuses to overwrite manual rows.
#   'set_abbrev'  — set abbreviation in canonical_sets JSON (e.g. TC
#                   "SV1S" → JP "SV1S") + same card number. High
#                   confidence, default 1.0.
#   'clip'        — visual CLIP cosine similarity ≥ 0.92 against the JP
#                   equivalent. confidence = the actual cosine score so
#                   the operator can sort low-confidence rows for review.
#   'unmatched'   — best-effort attempt failed; row exists for audit
#                   trail. All region ids NULL except the one we tried
#                   to link FROM (so the dashboard can show "47 TC cards
#                   could not be aliased").
#
# last_verified_at lets the nightly aliaser skip rows that were checked
# recently — only stale rows (or unmatched ones) get re-tried each pass.
DDL_CARD_ALIAS = """
CREATE TABLE IF NOT EXISTS card_alias (
    canonical_key      TEXT PRIMARY KEY,
    jp_id              TEXT,
    kr_id              TEXT,
    en_id              TEXT,
    zh_tc_id           TEXT,
    zh_sc_id           TEXT,
    match_method       TEXT NOT NULL DEFAULT 'unmatched',
    confidence         REAL NOT NULL DEFAULT 0.0,
    source             TEXT NOT NULL DEFAULT 'auto',
    notes              TEXT NOT NULL DEFAULT '',
    created_at         BIGINT NOT NULL DEFAULT 0,
    last_verified_at   BIGINT NOT NULL DEFAULT 0,
    CHECK (match_method IN ('manual', 'set_abbrev', 'clip', 'unmatched'))
);
CREATE INDEX IF NOT EXISTS idx_card_alias_jp     ON card_alias (jp_id)
    WHERE jp_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_card_alias_kr     ON card_alias (kr_id)
    WHERE kr_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_card_alias_zh_tc  ON card_alias (zh_tc_id)
    WHERE zh_tc_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_card_alias_zh_sc  ON card_alias (zh_sc_id)
    WHERE zh_sc_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_card_alias_method ON card_alias
    (match_method, last_verified_at DESC);
"""


# ─── Per-source price breakdown ───────────────────────────────────────────
#
# price_aggregator stores ONE row per query in price_quotes — the median
# across every source. That's the right number to quote at the booth, but
# it hides a class of failure that bites us regularly: one source
# (typically a single thin auction on tcgkorea) skewing the median 2-3×
# higher than every other source agrees on.
#
# This table records a per-source slice so we can A) graph divergence
# over time, and B) auto-flag cards whose sources disagree by more than
# 1.5×. The 1.5× threshold is empirical: TCG market prices rarely
# disagree more than that in a healthy market — once they do, it's
# almost always a stale listing that needs a manual fetch.
#
# Keyed on (card_id, source, fetched_at) — fetched_at is in the key so
# we keep history for trend analysis. Cleanup of old rows is a future
# concern; at ~5 sources × ~daily refresh × 50k cards × 4 bytes ≈ 4 MB/yr
# the table grows slowly enough that an annual prune is fine.
DDL_PRICE_QUOTE_SOURCE = """
CREATE TABLE IF NOT EXISTS price_quote_source (
    card_id       TEXT NOT NULL,
    source        TEXT NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'USD',
    price_usd     NUMERIC(12,4),
    sample_count  INTEGER NOT NULL DEFAULT 0,
    fetched_at    BIGINT  NOT NULL,
    PRIMARY KEY (card_id, source, fetched_at)
);
CREATE INDEX IF NOT EXISTS idx_price_quote_source_card
    ON price_quote_source (card_id, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_price_quote_source_recent
    ON price_quote_source (fetched_at DESC);
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
    ("ref_set_alias",           DDL_REF_SET_ALIAS),
    ("ref_promo_class",         DDL_REF_PROMO_CLASS),
    ("ref_variant_terms",       DDL_REF_VARIANT_TERMS),
    ("ref_promo_provenance",    DDL_REF_PROMO_PROVENANCE),
    ("ref_pokedex_species",     DDL_REF_POKEDEX_SPECIES),
    ("cards_master",            DDL_CARDS_MASTER),
    ("discovery_queue",         DDL_DISCOVERY_QUEUE),
    ("bg_task_queue",           DDL_BG_TASK_QUEUE),
    ("bg_worker_run",           DDL_BG_WORKER_RUN),
    ("image_health_check",      DDL_IMAGE_HEALTH_CHECK),
    ("card_image_embedding",    DDL_CARD_IMAGE_EMBEDDING),
    ("card_ocr",                DDL_CARD_OCR),
    ("card_language_extra",     DDL_CARD_LANGUAGE_EXTRA),
    ("data_analysis_report",    DDL_DATA_ANALYSIS_REPORT),
    ("discovery_log",           DDL_DISCOVERY_LOG),
    ("mirror_fetch_failure",    DDL_MIRROR_FETCH_FAILURE),
    ("kr_set_gap",              DDL_KR_SET_GAP),
    ("card_alias",              DDL_CARD_ALIAS),
    ("price_quote_source",      DDL_PRICE_QUOTE_SOURCE),
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
        cur.execute(DDL_CARD_OCR_TRGM)
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
