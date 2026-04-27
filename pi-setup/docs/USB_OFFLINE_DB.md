# Offline Card Database on USB — Operator Guide

This is the deploy + operator guide for the USB-resident offline card
database introduced in the multi-language card rollout. The system runs
entirely on the Pi (Tailscale IP `100.125.5.34`, hostname `hanryxvault`,
user `ngansen`); nothing in this stack needs internet to **read** card
data once the initial sync has completed.

## Architecture in one paragraph

Postgres on the SD card holds the live writes — inventory, sales, laybys,
KR/JPN/CHS/JPN_POCKET card pools, price history. A small `sync` container
runs every 6 minutes and projects the read-only card + recent-price tables
into a single SQLite file at `/mnt/cards/pokedex_local.db` on the USB
drive. The POS, the multilingual fuzzy search, and the AI cashier
assistant all read that SQLite mirror, so when WiFi dies at a trade show
the system stays fully usable. Pulling the USB and plugging it into a
backup Pi gives you an identical read-only kiosk in 30 seconds.

## One-time deploy on the Pi

These steps assume the USB drive is already formatted ext4 with label
`hanryxcards` and mounted at `/mnt/cards` per the existing fstab entry
(`UUID=381e39df-5f24-44ad-a925-38d5b60e4256/mnt/cards ext4 defaults,noatime,nofail 0 2`).

### 1. Pull the new code

```bash
cd ~/Hanryx-Vault-POS
git pull
```

### 2. Seed the USB SQLite from any existing pokedex_local.db

```bash
sudo bash pi-setup/scripts/migrate-db-to-usb.sh
```

The script:
- Refuses to write unless `/mnt/cards` is on a different filesystem from
  `/` (i.e. the USB is actually mounted).
- Looks in every known location for an existing `pokedex_local.db` and
  copies the freshest one onto the USB.
- Creates `/mnt/cards/{faiss,logs,backups}/` for the new code paths.
- Backs up any existing USB copy to `/mnt/cards/backups/` before
  overwriting.

If no existing DB is found, that's fine — the sync orchestrator builds
one from Postgres on its first tick.

### 3. Bring up the new + updated services

```bash
cd ~/Hanryx-Vault-POS/pi-setup
docker compose up -d --build pos recognizer sync assistant
```

Both `sync` and `assistant` are new services. `pos` and `recognizer` are
rebuilt to pick up the bind mount and the new env var.

### 4. Pull the Qwen 2.5 model (one-time, ~2 GB)

```bash
bash pi-setup/scripts/setup-ollama.sh
```

This runs inside the assistant container and caches the model in the
`ollama-data` Docker volume — survives container rebuilds.

### 5. Optional: bulk-embed every card image for visual recognition

This is the biggest one-time job — downloads every card image from KR /
JPN / JPN_POCKET / CHS / EN sources (~70k images, several GB) and
generates CLIP embeddings into pgvector. Resumable; safe to interrupt.

```bash
sudo docker exec -it pi-setup-pos-1 \
    python3 /app/scripts/embed-all-cards.py --source all --resume
```

Run this overnight on first deploy. Subsequent days only need the
`--resume` rerun for newly-released sets.

## Verifying the deploy

```bash
# Did the SQLite mirror build?
ls -lh /mnt/cards/pokedex_local.db
sqlite3 /mnt/cards/pokedex_local.db "SELECT name, COUNT(*) FROM sqlite_master \
    WHERE type='table' GROUP BY name;"

# Is the orchestrator running?
docker logs --tail 50 pi-setup-sync-1

# Is /admin/usb-sync/status reporting fresh data?
curl -s http://localhost:8080/admin/usb-sync/status | jq

# Does multilingual search work?
curl -s 'http://localhost:8080/tcg/search-multi?q=Charizard&limit=5' | jq

# Is the AI cashier assistant up?
curl -s http://localhost:8080/ai/health | jq
curl -s -X POST http://localhost:8080/ai/chat \
    -H 'Content-Type: application/json' \
    -d '{"message":"how many lugias do we have"}' | jq
```

## Files added in this rollout

| File | Purpose |
|---|---|
| `pi-setup/cards_db_path.py` | Single source of truth for `/mnt/cards/*` paths |
| `pi-setup/pi_setup_compat.py` | Shared `sqlite_connect()` with WAL + busy_timeout |
| `pi-setup/usb_mirror.py` | Postgres → SQLite mirror logic |
| `pi-setup/sync_orchestrator.py` | Long-lived scheduler (every 6 min / 1 hr / 6 hr / 24 hr) |
| `pi-setup/cards/__init__.py` | Package marker |
| `pi-setup/cards/fuzzy_search.py` | Multilingual rapidfuzz search across all 4 langs |
| `pi-setup/cards/ai_assistant.py` | Flask blueprint for `/ai/chat`, `/ai/health` |
| `pi-setup/scripts/migrate-db-to-usb.sh` | One-time seed of `/mnt/cards/pokedex_local.db` |
| `pi-setup/scripts/setup-ollama.sh` | One-time Ollama + Qwen 2.5 3B pull |
| `pi-setup/scripts/embed-all-cards.py` | Bulk CLIP embedding for visual recognition |

## Files modified in this rollout

| File | Change |
|---|---|
| `pi-setup/tcg_lookup.py` | `_DB_PATH` → resolves via `cards_db_path` |
| `pi-setup/import_tcg_db.py` | Same |
| `pi-setup/sync_tcg_db.py` | Same |
| `pi-setup/server.py` | `_LOCAL_TCG_DB_PATH` and FAISS paths via `cards_db_path`; registers tcg_lookup, fuzzy_search, ai_assistant blueprints; adds `/admin/usb-sync/status` |
| `pi-setup/docker-compose.yml` | `/mnt/cards` bind mount on `pos` + `recognizer`; `HANRYX_LOCAL_DB_DIR` env; new `sync` + `assistant` services |

## Sync schedule reference

| Job | Interval | Needs network | What it does |
|---|---|---|---|
| `mirror` | 6 min | no | Postgres → SQLite mirror |
| `tcg_db` | 1 hr | yes | English card pool from pokemontcg.io |
| `tcgplayer` | 1 hr | yes | TCGplayer raw market prices for top 1000 cards |
| `ebay_sweep` | 6 hr | yes | eBay sold listings sweep for top 200 valuable cards |
| `kr_cards` | 24 hr | yes | Korean card pool refresh |
| `jpn_cards` | 24 hr | yes | Japanese card pool refresh |
| `jpn_pocket` | 24 hr | yes | Japanese TCG Pocket cards |
| `chs_cards` | 24 hr | yes | Chinese card pool refresh |
| `card_hashes` | 24 hr | no | Recognizer container's image-hash index |

Network-dependent jobs are silently skipped (not marked failed) when the
network is down — when connectivity comes back, the next due tick runs
them normally.

## Rollback

If anything goes wrong, the change is reversible without data loss:

```bash
cd ~/Hanryx-Vault-POS
git revert HEAD                      # revert the rollout commit
docker compose up -d --build pos recognizer
docker compose stop sync assistant   # leaves USB data untouched
```

The USB drive's `/mnt/cards/pokedex_local.db` is left in place — it just
stops being read, since the legacy `pi-setup/pokedex_local.db` path
takes over. The Postgres data is never touched by the new code.

## Trade-show "USB-only" mode

To deploy a backup kiosk to a trade show with just a USB stick:

1. Eject `/mnt/cards` from the main Pi: `sudo umount /mnt/cards`
2. Plug into the satellite Pi.
3. On the satellite Pi, mount it at the same path.
4. Run `docker compose up -d pos` — the POS reads the SQLite mirror as
   normal and serves the offline-search page at `/offline-search`.
5. The satellite Pi has no Postgres, so /admin and /sale-history won't
   work — but the cashier-facing search and pricing pages will.

---

## Unified Card DB (multi-language master table)

The "per-language" tables above (`cards_kr`, `cards_jpn`, `cards_chs`,
`cards_jpn_pocket`, plus the English `tcg_cards`) each speak ONE
language and use slightly different schemas. That works for
language-specific lookups but it forces the cashier to know which table
to query when a customer hands them a card without telling them what
country it's from. The unified card DB collapses all of those — plus
TCGdex, the Card-Database Excel files, the Korean/Chinese master
mapping spreadsheets, and PokéAPI — into a single table called
`cards_master` where every row carries every language's name.

### Layered data model

```
                                ┌─ ref_set_mapping       (cross-language set names)
   ref_*  (reference data) ─────┼─ ref_variant_terms     (KR/JP/CN promo variant codes)
                                ├─ ref_pokedex_species   (PokéAPI species, 9 languages)
                                └─ ref_promo_provenance  (KR Korean_Cards.txt parsed)

   src_*  (one table per      ┌─ src_eng_xlsx           (~32K English rows from Excel)
          upstream source —   ├─ src_eng_ex_codes       (EX serial-code Excel)
          NEVER edited by     ├─ src_jp_xlsx            (1996-2017 Japanese 2.0 Excel)
          the consolidator;   ├─ src_jp_ex_codes        (JP EX serial Excel)
          re-imported from    ├─ src_jp_pokemoncardcom  (pokemon-card.com fork)
          scratch each run)   ├─ src_jp_pocket_limitless(TCG Pocket via Limitless)
                              └─ src_tcgdex_multi       (TCGdex EN/KR/JP/zh-CN/zh-TW)

   cards_master (consolidator output, rebuilt from above by
                 build_cards_master.py — one row per unique card,
                 with name_en / name_kr / name_jp / name_chs /
                 name_cht / name_fr / name_de / name_it / name_es,
                 plus rarity, hp, image_url, ex_serial_codes,
                 source_refs JSONB for full per-field auditability).
```

The legacy per-language tables stay put — `server.py` has hundreds of
references to `cards_kr` / `cards_jpn` / `cards_chs`, and rewriting all
of those at once was too risky. Instead, `fuzzy_search.py` now lists
`cards_master` FIRST and the legacy tables AFTER, so a hit in the
unified table wins, and a cashier searching with the consolidator
offline still gets results from the per-language tables.

### Offline-first promise

Everything except the importers is offline-only. `cards_master` and
every supporting `ref_*` / `src_*` table is mirrored to the USB stick
by `usb_mirror.py` on its 6-minute tick, so a Pi disconnected from
network can:

1. Read the unified table for cashier-facing search.
2. Re-run `build_cards_master.py` against the mirrored `src_*` /
   `ref_*` tables to rebuild `cards_master` from scratch — useful if
   the consolidator's priority rules have changed and you want to
   re-rank without waiting for an importer cycle.

The `src_*` data only changes weeks apart (most are static Excel files
in the `Ngansen/Card-Database` GitHub repo), so the importer schedule
is intentionally relaxed:

| Job | Interval | What it does |
|---|---|---|
| `ref_mappings` | 7 days | KR + CN master DB Excel → `ref_set_mapping`, `ref_variant_terms` |
| `eng_xlsx` | 7 days | All-English-cards Excel → `src_eng_xlsx` (~32K rows) |
| `ex_codes` | 7 days | EX serial-code Excel (EN+JP) → `src_*_ex_codes` |
| `jp_xlsx` | 7 days | Japanese 2.0 Excel (1996→Dec 2017) → `src_jp_xlsx` |
| `kr_promos` | 7 days | `Korean_Cards.txt` parser → `ref_promo_provenance` |
| `pokeapi_species` | 7 days | PokéAPI species CSVs → `ref_pokedex_species` |
| `tcgdex` | 24 hr | TCGdex REST API per-language → `src_tcgdex_multi` |
| `jp_pcc` | 24 hr | pokemon-card.com fork → `src_jp_pokemoncardcom` |
| `pocket_lt` | 24 hr | TCG Pocket via Limitless → `src_jp_pocket_limitless` |
| `build_master` | 12 hr | Consolidator: all `src_*`/`ref_*` → `cards_master` |

`build_master` is offline (`needs_network=False`) because by the time
it runs, every input is already in Postgres locally.

### `/tcg/search-multi` behaviour

```bash
curl 'http://hanryxvault:8080/tcg/search-multi?q=리자몽&limit=5'
```

Returns hits from `cards_master` first (one row, all four languages
attached), then falls through to the legacy per-language tables if the
consolidator hasn't run yet on this Pi or the term doesn't appear in
`cards_master`. The response shape is unchanged from the previous
release — `{"query","languages","hits":[...]}` — so the cashier UI
needs no changes.

### `/ai/admin/db-coverage` — operator dashboard endpoint

Mounted under the `ai_bp` blueprint, so the full path is `/ai/admin/db-coverage`
(NOT `/admin/db-coverage`). Same blueprint as `/ai/chat` and `/ai/health`.

```bash
curl 'http://hanryxvault:8080/ai/admin/db-coverage' | jq
```

Returns a JSON snapshot:

* `totals` — overall row count plus how many `cards_master` rows
  have each language populated and how many carry an EX serial code
  or promo provenance entry.
* `per_set_top50` — the 50 sets with the most rows, with per-language
  fill percentages so operators can spot a set whose Korean import is
  missing a sheet at a glance.
* `source_share_sample` — over a 5,000-row sample, counts which
  Layer-1 source ended up "winning" the priority race for each major
  field. Useful when bumping rules in `unified/priority.py`.

Returns 503 if `cards_master` doesn't exist on the USB mirror yet —
i.e. the consolidator hasn't run since the box was set up.

### Manual rebuild

If you want to force a fresh consolidator pass without waiting for the
12 h tick:

```bash
docker compose exec sync python3 /app/build_cards_master.py
docker compose exec sync python3 /app/usb_mirror.py
```

The first builds `cards_master` in Postgres; the second re-projects
the table to the USB SQLite. Both are idempotent — `build_cards_master`
runs `BEGIN; DELETE FROM cards_master; bulk INSERT; COMMIT` so a
reader during the rebuild still sees the previous snapshot, and
`usb_mirror` swaps the SQLite file atomically.

### Unified search UI — `/admin/search`

A single admin page that queries **both** halves at once: live inventory
in Postgres and the offline `cards_master` catalogue on the USB SQLite
mirror. One result table, three colour-coded badges:

* **In stock ×N** (green) — catalogue card the shop currently holds.
  Match keys, in priority order: `tcg_id` exact, `set_id-card_number`,
  `set_id-card_number` with leading-zeros stripped, then exact name in
  EN/KR/JP/CHS/CHT.
* **Catalogue only** (grey) — known card not in stock. Comes with a
  `+ Add to inventory` button that opens the existing add flow with
  `cards_master.master_id` pre-filled so the operator only confirms
  condition + price.
* **Stock N (no catalogue match)** (amber) — inventory rows with no
  catalogue counterpart. Usually means: legacy SKU from before the
  unified DB shipped, sealed product (which `cards_master` deliberately
  excludes), or a card whose import is still missing — tomorrow's
  consolidator pass should clear it.

Each row shows the card image, every populated language name on one
line, set + card number, rarity, the badge, and an action. The query
input accepts any language plus set codes and QR codes. Up to 200 rows
per page.

Operators get one place to:

1. Look up a card by its Korean / Japanese / Chinese / English name
   without caring which database has it.
2. See instantly whether the shop holds it, and if so how many.
3. Add a known catalogue card to inventory in one click without
   retyping the multilingual names.


---

# Continuous Discovery v1

The unified card DB stays current automatically. Two background jobs
(wired into `sync_orchestrator.py`) detect new sets the moment TCGdex
publishes them and feed them through the existing per-language
importers without operator action.

## Architecture (probe → queue → dispatch → master)

```
   ┌────────────────────────────────────────────────────────┐
   │  discover_new_sets.py        runs DAILY, needs net     │
   │  ┌─────────┬─────────┬─────────┬─────────┬─────────┐   │
   │  │ /v2/en  │ /v2/ja  │ /v2/ko  │/v2/zh-tw│/v2/zh-cn│   │
   │  │  /sets  │  /sets  │  /sets  │  /sets  │  /sets  │   │
   │  └─────────┴─────────┴─────────┴─────────┴─────────┘   │
   │   (parallel ThreadPoolExecutor — one slow lang        │
   │    doesn't block the others)                          │
   │              │ aggregate by set_id                    │
   │              ▼                                        │
   │       diff vs ref_set_mapping + queue                 │
   │              │ unknown set_ids                        │
   │              ▼                                        │
   └──────────────┼────────────────────────────────────────┘
                  ▼
           ╭──────────────────────╮
           │   discovery_queue    │  unique partial idx on set_id
           │   (kind=set/report)  │  prevents dupes
           ╰──────────────────────╯
                  │
                  ▼   FOR UPDATE SKIP LOCKED — safe to run concurrently
   ┌──────────────┼────────────────────────────────────────┐
   │  discovery_dispatch.py       runs every 30 min        │
   │  per row, routes by language tags in payload:         │
   │   ─ always: import_tcgdex.py (multilingual)           │
   │   ─ if 'ja' & JP-exclusive: import_jp_pokemoncardcom  │
   │   ─ if 'ko':                import_kr_cards.py        │
   │   ─ if 'zh-cn':             import_chs_cards.py       │
   │   ─ then:                   build_cards_master.py     │
   │  writes to discovery_log per attempt                  │
   │  backoff: 1h / 6h / 24h, then 'failed' for triage     │
   └────────────────────────────────────────────────────────┘
                  │
                  ▼
           cards_master grows
                  │
                  ▼
         next mirror tick → USB SQLite
                  │
                  ▼
        Pi-attached tablets see the new cards
```

## Why per-language probing matters

TCGdex's English endpoint **deliberately omits** sets that have no
English release: most JP-exclusive promo bundles, every Pokemon Korea
exclusive, the Simplified-Chinese reprints. A POS that only probes the
EN endpoint misses exactly the cards multilingual customers are most
likely to bring in. Hitting all five language endpoints catches ~95%
of new releases without any HTML scraping.

## Operator escape hatch — "Report missing card"

The zero-results state on `/admin/search` exposes a **🔍 Report missing
card** button. Clicking it POSTs the current query to
`/admin/discovery/report` which inserts a `kind='report'` row.

The dispatcher rechecks every report against `cards_master` on its
next 30-minute tick. The most common reason for a "missing" card on
day 1 is that its set hasn't been imported yet — by day 2 the set
discovery probe has caught it, the importer has pulled it down, and
the report flips to `resolved` automatically.

## Admin pages

* **`/admin/discovery`** — three-tab queue browser (Pending / Resolved
  7d / Failed) with a per-row Retry button on the Failed tab. Surfaces
  failures that gave up after 3 attempts so a human can intervene.
* **`/admin` dashboard** — compact "Recently discovered (last 7 days)"
  panel showing the 5 most recent landed sets / reports. Renders
  nothing on a fresh install (no noise).
* Nav pill 🆕 **Discovery** added between Search and Market.

## JSON feed for the tablet

`GET /admin/discovery/queue.json?status=pending&kind=set&limit=50`
returns the queue as JSON — tablet agents poll this rather than
scraping HTML. See `TABLET_APK_SPEC.md` §4.5–4.7 for the full contract.

## Manual run / debugging

```bash
# Run the probe by hand (safe — idempotent)
docker exec hanryxvault-pos python3 /app/discover_new_sets.py

# Run only specific languages
docker exec hanryxvault-pos python3 /app/discover_new_sets.py \
    --languages en ja

# Drain the queue once (prints JSON summary of what was processed)
docker exec hanryxvault-pos python3 /app/discovery_dispatch.py

# Dry-run dispatcher — log what it WOULD do, run no importer
docker exec hanryxvault-pos python3 /app/discovery_dispatch.py --dry-run

# Check queue depth
docker exec hanryxvault-pg psql -U postgres -c \
  "SELECT status, COUNT(*) FROM discovery_queue GROUP BY status"
```

## Acceptance

A new TCGdex set ID should land in `cards_master` within **24 hours**
(probe interval) + **30 minutes** (dispatcher interval) + the time the
importer takes (~2 min on the Pi). The next `mirror` job (6 min cycle)
copies it to the USB SQLite — total worst-case ~25 hours, typical
~30 minutes if the operator triggers a manual probe.
