# HanryxVault — Unified Card Database Plan

Goal: take everything you've collected (your own Excel/text files in `Card-Database`, plus all the data forks under `Ngansen/*`) and compile it into one offline-first database on the Pi that the POS can search across all four languages instantly.

This document is the **plan only** — no code changes yet.

---

## Part 1 — Inventory of every data source

### 1A. Your own collected data (`Ngansen/Card-Database`)

| File | Rows | Structure | What it is |
|---|---:|---|---|
| `ALL English Pokémon Cards.xlsx` | **~32,050** | `ID, Set, Number, PokéDex ID, Card Name, Type, Rarity / Variant, Other Pokémon in Artwork, EX Serial Number(s)` | The most comprehensive English catalogue you have — every set from Base through Crown Zenith, with reverse-holo variants tracked as separate rows |
| `Pokemon TCG Spreadsheet V3.2.xlsx` | unknown (4.2 MB) | not yet inspected | Another comprehensive English source — likely overlaps with above |
| `Pokémon TCG Checklist.xlsx` | small (43 KB) | not yet inspected | Set/card checklist |
| `Pokemon Sets Collection.xlsx` | small (20 KB) | not yet inspected | Set list only |
| `Pokemon TCG ex Serial Codes.xlsx` | ~862/sheet × 8 sheets = **~6,900** | per-set sheets: `Name, Type, HP, Stage, #, Rarity, Code 1, RH Code` | EX-era online code authentication (Delta Species, Legend Maker, Holon Phantoms, Crystal Guardians, Dragon Frontiers, Power Keepers, Nintendo Promos, Jumbo Cards) |
| `Pokemon TCG ex Serial Codes - Japanese.xlsx` | similar | similar | JP equivalent |
| `Japanese Pokemon Card Master List 1996 - May 2016.xlsx` | **11,605** | `Card, Era, Type, Rarity, Release Date, Set Name ENG, Set Name JPN, Set Number, Promotional Card Number, Obtained, Personal Card Notes` | Historical JP catalogue 1996–2016 |
| `Japanese Pokemon Card Spreadsheet 2.0 1996-Dec 2017.xlsx` | **13,538** | same + `Special Rarity` | Newer JP catalogue (superset of above) |
| `Copy of Main Pokemon TCG Pocket Tracker v2.xlsx` | unknown (15 MB) | not yet inspected | TCG Pocket tracker (huge — likely deck-builder data) |
| `Korean_Pokemon_Global_Master_Database.xlsx` | small (~30 rows total) | 3 sheets: `Set Registry`, `Master Card Mapping`, `Variant Logic` | **Reference / mapping schema** — KR ↔ EN set names, KR variant terms (e.g. "마스터볼 미러" → Master Ball Holo → `MBH`). Tiny but conceptually important |
| `Chinese_Pokemon_Global_Master_Database.xlsx` | small (~20 rows total) | same 3-sheet layout, includes **Traditional + Simplified** | Same kind of cross-language reference for Chinese |
| `Korean_Cards.txt` | 718 lines | tabular text: `# / Set, Name KR, Name ENG, Source` | Detailed KR **promo** card source-tracking (Movie Promos / Purchase Bonuses / Theme Decks / League & Tournaments / Promo Packs / Event Participation / Misc), Base Set → Sword & Shield era |
| `Pokemon Name List.txt` | ~1,025 | `Number, Name` | Plain English national Pokédex (#001–#1025-ish) |
| `National Dex Reimagined.xlsx` | small (71 KB) | not yet inspected | Custom Pokédex variant — probably skip for POS |
| `Helpful Websites of Data input.txt` | 1 line | URL | One Korean info link (poisonpie.com) |

**Key insight:** the Korean and Chinese "Global Master Database" Excel files are **schema/mapping references**, not bulk data. They're the Rosetta Stone that lets us link Korean/Chinese card data to its English equivalent. The bulk data for those two languages comes from the forks below.

### 1B. Your forks — actual card data

| Repo | Size | Content | Format |
|---|---:|---|---|
| **`ptcg-kr-db`** | 14 MB | Korean cards, scraped from official `pokemoncard.co.kr`. Subdirs: `card_data/` (per-Pokémon cards), `card_data_product/` (per-product card lists), `card_img/` (images), `product_data/` (sets/releases/prices), `supply_data/` (sleeves/binders) | JSON files |
| **`PTCG-CHS-Datasets`** | 5.7 GB | Simplified Chinese cards. One big `ptcg_chs_infos.json` (21 MB) + `img/` directory (the rest of the 5.7 GB is images) | Single JSON + images |
| **`pokemon-tcg-pocket-database`** | 4.3 MB | TCG Pocket cards. `cards.json` (basic), `cards.extra.json` (detailed), `cards.min.json` (minified), plus `sets.json`. v2 of dataset, npm-published | JSON files |
| **`pokemon-tcg-pocket-cards`** | 1.06 GB | **Alternate** TCG Pocket source. `v1.json`, `v2.json`, `v3.json`, `v4.json` (latest, 1 MB), `expansions.json`. Scraped from Limitless TCG. Also has `images/` dir | JSON files |
| **`cards-database`** (TCGdex) | 64 MB | **Multilingual** TCG card DB (EN/FR/DE/IT/ES/PT/JP/KR/CN). Two trees: `data/` and `data-asia/`. TypeScript-based, has full interfaces in `interfaces.d.ts` | TypeScript modules + JSON |
| **`pokemon-card-jp-database`** | 427 KB | Japanese cards scraped from `pokemon-card.com`. Single `cards.json` (3.2 MB) + `types.ts` schema | Single JSON |
| **`pokemon-tcg-tracker`** | 927 KB | JP card collection tracker. Scraped from Pokellector. Stored in ClickHouse, has Flask app. Mostly **code**, scraper sources in `pokemon_data_scraper/` | Python scraper + DB code |
| **`Pokemon-Card-Database`** | 2.5 MB | Generic JSON DB, format unknown until we look in `data/` | JSON |
| **`PokemonCardDatabaseMaker`** | 3 KB | **Tool only** — `main.py` + `ColumnReferences.py` + a tiny example `PkmnCards.db` (20 KB SQLite). Useful as a reference for column-mapping strategy | Python tool |
| **`PokeScraper_3.0`** | 130 KB | **Tool only** — `01_Pokellector_V3.py` + `0_Poke_Sets_V2.py`. Selenium-based scraper that produces the CSVs your existing `import_jpn_cards.py` reads | Python scraper |
| **`pokeapi`** | 53 MB | The official PokéAPI codebase (Pokémon species/moves/abilities), **not card data** — useful only for enrichment (e.g. linking each card's `PokéDex ID` to species data) | Django + JSON fixtures |

### 1C. Data-source forks I can ignore for this plan

`tcgcollector`, `tcglookup-cli`, `tcglookup-js`, `Postman-Api`, `Pokemon-TCG-Price-Scanner`, `pokemon-scanner`, `Pokemon-Card-Scanner`, `pokemon-card-recognizer`, `pokemoncards`, `cardex` — these are tools/UIs, not card data sources.

---

## Part 2 — What's already wired into the POS

| Pi POS table | Importer | Source |
|---|---|---|
| `cards` (English) | `import_tcg_db.py` | Pokémon TCG API (online) |
| `cards_kr` | `import_kr_cards.py` | `ptcg-kr-db` fork |
| `cards_jpn` | `import_jpn_cards.py` | PokeScraper CSVs |
| `cards_jpn_pocket` | `import_jpn_pocket_cards.py` | `pokemon-tcg-pocket-database` fork |
| `cards_chs` | `import_chs_cards.py` | `PTCG-CHS-Datasets` fork |

---

## Part 3 — Gap analysis: what's missing today

1. **No use of your Excel collections.** ~32K English cards + 13K JP cards + EX serial codes are sitting unused in the `Card-Database` repo.
2. **No cross-language linking.** A customer hands over a Korean Pikachu — there's no way to know it's the same card as the English one.
3. **No second-source verification.** `cards-database` (TCGdex) is multilingual and well-structured — perfect as a "did the API miss this?" cross-check.
4. **Variant terminology isn't mapped.** Your KR/CN "Master Ball Holo" mapping sheets aren't loaded anywhere — the system can't recognise "마스터볼 미러" and "Master Ball Holo" as the same thing.
5. **EX-era serial codes aren't usable.** Authentication of online codes is impossible.
6. **No "promo source" tracking.** Your detailed `Korean_Cards.txt` (Movie Promos, Theme Decks, etc.) gives provenance — useful for pricing rare promos correctly — but isn't loaded.

---

## Part 4 — Proposed unified architecture

Three layers, every layer is on the USB drive and gets refreshed nightly:

### Layer 1 — Raw source tables (one per source, never merged)

Keeps every source separately so we can audit what came from where, and re-run a single importer without touching the others.

| Table | Source | Already exists? |
|---|---|---|
| `src_eng_api` | Pokémon TCG API | yes — currently called `cards` |
| `src_eng_xlsx` | Your `ALL English Pokémon Cards.xlsx` + `Pokemon TCG Spreadsheet V3.2.xlsx` | **NEW** |
| `src_eng_ex_codes` | Your `Pokemon TCG ex Serial Codes.xlsx` | **NEW** |
| `src_kr_official` | `ptcg-kr-db` fork | yes — currently called `cards_kr` |
| `src_kr_promos` | Your `Korean_Cards.txt` | **NEW** |
| `src_jp_pokellector` | PokeScraper CSVs | yes — currently `cards_jpn` |
| `src_jp_xlsx` | Your two Japanese spreadsheets (1996-2017) | **NEW** |
| `src_jp_pokemoncardcom` | `pokemon-card-jp-database` fork | **NEW** |
| `src_jp_ex_codes` | Your JP EX Serial Codes Excel | **NEW** |
| `src_chs_official` | `PTCG-CHS-Datasets` fork | yes — currently `cards_chs` |
| `src_pocket_official` | `pokemon-tcg-pocket-database` fork | yes — currently `cards_jpn_pocket` |
| `src_pocket_limitless` | `pokemon-tcg-pocket-cards` fork (alternate) | **NEW** |
| `src_tcgdex_multi` | `cards-database` (TCGdex, multilingual) | **NEW** |

### Layer 2 — Reference / mapping tables

Small but critical — these power cross-language search and variant detection.

| Table | Source | Purpose |
|---|---|---|
| `ref_set_mapping` | Korean + Chinese Master Database Excel | Set ID ↔ KR name ↔ EN name ↔ CN name ↔ JP name ↔ release year |
| `ref_variant_terms` | Korean + Chinese Master Database Excel `Variant Logic` sheets | "마스터볼 미러" / "大師球鏡面" / "Master Ball Holo" → internal code `MBH` |
| `ref_pokedex_species` | `pokeapi` fork (selected fixtures) | National Dex # ↔ name in EN/JP/KR/CN/FR/DE… (for typo-tolerant search) |
| `ref_promo_provenance` | Your `Korean_Cards.txt` | Per-promo: source category (Movie/Tournament/Purchase Bonus/etc.) — drives pricing rules |

### Layer 3 — `cards_master` (the unified view)

One row per **logical card** (set + number + variant). All language names + all rarity codes + all serial codes joined together. This is what the POS searches.

```
cards_master:
  master_id          (synthetic primary key)
  set_id             (canonical TCGdex set ID, e.g. 'sv8')
  card_number        ('001/106')
  variant_code       ('MBH', 'PBH', 'SAR', '1ED', 'STD', ...)
  pokedex_id         (NULL for Trainers/Energy)
  name_en            'Pikachu ex'
  name_kr            '피카츄 ex'
  name_jp            'ピカチュウ ex'
  name_chs           '皮卡丘 ex'
  name_cht           '皮卡丘 ex'
  type               'Lightning'
  rarity_code        'RR'
  hp                 70
  stage              'Basic'
  ex_serial_codes    JSON array (for online code lookup)
  promo_source       'Movie Promos' (or NULL)
  source_refs        JSON: which Layer-1 rows contributed each field
  image_url          best available
  first_seen         timestamp
  last_updated       timestamp
```

The unified row is built by a **consolidator script** that:
1. Reads every Layer-1 source for a given set + card number.
2. Picks the best value for each field using a documented priority order (e.g. for `name_en`, prefer TCGdex > Pokémon TCG API > xlsx; for `name_kr`, prefer ptcg-kr-db > KR Master Database).
3. Records which source contributed each field in `source_refs` (auditability).

---

## Part 5 — Implementation tasks (proposed order)

Each task is a separate, mergeable unit so we can stop after any of them and still have a working improvement.

| # | Task | Adds | Effort |
|---|---|---|---|
| **U1** | Schema migration: rename `cards`/`cards_kr`/`cards_jpn`/`cards_jpn_pocket`/`cards_chs` → `src_*_*` tables (views kept for back-compat) | 0 new data, just renames | S |
| **U2** | Reference loader: `import_ref_mappings.py` reads the two Korean/Chinese Master Database xlsx files into `ref_set_mapping` + `ref_variant_terms` | ~30 set mappings, ~10 variant terms | S |
| **U3** | English xlsx loader: `import_eng_xlsx.py` reads `ALL English Pokémon Cards.xlsx` into `src_eng_xlsx` | ~32K rows | M |
| **U4** | English EX serial codes loader: `import_ex_codes.py` reads both EX serial-code xlsx files into `src_eng_ex_codes` + `src_jp_ex_codes` | ~6,900 rows × 2 | S |
| **U5** | Japanese xlsx loader: `import_jp_xlsx.py` reads both JP spreadsheets into `src_jp_xlsx` | ~14K rows | M |
| **U6** | Korean promos loader: `import_kr_promos.py` parses `Korean_Cards.txt` into `ref_promo_provenance` | ~700 rows | S |
| **U7** | TCGdex loader: `import_tcgdex.py` reads the `cards-database` fork's TS modules into `src_tcgdex_multi` | many — multilingual | L |
| **U8** | Alt-source loaders: `import_jp_pokemoncardcom.py` + `import_pocket_limitless.py` | medium | M |
| **U9** | Consolidator: `build_cards_master.py` produces `cards_master` from all Layer-1 + Layer-2 tables, with priority rules in a YAML config | core query target | L |
| **U10** | Update `usb_mirror.py` to mirror the new tables to SQLite on `/mnt/cards` | — | S |
| **U11** | Update `cards/fuzzy_search.py` + the `/tcg/search-multi` endpoint to query `cards_master` instead of per-language tables | one query covers all four languages | S |
| **U12** | Add `/admin/db-coverage` endpoint that reports per-set / per-language completeness | operator dashboard | S |
| **U13** | Update `pi-setup/docs/USB_OFFLINE_DB.md` with the new architecture + import order | docs | S |

Effort key: S = <2 hours, M = 2–6 hours, L = full day. Total: ~3–4 sessions.

---

## Part 6 — Decisions I need from you before writing any code

1. **Scope of first slice.** Should I build all 13 tasks, or start with a Minimum Viable Slice (recommended: U1 + U2 + U3 + U7 + U9 + U11)? That gives you cross-language search working with English Excel + TCGdex + existing data, with the consolidator in place — and we can layer the rest on later without rework.

2. **English source priority.** When `ALL English Pokémon Cards.xlsx` and the Pokémon TCG API both have a card, which name wins? My recommendation: **API wins on official names, Excel wins on EX serial codes and "Other Pokémon in Artwork"** (which the API doesn't have).

3. **Excel files: pull from GitHub or commit copies into `Hanryx-Vault-POS`?** Option A: keep them in the `Card-Database` repo and have the importer `git clone` it (consistent with existing `ptcg-kr-db` pattern). Option B: copy the xlsx files into `pi-setup/data/excel/` so the Pi has everything in one repo. I lean toward Option A.

4. **Trade-show urgency.** Is there a date by which the unified DB needs to be live, or can we build incrementally?

5. **Do you want PokéAPI species linking?** Useful for typo-tolerant search ("pikachoo" → Pikachu) but adds 53 MB. Skip if disk space matters.

Tell me your answers (or just "go with your recommendations") and I'll start building.
