# Workspace

## Overview

pnpm workspace monorepo using TypeScript. Each package manages its own dependencies.

## Pi Deployment Architecture (`pi-setup/`)

All services run on the Raspberry Pi 5 via Docker Compose. Three containers share one PostgreSQL instance.

| Service | Port | Access | Description |
|---|---|---|---|
| `pos` | 8080 | LAN + Tailscale VPN | Flask POS server + admin dashboard |
| `storefront` | 3000 | Public (via nginx) | HanRyx-Vault Node.js customer website |
| `db` | 5432 (internal) | Docker network only | PostgreSQL — databases: `vaultpos` + `storefront` |

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
- **Desktop Monitor (cross-platform)** — `pi-setup/desktop_monitor.py` runs on Windows, Linux, and Pi. Uses `psutil` for CPU/RAM/disk/temp; all POS data pulled from `GET /admin/monitor-stats` (JSON, no direct DB access needed). **Tabs**: Dashboard (sales KPIs, stock alerts, service health, server ping), Business (open laybys + outstanding balance, open POs, open trade-ins, EOD status, 30-day P&L), System (live bars + DB size + server uptime), Sites (website ping), Logs (journalctl on Pi; browser links on Windows), Settings (Pi IP/port saved to `~/.hanryxvault_monitor.json`). **Build EXE**: run `build_exe.bat` on Windows (or `build_exe.sh` on Linux) to produce a single `HanryxVaultMonitor.exe` via PyInstaller — no Python needed on end-user machine. Dependencies: `monitor_requirements.txt` (`psutil>=5.9`, `pyinstaller>=6.0`). PyInstaller `dist/` and `build/` folders are git-ignored.
- **`GET /admin/monitor-stats`** — JSON endpoint returning all monitor KPIs in one call: today's sales/revenue/tips, all-time sales, inventory count, low/out-of-stock counts, pending scans, open trade-ins, open laybys + outstanding balance, open POs, EOD reconciled today (bool), 30-day P&L (revenue/COGS/profit/margin), DB size, server uptime.
- **Scan lag optimizations** — Three-pronged speed-up: (1) `GET /card/scan?qr=CODE` fast endpoint on POS — exact QR match only, in-memory LRU cache (500 entries, 5 min TTL) with CORS so the phone can call it directly without the scanner server proxy hop. Cache is evicted per-QR when that card's stock changes. (2) Mobile `lookupProduct` now fires the direct POS call AND the scanner server proxy in parallel (Promise.race); whichever resolves first with a non-null result wins — cuts lookup latency nearly in half on the local network. (3) Scan registration is now fire-and-forget: the modal opens the instant the lookup resolves instead of waiting for the scan record to be saved. Cooldown also tightened from 800 ms → 500 ms.
- **Collection Goals** — `goals` table with CRUD (`GET/POST /admin/goals`, `PATCH/DELETE /admin/goals/<id>`). Shows progress bars on admin dashboard for card_count, value_target, set_completion types.
- **Collection Sharing** — `POST /admin/share-token` generates a public read-only link (`/share/<token>`). No auth required on public page. Revocable via `DELETE /admin/share-token`.
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
- **OCR card scanning** — `pytesseract>=0.3.10` added; `tesseract-ocr tesseract-ocr-eng` installed in Dockerfile (both builder and runtime stages). Foundation for `POST /api/v1/scan/ocr` image-to-text pipeline.

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
