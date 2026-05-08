# HanryxVault POS — Production Readiness Review

_Snapshot: 2026-05-08, ahead of trade-show deployment._
_Reviewed against `main` @ `fffbb74`._

---

## 1. What is currently running and verified

### Main Pi — `hanryxvault` (100.125.5.34)

| Layer | State | Notes |
|---|---|---|
| Docker stack (`pi-setup/docker-compose.yml`) | ✅ up 22h+ | db, redis, pgbouncer, pos, sync, recognizer, assistant, pokeapi, storefront — all with healthchecks + `restart: unless-stopped`, all bind-mounted to `/mnt/cards`. |
| Proxy stack (NPM + Adminer) | ✅ | Bound to Tailscale IP only. |
| Monitoring stack (Prometheus + Grafana + node-exporter) | ✅ | Grafana on `localhost:3001`. |
| 7″ Grafana diagnostic display | ✅ **fixed today** | lightdm autologin → labwc → chromium kiosk → `hanryx-pi-ops` dashboard. CPU/RAM/SoC temp/uptime/load/storage rendering live. |
| Self-heal (`hanryx-heal.timer`) | ✅ | 60 s sweep. |
| Postgres backup (hourly + nightly S3/SFTP) | ✅ | `hanryx-postgres-backup.timer`, `hanryxvault-backup.timer`. |

### Satellite Pi — `hanryxvault-sat` (100.121.45.69)

| Layer | State | Notes |
|---|---|---|
| Local POS (`hanryxvault.service`) | ✅ | `Restart=on-failure`. |
| Dual-monitor kiosk launcher (`/home/ngansen/.hanryx-dual-monitor.sh`) | ✅ | 10.1″ admin + 5″ customer welcome both stable. Heredoc re-extraction documented. |
| Watchdog (`hanryx-watchdog.service`) | ✅ **tightened today** | `pkill -f 'user-data-dir=/home/ngansen/.hanryx/'` — no longer kills unrelated chromium. |
| Satellite sync (`hanryxvault-satellite-sync.service`) | ✅ | Polls every 30 s. |
| Scan hub, kiosk-restart timer, video-refresh timer | ✅ | All present. |

### Recent fixes captured in repo

| Commit | Change |
|---|---|
| `594d769` | Hostname guard on `diagnostics-grafana-kiosk.sh` (exits 2 if not on `hanryxvault`); watchdog `pkill` scoped to kiosk profiles only. |
| `65d344a` | replit.md gotcha: lightdm needs `rpd-labwc` → `labwc.desktop` shim. |
| `fffbb74` | `--password-store=basic --use-mock-keychain` to suppress gnome-keyring modal blocking the kiosk on first boot. |

---

## 2. Issues to resolve **before** the show

### 🔴 Critical (security exposure)

1. **Default fallback passwords in `pi-setup/docker-compose.yml`**
   - `POSTGRES_PASSWORD: ${DB_PASSWORD:-vaultpos}` (line 23)
   - `ADMIN_PASSWORD: ${ADMIN_PASSWORD:-hanryxvault}` (line 144)
   - `SESSION_SECRET: ${SESSION_SECRET:-change-me-on-the-pi}` (lines 143, 516)

   If the live `.env` is ever missing or partially merged, the stack silently boots with publicly-known credentials. **Action**: drop the fallbacks — `${DB_PASSWORD:?DB_PASSWORD is required}` — so compose refuses to start instead of using a default. Verify on each Pi that `.env` actually defines all three.

2. **Grafana admin password left at `admin/admin`** (`pi-setup/monitoring/docker-compose.yml:62` comment confirms intent).

   Grafana on the main Pi exposes anonymous read for the kiosk dashboard but `admin/admin` lets anyone on the Tailnet rewrite dashboards or add data sources. **Action**: set `GF_SECURITY_ADMIN_PASSWORD` from `.env` and rotate.

### 🟠 High (stability under stress)

3. **Floating `:latest` tags in monitoring + proxy stacks**
   - `prom/prometheus:latest`, `grafana/grafana:latest`, `prom/node-exporter:latest`, `jc21/nginx-proxy-manager:latest`, `adminer:latest`.
   - Violates the project's stated reproducible-build policy (and CI guard for the **main** compose only catches the main file).
   - **Action**: pin to digests, like the main compose already does. Extend `pi-setup-security.yml` to also lint `monitoring/docker-compose.yml` and `proxy/docker-compose.yml`.

4. **Hardcoded Tailscale IP `100.125.5.34`** in `pi-setup/proxy/docker-compose.yml` (lines 39–41, 68) and DNS hostnames in `pi-setup/nginx/hanryxvault.conf` (lines 46, 123).
   - Survives normal operation, but a Tailscale account migration or DDNS rename silently breaks routing.
   - **Action**: pull these from `.env` (`TAILSCALE_HOST_IP`, `PUBLIC_HOSTNAME`) and template via `envsubst` at boot, OR document the swap procedure in `MOVE_PLAYBOOK.md`.

5. **Ollama model volume on SD card** (`ollama-data` named volume, not bind-mounted to `/mnt/cards`).
   - A `docker compose down -v` wipes the model, forcing a multi-GB re-download on slow venue Wi-Fi.
   - **Action**: bind-mount to `/mnt/cards/ollama-data` like every other stateful service.

### 🟡 Medium

6. **No healthchecks on Prometheus, Grafana, node-exporter, NPM, Adminer.** `restart: unless-stopped` only catches process death, not unresponsive HTTP. Add `wget --spider http://localhost:9090/-/ready` etc.

7. **`frontail` log viewer disabled** (amd64-only). Replace with `gotty` or drop the commented-out block to reduce confusion.

8. **No active monitoring of the watchdog itself.** If `hanryx-watchdog.service` exits, nothing pages. Add it to a Prometheus blackbox check or a cron heartbeat.

### 🟢 Low / quick wins

9. The `2 lightdm session-child` processes seen during today's debug — confirm they reduce to 1 after the `rpd-labwc` shim has been applied to a freshly-rebooted main Pi (you've not yet rebooted post-fix).

10. `ssh hanryxvault` from inside `hanryxvault` opened a nested session and ate diagnostic commands earlier — minor operator UX. Consider a `~/.bash_profile` check that warns when nesting.

---

## 3. Pre-show go-live checklist (run on each Pi, in order)

```bash
# === MAIN PI ===
ssh hanryxvault
cd ~/Hanryx-Vault-POS && git pull

# 1. confirm secrets are real, not defaults
grep -E '^(DB_PASSWORD|ADMIN_PASSWORD|SESSION_SECRET|GF_SECURITY_ADMIN_PASSWORD)=' pi-setup/.env \
  | grep -vE '=(vaultpos|hanryxvault|change-me-on-the-pi|admin)$' \
  || { echo "❌ default password found in .env"; exit 1; }

# 2. bind-mount survives down
sudo mountpoint -q /mnt/cards && echo "✅ /mnt/cards mounted" || { echo "❌ /mnt/cards NOT mounted"; exit 1; }

# 3. all containers healthy
cd pi-setup && docker compose ps --format json | jq -r '.[] | "\(.Name)  \(.State)  \(.Health // "n/a")"'

# 4. backups ran in last 25 h
ls -lt /mnt/cards/backups/postgres/ | head -3
journalctl -u hanryxvault-backup.service --since '25 hours ago' --no-pager | tail -5

# 5. 7" kiosk
pgrep -af 'labwc|chromium-grafana' | grep -v grep
ls -la /home/ngansen/grafana-kiosk.log

# 6. self-heal active
systemctl status hanryx-heal.timer --no-pager | head -5

# === SATELLITE PI ===
ssh hanryxvault-sat
cd ~/Hanryx-Vault-POS && git pull

# 7. kiosk processes
pgrep -af 'hanryx-dual-monitor.sh|chromium' | grep -v grep
# expect: 1× launcher, 1× admin chromium (--user-data-dir=...admin), 1× kiosk chromium (--user-data-dir=...kiosk), zygotes

# 8. watchdog running with tightened pkill
systemctl is-active hanryx-watchdog.service
grep -q "user-data-dir=/home/ngansen/.hanryx/" /etc/systemd/system/hanryx-watchdog.service \
  && echo "✅ watchdog pkill is scoped" \
  || echo "❌ watchdog still uses greedy pkill"

# 9. NO stray diagnostics-grafana-kiosk on satellite (the bug from yesterday)
pgrep -af diagnostics-grafana-kiosk && echo "❌ stray grafana kiosk on satellite!" || echo "✅ none"

# 10. satellite sync alive
systemctl is-active hanryxvault-satellite-sync.service
journalctl -u hanryxvault-satellite-sync.service --since '1 hour ago' --no-pager | tail -10

# 11. local POS responds
curl -fsS http://localhost:8080/healthz && echo "✅ POS up"

# === BOTH PIs: physical ===
# 12. eyeball check
#   - MAIN: 7" shows live Grafana panels (CPU/RAM/temp/load/uptime/storage)
#   - SATELLITE 10.1": admin login OR last-used admin page
#   - SATELLITE 5":     customer welcome / current product
```

---

## 4. Recommended changes I can make next (in priority order)

| # | Change | Risk | Est. effort |
|---|---|---|---|
| A | Drop `${VAR:-default}` fallbacks for the three secrets, fail-fast instead | low | 5 min |
| B | Set Grafana `GF_SECURITY_ADMIN_PASSWORD` env, document rotation | low | 10 min |
| C | Pin `:latest` tags to digests in `monitoring/` + `proxy/` compose files | low | 20 min |
| D | Bind-mount `ollama-data` to `/mnt/cards/ollama-data` (one-time copy needed) | medium — needs `docker compose down ollama` | 15 min |
| E | Add healthchecks to Prometheus / Grafana / node-exporter / NPM / Adminer | low | 20 min |
| F | Extend `.github/workflows/pi-setup-security.yml` to lint `monitoring/` + `proxy/` compose for floating tags | low | 10 min |
| G | Add a Prometheus blackbox probe for `hanryx-watchdog.service` liveness on satellite | medium | 30 min |

Any combination is safe to do now (everything's in git, no in-flight feature work). I'd recommend **A + B + C + F** as a single hardening commit before the show — they're all config-only and reversible.

---

## 5. What's NOT in scope of this review

- Application-level POS logic (`server.py`, route handlers) — unchanged in this session.
- TCG card-data correctness, image recognition tuning — separate workstreams.
- The known **labwc 0.9.2 `MoveToOutput HDMI-A-2` rendering quirk on the 5″ MPI5008** — already documented in `replit.md`, awaiting hardware-swap test or labwc upgrade. Does not block the show; the 5″ is showing the correct content, just via the workaround path.
