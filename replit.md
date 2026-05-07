# HanRyx Vault POS

A point-of-sale (POS) system for managing inventory, sales, and customer interactions, with advanced features for trading card game (TCG) businesses.

## Run & Operate

*   **Deploy**: `cd pi-setup && cp .env.example .env` (edit `.env`), then `docker compose up -d --build`
*   **Typecheck**: `pnpm run typecheck` (runs `tsc --build --emitDeclarationOnly` from root)
*   **Build**: `pnpm run build` (runs `typecheck`, then `build` in all packages)
*   **DB Push (Dev)**: `pnpm --filter @workspace/db run push` (falls back to `push-force`)
*   **Codegen**: `pnpm --filter @workspace/api-spec run codegen`
*   **Env Vars**:
    *   `DATABASE_URL`: PostgreSQL connection string
    *   `PORT`: API server port
    *   `CLOUD_INVENTORY_SOURCES`: URL for POS to pull products (e.g., `http://storefront:3000/api/products`)
    *   `HANRYX_POS_PUSH_URL`: URL for storefront to push inventory (e.g., `http://pos:8080/push/inventory`)
    *   `HANRYX_LOCAL_DB_DIR`: Path for offline card database (e.g., `/mnt/cards`)
    *   `SMTP_USER`, `SMTP_APP_PASSWORD`: For email notifications
    *   `NOTIFY_EMAIL`: Email recipient for notifications
    *   `OPENAI_API_KEY`: For AI features like GPT-4o Vision and embeddings
    *   `PTCG_API_KEY`: Optional, for increased TCG API rate limits
    *   `HANRYX_DEBUG_INSECURE_GIT=1`: Allows insecure Git operations for debugging (logs warning)

## Stack

*   **Monorepo**: pnpm workspaces
*   **Runtime**: Node.js 24, Python 3
*   **Package Manager**: pnpm
*   **TypeScript**: 5.9
*   **API Framework**: Express 5 (Node.js), Flask (Python)
*   **Database**: PostgreSQL 16 (pgvector enabled), SQLite (offline mirror)
*   **ORM**: Drizzle ORM
*   **Validation**: Zod (v4), `drizzle-zod`
*   **API Codegen**: Orval (from OpenAPI spec)
*   **Build Tool**: esbuild (Node.js)

## Where things live

*   `/pi-setup`: Docker Compose setup, services, and core Python backend (`server.py`)
    *   `/pi-setup/nginx/hanryxvault.conf`: Nginx routing configuration
    *   `/pi-setup/init-db/01-create-storefront-db.sh`: PostgreSQL initialization script
    *   `/pi-setup/docs/USB_OFFLINE_DB.md`: USB offline DB guide, including unified card DB details
    *   `/pi-setup/docs/REPRODUCIBILITY.md`: Reproducible builds documentation
    *   `/pi-setup/unified/schema.py`: Unified card database schema
    *   `/pi-setup/server.py`: Main Flask POS backend
    *   `/pi-setup/desktop_monitor.py`: Cross-platform desktop monitoring application
*   `/artifacts-monorepo`: TypeScript monorepo root
    *   `/artifacts/api-server`: Express API server (Node.js)
    *   `/lib/api-spec/openapi.yaml`: OpenAPI 3.1 specification (source of truth for API contracts)
    *   `/lib/db/src/schema/`: Drizzle ORM database schema models
*   `/mnt/cards`: Bind-mount for offline card database and assets on Raspberry Pi
*   `.github/workflows/pi-setup-security.yml`: CI workflows for security checks

## Architecture decisions

*   **Offline-First with USB Sync**: Core POS functionality, including card lookup and visual recognition, is mirrored to a USB drive (`/mnt/cards`) using SQLite for resilience against network outages, especially critical for trade shows. PostgreSQL on the SD card remains the source of truth for live writes.
*   **Unified Multilingual Card Database**: Consolidates multiple upstream TCG data sources into a single `cards_master` table with multilingual support, prioritized and auditable, while retaining legacy tables for backward compatibility during transition.
*   **Reproducible Docker Builds**: All custom Docker containers are locked by content-hash for base images, `apt/apk` packages, `pip` dependencies (with `requirements.txt` hashes), and Git sources, ensuring byte-identical builds across environments.
*   **Strict Security Policies**: Enforces TLS verification for all external network calls by default, with explicit, logged debug bypasses only. Also, a linting guard prevents plaintext HTTP/WS/MQTT/FTP external URLs.
*   **AI Integration for Card Management**: Incorporates CLIP for visual card identification and FAISS for vector search, along with Ollama (Qwen 2.5 3B) for an AI cashier assistant, using a constrained intent grammar to prevent arbitrary SQL execution.

## Product

*   **Point-of-Sale (POS)**: Core sales, inventory management, customer checkout.
*   **TCG Card Management**: Comprehensive tools for managing trading cards, including multilingual fuzzy search, visual recognition, price history, and automated catalog enrichment.
*   **Offline Capability**: POS operates effectively without internet via a local USB database.
*   **AI Cashier Assistant**: AI-powered assistant for card lookup and inventory queries.
*   **Inventory Workflow**: Features for bulk CSV import/export, purchase orders, trade-ins, bundle creation, and stock syncing with a public storefront.
*   **Financial Reporting**: Profit & Loss, End-of-Day cash reconciliation, valuation reports.
*   **Monitoring & Diagnostics**: Desktop monitor application for system health and business KPIs, tailored for Raspberry Pi deployments.

## User preferences

_Populate as you build_

## Gotchas

*   **Typechecking**: Always run `pnpm run typecheck` from the monorepo root; `tsc` inside a single package might fail if cross-package dependencies aren't built.
*   **Docker Volumes**: `/data/` within containers is ephemeral; bind-mount important data to `/mnt/cards` (or similar) to persist across `docker compose down` operations.
*   **Python Dependencies**: After modifying `pi-setup/requirements.in`, regenerate `requirements.txt` with `./scripts/lock-python-deps.sh pi-setup`.
*   **Floating Docker Tags**: CI will fail if Dockerfiles or compose files use non-full-point-release tags (e.g., `python:3.11-slim`). Use content hashes or explicitly allow-list.
*   **Healthchecks must use image-native tools**: `ollama/ollama` and the storefront's `node` base image do **not** ship `curl` or `wget`. Use `ollama list` (with a `/dev/tcp` fallback) for the assistant and `node -e "require('http').get(...)"` for the storefront. Any new service: verify the binary exists in the image before adding a `healthcheck.test`.
*   **labwc lazy-spawns Xwayland**: the kiosk launcher must wait for `/tmp/.X11-unix/X${DISPLAY#:}` to exist before spawning chromium, otherwise `connect()` returns ECONNREFUSED ("Missing X server"). When stripping the screen suffix, strip from `$DISPLAY` (e.g. `:0.0` → `0`), **not** from the socket path — `${path%%.*}` greedily matches the dot in the directory name `.X11-unix` and turns the path into `/tmp/`.
*   **Healthcheck-only compose changes still need `--force-recreate`**: a plain `docker compose up -d` won't pick up a modified `healthcheck.test`; the container keeps its old probe until recreated.

## Pointers

*   [Drizzle ORM Documentation](https://orm.drizzle.team/docs/overview)
*   [Zod Documentation](https://zod.dev/)
*   [Orval Documentation](https://orval.dev/)
*   [pnpm Workspaces Documentation](https://pnpm.io/workspaces)
*   [OpenAPI Specification](https://swagger.io/specification/)
*   [Docker Compose Documentation](https://docs.docker.com/compose/)
*   [Raspberry Pi Documentation](https://www.raspberrypi.com/documentation/)
*   [Ngansen/HanRyx-Vault GitHub](https://github.com/Ngansen/HanRyx-Vault)
*   [Ngansen/Card-Database GitHub](https://github.com/Ngansen/Card-Database)
*   `pi-setup/docs/USB_OFFLINE_DB.md`
*   `pi-setup/docs/REPRODUCIBILITY.md`
*   `pi-setup/docs/TABLET_APK_SPEC.md`