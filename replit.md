# Workspace

## Overview

pnpm workspace monorepo using TypeScript. Each package manages its own dependencies.

## HanryxVault Pi Server (`pi-setup/server.py`)

Flask + PostgreSQL POS backend that runs on a Raspberry Pi 5. Key features:

- **Pricing engine** — `_calculate_final_price(base, language, item_type, grade)` applies language discounts (JP 0.55x, KR 0.40x), grade premiums (PSA 10 = 2.5x), item-type undercuts (Single 0.95x, Graded 1.10x), and step rounding. Accessible via `GET /admin/price-calc`.
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
