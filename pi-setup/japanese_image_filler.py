#!/usr/bin/env python3
"""
japanese_image_filler.py — fill missing Japanese card images in
cards_master from three community sources.

Primary source:  https://www.artofpkm.com/sets/<id>     (high-res scans)
Fallback 1:      https://jp.pokellector.com/sets        (broad coverage)
Fallback 2:      https://www.pokeguardian.com/sets/set-lists/japanese-sets

For every cards_master row whose image_url_alt does NOT yet contain a
Japanese-source URL, this script:

  1. Resolves the set on each source (matching by set_id slug or name).
  2. Parses {card_number → image URL} from the source's set page.
  3. Downloads each image to
       /mnt/cards/japanese/<source>/<set_id>/<card_number>.<ext>
  4. Appends a "file://..." URL to cards_master.image_url_alt
     (a JSONB array). Sources are tried in priority order; the first
     source that yields a usable image wins per card. Other sources are
     still tried later if the row remains image-less from JP perspective
     so we collect alternate art.
  5. If cards_master.image_url is empty (no English image either), the
     first JP image is also written there so the POS has *something*
     to display.

Idempotent + resumable:
  - Skips rows whose image_url_alt already references the chosen source's
    cache directory.
  - Skips downloads when the local file already exists.
  - Per-source set-index cached at <cache>/<source>/.set_index.json.
  - Per-source set page cached at
    <cache>/<source>/<set_id>/.cards.json.

Polite scraping: ~1 req/sec via PoliteClient, custom UA, 5xx retries
with exponential backoff, 403/429 fail-fast.

Usage (inside the pos container, /mnt/cards bind-mounted):

    docker compose exec pos python3 japanese_image_filler.py
    docker compose exec pos python3 japanese_image_filler.py --dry-run
    docker compose exec pos python3 japanese_image_filler.py --limit-sets 3
    docker compose exec pos python3 japanese_image_filler.py --set-id sv-base
    docker compose exec pos python3 japanese_image_filler.py --source artofpkm
    docker compose exec pos python3 japanese_image_filler.py --source pokellector
    docker compose exec pos python3 japanese_image_filler.py --source pokeguardian
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

log = logging.getLogger("japanese_image_filler")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

ARTOFPKM_BASE   = "https://www.artofpkm.com"
ARTOFPKM_INDEX  = f"{ARTOFPKM_BASE}/sets"
POKELL_BASE     = "https://jp.pokellector.com"
POKELL_INDEX    = f"{POKELL_BASE}/sets"
PG_BASE         = "https://www.pokeguardian.com"
PG_INDEX        = f"{PG_BASE}/sets/set-lists/japanese-sets"

DEFAULT_CACHE_DIR = Path("/mnt/cards/japanese")
USER_AGENT = (
    "HanryxVault-POS/1.0 (jp-image-filler; "
    "+https://github.com/Ngansen/Hanryx-Vault-POS)"
)
IMAGE_EXTS_PRIORITY = [".webp", ".jpg", ".jpeg", ".png"]


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s


def parse_card_number(s: str) -> str:
    if not s:
        return ""
    m = re.search(r"\b(\d{1,3}[a-zA-Z]?)\b(?:\s*/\s*\d{1,3})?", s)
    if not m:
        return ""
    return m.group(1).lstrip("0") or m.group(1)


def looks_image_url(url: str) -> bool:
    if not url:
        return False
    p = urlparse(url).path.lower()
    return any(p.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp"))


# ─── HTTP client ──────────────────────────────────────────────────────────


class PoliteClient:
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

    def get_text(self, url: str) -> Optional[str]:
        resp = self.get(url)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            log.warning("  HTTP %d for %s", resp.status_code, url)
            return None
        if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding
        return resp.text

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


# ─── Source: artofpkm.com ─────────────────────────────────────────────────


def discover_artofpkm_sets(client: PoliteClient,
                           cache: Path) -> dict[str, str]:
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    log.info("[artofpkm] fetching set index: %s", ARTOFPKM_INDEX)
    text = client.get_text(ARTOFPKM_INDEX)
    if not text:
        return {}
    soup = BeautifulSoup(text, "lxml")

    out: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        path = urlparse(urljoin(ARTOFPKM_BASE, a["href"])).path
        m = re.match(r"^/sets/([0-9]+)/?$", path)
        if not m:
            continue
        full = urljoin(ARTOFPKM_BASE, a["href"])
        out.setdefault(m.group(1), full)
        text_slug = slugify(a.get_text(strip=True))
        if text_slug:
            out.setdefault(text_slug, full)

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, indent=2, sort_keys=True), encoding="utf-8")
    log.info("[artofpkm] → %d set entries cached", len(out))
    return out


# ─── Source: jp.pokellector.com ───────────────────────────────────────────


def discover_pokellector_sets(client: PoliteClient,
                              cache: Path) -> dict[str, str]:
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    log.info("[pokellector] fetching set index: %s", POKELL_INDEX)
    text = client.get_text(POKELL_INDEX)
    if not text:
        return {}
    soup = BeautifulSoup(text, "lxml")

    out: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        path = urlparse(a["href"]).path
        m = re.match(r"^/(?:Expansion|sets)/([0-9]*-?[a-z0-9\-]+)/?$",
                     path, re.I)
        if not m:
            continue
        full = urljoin(POKELL_BASE, a["href"])
        url_slug = re.sub(r"^[0-9]+-", "", m.group(1).lower())
        out.setdefault(url_slug, full)
        text_slug = slugify(a.get_text(strip=True))
        if text_slug:
            out.setdefault(text_slug, full)

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, indent=2, sort_keys=True), encoding="utf-8")
    log.info("[pokellector] → %d set entries cached", len(out))
    return out


# ─── Source: pokeguardian.com ─────────────────────────────────────────────


def discover_pokeguardian_sets(client: PoliteClient,
                               cache: Path) -> dict[str, str]:
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    log.info("[pokeguardian] fetching set index: %s", PG_INDEX)
    text = client.get_text(PG_INDEX)
    if not text:
        return {}
    soup = BeautifulSoup(text, "lxml")

    out: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("mailto:"):
            continue
        path = urlparse(urljoin(PG_INDEX, href)).path
        m = re.match(r"^/sets/set-lists/japanese-sets/([a-z0-9\-/]+)/?$",
                     path, re.I)
        if not m:
            continue
        post_slug = m.group(1).rstrip("/").split("/")[-1].lower()
        if not post_slug or len(post_slug) < 3:
            continue
        full = urljoin(PG_INDEX, href)
        out.setdefault(post_slug, full)
        text_slug = slugify(a.get_text(strip=True))
        if text_slug and text_slug not in out:
            out[text_slug] = full

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, indent=2, sort_keys=True), encoding="utf-8")
    log.info("[pokeguardian] → %d set entries cached", len(out))
    return out


# ─── Generic per-set image parser ─────────────────────────────────────────


def fetch_set_images(client: PoliteClient, set_url: str,
                     set_cache: Path, *, debug: bool = False) -> dict[str, str]:
    """
    {card_number → image URL} from any of the three sites' set pages.

    Strategy: walk every <img> on the page. For each image with a URL
    that ends in .jpg/.png/.webp, find a card number in the same wrapper
    (article/li/figure/div) — either from a per-card link slug
    (/Card/123-name/, /card/<id>) or from a '#NN' token in nearby text.
    First image wins per number. Result is cached.
    """
    if set_cache.exists():
        try:
            return json.loads(set_cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    text = client.get_text(set_url)
    if not text:
        return {}
    soup = BeautifulSoup(text, "lxml")

    out: dict[str, str] = {}
    seen_imgs: set[str] = set()

    for img in soup.find_all("img"):
        src = (img.get("src") or img.get("data-src") or
               img.get("data-lazy-src") or "").strip()
        if not src:
            continue
        if src.startswith("//"):
            src = "https:" + src
        else:
            src = urljoin(set_url, src)
        if not looks_image_url(src):
            continue
        if src in seen_imgs:
            continue
        seen_imgs.add(src)

        # Walk up parents looking for a wrapper that knows the card number.
        num = ""
        wrapper = img
        for _ in range(5):
            wrapper = wrapper.parent
            if wrapper is None:
                break
            a = wrapper.find("a", href=True)
            if a:
                href = a["href"]
                m = re.search(
                    r"/(?:Card|cards|card)/(?:\d+-)?([0-9]+[a-z]?)\b",
                    href, re.I,
                )
                if m:
                    num = m.group(1).lstrip("0") or m.group(1)
                    break
            text_block = wrapper.get_text(" ", strip=True)
            n = ""
            m = re.search(r"#\s*([0-9]+[a-z]?)\b", text_block)
            if m:
                n = m.group(1).lstrip("0") or m.group(1)
            else:
                n = parse_card_number(text_block)
            if n:
                num = n
                break

        if not num:
            # Last-ditch: number embedded in the image filename itself.
            fname = Path(urlparse(src).path).stem
            m = re.search(r"(?:^|[\-_/])([0-9]{1,3}[a-z]?)(?:[\-_]|$)", fname)
            if m:
                num = m.group(1).lstrip("0") or m.group(1)

        if num and num not in out:
            out[num] = src
            if debug and len(out) <= 5:
                log.info("  [debug] #%s → %s", num, src)

    if out:
        set_cache.parent.mkdir(parents=True, exist_ok=True)
        set_cache.write_text(
            json.dumps(out, indent=2, sort_keys=True), encoding="utf-8",
        )
    log.info("  → %d card→image entries parsed", len(out))
    return out


# ─── Set-id matching ──────────────────────────────────────────────────────


def best_set_match(our_set_id: str, our_name: str,
                   index: dict[str, str]) -> Optional[str]:
    if not index:
        return None
    if our_set_id in index:
        return index[our_set_id]
    name_slug = slugify(our_name or "")
    if name_slug and name_slug in index:
        return index[name_slug]
    cleaned = re.sub(
        r"-(trainer-gallery|galarian-gallery|promos?|black-star)$",
        "", our_set_id,
    )
    if cleaned != our_set_id and cleaned in index:
        return index[cleaned]
    for key, url in index.items():
        if our_set_id in key or key in our_set_id:
            return url
        if name_slug and (name_slug in key or key in name_slug):
            return url
    return None


# ─── DB orchestration ─────────────────────────────────────────────────────


def _row_already_has_jp_image(row, source_dir: Path) -> bool:
    """
    image_url_alt is JSONB; psycopg2 DictCursor parses it to a Python list.
    A row 'has' a JP image from this source if any URL in the array
    starts with the source's local cache prefix.
    """
    alt = row.get("image_url_alt") or []
    if isinstance(alt, str):
        try:
            alt = json.loads(alt)
        except Exception:
            alt = []
    prefix = f"file://{source_dir}"
    for u in alt:
        if isinstance(u, str) and u.startswith(prefix):
            return True
    return False


def _merge_alt(existing, new_url: str) -> list[str]:
    """Append new_url to image_url_alt list, deduped, preserving order."""
    if existing is None:
        existing = []
    if isinstance(existing, str):
        try:
            existing = json.loads(existing)
        except Exception:
            existing = []
    if not isinstance(existing, list):
        existing = list(existing)
    out: list[str] = []
    seen: set[str] = set()
    for u in existing:
        if isinstance(u, str) and u not in seen:
            out.append(u)
            seen.add(u)
    if new_url not in seen:
        out.append(new_url)
    return out


def fill_jp_images(db, client: PoliteClient, cache_dir: Path, *,
                   dry_run: bool, limit_sets: Optional[int],
                   filter_set_id: Optional[str], sources: list[str],
                   debug: bool) -> dict:
    stats = {
        "sets_attempted":     0,
        "sets_unmapped":      0,
        "images_downloaded":  0,
        "images_skipped":     0,
        "images_unmatched":   0,
        "rows_alt_updated":   0,
        "rows_main_updated":  0,
        "by_source":          {s: 0 for s in sources},
        "errors":             [],
    }

    indices: dict[str, dict[str, str]] = {}
    if "artofpkm" in sources:
        indices["artofpkm"] = discover_artofpkm_sets(
            client, cache_dir / "artofpkm" / ".set_index.json",
        )
    if "pokellector" in sources:
        indices["pokellector"] = discover_pokellector_sets(
            client, cache_dir / "pokellector" / ".set_index.json",
        )
    if "pokeguardian" in sources:
        indices["pokeguardian"] = discover_pokeguardian_sets(
            client, cache_dir / "pokeguardian" / ".set_index.json",
        )

    # We want to consider every set that has at least one row missing a JP
    # image from any of the configured sources. The cheapest filter is
    # "any cards exist for this set" — per-card filtering happens below.
    rows = db.execute("""
        SELECT set_id, MIN(name_en) AS name_en, COUNT(*) AS card_count
          FROM cards_master
         GROUP BY set_id
         ORDER BY COUNT(*) DESC
    """).fetchall()
    if filter_set_id:
        rows = [r for r in rows if r["set_id"] == filter_set_id]

    log.info("Sets to check for JP images: %d", len(rows))

    for r in rows:
        if limit_sets is not None and stats["sets_attempted"] >= limit_sets:
            break
        set_id = r["set_id"]

        name_row = db.execute(
            "SELECT name_en FROM ref_set_mapping WHERE set_id = ?",
            (set_id,),
        ).fetchone()
        name_en = (name_row and name_row.get("name_en")) or r["name_en"] or set_id

        per_source_url = {
            src: best_set_match(set_id, name_en, indices[src])
            for src in sources
        }
        if not any(per_source_url.values()):
            log.warning("[%s] no source match (name=%s)", set_id, name_en)
            stats["sets_unmapped"] += 1
            continue

        log.info("[%s] %d cards  ap=%s  pl=%s  pg=%s",
                 set_id, r["card_count"],
                 per_source_url.get("artofpkm")     or "—",
                 per_source_url.get("pokellector")  or "—",
                 per_source_url.get("pokeguardian") or "—")
        stats["sets_attempted"] += 1

        per_source_imgs: dict[str, dict[str, str]] = {}
        for src in sources:
            url = per_source_url.get(src)
            if not url:
                continue
            try:
                per_source_imgs[src] = fetch_set_images(
                    client, url, cache_dir / src / set_id / ".cards.json",
                    debug=debug,
                )
            except Exception as e:
                log.error("  [%s] fetch failed: %s", src, e)
                stats["errors"].append(f"{set_id}/{src}: fetch {e}")
                per_source_imgs[src] = {}

        cards = db.execute("""
            SELECT card_number, image_url, image_url_alt
              FROM cards_master
             WHERE set_id = ?
        """, (set_id,)).fetchall()

        for c in cards:
            cn = c["card_number"]
            num_only = parse_card_number(cn) or cn
            matched_any = False

            for src in sources:
                src_dir = cache_dir / src / set_id
                if _row_already_has_jp_image(c, src_dir):
                    matched_any = True  # we already have a JP image here
                    continue
                imgs = per_source_imgs.get(src, {})
                img_url = imgs.get(num_only) or imgs.get(cn)
                if not img_url:
                    continue
                matched_any = True

                ext = ".jpg"
                url_path = urlparse(img_url).path.lower()
                for e in IMAGE_EXTS_PRIORITY:
                    if url_path.endswith(e):
                        ext = e
                        break
                dest = src_dir / f"{num_only}{ext}"
                local_url = f"file://{dest}"

                if dry_run:
                    if (stats["images_downloaded"] +
                            stats["images_skipped"]) <= 20:
                        log.info("  [dry-run] %s/%s [%s] → %s",
                                 set_id, cn, src, img_url)
                    stats["by_source"][src] = stats["by_source"].get(src, 0) + 1
                    continue

                try:
                    if dest.exists():
                        stats["images_skipped"] += 1
                    else:
                        if not client.download(img_url, dest):
                            stats["errors"].append(
                                f"{set_id}/{cn}/{src}: download failed")
                            continue
                        stats["images_downloaded"] += 1

                    new_alt = _merge_alt(c.get("image_url_alt"), local_url)
                    db.execute(
                        "UPDATE cards_master "
                        "   SET image_url_alt = ?::jsonb, last_built = ? "
                        " WHERE set_id = ? AND card_number = ?",
                        (json.dumps(new_alt, ensure_ascii=False),
                         int(time.time() * 1000), set_id, cn),
                    )
                    stats["rows_alt_updated"] += 1

                    # If main image_url is empty, promote this JP image so
                    # the POS has something to render.
                    if not (c.get("image_url") or "").strip():
                        db.execute(
                            "UPDATE cards_master "
                            "   SET image_url = ?, last_built = ? "
                            " WHERE set_id = ? AND card_number = ? "
                            "   AND (image_url = '' OR image_url IS NULL)",
                            (local_url, int(time.time() * 1000),
                             set_id, cn),
                        )
                        stats["rows_main_updated"] += 1

                    stats["by_source"][src] = stats["by_source"].get(src, 0) + 1
                except Exception as e:
                    stats["errors"].append(f"{set_id}/{cn}/{src}: {e}")
                    try:
                        db.rollback()
                    except Exception:
                        pass

            if not matched_any:
                stats["images_unmatched"] += 1

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
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit-sets", type=int, default=None)
    ap.add_argument("--set-id", default=None)
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--source",
                    choices=["auto", "artofpkm", "pokellector", "pokeguardian"],
                    default="auto",
                    help="Which source(s) to consult. "
                         "'auto' = artofpkm → pokellector → pokeguardian chain.")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    if args.debug:
        log.setLevel(logging.DEBUG)

    sources = (
        ["artofpkm", "pokellector", "pokeguardian"] if args.source == "auto"
        else [args.source]
    )

    from server import _direct_db  # type: ignore[import-not-found]

    db = _direct_db()
    client = PoliteClient(sleep_s=args.sleep)

    log.info("Cache dir: %s  (sources=%s, dry_run=%s, sleep=%.1fs)",
             args.cache_dir, sources, args.dry_run, args.sleep)
    t0 = time.time()
    stats = fill_jp_images(
        db, client, args.cache_dir,
        dry_run=args.dry_run,
        limit_sets=args.limit_sets,
        filter_set_id=args.set_id,
        sources=sources,
        debug=args.debug,
    )
    dt = time.time() - t0
    db.close()

    log.info("─" * 60)
    log.info("DONE in %.1fs", dt)
    for k, v in stats.items():
        if k == "errors":
            log.info("  %-22s %d", k, len(v))
            for e in v[:10]:
                log.info("      %s", e)
            if len(v) > 10:
                log.info("      ... and %d more", len(v) - 10)
        elif k == "by_source":
            log.info("  %-22s %s", k, dict(v))
        else:
            log.info("  %-22s %s", k, v)


if __name__ == "__main__":
    main()
