# Workspace

## Overview

pnpm workspace monorepo using TypeScript. Each package manages its own dependencies.

## Pi Deployment Architecture (`pi-setup/`)

All services run on the Raspberry Pi 5 via Docker Compose. Multiple containers share one PostgreSQL instance.

| Service | Port | Access | Description |
|---|---|---|---|
| `pos` | 8080 | LAN + Tailscale VPN | Flask POS server + admin dashboard |
| `storefront` | 3000 | Public (via nginx) | HanRyx-Vault Node.js customer website |
| `db` | 5432 (internal) | Docker network only | PostgreSQL — databases: `vaultpos` + `storefront` |
| `recognizer` | 8081 (internal) | pos via Docker network | Card image recognition (CLIP + image-hash) |
| `pokeapi` | 80 (internal) | pos via Docker network | Offline PokeAPI mirror |
| `sync` | — | none (worker) | Postgres → USB-SQLite mirror + scheduled importers |
| `assistant` | 11434 (internal) | pos via Docker network | Ollama serving Qwen 2.5 3B for the AI cashier assistant |

**nginx routing** (`pi-setup/nginx/hanryxvault.conf`):
- `hanryxvault.duckdns.org` → storefront (:3000) — public, HTTPS via certbot
- `hanryxvault.tailcfc0a3.ts.net` + LAN catch-all → Flask POS (:8080)

**Internal service wiring:**
- POS `CLOUD_INVENTORY_SOURCES=http://storefront:3000/api/products` — POS pulls from storefront
- Storefront `HANRYX_POS_PUSH_URL=http://pos:8080/push/inventory` — storefront pushes to POS

**Storefront build** (`pi-setup/services/storefront/Dockerfile`):
- Clones `Ngansen/HanRyx-Vault` from GitHub at build time (multi-stage Node 20)
- Runs `drizzle-kit push` on first start to migrate the `storefront` database

**PostgreSQL init** (`pi-setup/init-db/01-create-storefront-db.sh`):
- Runs once on first volume init — creates `storefront` database alongside `vaultpos`

**Deploy command:**
```bash
cd pi-setup && cp .env.example .env  # edit .env
docker compose up -d --build
```

## Offline Card Database on USB (`/mnt/cards`)

> **Container path map** (critical for log/file access on the running Pi):
> - **`/mnt/cards/`** — UGreen 1.8 TB ext4 USB drive, **bind-mounted** into containers. Files written here are visible on the host and survive `docker compose down`. Used for: SQLite mirror (`pokedex_local.db`), card image mirror (`/mnt/cards/images/...`), FAISS index, sync status JSON.
> - **`/data/`** — **container-internal** scratch directory (NOT bind-mounted). Logs and progress files written here (e.g. `/data/sync_mirror_phaseC.log`) are only visible from inside the container. To read them from the host: `docker compose exec pos tail -f /data/sync_mirror_phaseC.log`. Lost on container recreation (use `docker compose down -v` cautiously).

Multi-language card lookup + price history + visual recognition + AI
cashier assistant — all read-replicated onto an ext4-formatted USB drive
mounted at `/mnt/cards`, so the POS keeps working when the trade-show
WiFi drops. Postgres on the SD card stays the source of truth for live
writes; the `sync` container projects card and recent-price tables into
a single SQLite file at `/mnt/cards/pokedex_local.db` every 6 minutes.

**Path resolution.** All four files that previously hard-coded `pokedex_local.db`
(`tcg_lookup.py`, `import_tcg_db.py`, `sync_tcg_db.py`, `server.py:2184`) now
resolve through `pi-setup/cards_db_path.py`. The resolver reads
`HANRYX_LOCAL_DB_DIR` (set to `/mnt/cards` in `docker-compose.yml`) and falls
back to the in-package path when unset, so dev shells without USB still work.

**FAISS index.** `_FAISS_INDEX_PATH` and `_FAISS_IDS_PATH` (server.py
~line 1015) moved off `/tmp/` (ephemeral) onto `/mnt/cards/faiss/`
(survives `docker compose build`). The `_build_faiss_index_bg()` thread
no longer has to spend 5–10 minutes rebuilding the index from inventory
images on every container restart.

**Sync orchestrator** (`pi-setup/sync_orchestrator.py`). Long-lived
container; sleeps in 60s ticks; runs jobs at: mirror = every 6 min,
tcgplayer + tcg_db = every 1 hr, eBay sweep = every 6 hr, KR/JPN/JPN_POCKET/CHS
imports + card_hashes = every 24 hr. Network-dependent jobs are
silently skipped when offline (not marked failed) so a flapping
trade-show WiFi doesn't spam the status with red. Status JSON written
to `/mnt/cards/logs/sync_status.json` and exposed via
`GET /admin/usb-sync/status`.

**Multilingual fuzzy search** (`pi-setup/cards/fuzzy_search.py`). rapidfuzz
across `name` / `name_kr` / `name_jp` / `name_en` / `name_chs` /
`commodity_name` columns of every language table. SQL substring pre-filter
+ rapidfuzz token-set scoring + score ≥60 cutoff. Exposed via
`GET /tcg/search-multi?q=…&languages=ko,en`. Phonetic transliteration
hook (`_phonetic_normalise`) is wired in but identity-implemented today —
adding `g2pk` / `pykakasi` / `pypinyin` is one `pip install` away.

**AI cashier assistant** (`pi-setup/cards/ai_assistant.py` + `assistant`
service in docker-compose). Ollama serving Qwen 2.5 3B. Constrained
intent grammar (`search_card` | `lookup_price` | `inventory_count` |
`unknown`) — model never writes SQL, only chooses an intent that maps
to a hand-authored read-only query. Two routes: `POST /ai/chat`,
`GET /ai/health`. One-time model pull via
`pi-setup/scripts/setup-ollama.sh` (~2 GB cached in `ollama-data` Docker
volume). 30-minute idle eviction; single concurrent request to avoid
Pi 5 CPU thrashing.

**Bulk CLIP embedding** (`pi-setup/scripts/embed-all-cards.py`). Extends
the existing `_build_faiss_index_bg()` (which only embeds inventory) to
all four language card pools. Writes to pgvector (`card_embeddings`
table, `vector(512)` with ivfflat cosine index) AND to the FAISS file on
USB. Resumable, batched, HTTPS-only image fetch. One-time overnight run
on first deploy: `docker exec pi-setup-pos-1 python3 /app/scripts/embed-all-cards.py --source all --resume`.

**Blueprints registered.** `tcg_lookup.tcg_bp` (was previously defined
but never registered — `/tcg/search`, `/tcg/card/<id>`, `/tcg/inventory/<id>`,
`/tcg/stats` only worked if you imported the module manually), the new
`/tcg/search-multi` route, and `cards.ai_assistant.ai_bp` are all wired in
just before the `if __name__ == "__main__":` block in `server.py`. Each
registration is wrapped in its own try/except so an import failure in
one blueprint (e.g. assistant container down) doesn't bring down the rest.

**Deploy guide.** Step-by-step in `pi-setup/docs/USB_OFFLINE_DB.md`.
Includes the one-time `migrate-db-to-usb.sh` seed, `setup-ollama.sh`
model pull, and a "trade-show USB-only mode" recipe for booting a
satellite Pi from just the USB stick.

## Unified Card DB — multilingual master table (`pi-setup/unified/`)

Collapses 21 GitHub forks plus the `Ngansen/Card-Database` Excel
workbooks plus PokéAPI plus the existing per-language tables into a
single `cards_master` table where every row carries every language's
name (EN / KR / JP / CHS / CHT / FR / DE / IT / ES). The legacy
per-language tables (`cards_kr`, `cards_jpn`, `cards_chs`,
`cards_jpn_pocket`, `tcg_cards`) stay untouched — server.py has
hundreds of references to them and a wholesale rename was too risky
mid-trade-show season.

**Schema** (`pi-setup/unified/schema.py`). Three layers of tables, all
pgtrgm-indexed:

* `ref_*` — cross-language reference data: `ref_set_mapping`,
  `ref_variant_terms`, `ref_pokedex_species`, `ref_promo_provenance`.
* `src_*` — one table per upstream source, never mutated by the
  consolidator: `src_eng_xlsx`, `src_eng_ex_codes`, `src_jp_xlsx`,
  `src_jp_ex_codes`, `src_jp_pokemoncardcom`, `src_jp_pocket_limitless`,
  `src_tcgdex_multi`.
* `cards_master` — consolidator output, rebuilt by
  `build_cards_master.py` with priority rules in `unified/priority.py`
  and full per-field auditability via a `source_refs JSONB` column.

**Importers** (all CLI-callable, idempotent, `execute_values`-batched,
`--force` flag): `import_ref_mappings.py`, `import_eng_xlsx.py`,
`import_ex_codes.py`, `import_jp_xlsx.py`, `import_kr_promos.py`,
`import_tcgdex.py`, `import_jp_pokemoncardcom.py`,
`import_pocket_limitless.py`, `import_pokeapi_species.py`. Source
files are pulled from `Ngansen/Card-Database` over HTTPS at runtime
(no checked-in copies, no licence concerns).

**Consolidator** (`pi-setup/build_cards_master.py`). TCGdex spine + 10
sources merged by `(set_id, card_number, variant)`. Runs
`BEGIN; DELETE; bulk INSERT; COMMIT` so a reader during the rebuild
still sees the previous snapshot.

**Wiring.**
* `usb_mirror.py` mirrors `cards_master` + every `ref_*` table to the
  USB SQLite so the offline POS can rebuild from scratch without
  network.
* `cards/fuzzy_search.py` lists `cards_master` FIRST, then the legacy
  per-language tables — a hit in the unified table wins (one result
  with all four language names attached), but the legacy tables still
  serve cashiers on a fresh Pi where the consolidator hasn't run yet.
* `/tcg/search-multi` is unchanged on the wire — same response shape,
  no cashier-UI changes.
* New `GET /ai/admin/db-coverage` returns per-set + per-language
  fill percentages plus a 5,000-row source-share sample so operators
  can spot a missing import sheet at a glance.
* New `GET /admin/search` is the operator-facing UI on top of all of
  the above: one query box, results from `cards_master` (offline) and
  `inventory` (live PG) merged into a single table with three badges —
  `in_stock` (green, with quantity), `catalogue_only` (grey, with a
  one-click `+ Add to inventory` button that pre-fills the existing
  add flow with the `master_id`), and `in_stock_only` (amber, flags
  legacy SKUs with no catalogue match). Search accepts EN/KR/JP/CHS/CHT
  names, set codes, TCG ids, or QR codes.

**Sync orchestrator schedule.** Excel-backed importers run weekly
(Excel files change months apart), TCGdex / pokemon-card.com / Pocket
run daily, `build_master` runs every 12 h. All are added to the
existing `JOBS` list in `pi-setup/sync_orchestrator.py`.

**Dependencies.** `openpyxl>=3.1.0` (read-only Excel) and
`PyYAML>=6.0.1` (TCGdex TS-module fallback parser) added to
`pi-setup/requirements.in` — regenerate the lockfile with
`./scripts/lock-python-deps.sh pi-setup` before deploying.

**Operator runbook.** Full architecture + manual rebuild + dashboard
example in `pi-setup/docs/USB_OFFLINE_DB.md` (the "Unified Card DB"
section appended at the bottom).

**Continuous Discovery v1** (`pi-setup/discover_new_sets.py` +
`pi-setup/discovery_dispatch.py`). Two-stage worker keeps
`cards_master` growing without operator action:

* **Probe** (`discover_new_sets.py`, daily) hits TCGdex's per-language
  set endpoints in parallel — `/v2/{en,ja,ko,zh-tw,zh-cn}/sets` —
  via a `ThreadPoolExecutor`. JP-, KR-, and CHS-exclusive sets only
  appear on their respective language endpoint, so probing all five
  catches ~95% of new releases the EN-only endpoint would miss.
  Aggregates by `set_id`, diffs against `ref_set_mapping` + the
  queue, and inserts unknown sets into `discovery_queue` with the
  per-language names attached and a `languages: [...]` tag.
  Idempotent via a unique partial index on `(payload->>'set_id')
  WHERE status='pending'`.

* **Dispatcher** (`discovery_dispatch.py`, every 30 min) uses
  `SELECT … FOR UPDATE SKIP LOCKED` to safely cooperate across
  concurrent runs. Routes by language tag in the payload: always
  runs `import_tcgdex.py`, plus `import_jp_pokemoncardcom.py` for
  JP-exclusive promos, `import_kr_cards.py` for KR-flagged sets,
  `import_chs_cards.py` for ZH-CN sets. Always finishes with
  `build_cards_master.py`. Backoff is 1 h / 6 h / 24 h with the row
  marked `failed` on the third failure (surfaces on the admin UI for
  manual triage). Audit trail in `discovery_log`.

* **Schema** — `discovery_queue` (kind / payload JSONB / source /
  status / attempts / discovered_at / next_attempt_at /
  resolved_master_id / last_error / reporter) + `discovery_log`
  (one row per dispatcher attempt). Both registered in
  `init_unified_schema()` so a fresh boot creates them.

* **Operator UI** — `/admin/search` zero-results state shows a
  🔍 **Report missing card** button that POSTs to
  `/admin/discovery/report` (handles dedupe — second click on the
  same query returns the existing queue id). Full queue browser at
  `/admin/discovery` with three tabs (Pending / Resolved 7d / Failed)
  and a Retry button per failed row. Compact "Recently discovered
  (7d)" panel on the `/admin` dashboard. Nav pill 🆕 **Discovery**
  added between Search and Market.

* **Tablet integration** — `POST /admin/discovery/report` (fire-and-
  forget, deduped server-side) and `GET /admin/discovery/queue.json`
  (incremental polling with `?since_ms=`). Full contract in
  `pi-setup/docs/TABLET_APK_SPEC.md` §4.5–4.7.

* **Orchestrator wiring** — two new `Job` rows in
  `pi-setup/sync_orchestrator.py`: `discover_sets` (24 h interval,
  needs network) and `discovery_dispatch` (30 min interval, needs
  network). Status visible at `/admin/usb-sync/status` alongside the
  rest of the sync jobs.

## Reproducible builds (`pi-setup/`)

The four custom-built containers in `pi-setup/` (`pos`, `recognizer`,
`pokeapi`, `storefront`) are locked end-to-end so two `docker compose build`
runs from the same git SHA produce byte-identical layers:

- **Base images** (`FROM` / compose `image:`): pinned by content-hash
  `@sha256:…` (Task #11 for Dockerfiles, Task #9 for compose). Bump
  with `pi-setup/scripts/refresh-image-digests.py` (Task #20) — it
  re-resolves every pinned tag against `registry-1.docker.io` (using
  the multi-arch image-index digest) and prints a diff (default) or
  rewrites the files in place (`--write`). Enforces the lock-step
  rules — POS Dockerfile builder vs runtime, storefront Dockerfile
  builder vs runtime, and POS vs recognizer Python pin — before
  touching anything.
- **`apt-get install`** (Debian images: pos, recognizer, storefront): pinned
  via `snapshot.debian.org` using the `APT_SNAPSHOT_DATE` build arg
  (`pi-setup/Dockerfile`, `pi-setup/recognizer/Dockerfile`,
  `pi-setup/services/storefront/Dockerfile`).
- **`apk add`** (Alpine pokeapi image): pinned via `pkg=version` build args
  `ALPINE_GIT_VERSION` / `ALPINE_BASH_VERSION`
  (`pi-setup/pokeapi/Dockerfile`).
- **`pip install`** (POS + recognizer): `requirements.in` is the human-edited
  source; `requirements.txt` is fully pinned with sha256 hashes for every
  wheel/sdist, generated by `uv pip compile --generate-hashes
  --python-platform=aarch64-unknown-linux-gnu`. The Dockerfiles install with
  `pip install --require-hashes -r requirements.txt`, so any drift fails the
  build loudly.
- **`pip install` (git+ URLs)** (POS only — currently OpenAI CLIP):
  `requirements-vcs.txt` pins the full 40-char commit SHA. A commit SHA IS
  itself a content hash, so reproducibility is preserved without a wheel
  hash.
- **Storefront source clone** (`Ngansen/HanRyx-Vault`): pinned by full
  40-char commit SHA in `STOREFRONT_GIT_REF` at the top of
  `pi-setup/services/storefront/build.sh` (Task #22 — same content-hash
  rationale as the base-image `@sha256:…` and the git+ pin in
  `requirements-vcs.txt`). The script clones with `--filter=blob:none`,
  checks out the pinned commit, rejects any ref that isn't a 40-char hex
  SHA, and re-verifies `git rev-parse HEAD` after checkout — so a fresh
  `docker compose build` on the Pi always installs the same storefront
  bits regardless of where upstream `main` has moved. Bump procedure:
  `pi-setup/docs/REPRODUCIBILITY.md` §4a.
- **`npm ci`** (storefront): requires committed `package-lock.json` in the
  upstream `Ngansen/HanRyx-Vault` repo. Two guards enforce this — one in
  `pi-setup/services/storefront/build.sh` (host-side, after clone) and one
  inside `pi-setup/services/storefront/Dockerfile` (`test -f
  /app/package-lock.json` before `npm ci` in both stages). Bumping npm
  deps requires a corresponding `STOREFRONT_GIT_REF` bump (above), since
  the lockfile only changes on the Pi when the pinned source SHA moves.

Bump procedure for each layer is documented in
`pi-setup/docs/REPRODUCIBILITY.md`. Lockfile regeneration is one command:
`./pi-setup/scripts/lock-python-deps.sh all` (requires `uv`).

### Automated guard — no floating Docker tags

`pi-setup/scripts/check-no-floating-tags.py` is a repository-level lint that fails CI if any `FROM` line in a `pi-setup/` Dockerfile or any `image:` line in a `pi-setup/` compose file uses a tag that is not a full point release (i.e. a tag without at least two `.` characters, such as `python:3.11-slim`, `node:20-slim` or `nginx:alpine`). It is wired into GitHub Actions via `.github/workflows/pi-setup-security.yml` (runs on PRs and pushes to `main` that touch `pi-setup/`) and can also be run locally:

```bash
python3 pi-setup/scripts/check-no-floating-tags.py
```

`FROM scratch` and multi-stage cross-references (`FROM <stage-name>`) are allowed. The optional `@sha256:…` digest is owned by Tasks #11 / #9 and is not validated by this guard.

**Allow-listing an audited exception.** Put the marker `hanryx-allow-floating-tag` (optionally with `: <reason>`) in a comment on the same line or the line immediately above the offending `FROM` / `image:` line. Pinning by `@sha256:…` digest is still safer than the allow marker — prefer it whenever the registry exposes a digest.

## Security Policy — TLS verification (`pi-setup/`)

TLS / SSL certificate verification is **always on by default** for every outbound network call in `pi-setup/` (git, curl, wget, pip, requests, docker, etc.).

Disabling verification is only allowed via an **explicit, named debug environment variable** (e.g. `HANRYX_DEBUG_INSECURE_GIT=1`), and any code path that honours such a flag **must log a clear warning** when the bypass takes effect. Silent or unconditional disabling of TLS verification is forbidden because of the MITM and supply-chain risk it creates.

Current debug bypass flags (audited as of Task #10):
- `HANRYX_DEBUG_INSECURE_GIT=1` — sets `GIT_SSL_NO_VERIFY=1` for git operations in:
  - `pi-setup/server.py` → `admin_ota_update` (OTA pull) — logs warning to OTA log
  - `pi-setup/services/storefront/build.sh` (storefront build clone) — echoes warning to build log

When adding a new flow that downloads or clones code, prefer leaving verification on. If a debug bypass is genuinely needed, follow the same pattern: gate it behind a clearly named `HANRYX_DEBUG_INSECURE_*` env var, log a warning when active, and document it in the list above.

### Automated guard

`pi-setup/scripts/check-no-insecure-tls.py` is a repository-level lint that fails CI if any new occurrence of a known insecure-TLS pattern appears in `pi-setup/`. It is wired into GitHub Actions via `.github/workflows/pi-setup-security.yml` (runs on PRs and pushes to `main` that touch `pi-setup/`) and can also be run locally:

```bash
python3 pi-setup/scripts/check-no-insecure-tls.py
```

Patterns currently covered: `verify=False`, `curl -k` / `curl --insecure`, `wget --no-check-certificate`, `pip --trusted-host`, `docker --insecure-registry`, `GIT_SSL_NO_VERIFY`, `urllib3.disable_warnings(...)`, `ssl._create_unverified_context`, `check_hostname=False`, `NODE_TLS_REJECT_UNAUTHORIZED`, `PYTHONHTTPSVERIFY=0`.

**Allow-listing an audited bypass.** Put the marker `hanryx-allow-insecure` (optionally with `: <reason>`) in a comment on the same line as the match or on the line immediately above it. The two debug bypasses listed above already carry this marker. Example:

```python
# hanryx-allow-insecure: gated by HANRYX_DEBUG_INSECURE_GIT, logs a warning.
env["GIT_SSL_NO_VERIFY"] = "1"
```

**Extending the guard.** Add a new entry to `INSECURE_PATTERNS` in `pi-setup/scripts/check-no-insecure-tls.py` (each entry is `(name, compiled regex, description)`); the marker mechanism applies automatically to anything you add.

### Automated guard — no plaintext-HTTP external URLs

`pi-setup/scripts/check-no-plaintext-http.py` is a sibling lint that catches the complementary risk: code that never attempts TLS in the first place (e.g. `requests.get("http://api.example.com/")`, `curl http://...`, `fetch("http://...")` for an *external* host). On a hostile network like trade-show Wi-Fi this is just as exploitable as a `verify=False` shortcut. It is wired into GitHub Actions via `.github/workflows/pi-setup-security.yml` (runs on PRs and pushes to `main` that touch `pi-setup/`) and can also be run locally:

```bash
python3 pi-setup/scripts/check-no-plaintext-http.py
```

**Schemes covered.** The same scanner enforces TLS for every URL scheme that has a TLS-protected sibling. As of Task #23 the covered insecure schemes are:

| Insecure | Use instead |
| --- | --- |
| `http://` | `https://` |
| `ws://` | `wss://` |
| `mqtt://` | `mqtts://` |
| `ftp://` | `ftps://` or `sftp://` |

The TLS-protected variants (`https://`, `wss://`, `mqtts://`, `ftps://`, `sftp://`) are never flagged. The internal-host allow-list and the `hanryx-allow-plaintext` / `hanryx-allow-insecure` markers behave identically for every scheme — the scheme list lives in `INSECURE_SCHEMES` in the script and is the single place to add a new one (e.g. `smtp` if a future task wires up SMTP delivery).

**Internal hosts are allow-listed by default.** The pi-setup intentionally talks to its own services over the Docker network and the LAN/VPN using plaintext, so URLs whose host matches any of the following are *not* findings (regardless of scheme): `localhost`, `127.0.0.1`, `0.0.0.0`, `::1`, RFC 1918 ranges (`10/8`, `172.16/12`, `192.168/16`), link-local `169.254/16`, Tailscale CGNAT `100.64/10`, anything ending in `.local` / `.internal` / `.ts.net` / `.lan` / `.home.arpa`, any bare hostname with no dots (treated as a Docker service name like `pos`, `storefront`, `db`, `redis`, `pgbouncer`, `mainpi`), any host that contains a shell/template variable (`${VAR}`, `$(cmd)`, `<PLACEHOLDER>`), and known XML/XSD namespace identifiers (`www.w3.org`, `schemas.xmlsoap.org`, etc.).

**Allow-listing an audited exception.** If a real external plaintext URL is genuinely required (the canonical example is NetworkManager's captive-portal probe at `http://connectivity-check.ubuntu.com`, which must be HTTP because captive portals serve 302s to themselves and an HTTPS probe would TLS-fail instead of detecting the captive state), put the marker `hanryx-allow-plaintext` (optionally with `: <reason>`) in a comment on the same line as the URL or on the line immediately above it. The sibling `hanryx-allow-insecure` marker is also honoured here so a single comment can satisfy both checks. The same marker mechanism applies to the new `ws://` / `mqtt://` / `ftp://` schemes. Example (`pi-setup/scripts/setup-network-failover.sh`):

```ini
# hanryx-allow-plaintext: NetworkManager captive-portal probe must be plaintext HTTP.
uri=http://connectivity-check.ubuntu.com
```

**Extending the guard.** To cover a new insecure scheme, append it to `INSECURE_SCHEMES` in `pi-setup/scripts/check-no-plaintext-http.py` (the regex, error messages, and "OK" message all read from that tuple). To allow a new internal hostname suffix or known-safe namespace prefix, edit `INTERNAL_SUFFIXES` / `INTERNAL_HOSTNAMES` / `XML_NAMESPACE_HOSTS` in the same script. The marker mechanism handles one-off exceptions — prefer it over broadening the global allow-list.

**Unit tests pin the contract.** `pi-setup/scripts/tests/test_check_no_plaintext_http.py` (Task #24) locks down the scanner's classification decisions so a future edit can't silently widen the allow-list (e.g. matching `evil.local.attacker.com` as internal because a refactor turned `endswith(".local")` into a substring `in` check). Covers every internal-host shape (loopback, RFC 1918, CGNAT, `.local`/`.ts.net`/`.internal`/`.lan`/`.home.arpa`, bare Docker service names, `${VAR}`/`<PLACEHOLDER>` placeholders), the XML-namespace skip, both allow-marker spellings on the same line and the line above, and the regex behaviour for every `http`/`ws`/`mqtt`/`ftp` scheme plus their TLS-protected siblings (`https`/`wss`/`mqtts`/`ftps`/`sftp` must never match). Runs in CI in the same `no-plaintext-http` job and locally with `python3 -m unittest discover -s pi-setup/scripts/tests` — stdlib only, no extra pip install.

## HanryxVault Pi Server (`pi-setup/server.py`)

Flask + PostgreSQL POS backend that runs on a Raspberry Pi 5. Key features:

- **Brute-force login protection** — `_check_login_rate()` / `_record_failed_login()` / `_clear_login_attempts()`. 5 failed attempts → 15-minute IP lockout at `/admin/login`. Shows remaining attempts in error message.
- **Email notifications (SMTP/Gmail)** — `_send_sale_email()` fires on every new sale in `sync_sales`. Config via `SMTP_USER` + `SMTP_APP_PASSWORD` env vars (Gmail App Password). `NOTIFY_EMAIL` overrides recipient. Status at `GET /admin/email-config`, test at `POST /admin/email-config/test`.
- **TCG DB background update routes** — `_update_lock` + `_update_status` + `_run_import_script()`. `POST /admin/update-prices` (fast, prices only), `POST /admin/update-db` (full, 10+ min), `GET /admin/update-status` (running bool + last result). All trigger `import_tcg_db.py` subprocess.
- **`/market/price`** — `POST /market/price { name, language, store_price, set_code, card_number }`. Weighted lookup: 30-day `sale_history` (trust 3) → inventory DB (trust 2) → store_price fallback (trust 1). Returns `marketPrice`, `confidence`, `localSales30d`, `tcgdbPrice`.
- **`featured` + `listed_for_sale` columns** — added to `inventory` via safe ALTER TABLE migration. `PATCH /admin/inventory/<qr_code>` accepts `featured` / `listedForSale` / `stock` / `price` for partial updates.
- **Admin dashboard panels** — TCG Database Update (update-prices / update-db buttons, live job status), Email Notifications (status + test button), Cloud Sync (☁️ Sync + Force Re-Sync).
- **`sale_history` recording** — `sync_sales()` writes each sold item to `sale_history` table automatically; used by `/market/price` for store-specific pricing intelligence.
- **QR code generation** — `GET /admin/qr/<qr_code>` returns a PNG QR image for any card (no auth required, 24h cached). `GET /admin/qr-sheet` is a print-ready HTML page of all in-stock cards as label tiles (filterable by name, category, stock status; 2–5 columns per row; browser Ctrl+P prints them). "Print QR Labels" and "All Items" buttons appear above the inventory table. Each inventory row has a "QR" button that opens the individual code. Uses `qrcode[pil]` + `Pillow` (added to `requirements.txt`).
- **Two-way stock sync** — `_push_stock_to_storefront(items)` fires in a background thread after every sale (`sync_sales()`) and direct deduction (`inventory_deduct()`). Sends `POST $STOREFRONT_URL/api/inventory/sync` with `{items:[{qrCode, delta}]}` so the public storefront stock always reflects POS sales. `STOREFRONT_URL` defaults to `http://storefront:3000` (Docker-internal). Non-blocking, non-fatal.
- **`GET /admin/sale-history`** — Returns per-item sale history with `limit` (max 500), `days` (default 30), `name` filter params. Response includes `items`, `count`, `days`, `total_revenue`.
- **`GET /offline-search`** — Standalone HTML page (no auth required) for searching the local card database without internet. Calls `/card/lookup`. Shows stock level, price, set/rarity badges. Useful at trade shows when the TCG API is unavailable. Linked from admin dashboard.
- **SSE exponential backoff** — `connectScanStream()` now uses `_sseDelay` starting at 1 s, doubling on each error up to 30 s max. Resets to 1 s on a successful message.
- **Admin "Sale History (30 days)" panel** — Purple panel below "Recent Sales" with live name filter, refresh button, and Offline Search link. Auto-loads on page open via `loadSaleHistory()`.
- **Pricing engine** — `_calculate_final_price(base, language, item_type, grade)` applies language discounts (JP 0.55x, KR 0.40x), grade premiums (PSA 10 = 2.5x), item-type undercuts (Single 0.95x, Graded 1.10x), and step rounding. Accessible via `GET /admin/price-calc`.
- **Card Photo Identification (GPT-4o Vision)** — `POST /card/identify-image` accepts a base64 image, calls OpenAI GPT-4o Vision to identify card name/set/number/condition, then enriches with TCG API. `OPENAI_API_KEY` env var controls access. "📷 Identify from Photo" button in the admin inventory form opens a modal for file upload or camera capture; auto-fills all form fields on success. `openai>=1.30.0` added to `requirements.txt`.
- **Trade-in Flow** — `trade_ins` + `trade_in_items` tables. `GET /admin/trade-in` lists open/completed trade-ins. `POST /admin/trade-in/create` starts one; add items via `/add-item`, remove via `/remove-item/<id>`, `POST .../complete` upserts all accepted cards into inventory (with condition recorded). "🔁 Trade-In" nav link added.
- **Deck/Bundle Checkout** — `bundles` + `bundle_items` tables. `GET /admin/bundles` shows all bundles with create form. Add/remove cards per bundle; set a single bundle price. `POST /admin/bundles/<id>/sell` checks stock for all items, deducts inventory, creates one sale transaction. Auto-fills card name/price from inventory via `/api/stock-check`. "📦 Bundles" nav link added.
- **Two-way POS ↔ Scanner sync** — `GET /api/stock-check?codes=A,B,C` returns `{name, stock, price}` for each code (no auth, read-only). `POST /api/push-scan` upserts a card into POS inventory with `stock_delta` increment. Scanner mobile app: `SessionContext` now includes `checkPosStock()` and `pushCardToPOS()` helpers; scanner result modal shows live POS stock with colour coding (green/amber/red) and a "Push to POS Inventory" button.
- **Bulk CSV Import/Export** — `GET /admin/inventory/export` downloads full inventory as CSV. `POST /admin/inventory/import` accepts a CSV upload and upserts all rows (add new + update existing). `GET /admin/inventory/template` serves a blank template. Accessible via "📥 Import/Export" nav tab.
- **Purchase Orders** — `purchase_orders` + `purchase_order_items` tables. State machine: draft → ordered → received/cancelled. Create PO with supplier, add line items (name/qty/unit cost), mark ordered, receive to auto-add stock and set `purchase_price` in inventory. "🛒 Purchases" nav tab.
- **Profit & Loss + Trade-in P&L** — `GET /admin/profit-loss?period=30` shows: total revenue, COGS (qty sold × current purchase_price), gross profit & margin, per-card breakdown (top 50), and trade-in P&L (market value vs paid-out for completed trade-ins). Period selector: 7d/30d/90d/1yr. "💰 P&L" nav tab.
- **Layby (Hold) System** — `laybys` + `layby_items` + `layby_payments` tables. Create layby for a customer (deposit, due date), add items (auto-fills name/price from inventory), record multiple payments, complete (deducts stock + records sale) or cancel. Balance tracking updates live. "🏷️ Layby" nav tab.
- **End-of-Day Cash Reconciliation** — `eod_reconciliations` table. `GET /admin/eod` shows today's KPIs (total/cash/card sales, layby cash, open laybys), payment breakdown, cash count form with live discrepancy calculator (opening float + cash sales + layby cash = expected). `POST /admin/eod/close` saves the record. 14-day history table. "🏧 End of Day" nav tab.
- **Desktop Monitor (cross-platform)** — `pi-setup/desktop_monitor.py` runs on Windows, Linux, and Pi. Uses `psutil` for CPU/RAM/disk/temp; all POS data pulled from `GET /admin/monitor-stats` (JSON, no direct DB access needed). **Tabs**: Dashboard (sales KPIs, stock alerts, service health, server ping), Business (open laybys + outstanding balance, open POs, open trade-ins, EOD status, 30-day P&L), **System** (extended diagnostics — see below), Sites (website ping), Logs (journalctl on Pi; browser links on Windows), Settings (Pi IP/port saved to `~/.hanryxvault_monitor.json`). **Kiosk mode**: `python3 desktop_monitor.py --kiosk` for fullscreen + hidden cursor + auto-connect to localhost (F11 or Ctrl-Alt-Q exits). **Build EXE**: run `build_exe.bat` on Windows (or `build_exe.sh` on Linux) to produce a single `HanryxVaultMonitor.exe` via PyInstaller — no Python needed on end-user machine. Dependencies: `monitor_requirements.txt` (`psutil>=5.9`, `pyinstaller>=6.0`). PyInstaller `dist/` and `build/` folders are git-ignored.
  - **Extended System tab** (Pi 5 portable home-lab diagnostics, 4 s refresh): existing CPU/Temp/RAM/Disk KPI cards + progress bars **plus** 4 new mini-cards (PMIC temp, throttle status decoded from `vcgencmd get_throttled` bits 0-3 / 16-19, 1/5/15 load avg, uptime), Pi hardware (model from `/proc/device-tree/model`, revision, serial, kernel), clocks/voltages (`vcgencmd measure_clock arm/core/v3d/emmc` + `measure_volts core/sdram_c`), **per-core CPU bars** (Pi 5 has 4 cores), memory detail (used/avail/cached/buffers + swap), per-mount storage (/, /mnt/cards, /boot/firmware) with disk I/O read/write rates, per-interface network bytes-sent/recv with live ↑/↓ rates plus Tailscale IP, top 5 processes by CPU and by RAM, and Docker container summary (running/stopped/unhealthy + per-container status with ⚠ marker on unhealthy). All 17 helper functions degrade gracefully on non-Pi hosts (graceful "n/a" / empty values, no exceptions). Polling runs in a separate background thread (`_bg_refresh_extras`) with a 0.4 s stagger from the main `_bg_refresh` to avoid `psutil.cpu_percent` interference; rate computations use cached per-instance delta state (`self._last_disk_io`, `self._last_net_io`).
- **`GET /admin/monitor-stats`** — JSON endpoint returning all monitor KPIs in one call: today's sales/revenue/tips, all-time sales, inventory count, low/out-of-stock counts, pending scans, open trade-ins, open laybys + outstanding balance, open POs, EOD reconciled today (bool), 30-day P&L (revenue/COGS/profit/margin), DB size, server uptime.
- **Scan lag optimizations** — Three-pronged speed-up: (1) `GET /card/scan?qr=CODE` fast endpoint on POS — exact QR match only, in-memory LRU cache (500 entries, 5 min TTL) with CORS so the phone can call it directly without the scanner server proxy hop. Cache is evicted per-QR when that card's stock changes. (2) Mobile `lookupProduct` now fires the direct POS call AND the scanner server proxy in parallel (Promise.race); whichever resolves first with a non-null result wins — cuts lookup latency nearly in half on the local network. (3) Scan registration is now fire-and-forget: the modal opens the instant the lookup resolves instead of waiting for the scan record to be saved. Cooldown also tightened from 800 ms → 500 ms.
- **Collection Goals** — `goals` table with CRUD (`GET/POST /admin/goals`, `PATCH/DELETE /admin/goals/<id>`). Shows progress bars on admin dashboard for card_count, value_target, set_completion types.
- **Collection Sharing** — `POST /admin/share-token` generates a public read-only link (`/share/<token>`). No auth required on public page. Revocable via `DELETE /admin/share-token`.
- **CLIP + FAISS Visual Card Identification** — Full AI visual scan pipeline. `_load_clip_model()` lazy-loads OpenAI `ViT-B/32` on CPU on first use. `_build_faiss_index_bg()` background thread fetches all inventory `image_url`s, embeds each with CLIP, and writes a `faiss.IndexFlatIP` to `/tmp/hanryx_cards.index` + JSON IDs. `_clip_find_top(img_bytes, k=3)` returns top-k matches with cosine similarity scores. Duplicate prevention via `_clip_recently_seen()` (2-second cooldown per QR code). Variant price multipliers in `_VARIANT_MULTIPLIERS` dict (1st Edition 3.5×, Rainbow Rare 1.8×, etc.) applied via `_apply_variant_multiplier()`. New endpoints: `POST /scan/frame` (live camera bulk scan, `bulk_mode=true` default, auto-adds at confidence ≥ 0.92, returns `skipped/auto_added/options`), `POST /scan/intelligent` (single-shot, file upload or base64 JSON), `POST /scan/intelligent/select` (user picks from top-3), `POST /admin/ai-index/rebuild` (trigger background index rebuild), `GET /admin/ai-index/status` (ready + count). Admin page `GET /admin/scan-ai`: live camera stream (320×240 JPEG 0.5, ~3 fps via `requestAnimationFrame` + 300ms timeout), bulk mode toggle, NM/LP/MP/HP condition selector, top-3 option cards with card images, auto-matched result card, Web Audio API beep, scan/add/skip counters, FAISS index rebuild + status badge. "🤖 AI Scan" added to nav and dashboard quick-action grid. Dependencies added to `requirements.txt`: `torch`, `torchvision` (CPU via PyTorch wheel index), `clip @ git+https://github.com/openai/CLIP.git`, `faiss-cpu`, `numpy`.
- **Admin UI — Square-style redesign** — Three changes aligned admin to the POS app's Square-like design: (1) Gold replaced throughout `#FFD700` → `#f59e0b` warm amber (60 replacements, including `#ffe033` hover → `#fbbf24`). (2) `_admin_nav` nav bar rebuilt from flat text tabs to pill-shaped `nav-pill` buttons (scrollable, no scrollbar visible, active pill fills solid amber, pill hover glows amber border). (3) Dashboard gets a 10-card Square-style quick-action grid (`qa-grid` / `qa-card`) above the stats section — each card shows large emoji icon, bold label, and muted sub-label; hover lifts card with amber border.
- **Price Change Alerts** — `GET /admin/price-alerts` returns cards with >15% market price movement (using price_history table). Shown live on admin dashboard with refresh button.
- **Valuation Report** — `GET /admin/valuation-report` generates a print-ready HTML table with name, set, condition, language, qty, market price, cost basis, sale price, and P/L per card. Window.print() compatible.
- **Enhanced inventory schema** — 10 new columns via safe ALTER TABLE migrations: `language`, `condition`, `item_type`, `grading_company`, `grade`, `cert_number`, `back_image_url`, `purchase_price`, `sale_price`, `tags`.
- **TCG import scripts** — `pi-setup/import_tcg_db.py` (bulk JSON import), `sync_tcg_db.py` (live API sync), `tcg_lookup.py` (CLI lookup) ported from Card-Scanner-AI project.
- **TCG API enrichment** — `_tcg_fetch()` / `_tcg_search()` hit `api.pokemontcg.io/v2` with 2-layer cache (in-memory 1h + PostgreSQL 24h). Optional `PTCG_API_KEY` env var for 20k/day rate limit.
- **`/card/enrich`** — combined local inventory + full TCG data (name, HP, types, image, market prices) in one call; used by scan/pending and admin dashboard.
- **`/card/condition/<qr>`** — GET/POST NM/LP/MP/HP/DMG condition per card stored in `card_conditions` table.
- **`/admin/export-cards`** — bulk JSON/CSV export for website upload; `?enrich=1` flag adds TCG images + market prices.
- **`/admin/webhook-config`** — configure a POST webhook URL; fires automatically when a card is saved via `/admin/inventory`.
- **Price flash overlay** — admin dashboard (`/admin`) connects to `/scan/stream` (SSE), on each scan calls `/card/enrich` and flashes a full-screen semi-transparent overlay with card name, rarity, set name, and price (gold `$XX.XX`). Progress bar auto-dismisses after 4s. Duplicate scan warning shown in red.
- **`⚡ Prefill from TCG API` button** — admin product form: enter a Set-Number (e.g. `SV1-1`), click button → name, rarity, set code, image, and TCG market price auto-fill.
- **`_normalize_qr()`** — handles pokemon.com, ptcg://, ptcgo.com, limitlesstcg.com, pkmncards.com, and generic path-based URLs.
- **Satellite sync** — token-authenticated sync from trade-show Pi via WireGuard VPN.
- **QR Scan Hub** — `barcode_daemon.py` HTTP hub on port 8765 with SSE, multi-app forwarding, duplicate suppression.
- **Smart search engine (latest)** — `card_number` (indexed), `variant`, `release_year` columns. `_score_card()` awards +8 for exact number/year match, +4 variant, +3 rarity, +5 full-name bonus. `_tokenize` keeps variant keywords (ex/gx/v/vmax/vstar). `_card_lookup` pipeline: exact QR → normalised QR → set+number (card_number col) → SET-NUM pattern → number-only cross-set → tokenised name+variant+rarity (LIKE + scoring) → vector fallback. `_detect_variant` covers 1st Ed, Reverse Holo, Rainbow, Secret, Gold, Full Art, VSTAR, VMAX, V, GX, EX, Holo, Promo.
- **pgvector semantic search** — Postgres image switched to `pgvector/pgvector:pg16`. `card_vector VECTOR(1536)` column + HNSW cosine index. `_embed_text()` calls OpenAI `text-embedding-3-small`. `_embed_card_bg()` runs async via `_bg()` after every card save. `_vector_search()` fires as step 6 in `_card_lookup` when all text steps return nothing. `POST /api/v1/embeddings/rebuild` queues background embedding for all un-embedded cards; `GET /api/v1/embeddings/status` shows coverage%. Fully graceful — silently skips when `OPENAI_API_KEY` or pgvector is absent.
- **Central Card API + Enrichment Pipeline** — Full-featured public API at `/api/v1/inventory` with paginated listing, filtering (category/rarity/set/condition/language/item_type/featured/listed/in_stock/price_range), sorting, text search. `/api/v1/inventory/<qr>` returns single card detail with TCG data + price history + eBay sold history. `/api/v1/inventory/search?q=` does smart search (pgvector semantic first, text fallback). `/api/v1/inventory/stats` returns totals, value, enrichment completeness. `/api/v1/inventory/categories` lists all filter options. Enrichment pipeline: `POST /api/v1/enrich/start` triggers background bulk enrichment (TCG API → eBay pricing → pgvector embedding, rate-limited ~1 card/sec). `POST /api/v1/enrich/card/<qr>` enriches a single card on demand. `POST /api/v1/enrich/price-refresh` triggers immediate stale price refresh. `GET /api/v1/enrich/status` shows progress. Background price refresh daemon runs every 6h, updating cards not refreshed in 24h. Admin dashboard at `/admin/enrich` ("🧬 Enrich" nav tab) shows enrichment completeness percentages, service status (TCG API/eBay/pgvector/OpenAI), missing-data table with per-card "Fix" buttons, recently updated cards with per-card "Enrich" buttons, live enrichment log, and pipeline controls (Enrich All / Refresh Prices).
- **eBay 90-day pricing engine (latest)** — `_dcat=183050` (Pokémon Individual Cards category) + `_sadis=90` + `_ipg=240` URL params; 6 pages scraped in parallel. Sold-date parsing from `.s-item__ended-date` / `.SECONDARY_INFO` spans. `ebay_sold_history` table persists raw scrapes with 24h dedup. `_build_period_model(items, days)` builds 7d/30d/90d windows. `_calc_price_trend()` groups by week, returns weekly medians + direction (up/down/flat).
- **IQR outlier filtering** — `_remove_outliers` replaced with **Tukey's fences** (Q1 − 1.5×IQR, Q3 + 1.5×IQR), robust against right-skewed price distributions. `$0.25` price floor rejects placeholder listings. IQR-zero fallback clips to ±60% of median. `_sanitize_listings` pre-filter strips bulk lots ("lot", "bundle", "x10" etc.) before scoring or modelling. `_score_listing` adds −8 penalty for bulk keywords. Response now includes `raw_sample` (before removal) and `iqr_bounds {lo, hi}` so the UI can show exactly what was kept.
- **PokéAPI canonical name cache** — `pokeapi_name_cache` table stores all ~1,050 species names (slug + display name + Pokédex number). `_pokeapi_fetch_and_store()` fetches from `pokeapi.co/api/v2/pokemon-species` on startup and weekly thereafter. `_SLUG_SPECIAL` dict handles tricky names (Mr. Mime, Ho-Oh, Type: Null, Tapu Koko, Paradox mons etc.). Module-level `_pokeapi_names_list` + `_pokeapi_names_strs` lists loaded into memory for zero-latency lookups.
- **Canonical name normalisation (`_normalize_to_canonical`)** — runs **Jaro-Winkler** (best for short species names, prefix-weighted) AND **rapidfuzz WRatio** (handles word-reordering), picks the higher score. Threshold 72 for pricing, 60 for suggestions. Used in three places: (1) `GET /api/v1/pricing/intelligent` corrects the name before the eBay query is built; (2) `_SmartScanner._get_suggestions()` adds a Pokédex "not in stock" hint with official artwork URL when inventory results are weak (score < 0.65); (3) `GET /api/v1/pokeapi/normalize?q=` endpoint for direct use by the tablet/scanner app.
- **New endpoints** — `GET /api/v1/pokeapi/normalize?q=<name>[&threshold=70]` returns canonical, slug, pokedex_no, score, exact, in_stock, inventory_item, sprite_url. `POST /api/v1/pokeapi/names/refresh` (admin) forces background re-fetch from PokeAPI. `GET /api/v1/pricing/history?name=&set=&number=&variant=&lang=&refresh=` returns full 90-day breakdown: `periods {7d,30d,90d,all}`, `trend {weeks, direction}`, `sold_listings [{title, price, sold_date, score}]`, `data_source` (stored|live).
- **Weekly PokeAPI refresh daemon** — `_pokeapi_weekly_refresh_loop()` is a daemon thread (started at server boot) that wakes every 24 h, checks cache age, and re-fetches from PokeAPI when older than 7 days. Keeps the name index current on long-running Pi installs without needing a restart.
- **Pricing improvements** — `variant` and `grade` are now accepted params on `/api/v1/pricing/intelligent` and `/api/v1/pricing/history`. Variant is added to the eBay query only when not already present in the card name (avoids duplicates like "Charizard VMAX VMAX"). `_score_listing` already penalises graded listings for raw lookups. Canonical name threshold aligned: 72 for pricing, 65 for suggestions.
- **Language pricing — 6 enhancements**:
  1. `_filter_and_score_lang()` — lenient scorer for JP/KR/CN/TW listings; drops name-match requirement (foreign titles won't contain English card name), keeps number-match (+5) and bulk-lot penalty; threshold 2 vs 6 for EN. `_fetch_ebay_lang_price` uses it automatically when `lang_suffix` is non-empty.
  2. eBay EN vs TCGPlayer delta bar — shown between the lang title and grid; green with ▲ when eBay > TCGPlayer by >5%, red with ▼ when softer, neutral ≈ when within 5%.
  3. Arbitrage flags per cell — `🔥 Premium` (red) when pct_off < 0, `⚠ Near parity` (orange) when pct_off ≤ 10% with ≥5 sales, `⚠ Low data` (yellow) when <5 sales.
  4. Trade-in price shown in every language cell (80% of condition-adjusted market price), blue text, updates live when condition changes.
  5. Data freshness display + `↺` refresh button — "fetched Xs ago" counter in title row (updates every 20s), force-refresh calls `/api/v1/pricing/language?refresh=1`.
  6. `_prewarm_lang_all_bg()` daemon — starts 10 min after boot (after EN pre-warm), iterates inventory, fires 5-parallel eBay scrapes per card (EN+JP+KR+CN+TW), caches in Redis (10 min TTL), 5s between items. Runs in gunicorn worker 1 and `__main__`.
- **eBay price history chart on Market page** — The `📈 Market` admin page now shows a full eBay sold history panel below the TCGPlayer result. Auto-triggered when a card is found. Includes: (1) period summary cards for 7d / 30d / 90d with median price + sale count; (2) Chart.js 4 line chart of weekly medians for the last 90 days (gold line, dark theme, hover tooltips in £); (3) scrollable sold listings table (title, price, date); (4) `● Cached` / `● Live eBay` source badge; (5) `↺ Live Refresh` button to force a fresh eBay scrape bypassing the cache. Chart.js served from jsDelivr CDN — no build step needed.
- **Pricing pre-warm engine** — `_prewarm_pricing_for_item(qr_code)` is fired via `_bg()` every time `_invalidate_inventory(qr_code)` is called with a specific card (add, edit, receive stock, trade-in complete, etc.). On server boot, `_prewarm_all_pricing_bg()` runs as a background daemon that iterates every item in inventory (ordered by most recently updated first), skips anything already in Redis/PG cache, and fires eBay scrapes with a 2.5 s delay between each to stay under rate limits. The gunicorn `post_fork` hook starts this thread inside worker 1 only (not all 4 workers), keeping eBay calls serialised. The `__main__` block also starts it for direct `python server.py` dev runs.
- **OCR card scanning** — `pytesseract>=0.3.10` added; `tesseract-ocr tesseract-ocr-eng` installed in Dockerfile (both builder and runtime stages). Foundation for `POST /api/v1/scan/ocr` image-to-text pipeline.
- **Offline-safe boot (v2)** — Both main and satellite kiosk boot scripts fully offline-safe: (1) `ExecStartPre=-` (dash prefix) ignores Docker pull failures when offline; (2) `Wants=network-online.target` (not `Requires`) so Docker starts even without internet; (3) `systemd-networkd-wait-online` timeout capped at 15 s via drop-in override — prevents boot hanging at trade shows with no Wi-Fi; (4) git pull is best-effort and skipped when offline; (5) Chromium launched via watchdog loop that auto-restarts if it crashes; (6) persistent Chromium profiles survive crashes; (7) all logs written to `/var/log/hanryx-kiosk.log`; (8) kiosk top bar shows live network status badge (🟢 Online / 🔴 Offline) via `navigator.onLine` + `/health` polling every 30 s.
- **Docker hardening (v2)** — All services have `mem_limit` (db 512m, redis 128m, pgbouncer 64m, pos 2g, storefront 512m) to prevent OOM on Pi. `logging` driver set to `json-file` with `max-size`/`max-file` rotation (prevents SD card fill-up). POS and storefront now have proper `healthcheck` blocks (curl-based, with `start_period`). Dockerfile runs as non-root `hanryx` user with dedicated group. `curl` added to runtime image for healthcheck support.
- **Flask error handlers** — Global `@app.errorhandler` for 404, 500, 405. API routes (`/api/*`) return JSON errors; admin/kiosk routes return branded HTML error pages (black/gold theme). 500 errors logged with full traceback via `exc_info=True`.
- **Kiosk animations (Pokémon-themed)** — Idle screen: CSS Pokéballs (regular/Ultra/Master Ball variants) and energy type icons (fire/electric/water/psychic) float with staggered delays; store name pulses gold glow; logo border shimmers. Cart: items slide-in with stagger delay; new items flash gold; grand total has gold price glow. Card processing: dual spinning gold rings orbit the card icon with 3D wobble. Brand pulse interstitial: on sale complete, shows full-size brand illustration (`/static/brand-pulse.jpeg`) with zoom-bounce animation, spinning gold rings, "SALE COMPLETE" text with letter-spacing reveal, radial gold flash — plays for 2.8 s before transitioning to thank-you. Thank you: Pokéball-catch bounce animation, confetti cannon (120 pieces, gold/red/purple/white), gold sparkle burst. Trade done: items pop-in, confetti fires. Ambient: continuous gold dust particle system (18 initial, spawns to 30 max, multiple gold shades with glowing box-shadows).

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)

## Structure

```text
artifacts-monorepo/
├── artifacts/              # Deployable applications
│   └── api-server/         # Express API server
├── lib/                    # Shared libraries
│   ├── api-spec/           # OpenAPI spec + Orval codegen config
│   ├── api-client-react/   # Generated React Query hooks
│   ├── api-zod/            # Generated Zod schemas from OpenAPI
│   └── db/                 # Drizzle ORM schema + DB connection
├── scripts/                # Utility scripts (single workspace package)
│   └── src/                # Individual .ts scripts, run via `pnpm --filter @workspace/scripts run <script>`
├── pnpm-workspace.yaml     # pnpm workspace (artifacts/*, lib/*, lib/integrations/*, scripts)
├── tsconfig.base.json      # Shared TS options (composite, bundler resolution, es2022)
├── tsconfig.json           # Root TS project references
└── package.json            # Root package with hoisted devDeps
```

## TypeScript & Composite Projects

Every package extends `tsconfig.base.json` which sets `composite: true`. The root `tsconfig.json` lists all packages as project references. This means:

- **Always typecheck from the root** — run `pnpm run typecheck` (which runs `tsc --build --emitDeclarationOnly`). This builds the full dependency graph so that cross-package imports resolve correctly. Running `tsc` inside a single package will fail if its dependencies haven't been built yet.
- **`emitDeclarationOnly`** — we only emit `.d.ts` files during typecheck; actual JS bundling is handled by esbuild/tsx/vite...etc, not `tsc`.
- **Project references** — when package A depends on package B, A's `tsconfig.json` must list B in its `references` array. `tsc --build` uses this to determine build order and skip up-to-date packages.

## Root Scripts

- `pnpm run build` — runs `typecheck` first, then recursively runs `build` in all packages that define it
- `pnpm run typecheck` — runs `tsc --build --emitDeclarationOnly` using project references

## Packages

### `artifacts/api-server` (`@workspace/api-server`)

Express 5 API server. Routes live in `src/routes/` and use `@workspace/api-zod` for request and response validation and `@workspace/db` for persistence.

- Entry: `src/index.ts` — reads `PORT`, starts Express
- App setup: `src/app.ts` — mounts CORS, JSON/urlencoded parsing, routes at `/api`
- Routes: `src/routes/index.ts` mounts sub-routers; `src/routes/health.ts` exposes `GET /health` (full path: `/api/health`)
- Depends on: `@workspace/db`, `@workspace/api-zod`
- `pnpm --filter @workspace/api-server run dev` — run the dev server
- `pnpm --filter @workspace/api-server run build` — production esbuild bundle (`dist/index.cjs`)
- Build bundles an allowlist of deps (express, cors, pg, drizzle-orm, zod, etc.) and externalizes the rest

### `lib/db` (`@workspace/db`)

Database layer using Drizzle ORM with PostgreSQL. Exports a Drizzle client instance and schema models.

- `src/index.ts` — creates a `Pool` + Drizzle instance, exports schema
- `src/schema/index.ts` — barrel re-export of all models
- `src/schema/<modelname>.ts` — table definitions with `drizzle-zod` insert schemas (no models definitions exist right now)
- `drizzle.config.ts` — Drizzle Kit config (requires `DATABASE_URL`, automatically provided by Replit)
- Exports: `.` (pool, db, schema), `./schema` (schema only)

Production migrations are handled by Replit when publishing. In development, we just use `pnpm --filter @workspace/db run push`, and we fallback to `pnpm --filter @workspace/db run push-force`.

### `lib/api-spec` (`@workspace/api-spec`)

Owns the OpenAPI 3.1 spec (`openapi.yaml`) and the Orval config (`orval.config.ts`). Running codegen produces output into two sibling packages:

1. `lib/api-client-react/src/generated/` — React Query hooks + fetch client
2. `lib/api-zod/src/generated/` — Zod schemas

Run codegen: `pnpm --filter @workspace/api-spec run codegen`

### `lib/api-zod` (`@workspace/api-zod`)

Generated Zod schemas from the OpenAPI spec (e.g. `HealthCheckResponse`). Used by `api-server` for response validation.

### `lib/api-client-react` (`@workspace/api-client-react`)

Generated React Query hooks and fetch client from the OpenAPI spec (e.g. `useHealthCheck`, `healthCheck`).

### `scripts` (`@workspace/scripts`)

Utility scripts package. Each script is a `.ts` file in `src/` with a corresponding npm script in `package.json`. Run scripts via `pnpm --filter @workspace/scripts run <script>`. Scripts can import any workspace package (e.g., `@workspace/db`) by adding it as a dependency in `scripts/package.json`.
