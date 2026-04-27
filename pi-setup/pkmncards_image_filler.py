#!/usr/bin/env python3
"""
pkmncards_image_filler.py — fill missing card images in cards_master from
pkmncards.com.

Walks every cards_master row whose image_url is empty, finds the matching
set on pkmncards.com, downloads the per-card image into
/mnt/cards/pkmncards/<set_id>/<card_number>.<ext>, and writes a
file:// URL back into cards_master.image_url.

Idempotent + resumable:
- Skips rows that already have a non-empty image_url.
- Skips downloads when the local file already exists.
- Caches the pkmncards set-index at <cache>/.set_index.json.
- Caches each set's parsed {card_number → image_url} at <cache>/<set>/.cards.json.

Polite scraping:
- ~1 request/sec by default (--sleep override) via PoliteClient.
- Custom User-Agent identifying the project.
- Retries on 5xx + transport errors with exponential backoff.
- On 403/429 the script aborts loudly — that's Cloudflare telling you to
  back off, not something to retry through.

Usage (inside the pos container, /mnt/cards bind-mounted):

    docker compose exec pos python3 pkmncards_image_filler.py
    docker compose exec pos python3 pkmncards_image_filler.py --dry-run
    docker compose exec pos python3 pkmncards_image_filler.py --limit-sets 3
    docker compose exec pos python3 pkmncards_image_filler.py --set-id xy-evolutions
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError as e:
    print(f"Missing dependency: {e}. Both 'requests' and 'beautifulsoup4' should be installed in the pos container.", file=sys.stderr)
    sys.exit(1)

log = logging.getLogger("pkmncards_image_filler")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

PKMN_BASE = "https://pkmncards.com"
SETS_URL = f"{PKMN_BASE}/sets/"
DEFAULT_CACHE_DIR = Path("/mnt/cards/pkmncards")
USER_AGENT = (
    "HanryxVault-POS/1.0 (image-filler; "
    "+https://github.com/Ngansen/Hanryx-Vault-POS)"
)
IMAGE_EXTS_PRIORITY = [".webp", ".jpg", ".jpeg", ".png"]


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s


# ─── HTTP client ──────────────────────────────────────────────────────────


class PoliteClient:
    """requests.Session with rate limiting, retries, and 429/403 fail-fast."""

    def __init__(self, sleep_s: float = 1.0, max_retries: int = 3):
        self.sleep_s = sleep_s
        self.max_retries = max_retries
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT})
        self._last = 0.0

    def _wait(self) -> None:
        delta = time.time() - self._last
        if delta < self.sleep_s:
            time.sleep(self.sleep_s - delta)
        self._last = time.time()

    def get(self, url: str, *, stream: bool = False) -> requests.Response:
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            self._wait()
            try:
                resp = self.s.get(url, timeout=20, stream=stream)
            except requests.RequestException as e:
                last_exc = e
                log.warning("  HTTP attempt %d/%d for %s failed: %s",
                            attempt + 1, self.max_retries, url, e)
                time.sleep(2 ** attempt)
                continue
            if resp.status_code in (429, 403):
                log.error("  Rate-limited / blocked at %s (HTTP %d). Aborting.",
                          url, resp.status_code)
                raise SystemExit(1)
            if resp.status_code >= 500:
                log.warning("  HTTP %d for %s, retrying", resp.status_code, url)
                time.sleep(2 ** attempt)
                continue
            return resp
        raise RuntimeError(f"Failed after {self.max_retries} attempts: {url} ({last_exc})")

    def download(self, url: str, dest: Path) -> bool:
        if dest.exists():
            return True
        dest.parent.mkdir(parents=True, exist_ok=True)
        resp = self.get(url, stream=True)
        if resp.status_code != 200:
            log.warning("  Download HTTP %d for %s", resp.status_code, url)
            return False
        tmp = dest.with_suffix(dest.suffix + ".part")
        with tmp.open("wb") as f:
            for chunk in resp.iter_content(8192):
                if chunk:
                    f.write(chunk)
        tmp.rename(dest)
        return True


# ─── pkmncards-specific scraping ──────────────────────────────────────────


def discover_set_index(client: PoliteClient, cache: Path) -> dict[str, str]:
    """{slug → pkmncards set URL}. Cached to disk for re-runs."""
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("Set-index cache unreadable (%s) — refetching", e)

    log.info("Fetching pkmncards set index from %s ...", SETS_URL)
    resp = client.get(SETS_URL)
    soup = BeautifulSoup(resp.text, "lxml")

    out: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        path = urlparse(a["href"]).path
        m = re.match(r"^/?set/([a-z0-9\-]+)/?$", path)
        if not m:
            continue
        pkmn_slug = m.group(1)
        full = urljoin(PKMN_BASE, a["href"])
        # Index by URL slug AND by link-text slug, for fuzzy matching.
        out.setdefault(pkmn_slug, full)
        text_slug = slugify(a.get_text(strip=True))
        if text_slug:
            out.setdefault(text_slug, full)

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, indent=2, sort_keys=True), encoding="utf-8")
    log.info("  → %d set mappings cached at %s", len(out), cache)
    return out


def best_set_match(our_set_id: str, our_name: str,
                   index: dict[str, str]) -> Optional[str]:
    """Look up by id slug, then by name slug, then by stripping decorations."""
    if our_set_id in index:
        return index[our_set_id]
    name_slug = slugify(our_name or "")
    if name_slug and name_slug in index:
        return index[name_slug]
    # Strip common Notion/POS decorations and retry.
    cleaned = re.sub(
        r"-(trainer-gallery|galarian-gallery|promos?|black-star)$",
        "", our_set_id,
    )
    if cleaned != our_set_id and cleaned in index:
        return index[cleaned]
    # Last-ditch substring scan.
    for key, url in index.items():
        if key == our_set_id or key == name_slug:
            return url
    return None


def fetch_set_cards(client: PoliteClient, set_url: str,
                    set_cache: Path) -> dict[str, str]:
    """
    Parse a pkmncards set page → {card_number: image_url}.

    The site renders cards in a grid where each card is wrapped in an
    <article>. We extract the collector number from the per-card link
    (.../card/<name>-<set>-<num>/) which is the most reliable signal,
    falling back to '#NN' tokens in the article text.

    Result is cached per-set so re-runs are cheap.
    """
    if set_cache.exists():
        try:
            return json.loads(set_cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    paged_url = set_url.rstrip("/") + "/?display=images&sort=number&count=999"
    log.info("  Fetching set page: %s", paged_url)
    resp = client.get(paged_url)
    soup = BeautifulSoup(resp.text, "lxml")

    out: dict[str, str] = {}
    for art in soup.find_all("article"):
        img = art.find("img")
        if not img:
            continue
        src = img.get("src") or img.get("data-src") or ""
        if not src:
            continue
        if src.startswith("//"):
            src = "https:" + src

        num = ""
        link = art.find("a", href=True)
        if link:
            href = link["href"]
            m = re.search(r"/card/[^/]+-([0-9]+[a-z]?)/?(?:$|\?)", href, re.I)
            if m:
                num = m.group(1).lstrip("0") or m.group(1)
        if not num:
            txt = art.get_text(" ", strip=True)
            m = re.search(r"#\s*([0-9]+[a-z]?)\b", txt)
            if m:
                num = m.group(1).lstrip("0") or m.group(1)

        if num:
            out.setdefault(num, src)

    if out:
        set_cache.parent.mkdir(parents=True, exist_ok=True)
        set_cache.write_text(
            json.dumps(out, indent=2, sort_keys=True), encoding="utf-8",
        )
    log.info("  → %d card→image entries parsed", len(out))
    return out


# ─── DB orchestration ─────────────────────────────────────────────────────


def fill_images(db, client: PoliteClient, cache_dir: Path, *,
                dry_run: bool, limit_sets: Optional[int],
                filter_set_id: Optional[str]) -> dict:
    stats = {
        "sets_attempted":    0,
        "sets_unmapped":     0,
        "images_downloaded": 0,
        "images_skipped":    0,
        "rows_updated":      0,
        "errors":            [],
    }

    set_index = discover_set_index(client, cache_dir / ".set_index.json")

    rows = db.execute("""
        SELECT set_id, MIN(name_en) AS name_en, COUNT(*) AS missing
          FROM cards_master
         WHERE image_url = '' OR image_url IS NULL
         GROUP BY set_id
         ORDER BY COUNT(*) DESC
    """).fetchall()

    if filter_set_id:
        rows = [r for r in rows if r["set_id"] == filter_set_id]
        if not rows:
            log.warning("No missing-image rows found for set_id=%s", filter_set_id)

    log.info("Sets with missing images: %d", len(rows))

    for r in rows:
        if limit_sets is not None and stats["sets_attempted"] >= limit_sets:
            break
        set_id = r["set_id"]

        name_row = db.execute(
            "SELECT name_en FROM ref_set_mapping WHERE set_id = ?",
            (set_id,),
        ).fetchone()
        name_en = (name_row and name_row.get("name_en")) or r["name_en"] or set_id

        pkmn_url = best_set_match(set_id, name_en, set_index)
        if not pkmn_url:
            log.warning("[%s] no pkmncards match (name=%s)", set_id, name_en)
            stats["sets_unmapped"] += 1
            continue

        log.info("[%s] %d missing → %s", set_id, r["missing"], pkmn_url)
        stats["sets_attempted"] += 1

        try:
            card_imgs = fetch_set_cards(
                client, pkmn_url, cache_dir / set_id / ".cards.json",
            )
        except Exception as e:
            log.error("  fetch_set_cards failed: %s", e)
            stats["errors"].append(f"{set_id}: fetch {e}")
            continue

        missing = db.execute("""
            SELECT card_number FROM cards_master
             WHERE set_id = ? AND (image_url = '' OR image_url IS NULL)
        """, (set_id,)).fetchall()

        for c in missing:
            cn = c["card_number"]
            num_only = cn.split("/", 1)[0].strip().lstrip("0") or cn
            img_url = card_imgs.get(num_only) or card_imgs.get(cn)
            if not img_url:
                continue

            ext = ".jpg"
            url_path = urlparse(img_url).path.lower()
            for e in IMAGE_EXTS_PRIORITY:
                if url_path.endswith(e):
                    ext = e
                    break
            dest = cache_dir / set_id / f"{num_only}{ext}"

            if dry_run:
                log.info("  [dry-run] %s/%s → %s", set_id, cn, img_url)
                continue

            try:
                if dest.exists():
                    stats["images_skipped"] += 1
                else:
                    if not client.download(img_url, dest):
                        continue
                    stats["images_downloaded"] += 1
                db.execute(
                    "UPDATE cards_master SET image_url = ?, last_built = ? "
                    "WHERE set_id = ? AND card_number = ? "
                    "  AND (image_url = '' OR image_url IS NULL)",
                    (f"file://{dest}", int(time.time() * 1000), set_id, cn),
                )
                stats["rows_updated"] += 1
            except Exception as e:
                stats["errors"].append(f"{set_id}/{cn}: {e}")
                try:
                    db.rollback()
                except Exception:
                    pass

        if not dry_run:
            db.commit()

    return stats


# ─── main ─────────────────────────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR,
                    help=f"Where to store cached images and indices (default: {DEFAULT_CACHE_DIR})")
    ap.add_argument("--dry-run", action="store_true",
                    help="Resolve sets and list intended downloads without writing.")
    ap.add_argument("--limit-sets", type=int, default=None,
                    help="Stop after N sets (debugging).")
    ap.add_argument("--set-id", default=None,
                    help="Restrict to one set_id (e.g. xy-evolutions).")
    ap.add_argument("--sleep", type=float, default=1.0,
                    help="Seconds between HTTP requests (default 1.0).")
    args = ap.parse_args()

    from server import _direct_db  # type: ignore[import-not-found]

    db = _direct_db()
    client = PoliteClient(sleep_s=args.sleep)

    log.info("Cache dir: %s  (dry_run=%s, sleep=%.1fs)",
             args.cache_dir, args.dry_run, args.sleep)
    t0 = time.time()
    stats = fill_images(
        db, client, args.cache_dir,
        dry_run=args.dry_run,
        limit_sets=args.limit_sets,
        filter_set_id=args.set_id,
    )
    dt = time.time() - t0
    db.close()

    log.info("─" * 60)
    log.info("DONE in %.1fs", dt)
    for k, v in stats.items():
        if k == "errors":
            log.info("  %-20s %d", k, len(v))
            for e in v[:10]:
                log.info("      %s", e)
            if len(v) > 10:
                log.info("      ... and %d more", len(v) - 10)
        else:
            log.info("  %-20s %s", k, v)


if __name__ == "__main__":
    main()
