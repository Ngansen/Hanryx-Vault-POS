# Offline Card Database on USB — Operator Guide

This is the deploy + operator guide for the USB-resident offline card
database introduced in the multi-language card rollout. The system runs
entirely on the Pi (Tailscale IP `100.125.5.34`, hostname `hanryxvault`,
user `ngansen`); nothing in this stack needs internet to **read** card
data once the initial sync has completed.

## Architecture in one paragraph

Postgres on the SD card holds the live writes — inventory, sales, laybys,
KR/JPN/CHS/JPN_POCKET card pools, price history. A small `sync` container
runs every 6 minutes and projects the read-only card + recent-price tables
into a single SQLite file at `/mnt/cards/pokedex_local.db` on the USB
drive. The POS, the multilingual fuzzy search, and the AI cashier
assistant all read that SQLite mirror, so when WiFi dies at a trade show
the system stays fully usable. Pulling the USB and plugging it into a
backup Pi gives you an identical read-only kiosk in 30 seconds.

## One-time deploy on the Pi

These steps assume the USB drive is already formatted ext4 with label
`hanryxcards` and mounted at `/mnt/cards` per the existing fstab entry
(`UUID=381e39df-5f24-44ad-a925-38d5b60e4256/mnt/cards ext4 defaults,noatime,nofail 0 2`).

### 1. Pull the new code

```bash
cd ~/Hanryx-Vault-POS
git pull
```

### 2. Seed the USB SQLite from any existing pokedex_local.db

```bash
sudo bash pi-setup/scripts/migrate-db-to-usb.sh
```

The script:
- Refuses to write unless `/mnt/cards` is on a different filesystem from
  `/` (i.e. the USB is actually mounted).
- Looks in every known location for an existing `pokedex_local.db` and
  copies the freshest one onto the USB.
- Creates `/mnt/cards/{faiss,logs,backups}/` for the new code paths.
- Backs up any existing USB copy to `/mnt/cards/backups/` before
  overwriting.

If no existing DB is found, that's fine — the sync orchestrator builds
one from Postgres on its first tick.

### 3. Bring up the new + updated services

```bash
cd ~/Hanryx-Vault-POS/pi-setup
docker compose up -d --build pos recognizer sync assistant
```

Both `sync` and `assistant` are new services. `pos` and `recognizer` are
rebuilt to pick up the bind mount and the new env var.

### 4. Pull the Qwen 2.5 model (one-time, ~2 GB)

```bash
bash pi-setup/scripts/setup-ollama.sh
```

This runs inside the assistant container and caches the model in the
`ollama-data` Docker volume — survives container rebuilds.

### 5. Optional: bulk-embed every card image for visual recognition

This is the biggest one-time job — downloads every card image from KR /
JPN / JPN_POCKET / CHS / EN sources (~70k images, several GB) and
generates CLIP embeddings into pgvector. Resumable; safe to interrupt.

```bash
sudo docker exec -it pi-setup-pos-1 \
    python3 /app/scripts/embed-all-cards.py --source all --resume
```

Run this overnight on first deploy. Subsequent days only need the
`--resume` rerun for newly-released sets.

## Verifying the deploy

```bash
# Did the SQLite mirror build?
ls -lh /mnt/cards/pokedex_local.db
sqlite3 /mnt/cards/pokedex_local.db "SELECT name, COUNT(*) FROM sqlite_master \
    WHERE type='table' GROUP BY name;"

# Is the orchestrator running?
docker logs --tail 50 pi-setup-sync-1

# Is /admin/usb-sync/status reporting fresh data?
curl -s http://localhost:8080/admin/usb-sync/status | jq

# Does multilingual search work?
curl -s 'http://localhost:8080/tcg/search-multi?q=Charizard&limit=5' | jq

# Is the AI cashier assistant up?
curl -s http://localhost:8080/ai/health | jq
curl -s -X POST http://localhost:8080/ai/chat \
    -H 'Content-Type: application/json' \
    -d '{"message":"how many lugias do we have"}' | jq
```

## Files added in this rollout

| File | Purpose |
|---|---|
| `pi-setup/cards_db_path.py` | Single source of truth for `/mnt/cards/*` paths |
| `pi-setup/pi_setup_compat.py` | Shared `sqlite_connect()` with WAL + busy_timeout |
| `pi-setup/usb_mirror.py` | Postgres → SQLite mirror logic |
| `pi-setup/sync_orchestrator.py` | Long-lived scheduler (every 6 min / 1 hr / 6 hr / 24 hr) |
| `pi-setup/cards/__init__.py` | Package marker |
| `pi-setup/cards/fuzzy_search.py` | Multilingual rapidfuzz search across all 4 langs |
| `pi-setup/cards/ai_assistant.py` | Flask blueprint for `/ai/chat`, `/ai/health` |
| `pi-setup/scripts/migrate-db-to-usb.sh` | One-time seed of `/mnt/cards/pokedex_local.db` |
| `pi-setup/scripts/setup-ollama.sh` | One-time Ollama + Qwen 2.5 3B pull |
| `pi-setup/scripts/embed-all-cards.py` | Bulk CLIP embedding for visual recognition |

## Files modified in this rollout

| File | Change |
|---|---|
| `pi-setup/tcg_lookup.py` | `_DB_PATH` → resolves via `cards_db_path` |
| `pi-setup/import_tcg_db.py` | Same |
| `pi-setup/sync_tcg_db.py` | Same |
| `pi-setup/server.py` | `_LOCAL_TCG_DB_PATH` and FAISS paths via `cards_db_path`; registers tcg_lookup, fuzzy_search, ai_assistant blueprints; adds `/admin/usb-sync/status` |
| `pi-setup/docker-compose.yml` | `/mnt/cards` bind mount on `pos` + `recognizer`; `HANRYX_LOCAL_DB_DIR` env; new `sync` + `assistant` services |

## Sync schedule reference

| Job | Interval | Needs network | What it does |
|---|---|---|---|
| `mirror` | 6 min | no | Postgres → SQLite mirror |
| `tcg_db` | 1 hr | yes | English card pool from pokemontcg.io |
| `tcgplayer` | 1 hr | yes | TCGplayer raw market prices for top 1000 cards |
| `ebay_sweep` | 6 hr | yes | eBay sold listings sweep for top 200 valuable cards |
| `kr_cards` | 24 hr | yes | Korean card pool refresh |
| `jpn_cards` | 24 hr | yes | Japanese card pool refresh |
| `jpn_pocket` | 24 hr | yes | Japanese TCG Pocket cards |
| `chs_cards` | 24 hr | yes | Chinese card pool refresh |
| `card_hashes` | 24 hr | no | Recognizer container's image-hash index |

Network-dependent jobs are silently skipped (not marked failed) when the
network is down — when connectivity comes back, the next due tick runs
them normally.

## Rollback

If anything goes wrong, the change is reversible without data loss:

```bash
cd ~/Hanryx-Vault-POS
git revert HEAD                      # revert the rollout commit
docker compose up -d --build pos recognizer
docker compose stop sync assistant   # leaves USB data untouched
```

The USB drive's `/mnt/cards/pokedex_local.db` is left in place — it just
stops being read, since the legacy `pi-setup/pokedex_local.db` path
takes over. The Postgres data is never touched by the new code.

## Trade-show "USB-only" mode

To deploy a backup kiosk to a trade show with just a USB stick:

1. Eject `/mnt/cards` from the main Pi: `sudo umount /mnt/cards`
2. Plug into the satellite Pi.
3. On the satellite Pi, mount it at the same path.
4. Run `docker compose up -d pos` — the POS reads the SQLite mirror as
   normal and serves the offline-search page at `/offline-search`.
5. The satellite Pi has no Postgres, so /admin and /sale-history won't
   work — but the cashier-facing search and pricing pages will.
