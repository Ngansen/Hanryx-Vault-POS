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

---

## Storage layout: SD card vs USB drive

Bulky and write-heavy state lives on the UGreen 1 TB USB ext4 drive
mounted at `/mnt/cards`. The SD card only holds the OS, container
images, application code, and a small bit of state that needs to keep
working when the drive is unplugged.

```
/mnt/cards/                  (USB drive — 1 TB, the source of truth)
  postgres-data/             ← Postgres data dir (was Docker volume `pgdata`)
  pos-data/                  ← POS app local data (was `pos-data`)
  card-images/               ← Card image blob store (was `card-images`)
  pokeapi-data/              ← Cached PokeAPI dump (was `pokeapi-data`)
  cards_master/              ← Reference card master (already on drive)
  faiss/                     ← CLIP FAISS indexes (already on drive)
  models/
    paddleocr/<lang>/det/    ← PaddleOCR per-language detection models
    paddleocr/<lang>/rec/    ← PaddleOCR per-language recognition models
    clip-vit-b32.onnx        ← CLIP image-similarity model
  pokedex_local.db           ← SQLite mirror of pokedex (HANRYX_LOCAL_DB_DIR)
  logs/                      ← Long-retention log archive
  backups/                   ← Postgres dumps (cron, see scripts/backup.sh)

SD card (kept small on purpose):
  /var/lib/docker/volumes/pi-setup_ollama-data/   ← Ollama LLM cache (~2 GB)
                                                    stays on SD so the
                                                    assistant container
                                                    still boots if the
                                                    USB drive is unplugged.
```

### Bind-mount env-var overrides

`docker-compose.yml` reads each storage path from an env var with the
on-drive default baked in. Leave them unset for a normal install — only
set them when you need to point a service somewhere else (an
emergency repair, or a temporary copy of the data on the SD card while
the drive is being replaced).

| Env var               | Default                                  | What lives there                           |
| --------------------- | ---------------------------------------- | ------------------------------------------ |
| `DB_DATA_DIR`         | `/mnt/cards/postgres-data`               | Postgres `/var/lib/postgresql/data`        |
| `POS_DATA_DIR`        | `/mnt/cards/pos-data`                    | POS app local data dir                     |
| `CARD_IMAGES_DIR`     | `/mnt/cards/card-images`                 | Card image blob store (`/app/card-images`) |
| `POKEAPI_DATA_DIR`    | `/mnt/cards/pokeapi-data`                | PokeAPI mirror cache                       |
| `OCR_MODELS_DIR`      | `/mnt/cards/models/paddleocr`            | PaddleOCR per-language model files         |
| `CLIP_MODEL_PATH`     | `/mnt/cards/models/clip-vit-b32.onnx`    | CLIP ONNX file                             |
| `HANRYX_LOCAL_DB_DIR` | `/mnt/cards`                             | Root for `pokedex_local.db`, faiss/, etc.  |

`OCR_MODELS_DIR` deserves a special note: setting it to the empty
string (`OCR_MODELS_DIR=`) is a deliberate escape hatch — the OCR
worker will fall back to PaddleOCR's own `~/.paddleocr` cache inside
the container. Useful if the drive is being repaired and you want OCR
to keep limping along; the trade-off is that the per-language model
files (50–100 MB each) will re-download on every `docker compose
build` because the SD-side cache lives inside the image layer.

### Migrating an existing install onto the drive

If you have a running deploy from before the bind-mount layout (data
still in `/var/lib/docker/volumes/pi-setup_pgdata/_data` etc), run:

```bash
# Dry run first — prints exactly what it would copy, writes nothing.
bash pi-setup/scripts/move-volumes-to-drive.sh --dry-run

# Then for real (interactive: asks before each volume).
bash pi-setup/scripts/move-volumes-to-drive.sh

# Or non-interactive after you've checked the dry run output.
bash pi-setup/scripts/move-volumes-to-drive.sh --yes
```

The script:

* Refuses to run unless `/mnt/cards` is actually a separate mount and
  is writable (so it can't accidentally fill the SD card if the drive
  failed to mount at boot).
* Stops the docker-compose stack so the source volumes are quiescent.
* For each of the four legacy named volumes (`pgdata`, `pos-data`,
  `card-images`, `pokeapi-data`), runs `cp -a` inside an alpine
  container that has both the named volume and the bind-mount target
  attached. Preserves attrs, xattrs, and uid/gid.
* Refuses to overwrite a target that already has files unless you
  pass `--force-overwrite`.
* Leaves the old named volumes intact — `docker volume rm` them
  yourself once you've run a full show on the new layout and verified
  everything works.

The ollama-data volume is intentionally NOT migrated: keeping it on
SD means the assistant container still comes up and answers "I'm
offline" if the USB drive is unplugged, instead of failing to start.

## Live OCR pipeline (tablet snapshot → text)

When the operator points the tablet camera at a card a customer is
buying or selling, three small modules in `pi-setup/workers/` run
in series to turn that snapshot into recognised text:

| Module                      | Role                                       |
| --------------------------- | ------------------------------------------ |
| `image_preprocess.py`       | Crops to the card, rotates landscape →     |
|                             | portrait, normalises contrast (CLAHE on    |
|                             | LAB L-channel). Lossless PNG output.       |
| `live_ocr.py`               | Synchronous PaddleOCR pass with KR-first   |
|                             | auto-detect and early-exit on confident    |
|                             | matches. Reuses the same per-language      |
|                             | model cache as the batch `ocr_indexer`.    |
| `ocr_pipeline.py`           | One-call wrapper: preprocess → live_ocr,   |
|                             | with automatic fallback to the original    |
|                             | image if preprocessing fails (e.g. cv2     |
|                             | not installed).                            |

The tablet API hands off to `OcrPipeline.ocr_snapshot(bytes)` and
gets back a single dict with `full_text`, `lang_hint`, per-line
confidences, the crop bbox in original-image coordinates, and a
`source` field (`"preprocessed"` or `"original"`) so the UI knows
whether a retry without preprocessing might help.

All three modules lazy-import their heavy dependencies (cv2 +
numpy for the preprocessor, paddleocr for the OCR engine), so a
fresh Pi without those packages still imports them cleanly and
returns `{"ok": False, "error": "NO_LIB"}` instead of crashing the
worker process. Tests inject fake `cv2_module` / `np_module` /
`paddle_factory` arguments so the suite runs in milliseconds
without the ~250 MB install.

