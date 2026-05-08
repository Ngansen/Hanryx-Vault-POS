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
*   **labwc `-C <dir>` overrides `~/.config/labwc`**: the satellite kiosk session starts labwc with `-C /etc/hanryx-kiosk/labwc` (set in `setup-satellite-kiosk-session.sh` / `-systemd.sh`). That dir's `rc.xml` is the ONLY one labwc reads — `~/.config/labwc/rc.xml` is silently ignored. Always write window rules to `/etc/hanryx-kiosk/labwc/rc.xml` (with sudo). Verify with `ps -ef | grep 'labwc -C'`.
*   **`labwc --reconfigure` from SSH fails with `LABWC_PID not set`**: the CLI looks at the `$LABWC_PID` env var which is only set inside labwc's autostart session. Reload from a child of the labwc autostart (e.g. the kiosk launcher), or `pkill -HUP -x labwc` from anywhere. A `systemctl restart hanryx-kiosk.service` triggers a reload via the launcher.
*   **Chromium `--class=foo` under XWayland on labwc**: confirmed sets the WM_CLASS class component to capitalised `"Foo"`, so labwc identifier rules need a case-insensitive glob (e.g. `[Ff]oo-bar`) to match both forms.
*   **Xwayland X-root spans all wlr-outputs but xrandr only lists HDMI-A-1**: under labwc, `xrandr` reports a single CRTC even with two physical outputs, but `Screen 0: current 1824 x 600` confirms the X coord space spans both. xdotool `windowmove $wid 1024 0` IS valid and lands the window in HDMI-A-2's region.
*   **Known issue — labwc 0.9.2 `MoveToOutput HDMI-A-2` rendering**: the rule moves the wayland surface (confirmed via test swap), but the secondary output (5″ MPI5008) keeps showing only labwc's bg colour, not the chromium content. Likely a wlroots/labwc bug with non-primary outputs at 800×480 or a quirk of that specific HDMI display. Not yet resolved; pending hardware-swap test or a labwc upgrade.
*   **`pi-setup/setup-satellite-kiosk-boot.sh` is the *installer*, not the launcher**: lines 389–861 are a heredoc (`cat > "$LAUNCH_SCRIPT" << 'LAUNCH'`) that gets extracted to `/home/ngansen/.hanryx-dual-monitor.sh` (~471 lines) when the installer runs. NEVER `sudo install` the setup script over the live launcher — that copies the wrapper, not the body. To re-deploy launcher changes without rerunning the full installer (apt, services, etc.), extract the heredoc directly: `sudo awk "/^cat > .* << 'LAUNCH'\$/{f=1;next} /^LAUNCH\$/{f=0} f" pi-setup/setup-satellite-kiosk-boot.sh | sudo tee /home/ngansen/.hanryx-dual-monitor.sh >/dev/null`.
*   **Old launcher loops survive `systemctl stop` because of `setsid nohup`**: the autostart spawns the launcher under `setsid nohup ... &` (in some older deploys), which detaches it from the systemd cgroup. Stopping/restarting the service does NOT kill it; the old bash keeps spawning chromium with its in-memory (stale) flags. Always `sudo pkill -9 -f hanryx-dual-monitor.sh` AND `sudo pkill -9 -f /usr/lib/chromium/chromium` before restarting after a launcher edit. The current heredoc in `setup-satellite-kiosk-systemd.sh` / `-session.sh` no longer uses `setsid nohup`, so re-extraction also fixes future restarts.
*   **Chromium only honours the LAST `--enable-features=` flag**: passing `--enable-features=UseOzonePlatform --enable-features=VaapiVideoDecoder` silently drops `UseOzonePlatform`. ALWAYS merge: `--enable-features=UseOzonePlatform,VaapiVideoDecoder`. Same applies to `--disable-features=`.
*   **Chromium 147 silently rejects `--no-sandbox` + `--disable-gpu` + `--use-gl=swiftshader` together on Pi 5 wayland-native**: adding all three to COMMON_FLAGS makes chromium exit code 1 in <1s with no useful stderr. The kiosk launcher's known-good baseline (commit 17b8636, April 2026) uses ONLY `--ozone-platform=wayland` + `--enable-features=UseOzonePlatform,VaapiVideoDecoder` + `--disable-dev-shm-usage` and lets the launch loop drop to software-rendering FALLBACK_FLAGS only after 2 quick crashes. Do NOT preemptively add software-rendering flags to defaults — chromium handles GPU process death gracefully when allowed.
*   **GPU process termination ≠ browser crash**: chromium can show `GPU.GPUProcessTerminationStatus2 = 4` in stderr and KEEP RUNNING fine (falls back to software compositing internally). Don't treat GPU process death messages as proof the browser is unusable; check the actual chromium PID etime.
*   **`pi-setup/diagnostics-grafana-kiosk.sh` is MAIN-pi-only — has a hostname guard**: the script launches a chromium kiosk pointed at `http://localhost:3001/d/hanryx-pi-ops/...` (Grafana). Grafana only listens on localhost on the **main** pi (hanryxvault). If this script is ever run on the satellite (e.g. as `sudo -u ngansen nohup bash …diagnostics-grafana-kiosk.sh &`), the chromium it spawns lives forever (PPID becomes systemd) and labwc places it on whichever HDMI output is free — **silently hijacking either the admin or kiosk screen with an `ERR_CONNECTION_REFUSED localhost` page**. Symptoms: an extra `chromium-browser (/tmp/chromium-grafana): localhost - Chromium` toplevel, surviving multiple `systemctl restart hanryx-kiosk.service` cycles. Hostname guard at the top now exits 2 on any host other than `hanryxvault`. To kill an already-running stray: `sudo pkill -9 -f 'diagnostics-grafana-kiosk.sh' && sudo pkill -9 -f 'user-data-dir=/tmp/chromium-grafana' && sudo rm -rf /tmp/chromium-grafana && sudo pkill -HUP -x labwc`.
*   **`hanryx-watchdog.service` `pkill -f chromium` was too greedy**: the satellite watchdog used to run `pkill -f chromium` after a health recovery, which kills *every* chromium on the box — including any unrelated diagnostic chromium and (more dangerously) any chromium debug session a developer is actively using. Tightened to `pkill -f 'user-data-dir=/home/ngansen/.hanryx/'` so only the two kiosk profiles (admin, kiosk) get bounced.
*   **Main pi 7″ Grafana kiosk needs lightdm + `rpd-labwc` session shim**: Pi OS Bookworm's stock `/etc/lightdm/lightdm.conf` ships with `user-session=rpd-labwc` and `autologin-session=rpd-labwc`, but the actual session file is `/usr/share/wayland-sessions/labwc.desktop` — `rpd-labwc.desktop` does NOT exist. With autologin enabled, lightdm logs `Failed to find session configuration rpd-labwc` and falls through to the **greeter** (login prompt with on-screen keyboard) instead of auto-starting labwc. Fix: `sudo ln -sf /usr/share/wayland-sessions/labwc.desktop /usr/share/wayland-sessions/rpd-labwc.desktop`. Also: `raspi-config nonint do_boot_behaviour B4` only sets `agetty --autologin` (text autologin) if lightdm isn't installed — install lightdm FIRST (`sudo apt-get install -y lightdm`) THEN re-run B4 to get true graphical autologin. Drop `/etc/systemd/system/getty@tty1.service.d/autologin.conf` so agetty doesn't race lightdm. The 7″ Grafana kiosk launches via `~/.config/autostart/hanryx-grafana-kiosk.desktop` (XDG autostart, picked up by labwc when started under a full lightdm session — NOT under a bare `labwc -C ...` greeter session).
*   **gnome-keyring blocks chromium kiosk on first launch with "Choose password for new keyring" modal**: when chromium starts under a fresh user-data-dir on a labwc/lightdm session, libsecret/gnome-keyring pops a "Choose password for new keyring" dialog requesting a password for the "Default Keyring". This modal blocks the kiosk URL behind it until dismissed — on a headless trade-show display nobody is there to click "Cancel". Fix: pass `--password-store=basic --use-mock-keychain` to chromium so it stops trying to use the system keyring entirely. Already applied to `pi-setup/diagnostics-grafana-kiosk.sh`. The same flags should be added to any other kiosk chromium invocation under labwc.
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