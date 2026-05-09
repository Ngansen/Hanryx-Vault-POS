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
    *   `POKEMONTCG_API_KEY`: Optional, for increased TCG API rate limits (legacy `PTCG_API_KEY` still honoured as fallback)
    *   `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`: Naver Open API keys for Korean shopping pricing (~10-char alphanumeric each). Register at https://developers.naver.com/apps/#/register with "검색" (Search) API enabled. Without these the naver pricing chip is empty (HTML site IP-bans this egress). Free tier 25k req/day.
    *   `HANRYX_DEBUG_INSECURE_GIT=1`: Allows insecure Git operations for debugging (logs warning)
    *   `ENABLE_PLAYWRIGHT_SCRAPER`: `1` (default) drives a headless chromium fallback. **As of C10 none of the active scrapers (naver/bunjang/hareruya2/cardmarket) require Playwright** — all use plain JSON APIs or SSR HTML. Kept on for the diagnostic harness and any future scraper that needs CF/JS-challenge defeat. Set `0` to disable (saves ~150MB RAM and ~280MB image size).
    *   `PLAYWRIGHT_NAV_TIMEOUT_MS` / `PLAYWRIGHT_SETTLE_MS`: per-fetch timeouts (defaults 12000 / 1500 ms).

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
    *   `/pi-setup/playwright_scraper.py`: Shared headless-chromium fetcher (background asyncio loop, per-domain context cache) used by `price_scrapers.py` to defeat anti-bot WAFs.
    *   `/pi-setup/price_scrapers.py`: Naver / Cardmarket / SnkrDunk / TCGkorea scrapers; tries Playwright first, falls back to requests-based `_safe_get`.
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
*   **Real-Browser Scraping for Native-Language Pricing**: Naver / SnkrDunk / TCGkorea defeat header-spoofed `requests` (TLS fingerprinting + JS-rendered SPAs). A single shared Playwright chromium instance lives in a background asyncio loop, with one persistent `BrowserContext` per domain so Cloudflare clearance and locale cookies survive across calls. Image/font/CSS/media resources are blocked at the network layer for speed. Adds ~280MB to the POS image and ~150MB RAM while scraping; gracefully degrades to the requests path when `ENABLE_PLAYWRIGHT_SCRAPER=0` or chromium fails to launch. **Cardmarket and TCGplayer pricing now go through tcgdex.net's free public API** instead — see the cardmarket gotcha below.

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

### Build / Python / CI

*   **Typecheck from monorepo root**: `pnpm run typecheck` only — `tsc` inside a single package fails when cross-package deps aren't built.
*   **Python deps regen**: after editing `pi-setup/requirements.in`, run `./scripts/lock-python-deps.sh pi-setup` to regenerate `requirements.txt` (with hashes).
*   **Floating Docker tags**: CI fails on non-pinned tags (e.g. `python:3.11-slim`). Use content hashes or explicit allow-list.

### Docker / Compose

*   **`/data/` is ephemeral**: bind-mount important data to `/mnt/cards` (or similar) to persist across `docker compose down`.
*   **Healthchecks need image-native tools**: `ollama/ollama` and the storefront's `node` base image don't ship `curl`/`wget`. Use `ollama list` (with `/dev/tcp` fallback) for the assistant and `node -e "require('http').get(...)"` for the storefront. Verify the binary exists in the image before adding `healthcheck.test`.
*   **Healthcheck-only changes need `--force-recreate`**: plain `docker compose up -d` keeps the old probe; the container won't pick up a new `healthcheck.test` until recreated.
*   **Playwright in compose needs `shm_size: 256mb`**: chromium writes lots of small files to `/dev/shm`; Docker's default 64MB causes silent tab crashes ("Target page, context or browser has been closed") that look like network errors but are OOM. Already set on `pos` — copy to any new chromium service.
*   **Dockerfile: do NOT re-copy `/opt/venv` after `playwright install`**: runtime stage copies the builder's venv ONCE (~line 103), then `playwright install --with-deps chromium` mutates `/opt/venv/lib/.../playwright/driver/` to register the downloaded chromium. A second `COPY --from=builder /opt/venv /opt/venv` later in the file silently reverts that → every fetch fails with `Executable doesn't exist at ...`. Keep the single-copy invariant.

### Database / Schema

*   **`price_history` schema (C11)**: added columns `source`, `grade`, `currency`, `price_usd`, `observed_at`, `query_used` plus indexes `idx_price_hist_observed` and `idx_price_hist_card_src`. Migration in `server.py:init_db()` is idempotent (`ADD COLUMN IF NOT EXISTS`). USB SQLite mirror is the `price_history_recent` table in `usb_mirror.py` (last 90 days, capped 200k rows). After deploy, force-run `mirror_once()` if you don't want to wait for the 6-min tick — first deploy of the table will silently 404 lookups otherwise.
*   **`usb_mirror` SELECT and INSERT must agree column-for-column**: pre-C11 had a SELECT pulling 9 cols and an INSERT writing 7, silently losing data on every mirror tick. When extending the mirror, count the `?` placeholders against the SELECT tuple and verify with `PRAGMA table_info(<table>)` afterwards.

### Git / GitHub API

*   **GitHub blob upload from python: REPO + GH_TOKEN MUST be exported to subprocess env**: when building a >100KB base64 blob via `subprocess.run([curl, ...])`, missing either env var causes `KeyError` inside the helper, the script swallows it, and returns an empty SHA. The subsequent tree-create writes a blob entry with `sha:""` which GitHub *accepts* and interprets as **delete this path** — file silently disappears from main on push. Always `export REPO=Ngansen/Hanryx-Vault-POS GH_TOKEN=$GITHUB_TOKEN` before invoking, or pass `env={**os.environ, "REPO":..., "GH_TOKEN":...}` to subprocess. Guard against this by `git --no-optional-locks show <commit>:<path>` immediately after push to confirm the blob is present.

### labwc / Kiosk session

*   **labwc lazy-spawns Xwayland**: kiosk launcher must wait for `/tmp/.X11-unix/X${DISPLAY#:}` before spawning chromium, else `connect()` returns ECONNREFUSED ("Missing X server"). Strip the screen suffix from `$DISPLAY` (e.g. `:0.0` → `0`), **not** from the socket path — `${path%%.*}` greedily matches the dot in `.X11-unix` and turns the path into `/tmp/`.
*   **labwc `-C <dir>` overrides `~/.config/labwc`**: the satellite kiosk session starts labwc with `-C /etc/hanryx-kiosk/labwc`; that dir's `rc.xml` is the ONLY one labwc reads. Always write window rules to `/etc/hanryx-kiosk/labwc/rc.xml` (with sudo). Verify with `ps -ef | grep 'labwc -C'`.
*   **`labwc --reconfigure` from SSH fails with `LABWC_PID not set`**: the CLI looks at `$LABWC_PID` which is only set inside labwc's autostart session. Reload from a child of the autostart (e.g. the kiosk launcher), or `pkill -HUP -x labwc`. `systemctl restart hanryx-kiosk.service` triggers a reload via the launcher.
*   **Main pi 7″ Grafana kiosk needs lightdm + `rpd-labwc` session shim**: Pi OS Bookworm's `/etc/lightdm/lightdm.conf` references `rpd-labwc` but the actual session file is `labwc.desktop`. Fix: `sudo ln -sf /usr/share/wayland-sessions/labwc.desktop /usr/share/wayland-sessions/rpd-labwc.desktop`. Install lightdm BEFORE running `raspi-config nonint do_boot_behaviour B4`, and drop `/etc/systemd/system/getty@tty1.service.d/autologin.conf` so agetty doesn't race lightdm. The 7″ kiosk launches via `~/.config/autostart/hanryx-grafana-kiosk.desktop` — only picked up under a full lightdm session, NOT under `labwc -C ...` greeter.
*   **gnome-keyring blocks chromium kiosk on first launch**: libsecret pops a "Choose password for new keyring" modal nobody dismisses on a headless display. Pass `--password-store=basic --use-mock-keychain` to chromium so it stops trying to use the system keyring. Already on `diagnostics-grafana-kiosk.sh`; add to any new kiosk chromium under labwc.

### Kiosk launcher (`hanryx-dual-monitor.sh`)

*   **`pi-setup/setup-satellite-kiosk-boot.sh` is the *installer*, not the launcher**: lines 389–861 are a heredoc (`cat > "$LAUNCH_SCRIPT" << 'LAUNCH'`) extracted to `/home/ngansen/.hanryx-dual-monitor.sh` (~471 lines) at install time. NEVER `sudo install` the setup script over the live launcher — that copies the wrapper, not the body. Re-deploy launcher changes alone with: `sudo awk "/^cat > .* << 'LAUNCH'\$/{f=1;next} /^LAUNCH\$/{f=0} f" pi-setup/setup-satellite-kiosk-boot.sh | sudo tee /home/ngansen/.hanryx-dual-monitor.sh >/dev/null`.
*   **Old launcher loops survive `systemctl stop` on `setsid nohup` deploys**: older autostarts spawned the launcher under `setsid nohup ... &`, detaching it from the systemd cgroup. Stopping/restarting the service does NOT kill it. Always `sudo pkill -9 -f hanryx-dual-monitor.sh` AND `sudo pkill -9 -f /usr/lib/chromium/chromium` before restarting after a launcher edit. Current heredoc no longer uses `setsid nohup`.
*   **`hanryx-watchdog.service` `pkill -f chromium` was too greedy**: tightened to `pkill -f 'user-data-dir=/home/ngansen/.hanryx/'` so only the two kiosk profiles (admin, kiosk) get bounced — not unrelated diagnostic chromium or developer debug sessions.
*   **`pi-setup/diagnostics-grafana-kiosk.sh` is MAIN-pi-only**: Grafana only listens on localhost on `hanryxvault`. If run on the satellite, the spawned chromium lives forever (PPID becomes systemd) and labwc places it on whichever HDMI output is free, **silently hijacking either screen with `ERR_CONNECTION_REFUSED localhost`**. Hostname guard at the top exits 2 on any other host. To kill an already-running stray: `sudo pkill -9 -f 'diagnostics-grafana-kiosk.sh' && sudo pkill -9 -f 'user-data-dir=/tmp/chromium-grafana' && sudo rm -rf /tmp/chromium-grafana && sudo pkill -HUP -x labwc`.

### Chromium flags

*   **NEVER pass `--app-id=<arbitrary-string>`**: `--app-id=ID` instructs chromium to launch the *installed Chrome App* whose extension ID is `ID`, and to **exit 0 immediately with no stderr** if that app isn't installed. Use ONLY `--class=` for window identity. (Side note: `--class=foo` under XWayland sets WM_CLASS to capitalised `"Foo"`; labwc identifier rules need a case-insensitive glob `[Ff]oo-bar` to match both forms.)
*   **Chromium honours only the LAST `--enable-features=` flag**: passing two `--enable-features=` flags silently drops the first. Always merge: `--enable-features=UseOzonePlatform,VaapiVideoDecoder`. Same for `--disable-features=`.
*   **Chromium 147 silently rejects `--no-sandbox` + `--disable-gpu` + `--use-gl=swiftshader` together on Pi 5 wayland-native**: exit code 1 in <1s, no stderr. Known-good baseline (commit `17b8636`, April 2026): `--ozone-platform=wayland` + `--enable-features=UseOzonePlatform,VaapiVideoDecoder` + `--disable-dev-shm-usage`. Drop to FALLBACK_FLAGS only after 2 quick crashes — chromium handles GPU process death gracefully when allowed.
*   **GPU process termination ≠ browser crash**: chromium can show `GPU.GPUProcessTerminationStatus2 = 4` and KEEP RUNNING fine (falls back to software compositing internally). Check actual chromium PID etime, not stderr noise.

### Display geometry

*   **Xwayland X-root spans all wlr-outputs but xrandr only lists HDMI-A-1**: under labwc, `xrandr` reports a single CRTC even with two physical outputs, but `Screen 0: current 1824 x 600` confirms the X coord space spans both. xdotool `windowmove $wid 1024 0` IS valid and lands the window in HDMI-A-2's region.
*   **Known issue — labwc 0.9.2 `MoveToOutput HDMI-A-2` rendering**: rule moves the wayland surface (confirmed via test swap), but the secondary 5″ MPI5008 output keeps showing only labwc's bg colour, not chromium content. Likely a wlroots/labwc bug with non-primary outputs at 800×480. Pending hardware-swap test or labwc upgrade.

### Scrapers — egress / anti-bot

*   **naver shopping IP-bans the trade-show egress — use the Open API**: `search.shopping.naver.com` returns "쇼핑 서비스 접속이 일시적으로 제한되었습니다" (their VPN/datacenter IP filter is at the network layer; no UA/playwright/stealth defeats it). The authenticated Open API at `https://openapi.naver.com/v1/search/shop.json` is exempt. Register an app at https://developers.naver.com/apps/#/register (tick "검색"/Search; pick WEB; any URL works — the **CLOVA section is the wrong one**, that's the AI assistant SDK), set `NAVER_CLIENT_ID` + `NAVER_CLIENT_SECRET` in `pi-setup/.env`. Free tier 25k req/day. Returned `title` wraps matched terms in `<b>...</b>` (`_NAVER_TAG_RE.sub("", title)` strips them).
*   **Cardmarket + TCGplayer go through `api.tcgdex.net`, not direct scraping**: cardmarket.com sits behind Cloudflare bot-fight from our egress (verified-defeated even with playwright + stealth + 15s IUAM wait — block is at IP/JA3 layer). tcgdex.dev publishes a free public REST embedding both Cardmarket EUR and TCGplayer USD per Pokémon card. No auth, ~150ms/call. Workflow: `GET /v2/en/cards?name=<q>` → `GET /v2/en/cards/<id>` (fetches top `TCGDEX_DETAIL_LIMIT`, default 5). Wrapper at `pi-setup/tcgdex_api.py`. Pokémon-only — for MTG use scryfall.
*   **`pricing.tcgplayer` field on tcgdex is documented but empirically empty**: of 30 top Pikachu hits, 19 had `pricing.cardmarket` populated and 0 had `pricing.tcgplayer`. The `tcgdex_api.tcgplayer()` wrapper IS implemented but intentionally NOT registered in `SCRAPERS` or `DRIFT_CANARIES` — avoid 1s/query empty-result waste and false drift warnings. Re-enable when/if tcgdex backfills.
*   **Cloudflare IUAM ("Just a moment...") needs ~10s, NOT the default 1.5s settle**: C6 added detection in `_fetch_html_async` — if `stealth=True` AND first 2KB shows `"Just a moment"` OR first 4KB shows `"cf-challenge"`/`"challenge-platform"`, wait up to `PLAYWRIGHT_CLOUDFLARE_WAIT_MS` (default 15000ms) for `document.title` to change. Two signals required to avoid mis-triggering on legit body copy.
*   **`playwright-stealth` v2 API is class-based**: v1 `from playwright_stealth import stealth_async` is gone. Use `from playwright_stealth import Stealth; await Stealth().apply_stealth_async(page)` (per-page) or `Stealth().use_async(playwright_instance)` (per-context). Wrap in try/except so missing-package state doesn't crash the fetch.

### Scrapers — catalog choices

*   **bunjang.co.kr exposes a clean public JSON API**: `GET https://api.bunjang.co.kr/api/1/find_v2.json?q=<kw>&order=score&page=0&n=10` returns `{list:[{pid,name,price(int KRW),product_image,status,location,...}]}` with no auth, no anti-bot. Item URL = `https://m.bunjang.co.kr/products/{pid}`. The HTML route `/search/products?q=` returns a 3KB SPA shell with 0 anchors — do NOT scrape HTML.
*   **hareruya2.com is a Shopify storefront — use `/search?q=`** (the modern Shopify-standard route). DO NOT use the legacy `?act=Sch&card_name=` parameter — it now redirects to a different category index and returns wrong-product hits (verified C10). Returns SSR HTML with product cards in `div.card-wrapper.product-card-wrapper` (Dawn theme): `.card__heading` for title, `.price-item--regular`/`.price__regular` for price, with a `[¥￥]\s*([\d,]+)` regex fallback on the card's text since hareruya2 sometimes nests prices in unstable spans. Plain `requests` works (no Cloudflare). Best results with katakana queries.
*   **mercari JP can NOT be scraped from non-browser clients** (probed 2026-05): SPA shell has 0 `/item/m\d+` anchors and no `__NEXT_DATA__`; `api.mercari.jp/search_index/search` is 404; `api.mercari.jp/v2/entities:search` requires a DPoP-signed bearer token minted per-session via JS WebCrypto. Pivoted to hareruya2 (Pokémon-card specialist, better signal-to-noise — no apparel/lots/sealed bundles to filter out).
*   **snkrdunk + tcgkorea were dropped in C10 — wrong-catalog**: snkrdunk is sneakers/streetwear (no Pokemon-card category, `/categories/pokemon-card` 404s, all Pokemon queries return the same おすすめアイテム carousel); tcgkorea is a 30-item Cafe24 sealed-product wholesaler (0 hits for `리자몽`). Replaced by bunjang + hareruya2. **Lesson**: any new marketplace scraper requires an end-to-end probe of at least 3 known-popular Pokémon names BEFORE adding to `SCRAPERS` — verify the items actually look like the queried card, not just that some items came back.

### Scrapers — i18n / cache

*   **bunjang/hareruya2/naver need native-language queries**: hareruya2's product index is Japanese-only (katakana), bunjang's hits are mostly hangul, naver Open API matches its KR catalog 5-10× better with hangul. `pi-setup/species_names.py` (1025 species, generated from PokéAPI via `build_species_names.py`) holds the en→ja_kana/ko/zh_hant/zh_hans/fr/de map; `price_scrapers.search_all()` auto-translates per source via `_TRANSLATE_LANG = {"hareruya2":"ja_kana", "bunjang":"ko", "naver":"ko"}` after stripping card suffixes (V/VMAX/ex/GX/BREAK/☆) and parentheticals. Refresh when new Pokémon release: `python3 pi-setup/build_species_names.py` (~14s, hits PokéAPI), commit. Per-CARD JP/KR name pipeline (`japanese_names_filler.py`/`korean_names_filler.py`) is a separate layer — that's set+number → localised card name, this species map is the SPECIES-level fallback for marketplace search. **Trap**: forgetting to add a new scraper to `_TRANSLATE_LANG` means it silently runs on the English query — usually returns `[]` and is invisible until you query postgres for source-row counts.
*   **`scrape_cache.cached` no longer caches empty results** (post-C6): pre-C6, `[]` from a Cloudflare timeout / naver 401 / transient blip got cached for the full TTL — config fixes appeared not to work. Decorator now skips `_set` when result is an empty list/dict/tuple/set, but still calls `_track_drift` so canary-query empties keep incrementing the drift counter. Non-collection types (str/int/etc) cached normally.
*   **bunjang/hareruya2 need native-language queries — use `species_names.translate()`**: hareruya2's product index is Japanese-only (katakana Pokémon names) and bunjang's hits are mostly hangul. English queries return few/no hits. `pi-setup/species_names.py` (1025 species, generated from PokéAPI) holds the en→ja_kana/ko/zh_hant/zh_hans/fr/de map, and `price_scrapers.search_all()` auto-translates per source before fan-out (`_TRANSLATE_LANG = {"hareruya2": "ja_kana", "bunjang": "ko"}`). Strips card suffixes (V, VMAX, ex, GX, BREAK, ☆, ...) and parentheticals before lookup, so 'Charizard ex' → 'リザードン'/'리자몽', 'Pikachu (Celebrations)' → 'ピカチュウ'/'피카츄'. Non-Pokemon queries (Lorcana, MTG, sealed product) fall through unchanged. Response includes a `query_used: {source: actual_string}` map for debugging. **To refresh when new Pokémon release**: `python3 pi-setup/build_species_names.py` (~14s, hits PokéAPI), commit the diff. PokéAPI's contributor community typically ingests new species within 1-7 days of a mainline game/DLC release. Note: there is also a per-CARD JP/KR name pipeline in `japanese_names_filler.py`/`korean_names_filler.py` (set+number → localised card name from Pokémon card DB) — that's a different layer; this species map is the SPECIES-level fallback for marketplace search.
*   **NEVER pass `--app-id=<arbitrary-string>` to chromium**: `--app-id=ID` instructs chromium to launch the *installed Chrome App* whose extension ID is `ID`, and to **exit 0 immediately with no stderr** if that app isn't installed. We had this bug in the kiosk launcher for weeks: synthetic identifiers like `hvault-admin` (used only for labwc window matching) were passed as both `--class=` (correct, for WM_CLASS / wlr app_id) AND `--app-id=` (catastrophic — chromium exits in 0s every launch loop iteration). Symptom: launcher logs "[Admin] crashed in 0s" with no chromium stderr in the log file, while the same flag set without `--app-id` runs fine for hours. Use ONLY `--class=` for window identity.

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