# HanryxVault — Raspberry Pi 5 Server Setup

Everything you need to run your entire operation locally on your Pi:

| Service | Domain | Location on Pi |
|---|---|---|
| HanryxVault POS server | `hanryxvault.tailcfc0a3.ts.net` | `/opt/hanryxvault/` |
| hanryxvault.cards website | `hanryxvault.cards` | `/var/www/hanryxvault.cards/` |
| hanryxvault.app website | `hanryxvault.app` | `/var/www/hanryxvault.app/` |
| Inventory scanner (Replit app) | served under hanryxvault.app or .cards | `/var/www/...` |
| Admin dashboard | any domain `/admin` | built into POS server |

---

## Step 1 — Copy this folder to your Pi

On your current computer, open a terminal and run:

```bash
scp -r pi-setup/ pi@<YOUR_PI_IP>:/home/pi/
```

Or use a USB drive — copy the `pi-setup/` folder to the Pi.

---

## Step 2 — Run the installer

SSH into your Pi and run:

```bash
ssh pi@<YOUR_PI_IP>
cd /home/pi/pi-setup
sudo bash install.sh
```

The installer will:
- Install Python 3, nginx, certbot, fail2ban
- Deploy your POS server with gunicorn (fast, production-ready)
- Create all 4 domain nginx configs
- Ask for your Zettle Client ID and Secret
- Start everything as a background service
- Lock down the firewall

---

## Step 3 — Export your Replit websites and copy them to the Pi

### For each Replit project (inventory scanner, POS system, hanryxvault.cards, hanryxvault.app):

**Option A — Download a built/static site:**

1. In Replit, open your project
2. Run the build command in the Shell tab (usually `npm run build` or `pnpm build`)
3. Download the output folder — usually called `dist/` or `build/`
4. Copy it to your Pi:

```bash
# hanryxvault.cards
scp -r dist/* pi@<YOUR_PI_IP>:/var/www/hanryxvault.cards/

# hanryxvault.app
scp -r dist/* pi@<YOUR_PI_IP>:/var/www/hanryxvault.app/
```

**Option B — Copy the whole project (Node.js app):**

```bash
# Copy the project
scp -r my-replit-project/ pi@<YOUR_PI_IP>:/opt/sites/hanryxvault-cards/

# SSH in and set it up
ssh pi@<YOUR_PI_IP>
cd /opt/sites/hanryxvault-cards
npm install
sudo bash /home/pi/pi-setup/scripts/add-site.sh
```

---

## Step 4 — Point your domains to your Pi

Log into your domain registrar (wherever you bought hanryxvault.cards and hanryxvault.app) and update the **A records**:

```
hanryxvault.cards   →  A record  →  <YOUR_PI_PUBLIC_IP>
hanryxvault.app     →  A record  →  <YOUR_PI_PUBLIC_IP>
```

To find your Pi's public IP:
```bash
curl ifconfig.me
```

> Note: DNS changes can take up to 24 hours but usually happen within minutes.

---

## Step 5 — Enable HTTPS (required for Zettle)

Once your DNS is pointing to your Pi, run this for each domain:

```bash
sudo bash scripts/enable-https.sh hanryxvault.cards
sudo bash scripts/enable-https.sh hanryxvault.app
```

Certificates renew automatically every 90 days.

---

## Step 6 — Update Zettle redirect URI

In your Zettle Developer app settings:
- Old URI: `https://hanryxvault.tailcfc0a3.ts.net/zettle/callback`
- Keep this one — it already works via Tailscale HTTPS

Or if you want to use your public domain instead:
- New URI: `https://hanryxvault.app/zettle/callback`

---

## Useful commands on the Pi

```bash
# Check if POS server is running
sudo systemctl status hanryxvault

# Watch live server logs
sudo journalctl -u hanryxvault -f

# Restart the POS server
sudo systemctl restart hanryxvault

# Test the server locally
curl http://localhost:8080/health

# Check nginx status
sudo systemctl status nginx
sudo nginx -t

# View which sites nginx is serving
ls /etc/nginx/sites-enabled/

# Check firewall
sudo ufw status

# Backup your database
cp /opt/hanryxvault/vault_pos.db ~/vault_pos_backup_$(date +%Y%m%d).db
```

---

## Syncing inventory from Replit to Pi

Your server automatically tries to pull inventory from your two Replit sites on startup.
Once you move everything to the Pi, you can change these source URLs in `server.py`:

```python
CLOUD_INVENTORY_SOURCES = [
    "https://inventory-scanner-ngansen84.replit.app/api/inventory",
    "https://hanryxvault.app/api/products",  # ← this will be your Pi once moved
]
```

After moving your sites to the Pi, update the second URL to `http://localhost:8080/inventory`
so it syncs locally without hitting the internet.

---

## Performance notes

- **gunicorn** replaces Flask's dev server — handles real traffic, 2 workers for Pi 5
- **WAL mode** SQLite — faster writes, no read locks during queries
- **nginx gzip** — compresses all responses automatically
- **Static asset caching** — JS/CSS/images cached for 1 year in browsers
- **fail2ban** — auto-bans IPs that try to brute-force SSH

---

## Folder structure on Pi after install

```
/opt/hanryxvault/
  server.py          ← your POS server
  vault_pos.db       ← SQLite database (all your sales + inventory)
  venv/              ← Python virtual environment
  hanryxvault.apk    ← APK file (if provided)

/var/www/
  hanryxvault.cards/ ← hanryxvault.cards website files
  hanryxvault.app/   ← hanryxvault.app website files

/etc/nginx/sites-available/
  hanryxvault        ← POS server (Tailscale)
  hanryxvault.cards  ← cards website
  hanryxvault.app    ← app website

/var/log/hanryxvault/
  access.log         ← HTTP request log
  error.log          ← Error log
```
