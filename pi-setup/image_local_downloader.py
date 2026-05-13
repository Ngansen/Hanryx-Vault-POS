#!/usr/bin/env python3
"""image_local_downloader.py — download missing image files referenced by
cards_master.image_url_alt entries.

Walks every image_url_alt entry whose `local` path is set (non-empty)
but the file doesn't exist on disk, fetches `url`, and writes the
bytes to `local`. Idempotent + resumable: skips entries whose local
file already exists and is non-empty.

Does NOT modify the database — `local` is already populated by an
earlier ingest step. This script only materialises the bytes that
the CLIP embedder + image_health worker expect to find on disk.

Per-host throttle: CDN hosts (tcgdex, pokemontcg) are tight (~0.1s);
unknown hosts default to 0.5s. Polite UA. Atomic write via .tmp +
rename so a partial download never poisons the local cache.

Usage (inside the pos container):
    docker compose exec -T pos python3 /app/image_local_downloader.py
    docker compose exec -T pos python3 /app/image_local_downloader.py --dry-run --limit 20
    docker compose exec -T pos python3 /app/image_local_downloader.py --src tcgdex
    docker compose exec -T pos python3 /app/image_local_downloader.py --progress-every 500

Detached overnight:
    nohup docker compose exec -T pos python3 /app/image_local_downloader.py \
        > /mnt/cards/logs/image_dl_$(date +%F_%H%M).log 2>&1 &
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras
import requests

UA = ("HanryxVault-ImageDownloader/1.0 "
      "(+https://github.com/Ngansen/Hanryx-Vault-POS)")

# Per-host minimum gap between requests (seconds)
HOST_SLEEP: dict[str, float] = {
    "assets.tcgdex.net":      0.10,
    "images.pokemontcg.io":   0.15,
}
DEFAULT_SLEEP = 0.50

log = logging.getLogger("image_local_downloader")


def fetch_one(url: str, dest: Path, timeout: int = 30) -> tuple[bool, str]:
    """Download `url` to `dest` atomically. Returns (ok, reason)."""
    try:
        r = requests.get(url, headers={"User-Agent": UA},
                         timeout=timeout, stream=True)
        if r.status_code != 200:
            return False, f"http_{r.status_code}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk:
                    f.write(chunk)
        size = tmp.stat().st_size
        if size < 100:
            tmp.unlink(missing_ok=True)
            return False, f"too_small_{size}"
        tmp.replace(dest)
        return True, "ok"
    except requests.exceptions.Timeout:
        return False, "timeout"
    except requests.exceptions.ConnectionError:
        return False, "conn_err"
    except Exception as e:  # noqa: BLE001
        return False, f"exc_{type(e).__name__}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="cap entries to fetch (after the missing-file scan)")
    ap.add_argument("--src",
                    help="only fetch entries whose `src` tag equals this")
    ap.add_argument("--dry-run", action="store_true",
                    help="enumerate work but don't download")
    ap.add_argument("--progress-every", type=int, default=200,
                    help="log progress every N entries")
    args = ap.parse_args()

    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
    )

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        log.error("DATABASE_URL not set"); return 2

    conn = psycopg2.connect(db_url)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT set_id, card_number, image_url_alt
          FROM cards_master
         WHERE image_url_alt IS NOT NULL
           AND jsonb_array_length(image_url_alt) > 0
    """)
    rows = cur.fetchall()
    log.info("cards_master rows with image_url_alt: %d", len(rows))

    # Flatten + filter to (set, num, src, url, local) tuples that need work.
    # Skip if local file already exists with bytes — that's the resume guard.
    to_fetch: list[tuple[str, str, str, str, str]] = []
    src_seen: dict[str, int] = {}
    for r in rows:
        for entry in r["image_url_alt"] or []:
            if not isinstance(entry, dict):
                continue
            local = (entry.get("local") or "").strip()
            url   = (entry.get("url")   or "").strip()
            src   = (entry.get("src")   or "").strip()
            if not local or not url:
                continue
            src_seen[src] = src_seen.get(src, 0) + 1
            if args.src and src != args.src:
                continue
            try:
                if os.path.exists(local) and os.path.getsize(local) > 0:
                    continue
            except OSError:
                pass
            to_fetch.append((r["set_id"], r["card_number"], src, url, local))

    log.info("entries with non-empty local across all srcs: %s",
             json.dumps(src_seen))
    log.info("entries needing download: %d", len(to_fetch))

    if args.limit:
        to_fetch = to_fetch[: args.limit]
        log.info("limited to first %d entries", len(to_fetch))

    if args.dry_run:
        for t in to_fetch[:10]:
            log.info("DRY: %s/%s [%s] %s -> %s", *t)
        log.info("dry-run; %d entries would be fetched", len(to_fetch))
        return 0

    last_host_fetch: dict[str, float] = {}
    stats = {"ok": 0, "fail": 0,
             "by_src": {}, "fail_reasons": {}}
    t0 = time.time()
    n = len(to_fetch)

    for i, (sid, num, src, url, local) in enumerate(to_fetch, 1):
        host = urlparse(url).netloc
        sleep_s = HOST_SLEEP.get(host, DEFAULT_SLEEP)
        last = last_host_fetch.get(host, 0.0)
        wait = sleep_s - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        last_host_fetch[host] = time.time()

        ok, reason = fetch_one(url, Path(local))
        bs = stats["by_src"].setdefault(src, {"ok": 0, "fail": 0})
        if ok:
            stats["ok"] += 1
            bs["ok"] += 1
        else:
            stats["fail"] += 1
            bs["fail"] += 1
            stats["fail_reasons"][reason] = \
                stats["fail_reasons"].get(reason, 0) + 1

        if i % args.progress_every == 0:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0.0
            eta_min = ((n - i) / rate / 60.0) if rate > 0 else 0.0
            log.info("[%d/%d] ok=%d fail=%d  rate=%.1f/s  eta=%.1fm",
                     i, n, stats["ok"], stats["fail"], rate, eta_min)

    elapsed = time.time() - t0
    log.info("─" * 60)
    log.info("DONE in %.1fs  ok=%d fail=%d", elapsed, stats["ok"], stats["fail"])
    log.info("by_src: %s", json.dumps(stats["by_src"]))
    log.info("fail_reasons: %s", json.dumps(stats["fail_reasons"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
