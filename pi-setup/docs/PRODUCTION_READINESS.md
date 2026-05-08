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

### ✅ Resolved in the hardening commit (2026-05-08)

1. ✅ **Default fallback passwords removed.** `pi-setup/docker-compose.yml` now uses `${DB_PASSWORD:?...}`, `${SESSION_SECRET:?...}`, `${ADMIN_PASSWORD:?...}` — compose refuses to start if any is missing instead of silently booting with `vaultpos` / `hanryxvault` / `change-me-on-the-pi`. Verified by `docker compose config` failing with a clear error when a var is unset.
2. ✅ **Grafana admin password now declarative.** `GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:?...}` added to `monitoring/docker-compose.yml`. `.env.example` updated with the new required key. No more `admin/admin` on first boot.
3. ✅ **All 5 floating `:latest` tags pinned to digests.** `prom/prometheus:v3.11.3`, `grafana/grafana:13.1.0-25469333600-ubuntu`, `prom/node-exporter:v1.11.1`, `jc21/nginx-proxy-manager:2.14.0`, `adminer:5.4.2-standalone` — each with a multi-arch `@sha256:...` digest. CI guard (`check-no-floating-tags.py`) was already scanning all of `pi-setup/` and is now passing (was previously failing on `main`).

### 🟠 Remaining — High (stability under stress)

4. **Hardcoded Tailscale IP `100.125.5.34`** in `pi-setup/proxy/docker-compose.yml` (lines 39–41, 68) and DNS hostnames in `pi-setup/nginx/hanryxvault.conf` (lines 46, 123).
   - Survives normal operation, but a Tailscale account migration or DDNS rename silently breaks routing.
   - **Action**: pull these from `.env` (`TAILSCALE_HOST_IP`, `PUBLIC_HOSTNAME`) and template via `envsubst` at boot, OR document the swap procedure in `MOVE_PLAYBOOK.md`.

5. **Ollama model volume on SD card** _(intentional — see comment in `docker-compose.yml:543`: keeps assistant up if USB drive unplugged)_ (`ollama-data` named volume, not bind-mounted to `/mnt/cards`).
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

## 4. Hardening commit applied + remaining backlog

**Done in this commit (A + B + C; F was already covered by the existing scanner):**

- ✅ A — Fail-fast secret interpolation in `docker-compose.yml`
- ✅ B — `GRAFANA_ADMIN_PASSWORD` required, no more `admin/admin`
- ✅ C — All 5 `:latest` tags in `monitoring/` + `proxy/` pinned to multi-arch digests
- ✅ F — Verified `check-no-floating-tags.py` already scans the whole `pi-setup/` tree (no CI extension needed; the rule was already there, the offending tags just predated it being enforced)

**Operator action required after pulling this commit:**

1. SSH to the main Pi, `cd ~/Hanryx-Vault-POS && git pull`
2. Edit `pi-setup/.env` — add a line `GRAFANA_ADMIN_PASSWORD=<choose a strong password>`. Verify `DB_PASSWORD`, `SESSION_SECRET`, `ADMIN_PASSWORD` are also set to non-default values.
3. Re-up the affected stacks:
   ```bash
   cd pi-setup && docker compose up -d              # validates secrets, no-op if unchanged
   cd monitoring && docker compose up -d --force-recreate grafana   # picks up new admin password
   cd ../proxy && docker compose pull && docker compose up -d        # pulls pinned NPM/adminer
   cd ../monitoring && docker compose pull && docker compose up -d   # pulls pinned prom/grafana/node-exp
   ```
4. Verify Grafana login at the new password; old `admin/admin` should fail.

**Backlog (safe to defer past the show):**

| # | Change | Risk | Est. effort |
|---|---|---|---|
| D | Templatize hardcoded Tailscale IP via `envsubst` at boot | medium | 30 min |
| E | Add healthchecks to Prometheus / Grafana / node-exporter / NPM / Adminer | low | 20 min |
| G | Add a Prometheus blackbox probe for `hanryx-watchdog.service` liveness | medium | 30 min |
| H | Replace disabled `frontail` with `gotty` or `logdy` (ARM64 log viewer) | low | 1 h |

---

## 5. What's NOT in scope of this review

- Application-level POS logic (`server.py`, route handlers) — unchanged in this session.
- TCG card-data correctness, image recognition tuning — separate workstreams.
- The known **labwc 0.9.2 `MoveToOutput HDMI-A-2` rendering quirk on the 5″ MPI5008** — already documented in `replit.md`, awaiting hardware-swap test or labwc upgrade. Does not block the show; the 5″ is showing the correct content, just via the workaround path.
