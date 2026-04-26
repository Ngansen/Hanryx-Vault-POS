"""
unified/priority.py — consolidator priority rules.

Centralised configuration for which source "wins" when multiple
Layer-1 tables have data for the same field on the same logical card.
The consolidator (`build_cards_master.py`) consults this map for
every field and uses the first non-empty value in the listed order.

Source labels used here are short identifiers chosen by the
consolidator; they map to actual table reads inside
`build_cards_master.py`. The mapping is kept centralised here so
adjusting priority is a one-line change without touching the
consolidator logic.

Rules below reflect the user's documented decisions:
  * English names: official Pokémon TCG API wins (it's the licensed
    source-of-truth).
  * EX serial codes: your hand-curated Excel wins (the API doesn't
    have these — it's literally the only source).
  * 'Other Pokémon in Artwork': your Excel wins (same reason).
  * Korean / Chinese / Japanese names: the language-specific scraped
    source wins over TCGdex (which is sometimes English-translated).
"""
from __future__ import annotations

# Source IDs used throughout the consolidator (and surfaced in the
# cards_master.source_refs JSONB for auditability).
#
#   tcg_api       cards table (Pokemon TCG API — official EN source)
#   tcgdex        src_tcgdex_multi (multilingual)
#   eng_xlsx      src_eng_xlsx (your ALL English xlsx)
#   eng_ex        src_eng_ex_codes
#   jp_ex         src_jp_ex_codes
#   kr_official   cards_kr (ptcg-kr-db)
#   jp_pokell     cards_jpn (PokeScraper / Pokellector)
#   jp_xlsx       src_jp_xlsx (your JP master spreadsheets)
#   jp_pcc        src_jp_pokemoncardcom
#   chs_official  cards_chs (PTCG-CHS-Datasets)
#   pocket_off    cards_jpn_pocket (flibustier)
#   pocket_lt     src_pocket_limitless (chase-manning)
#   ref_dex       ref_pokedex_species (PokéAPI species names)
#   ref_promo     ref_promo_provenance
#
# A source listed first wins. Empty string / NULL counts as missing
# and the consolidator falls through to the next source.

PRIORITY: dict[str, list[str]] = {
    # Name resolution
    "name_en":    ["tcg_api", "tcgdex", "eng_xlsx", "ref_dex", "jp_pokell"],
    "name_kr":    ["kr_official", "tcgdex", "ref_dex"],
    "name_jp":    ["jp_pokell", "tcgdex", "jp_xlsx", "jp_pcc", "ref_dex"],
    "name_chs":   ["chs_official", "tcgdex", "ref_dex"],
    "name_cht":   ["tcgdex", "ref_dex"],
    "name_fr":    ["tcgdex", "ref_dex"],
    "name_de":    ["tcgdex", "ref_dex"],
    "name_it":    ["tcgdex"],
    "name_es":    ["tcgdex"],

    # Card attributes
    "card_type":     ["tcg_api", "tcgdex", "eng_xlsx", "kr_official"],
    "energy_type":   ["tcg_api", "tcgdex", "eng_xlsx", "kr_official", "jp_pcc"],
    "subtype":       ["tcg_api", "kr_official"],
    "stage":         ["tcg_api", "tcgdex", "eng_ex", "kr_official"],
    "rarity":        ["tcg_api", "tcgdex", "eng_xlsx", "kr_official", "chs_official"],
    "rarity_code":   ["kr_official", "chs_official", "eng_xlsx"],
    "hp":            ["tcg_api", "tcgdex", "kr_official", "chs_official", "jp_pcc"],
    "artist":        ["tcg_api", "tcgdex", "kr_official"],
    "pokedex_id":    ["tcg_api", "eng_xlsx", "tcgdex", "jp_pcc"],

    # Per-decision: Excel wins on these
    "other_pokemon": ["eng_xlsx"],

    # Image — best-available (TCGdex CDN is most consistent)
    "image_url":     ["tcg_api", "tcgdex", "kr_official", "jp_pokell",
                      "chs_official", "pocket_off", "pocket_lt"],
}


# Compound fields that AGGREGATE across all sources (no first-wins).
# The consolidator collects every non-empty value from every listed
# source and stores them all in the cards_master JSONB column.
AGGREGATES: dict[str, list[str]] = {
    "ex_serial_codes": ["eng_ex", "jp_ex"],
    "image_url_alt":   ["tcg_api", "tcgdex", "kr_official", "jp_pokell",
                        "chs_official", "pocket_off", "pocket_lt"],
}


# Promo source comes from a single reference table.
PROMO_SOURCE_TABLE = "ref_promo"
