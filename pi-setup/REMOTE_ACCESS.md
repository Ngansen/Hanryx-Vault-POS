# Remote access — Nginx Proxy Manager + Prometheus + Grafana

End-to-end walkthrough for the tailnet-only remote-access stack on
the HanryxVault setup. After running through this once, you'll have:

| URL | What it shows |
|---|---|
| `https://hanryxvault.tailcfc0a3.ts.net/`         | The POS UI |
| `https://hanryxvault.tailcfc0a3.ts.net/logs/`    | Live tail of `/mnt/cards/zhsync.log` |
| `https://hanryxvault.tailcfc0a3.ts.net/grafana/` | Dashboards (CPU/RAM/disk/temp for both Pis) |
| `http://100.125.5.34:8081/`                      | Adminer (Postgres web UI) |
| `http://100.125.5.34:9090/`                      | Prometheus (PromQL console) |
| `http://100.125.5.34:81/`                        | NPM admin GUI |

All of the above are reachable **only from devices on your tailnet**.
The Pi never opens a port to the open internet.

---

## Architecture

Two compose stacks, one shared docker network:

```
┌─ MAIN PI: hanryxvault (tailnet 100.125.5.34) ──────────────┐
│                                                            │
│  Docker network: hanryx_net (external, shared)             │
│                                                            │
│  pi-setup/proxy/docker-compose.yml                         │
│    • npm        80, 443, 81  (bound to 100.125.5.34 only)  │
│    • frontail   internal :9001  ── reached via NPM /logs/  │
│    • adminer    8081  (bound to 100.125.5.34 only)         │
│                                                            │
│  pi-setup/monitoring/docker-compose.yml                    │
│    • prometheus     9090  (bound to 100.125.5.34 only)     │
│    • grafana        internal :3000  ── via NPM /grafana/   │
│    • node-exporter  host network :9100  (this Pi)          │
│                                                            │
│  Postgres            host install via apt :5432            │
│  POS UI              host process :8080                    │
│                                                            │
└────────────────────────────────────────────────────────────┘
                            ▲
                            │ Prometheus scrapes 192.168.86.22:9100
                            │
┌─ KIOSK PI: 192.168.86.22 ─┴────────────────────────────────┐
│                                                            │
│  prometheus-node-exporter   apt install, systemd, :9100    │
│  Chromium kiosk             unchanged                      │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

---

## Prerequisites

- `docker` and the `docker compose` plugin installed on the main Pi
- `tailscale` running on both Pis (`tailscale status` shows both)
- `/mnt/cards` mounted on the main Pi
- The main Pi's tailnet IP is `100.125.5.34` (confirm with `tailscale ip -4`).
  If different, search/replace it across both compose files before bringing them up.

---

## Step 1 — pull the repo and create the shared network

On the **main Pi**:

```bash
cd ~/Hanryx-Vault-POS && git pull
docker network create hanryx_net
```

`docker network create hanryx_net` only needs to run once. Subsequent
runs error with "already exists" — harmless, just means it's already
there.

---

## Step 2 — bring up the proxy stack

```bash
cd ~/Hanryx-Vault-POS/pi-setup/proxy
docker compose up -d
```

Verify all three containers are running:

```bash
docker compose ps
# Expected: npm, frontail, adminer all "Up"
```

You should now be able to reach NPM's admin GUI from any tailnet
device at <http://100.125.5.34:81/> — log in with the default
credentials and immediately change them on the prompt:

- Email: `admin@example.com`
- Password: `changeme`

(NPM forces a password change on first login. Per the install
conversation we agreed to leave the seed credentials and change them
interactively here.)

---

## Step 3 — bring up the monitoring stack

Create the persistent-data directories with the right ownership
**before** the first `docker compose up`, otherwise Prometheus and
Grafana will fail to write and crash-loop:

```bash
cd ~/Hanryx-Vault-POS/pi-setup/monitoring
mkdir -p prometheus-data grafana-data
sudo chown -R 65534:65534 prometheus-data    # nobody:nobody — prometheus user
sudo chown -R 472:472     grafana-data       # grafana user inside the container
docker compose up -d
```

Verify:

```bash
docker compose ps
# Expected: prometheus, grafana, node-exporter all "Up"
docker compose logs prometheus | tail -20
# Should end with "Server is ready to receive web requests."
```

Quick sanity check — query Prometheus directly:

```bash
curl -s http://100.125.5.34:9090/api/v1/targets | jq '.data.activeTargets[] | {job: .labels.job, health: .health, lastScrape: .lastScrape}'
```

You should see three targets: `prometheus` (UP), `node-main` (UP), and
`node-kiosk` (DOWN — we haven't installed the exporter on the kiosk yet).

---

## Step 4 — install node_exporter on the kiosk Pi

SSH into the kiosk and install the apt package — it's a single static
binary plus a systemd unit, takes about 30 seconds:

```bash
ssh ngansen@192.168.86.22
sudo apt update
sudo apt install -y prometheus-node-exporter
sudo systemctl enable --now prometheus-node-exporter
# Verify:
curl -s http://localhost:9100/metrics | head -20
exit
```

Back on the main Pi, the `node-kiosk` Prometheus target should flip
to UP within 15 s (the scrape interval). Re-run the curl from Step 3
to confirm.

---

## Step 5 — get a Tailscale HTTPS cert for the main Pi

Tailscale will issue a real, browser-trusted cert for your machine's
FQDN. Run on the **main Pi**:

```bash
sudo tailscale cert hanryxvault.tailcfc0a3.ts.net
```

This drops two files in the current directory:

- `hanryxvault.tailcfc0a3.ts.net.crt`
- `hanryxvault.tailcfc0a3.ts.net.key`

Copy them somewhere convenient and `chmod 644` them so they're
readable when you upload via the NPM web GUI:

```bash
sudo cp hanryxvault.tailcfc0a3.ts.net.{crt,key} /tmp/
sudo chmod 644 /tmp/hanryxvault.tailcfc0a3.ts.net.{crt,key}
```

(`tailscale cert` is good for 90 days. Renewing is the same command —
re-upload to NPM. Putting renewal in cron is a nice-to-have for later.)

---

## Step 6 — upload the cert to NPM

In the NPM admin GUI (<http://100.125.5.34:81/>):

1. Top nav → **SSL Certificates**
2. **Add SSL Certificate** → **Custom**
3. Name: `tailscale-hanryxvault`
4. Certificate Key: paste the `.key` contents  *(or use the file picker)*
5. Certificate: paste the `.crt` contents
6. Intermediate Certificate: leave blank
7. **Save**

Should show "Custom Certificate added".

---

## Step 7 — create the Proxy Host with Custom Locations

Still in the NPM GUI:

1. Top nav → **Hosts → Proxy Hosts**
2. **Add Proxy Host**
3. **Details** tab:
   - Domain Names: `hanryxvault.tailcfc0a3.ts.net`
   - Scheme: `http`
   - Forward Hostname / IP: `host.docker.internal`
   - Forward Port: `8080`
   - Enable: **Block Common Exploits**, **Websockets Support**
4. **Custom locations** tab — click **Add location** three times:

   **Location 1 — frontail (live log tail):**
   - location: `/logs`
   - Scheme: `http`
   - Forward Hostname / IP: `frontail`
   - Forward Port: `9001`
   - Click the gear icon → paste in the "Custom Nginx Configuration":
     ```
     proxy_set_header Upgrade $http_upgrade;
     proxy_set_header Connection "upgrade";
     ```
     *(needed because frontail uses websockets to push new log lines)*

   **Location 2 — Grafana:**
   - location: `/grafana`
   - Scheme: `http`
   - Forward Hostname / IP: `grafana`
   - Forward Port: `3000`
   - Click the gear icon → Custom Nginx Configuration:
     ```
     proxy_set_header Upgrade $http_upgrade;
     proxy_set_header Connection "upgrade";
     proxy_set_header Host $host;
     ```

5. **SSL** tab:
   - SSL Certificate: select `tailscale-hanryxvault`
   - Enable: **Force SSL**, **HTTP/2 Support**, **HSTS Enabled**
6. **Save**

---

## Step 8 — verify the URLs

From any tailnet device (your laptop, your phone with Tailscale on,
etc.):

```
https://hanryxvault.tailcfc0a3.ts.net/         → POS UI
https://hanryxvault.tailcfc0a3.ts.net/logs/    → frontail (note trailing /)
https://hanryxvault.tailcfc0a3.ts.net/grafana/ → Grafana login
http://100.125.5.34:8081/                       → Adminer
http://100.125.5.34:9090/                       → Prometheus
http://100.125.5.34:81/                         → NPM admin
```

Browser should show a green padlock on the HTTPS URLs (real cert,
not self-signed). If it shows "Not secure", the cert upload in Step 6
didn't take — re-upload and reload the page.

---

## Step 9 — first Grafana login + import the Node Exporter dashboard

1. Open `https://hanryxvault.tailcfc0a3.ts.net/grafana/`
2. Login with `admin` / `admin`. Grafana forces a password change.
3. Verify the data source: left nav → **Connections → Data sources** —
   "Prometheus" should be there, marked Default. Click it → scroll to
   bottom → **Save & test** should show "Data source is working".
4. Import dashboard **1860** (Node Exporter Full):
   - Left nav → **Dashboards → New → Import**
   - Grafana.com dashboard ID: `1860` → **Load**
   - Select Prometheus data source → **Import**
5. The dashboard's "Host" dropdown at the top will list both Pis
   (`hanryxvault` and `kiosk`). Switch between them to see CPU,
   memory, disk, network, temperature graphs side-by-side.

---

## Step 10 — log into Adminer

1. Open `http://100.125.5.34:8081/`
2. System: **PostgreSQL**
3. Server: `host.docker.internal` (pre-filled by `ADMINER_DEFAULT_SERVER`)
4. Username: your Postgres user
5. Password: your Postgres password
6. Database: leave blank to see all DBs, or fill in for direct access

---

## Maintenance / common tasks

**Renewing the Tailscale cert** *(every 90 days)*:

```bash
sudo tailscale cert hanryxvault.tailcfc0a3.ts.net
sudo cp hanryxvault.tailcfc0a3.ts.net.{crt,key} /tmp/
sudo chmod 644 /tmp/hanryxvault.tailcfc0a3.ts.net.{crt,key}
# Then in NPM: SSL Certificates → tailscale-hanryxvault →
# pencil icon → re-upload both files → Save.
```

**Adding a new scrape target** *(e.g. a third Pi or a NAS)*:

1. Install `prometheus-node-exporter` on that machine (Step 4)
2. Edit `pi-setup/monitoring/prometheus.yml`, add another `job_name:`
   block with the new IP
3. `docker kill -s HUP prometheus` (zero-downtime config reload)

**Restarting only the proxy stack** *(e.g. after editing its compose)*:

```bash
cd ~/Hanryx-Vault-POS/pi-setup/proxy && docker compose up -d
```

**Restarting only the monitoring stack:**

```bash
cd ~/Hanryx-Vault-POS/pi-setup/monitoring && docker compose up -d
```

The two stacks share `hanryx_net` but have independent lifecycles —
restarting one never touches the other.

---

## Troubleshooting

**Symptom: NPM container won't start, error `bind: cannot assign requested address`**
→ The Pi's tailnet IP isn't `100.125.5.34` (or `tailscaled` isn't up
yet at boot). Check `tailscale ip -4` and update both compose files.

**Symptom: NPM proxy host returns 502 Bad Gateway**
→ The upstream service isn't reachable. For host services
(POS UI on :8080) confirm it's actually running with `ss -tlnp | grep 8080`.
For docker services (frontail, grafana) confirm they're on `hanryx_net`:
`docker network inspect hanryx_net | jq '.[0].Containers'`.

**Symptom: Grafana login redirects to a broken URL**
→ `GF_SERVER_ROOT_URL` doesn't match the URL the browser is using.
Edit `pi-setup/monitoring/docker-compose.yml`, fix the env, then
`docker compose up -d grafana`.

**Symptom: Prometheus shows `node-kiosk` target as DOWN**
→ SSH the kiosk and check `systemctl status prometheus-node-exporter`.
If running, check `curl http://192.168.86.22:9100/metrics` from the
main Pi. If that fails, the kiosk's UFW or iptables is blocking the
LAN — `sudo ufw allow from 192.168.86.0/24 to any port 9100`.

**Symptom: frontail shows the log on first load but doesn't update**
→ Websocket isn't passing through NPM. Re-check Step 7 location 1's
Custom Nginx Configuration block contains the `Upgrade`/`Connection`
headers.

**Symptom: Adminer login: "could not connect to server"**
→ Postgres on the host isn't listening on the docker bridge. Edit
`/etc/postgresql/*/main/postgresql.conf`, set
`listen_addresses = 'localhost,172.17.0.1'` (the docker0 bridge IP),
restart Postgres, allow the bridge in `pg_hba.conf` with a line like
`host all all 172.17.0.0/16 md5`.

---

## What this stack does NOT do

- **No public internet exposure.** This is intentional. If you ever
  need a public-facing page (a customer-facing portal, say), add a
  separate Proxy Host in NPM bound to a different cert + a real domain
  with port-forwarding from your home router. Don't touch the
  tailnet-only setup.
- **No automatic alerting.** Prometheus has alert rules, Grafana has
  alert channels — neither is configured. Add later if you want
  email/SMS when disk runs low or a Pi goes down.
- **No log aggregation across Pis.** frontail tails one file on the
  main Pi. If you ever want kiosk-Pi logs centrally, add Loki + Promtail
  later (same architectural pattern: Loki container in the monitoring
  stack, Promtail on each Pi shipping logs to it).
- **No SSH proxy.** Use Tailscale SSH directly (`ssh ngansen@hanryxvault`
  or `ssh ngansen@192.168.86.22` from any tailnet device). NPM is
  HTTP/HTTPS only.
