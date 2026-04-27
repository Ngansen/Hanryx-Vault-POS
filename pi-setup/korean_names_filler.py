#!/usr/bin/env python3
"""
korean_names_filler.py — fill missing Korean card names in cards_master by
scraping community catalogues.

Primary source:  https://www.poisonpie.com/toys/korean/index.html
Fallback 1:      http://www.koreanpokemoncards.com/home.html
Fallback 2:      https://krystalkollectz.com/blogs/cardlists

Walks every cards_master row whose name_kr is empty, finds the matching set
on the primary source, parses {card_number → name_kr}, and writes the
Korean name back into cards_master. For any card the primary couldn't
resolve, the script then queries the fallback sources in order.

Idempotent + resumable:
- Skips rows that already have a non-empty name_kr.
- Caches each source's set-index at <cache>/<source>/.set_index.json.
- Caches each set's parsed {card_number → name_kr} at
  <cache>/<source>/<set_id>/.cards.json.

Polite scraping:
- ~1 request/sec by default (--sleep override).
- Custom User-Agent identifying the project.
- Retries on 5xx + transport errors with exponential backoff.
- 403/429 aborts the script — that's the site asking us to stop.

Both sites are old static HTML with idiosyncratic markup. The parsers try
multiple selector strategies (table rows, definition lists, "<num> Korean
EnglishName" line patterns) and log what succeeded so you can audit. If a
specific set's selector misses, run with `--limit-sets 1 --set-id <id>
--debug` to see the parser output and tune.

Usage (inside the pos container):

    docker compose exec pos python3 korean_names_filler.py
    docker compose exec pos python3 korean_names_filler.py --dry-run
    docker compose exec pos python3 korean_names_filler.py --limit-sets 3
    docker compose exec pos python3 korean_names_filler.py --set-id xy-evolutions
    docker compose exec pos python3 korean_names_filler.py --source poisonpie       # primary only
    docker compose exec pos python3 korean_names_filler.py --source kpc             # fallback 1 only
    docker compose exec pos python3 korean_names_filler.py --source krystalkollectz # fallback 2 only
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

log = logging.getLogger("korean_names_filler")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

POISONPIE_INDEX = "https://www.poisonpie.com/toys/korean/index.html"
KPC_INDEX       = "http://www.koreanpokemoncards.com/home.html"
KK_INDEX        = "https://krystalkollectz.com/blogs/cardlists"
KK_MAX_PAGES    = 10  # Shopify blog index pagination cap (safety)
DEFAULT_CACHE_DIR = Path("/mnt/cards/korean_names")
USER_AGENT = (
    "HanryxVault-POS/1.0 (kr-name-filler; "
    "+https://github.com/Ngansen/Hanryx-Vault-POS)"
)

# Hangul Unicode block — used to detect that a parsed token is actually
# Korean text and not a leftover English caption / number / punctuation.
_HANGUL_RX = re.compile(r"[\uac00-\ud7a3]")


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s


def looks_korean(s: str) -> bool:
    return bool(s and _HANGUL_RX.search(s))


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

    def get_text(self, url: str) -> Optional[str]:
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            self._wait()
            try:
                resp = self.s.get(url, timeout=20)
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
            if resp.status_code == 404:
                return None
            if resp.status_code >= 500:
                log.warning("  HTTP %d for %s, retrying", resp.status_code, url)
                time.sleep(2 ** attempt)
                continue
            # Older static sites often serve EUC-KR / cp949 — let bs4 figure
            # out the right decoder via apparent_encoding.
            if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding
            return resp.text
        log.warning("  Giving up on %s after %d attempts (%s)",
                    url, self.max_retries, last_exc)
        return None


# ─── Source: poisonpie.com ────────────────────────────────────────────────


def discover_poisonpie_sets(client: PoliteClient, cache: Path) -> dict[str, str]:
    """{slug → set page URL}. Cached."""
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    log.info("[poisonpie] fetching set index: %s", POISONPIE_INDEX)
    text = client.get_text(POISONPIE_INDEX)
    if not text:
        return {}
    soup = BeautifulSoup(text, "lxml")

    out: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("mailto:"):
            continue
        # Skip anything that doesn't look like an HTML page (images, etc).
        if not re.search(r"\.html?(?:$|[?#])", href, re.I):
            continue
        text_slug = slugify(a.get_text(strip=True))
        if not text_slug or len(text_slug) < 3:
            continue
        full = urljoin(POISONPIE_INDEX, href)
        out.setdefault(text_slug, full)

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, indent=2, sort_keys=True), encoding="utf-8")
    log.info("[poisonpie] → %d set entries cached", len(out))
    return out


def parse_card_number(s: str) -> str:
    """Pull '001' / '12' / '12a' out of '#012/100', '12 / 100', etc."""
    if not s:
        return ""
    m = re.search(r"\b(\d{1,3}[a-zA-Z]?)\b(?:\s*/\s*\d{1,3})?", s)
    if not m:
        return ""
    return m.group(1).lstrip("0") or m.group(1)


def fetch_set_cards_generic(client: PoliteClient, set_url: str,
                            set_cache: Path, *, debug: bool = False) -> dict[str, str]:
    """
    {card_number → name_kr} from any old-static HTML set page. Tries multiple
    parser strategies in priority order; the first one that yields ≥3 entries
    with Hangul wins. Tuned conservatively because we'd rather skip a set
    than poison the DB with junk Korean strings.
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

    candidates: list[dict[str, str]] = []

    # Strategy A: classic table with rows like [#, English, Korean] (any order).
    for tbl in soup.find_all("table"):
        out: dict[str, str] = {}
        for tr in tbl.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            num = ""
            kr = ""
            for cell in cells:
                if not num:
                    n = parse_card_number(cell)
                    if n:
                        num = n
                if not kr and looks_korean(cell):
                    kr = cell
            if num and kr:
                out.setdefault(num, kr)
        if len(out) >= 3:
            candidates.append(out)
            if debug:
                log.info("  [debug] table strategy yielded %d entries", len(out))

    # Strategy B: definition lists.
    for dl in soup.find_all("dl"):
        out = {}
        terms = dl.find_all("dt")
        defs  = dl.find_all("dd")
        for dt, dd in zip(terms, defs):
            n = parse_card_number(dt.get_text(" ", strip=True))
            kr_text = dd.get_text(" ", strip=True)
            if n and looks_korean(kr_text):
                out.setdefault(n, kr_text.split("\n")[0].strip())
        if len(out) >= 3:
            candidates.append(out)

    # Strategy C: line-based scan. Walk text content; for any line that
    # starts with a 1-3 digit number followed by Korean characters, capture.
    body_text = soup.get_text("\n")
    out = {}
    for line in body_text.splitlines():
        line = line.strip()
        if not line or not looks_korean(line):
            continue
        n = parse_card_number(line)
        if not n:
            continue
        # Drop the leading number token from the captured Korean text.
        kr = re.sub(r"^[\s#0-9/\-:.,]+", "", line).strip()
        # If the line still contains an English Pokémon name first, take the
        # Korean cluster after the last Latin run.
        m = re.search(r"([\uac00-\ud7a3][\uac00-\ud7a3\s·,]*)", kr)
        if m:
            kr = m.group(1).strip()
        if kr and looks_korean(kr):
            out.setdefault(n, kr)
    if len(out) >= 3:
        candidates.append(out)

    if not candidates:
        return {}

    # Pick the strategy with the most entries.
    best = max(candidates, key=len)
    set_cache.parent.mkdir(parents=True, exist_ok=True)
    set_cache.write_text(
        json.dumps(best, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    return best


# ─── Source: koreanpokemoncards.com (fallback) ────────────────────────────


def discover_kpc_sets(client: PoliteClient, cache: Path) -> dict[str, str]:
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    log.info("[kpc] fetching set index: %s", KPC_INDEX)
    text = client.get_text(KPC_INDEX)
    if not text:
        return {}
    soup = BeautifulSoup(text, "lxml")

    out: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("mailto:"):
            continue
        if not re.search(r"\.html?(?:$|[?#])", href, re.I):
            continue
        text_slug = slugify(a.get_text(strip=True))
        if not text_slug or len(text_slug) < 3:
            continue
        full = urljoin(KPC_INDEX, href)
        out.setdefault(text_slug, full)

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, indent=2, sort_keys=True), encoding="utf-8")
    log.info("[kpc] → %d set entries cached", len(out))
    return out


# ─── Source: krystalkollectz.com (Shopify blog) ───────────────────────────


def discover_krystalkollectz_sets(client: PoliteClient,
                                  cache: Path) -> dict[str, str]:
    """
    {slug → blog-post URL}. The site is a Shopify blog at /blogs/cardlists
    where each post is one set's card list. We walk the paginated index
    (/blogs/cardlists?page=N) and collect every link of the form
    /blogs/cardlists/<slug>. Cached.
    """
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    out: dict[str, str] = {}
    for page in range(1, KK_MAX_PAGES + 1):
        url = KK_INDEX if page == 1 else f"{KK_INDEX}?page={page}"
        log.info("[krystalkollectz] fetching index page %d: %s", page, url)
        text = client.get_text(url)
        if not text:
            break
        soup = BeautifulSoup(text, "lxml")

        added_this_page = 0
        for a in soup.find_all("a", href=True):
            path = urlparse(a["href"]).path
            m = re.match(r"^/blogs/cardlists/([a-z0-9\-]+)/?$", path)
            if not m:
                continue
            post_slug = m.group(1)
            full = urljoin(KK_INDEX, a["href"])
            if post_slug not in out:
                out[post_slug] = full
                added_this_page += 1
            text_slug = slugify(a.get_text(strip=True))
            if text_slug and text_slug not in out:
                out[text_slug] = full

        if added_this_page == 0:
            # Reached end of pagination (or empty page).
            break

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, indent=2, sort_keys=True), encoding="utf-8")
    log.info("[krystalkollectz] → %d set entries cached", len(out))
    return out


# ─── Generic set-id matching ──────────────────────────────────────────────


def best_set_match(our_set_id: str, our_name: str,
                   index: dict[str, str]) -> Optional[str]:
    if not index:
        return None
    if our_set_id in index:
        return index[our_set_id]
    name_slug = slugify(our_name or "")
    if name_slug and name_slug in index:
        return index[name_slug]
    # Strip common decorations and try again.
    cleaned = re.sub(
        r"-(trainer-gallery|galarian-gallery|promos?|black-star)$",
        "", our_set_id,
    )
    if cleaned != our_set_id and cleaned in index:
        return index[cleaned]
    # Substring match (e.g. our 'sword-shield' vs their 'sword-shield-base').
    for key, url in index.items():
        if our_set_id in key or key in our_set_id:
            return url
        if name_slug and (name_slug in key or key in name_slug):
            return url
    return None


# ─── DB orchestration ─────────────────────────────────────────────────────


def fill_kr_names(db, client: PoliteClient, cache_dir: Path, *,
                  dry_run: bool, limit_sets: Optional[int],
                  filter_set_id: Optional[str], sources: list[str],
                  debug: bool) -> dict:
    stats = {
        "sets_attempted":  0,
        "sets_unmapped":   0,
        "names_filled":    0,
        "names_unmatched": 0,
        "rows_updated":    0,
        "by_source":       {s: 0 for s in sources},
        "errors":          [],
    }

    # Build set indices for each enabled source.
    indices: dict[str, dict[str, str]] = {}
    if "poisonpie" in sources:
        indices["poisonpie"] = discover_poisonpie_sets(
            client, cache_dir / "poisonpie" / ".set_index.json",
        )
    if "kpc" in sources:
        indices["kpc"] = discover_kpc_sets(
            client, cache_dir / "kpc" / ".set_index.json",
        )
    if "krystalkollectz" in sources:
        indices["krystalkollectz"] = discover_krystalkollectz_sets(
            client, cache_dir / "krystalkollectz" / ".set_index.json",
        )

    rows = db.execute("""
        SELECT set_id, MIN(name_en) AS name_en, COUNT(*) AS missing
          FROM cards_master
         WHERE name_kr = '' OR name_kr IS NULL
         GROUP BY set_id
         ORDER BY COUNT(*) DESC
    """).fetchall()
    if filter_set_id:
        rows = [r for r in rows if r["set_id"] == filter_set_id]

    log.info("Sets with missing Korean names: %d", len(rows))

    for r in rows:
        if limit_sets is not None and stats["sets_attempted"] >= limit_sets:
            break
        set_id = r["set_id"]

        name_row = db.execute(
            "SELECT name_en FROM ref_set_mapping WHERE set_id = ?",
            (set_id,),
        ).fetchone()
        name_en = (name_row and name_row.get("name_en")) or r["name_en"] or set_id

        # Resolve a URL from each source.
        per_source_url = {
            src: best_set_match(set_id, name_en, indices[src])
            for src in sources
        }
        if not any(per_source_url.values()):
            log.warning("[%s] no source match (name=%s)", set_id, name_en)
            stats["sets_unmapped"] += 1
            continue

        log.info("[%s] %d missing  pp=%s  kpc=%s  kk=%s",
                 set_id, r["missing"],
                 per_source_url.get("poisonpie")        or "—",
                 per_source_url.get("kpc")              or "—",
                 per_source_url.get("krystalkollectz")  or "—")
        stats["sets_attempted"] += 1

        # Pull each source's {num → kr} dict (cached after first fetch).
        per_source_cards: dict[str, dict[str, str]] = {}
        for src in sources:
            url = per_source_url.get(src)
            if not url:
                continue
            try:
                per_source_cards[src] = fetch_set_cards_generic(
                    client, url, cache_dir / src / set_id / ".cards.json",
                    debug=debug,
                )
            except Exception as e:
                log.error("  [%s] fetch failed: %s", src, e)
                stats["errors"].append(f"{set_id}/{src}: {e}")
                per_source_cards[src] = {}
            log.info("  [%s] %d cards parsed", src, len(per_source_cards[src]))

        # Per-card resolution: try sources in order; first source with a
        # Hangul-bearing match wins.
        missing = db.execute("""
            SELECT card_number FROM cards_master
             WHERE set_id = ? AND (name_kr = '' OR name_kr IS NULL)
        """, (set_id,)).fetchall()

        for c in missing:
            cn = c["card_number"]
            num_only = parse_card_number(cn) or cn
            kr = ""
            chosen_src = ""
            for src in sources:
                cards = per_source_cards.get(src, {})
                kr_candidate = cards.get(num_only) or cards.get(cn)
                if kr_candidate and looks_korean(kr_candidate):
                    kr = kr_candidate.strip()
                    chosen_src = src
                    break

            if not kr:
                stats["names_unmatched"] += 1
                continue

            stats["names_filled"] += 1
            stats["by_source"][chosen_src] = stats["by_source"].get(chosen_src, 0) + 1

            if dry_run:
                if stats["names_filled"] <= 20:  # don't drown the log
                    log.info("  [dry-run] %s/%s → %s  (%s)", set_id, cn, kr, chosen_src)
                continue

            try:
                db.execute(
                    "UPDATE cards_master SET name_kr = ?, last_built = ? "
                    "WHERE set_id = ? AND card_number = ? "
                    "  AND (name_kr = '' OR name_kr IS NULL)",
                    (kr, int(time.time() * 1000), set_id, cn),
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
                    help=f"Where to cache parsed indices (default: {DEFAULT_CACHE_DIR})")
    ap.add_argument("--dry-run", action="store_true",
                    help="Resolve names but don't write to the DB.")
    ap.add_argument("--limit-sets", type=int, default=None)
    ap.add_argument("--set-id", default=None)
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--source",
                    choices=["auto", "poisonpie", "kpc", "krystalkollectz"],
                    default="auto",
                    help="Which source(s) to consult. "
                         "'auto' = poisonpie → kpc → krystalkollectz fallback chain.")
    ap.add_argument("--debug", action="store_true",
                    help="Verbose parser logging (for selector tuning).")
    args = ap.parse_args()

    if args.debug:
        log.setLevel(logging.DEBUG)

    sources = (
        ["poisonpie", "kpc", "krystalkollectz"] if args.source == "auto"
        else [args.source]
    )

    # Use the standalone connector (filler_db) instead of `from server import …`
    # so an unhealthy /mnt/cards never blocks DB-only fill jobs at import time.
    from filler_db import _direct_db  # type: ignore[import-not-found]

    db = _direct_db()
    client = PoliteClient(sleep_s=args.sleep)

    log.info("Cache dir: %s  (sources=%s, dry_run=%s, sleep=%.1fs)",
             args.cache_dir, sources, args.dry_run, args.sleep)
    t0 = time.time()
    stats = fill_kr_names(
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
            log.info("  %-20s %d", k, len(v))
            for e in v[:10]:
                log.info("      %s", e)
            if len(v) > 10:
                log.info("      ... and %d more", len(v) - 10)
        elif k == "by_source":
            log.info("  %-20s %s", k, dict(v))
        else:
            log.info("  %-20s %s", k, v)


if __name__ == "__main__":
    main()
