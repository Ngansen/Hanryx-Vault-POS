#!/usr/bin/env python3
"""
japanese_names_filler.py — fill missing Japanese card names in cards_master
by scraping community catalogues.

Primary source:  https://jp.pokellector.com/sets
Fallback 1:      https://www.pokeguardian.com/sets/set-lists/japanese-sets
Fallback 2:      https://www.artofpkm.com/sets

Walks every cards_master row whose name_jp is empty, finds the matching set
on each source, parses {card_number → name_jp}, and writes the Japanese
name back into cards_master. Sources are tried in order; the first source
with a Hiragana/Katakana-bearing match wins.

Idempotent + resumable:
- Skips rows that already have a non-empty name_jp.
- Caches each source's set-index at <cache>/<source>/.set_index.json.
- Caches each set's parsed {card_number → name_jp} at
  <cache>/<source>/<set_id>/.cards.json.

Polite scraping:
- ~1 request/sec by default (--sleep override).
- Custom User-Agent identifying the project.
- Retries on 5xx + transport errors with exponential backoff.
- 403/429 aborts the script — that's the site asking us to stop.

The three sites use very different markup. The parser tries multiple
strategies (table rows, definition lists, image-with-alt tiles, line
scans) and picks whichever yields the most Japanese-looking entries. If
a specific set's selector misses, run with `--limit-sets 1
--set-id <id> --debug` to see what each source returned.

Usage (inside the pos container):

    docker compose exec pos python3 japanese_names_filler.py
    docker compose exec pos python3 japanese_names_filler.py --dry-run
    docker compose exec pos python3 japanese_names_filler.py --limit-sets 3
    docker compose exec pos python3 japanese_names_filler.py --set-id sv-base
    docker compose exec pos python3 japanese_names_filler.py --source pokellector
    docker compose exec pos python3 japanese_names_filler.py --source pokeguardian
    docker compose exec pos python3 japanese_names_filler.py --source artofpkm
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

log = logging.getLogger("japanese_names_filler")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

POKELL_BASE     = "https://jp.pokellector.com"
POKELL_INDEX    = f"{POKELL_BASE}/sets"
PG_BASE         = "https://www.pokeguardian.com"
PG_INDEX        = f"{PG_BASE}/sets/set-lists/japanese-sets"
ARTOFPKM_BASE   = "https://www.artofpkm.com"
ARTOFPKM_INDEX  = f"{ARTOFPKM_BASE}/sets"

DEFAULT_CACHE_DIR = Path("/mnt/cards/japanese_names")
USER_AGENT = (
    "HanryxVault-POS/1.0 (jp-name-filler; "
    "+https://github.com/Ngansen/Hanryx-Vault-POS)"
)

# Hiragana U+3040–309F + Katakana U+30A0–30FF. Their presence is the
# strongest signal that a string is Japanese rather than generic CJK
# (which could be Chinese or Korean Hanja).
_KANA_RX = re.compile(r"[\u3040-\u30ff]")
# CJK Unified Ideographs — kanji-only JP card names (rare for Pokémon)
# trigger this without the kana check; we treat them as JP only when the
# kana check failed but no other JP-blocking signals exist.
_CJK_RX = re.compile(r"[\u3400-\u9fff]")
# Hangul block — used to *exclude* Korean strings that slipped through
# CJK detection (some pages mix KR and JP rows).
_HANGUL_RX = re.compile(r"[\uac00-\ud7a3]")


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s


def looks_japanese(s: str) -> bool:
    if not s:
        return False
    if _HANGUL_RX.search(s):
        return False  # Korean block: definitely not JP.
    if _KANA_RX.search(s):
        return True
    # Fallback: kanji-only string with no kana. Accept iff it's short
    # enough to plausibly be a card name and contains CJK ideographs.
    return bool(_CJK_RX.search(s)) and len(s) <= 40


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
            # Most JP-card sites serve UTF-8, but force apparent_encoding
            # if the server lazily defaulted to ISO-8859-1.
            if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding
            return resp.text
        log.warning("  Giving up on %s after %d attempts (%s)",
                    url, self.max_retries, last_exc)
        return None


def parse_card_number(s: str) -> str:
    """Pull '001' / '12' / '012a' out of '#012/100', '12 / 100', etc."""
    if not s:
        return ""
    m = re.search(r"\b(\d{1,3}[a-zA-Z]?)\b(?:\s*/\s*\d{1,3})?", s)
    if not m:
        return ""
    return m.group(1).lstrip("0") or m.group(1)


# ─── Source: jp.pokellector.com ───────────────────────────────────────────


def discover_pokellector_sets(client: PoliteClient,
                              cache: Path) -> dict[str, str]:
    """{slug → set page URL}. Cached.

    The /sets index lists every JP set; per-set links look like
    /Expansion/<numeric-id>-<slug> so we index by both.
    """
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
    """{slug → set page URL}. Cached.

    Pokeguardian's Japanese-set index is a single page with anchors to
    nested set pages under /sets/set-lists/japanese-sets/<slug>.
    """
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
        # Take the last path component as the set slug.
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


# ─── Source: artofpkm.com ─────────────────────────────────────────────────


def discover_artofpkm_sets(client: PoliteClient,
                           cache: Path) -> dict[str, str]:
    """{slug → set page URL}. Cached.

    Per-set URLs are /sets/<numeric-id> (e.g. /sets/577). The /sets index
    page lists each set with its title; we index by the title slug AND by
    the bare numeric id (so callers can pass either).
    """
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
        numeric_id = m.group(1)
        full = urljoin(ARTOFPKM_BASE, a["href"])
        out.setdefault(numeric_id, full)
        text_slug = slugify(a.get_text(strip=True))
        if text_slug and text_slug not in out:
            out[text_slug] = full

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, indent=2, sort_keys=True), encoding="utf-8")
    log.info("[artofpkm] → %d set entries cached", len(out))
    return out


# ─── Generic per-set parser ───────────────────────────────────────────────


def fetch_set_cards_generic(client: PoliteClient, set_url: str,
                            set_cache: Path, *, debug: bool = False) -> dict[str, str]:
    """
    {card_number → name_jp} from any of the three sites' set pages.
    Tries multiple parser strategies and keeps whichever yields the most
    Japanese-looking entries (≥3 minimum, conservatively).

    Strategies (in priority order):
      A) Image grids — <img alt="...JP...">  next to a #NN number token.
      B) Tables — rows containing a card number and a JP cell.
      C) Definition lists.
      D) Line-based fallback scan.
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

    # Strategy A: tile/grid layouts (pokellector + artofpkm). Each card is
    # a wrapper with an <img alt="JP-name"> and a number token nearby.
    for wrapper_sel in ("article", "li", "div.card", "div.tile",
                        "div.expansion", "figure"):
        try:
            wrappers = soup.select(wrapper_sel)
        except Exception:
            wrappers = []
        out: dict[str, str] = {}
        for w in wrappers:
            img = w.find("img")
            if not img:
                continue
            jp_candidates: list[str] = []
            for attr in ("alt", "title", "aria-label"):
                v = (img.get(attr) or "").strip()
                if v and looks_japanese(v):
                    jp_candidates.append(v)
            text_block = w.get_text(" ", strip=True)
            for chunk in re.split(r"\s{2,}|/", text_block):
                if looks_japanese(chunk) and chunk.strip() not in jp_candidates:
                    jp_candidates.append(chunk.strip())
            if not jp_candidates:
                continue
            num = parse_card_number(text_block)
            if not num:
                # Try the per-card link slug, e.g. /Card/123-pikachu/
                a = w.find("a", href=True)
                if a:
                    m = re.search(r"/(?:Card|cards|card)/(\d+)[\-/]",
                                  a["href"])
                    if m:
                        num = m.group(1).lstrip("0") or m.group(1)
            if not num:
                continue
            jp = jp_candidates[0]
            # Trim noise: trailing English/digits.
            jp = re.split(r"\s+\(", jp)[0].strip()
            if jp:
                out.setdefault(num, jp)
        if len(out) >= 3:
            candidates.append(out)
            if debug:
                log.info("  [debug] selector %r → %d entries", wrapper_sel, len(out))

    # Strategy B: classic tables with rows like [#, Japanese, English] (any order).
    for tbl in soup.find_all("table"):
        out = {}
        for tr in tbl.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            num = ""
            jp = ""
            for cell in cells:
                if not num:
                    n = parse_card_number(cell)
                    if n:
                        num = n
                if not jp and looks_japanese(cell):
                    jp = cell
            if num and jp:
                out.setdefault(num, jp)
        if len(out) >= 3:
            candidates.append(out)
            if debug:
                log.info("  [debug] table strategy → %d entries", len(out))

    # Strategy C: definition lists.
    for dl in soup.find_all("dl"):
        out = {}
        terms = dl.find_all("dt")
        defs  = dl.find_all("dd")
        for dt, dd in zip(terms, defs):
            n = parse_card_number(dt.get_text(" ", strip=True))
            jp_text = dd.get_text(" ", strip=True)
            if n and looks_japanese(jp_text):
                out.setdefault(n, jp_text.split("\n")[0].strip())
        if len(out) >= 3:
            candidates.append(out)

    # Strategy D (REMOVED 2026-05): line-based body scan was the source of
    # systemic offset bugs in the sister korean_names_filler (Mew XY10/29
    # → 피그킹) — same code shape here, same risk class. Page navigation,
    # sidebars, and unrelated set listings frequently contain a stray
    # number AND a JP Pokémon name on the same line, but for a totally
    # different card. Strategy D paired them happily and would poison
    # cards_master.name_jp the same way. Skipping this strategy means
    # some old/oddly-formatted set pages will now yield nothing — that's
    # the correct behaviour: an empty name_jp lets _lookup_native_name
    # fall back to species_names.translate(name_en, "ja_kana") at query
    # time, which is lossy but never produces a WRONG-card lookup.
    # If a future site genuinely needs line-based parsing, gate it on the
    # SAME line containing both the EN Pokémon name AND the JP name AND
    # the number, and verify against species_names before accepting.

    if not candidates:
        return {}

    best = max(candidates, key=len)
    set_cache.parent.mkdir(parents=True, exist_ok=True)
    set_cache.write_text(
        json.dumps(best, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    return best


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


def fill_jp_names(db, client: PoliteClient, cache_dir: Path, *,
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

    indices: dict[str, dict[str, str]] = {}
    if "pokellector" in sources:
        indices["pokellector"] = discover_pokellector_sets(
            client, cache_dir / "pokellector" / ".set_index.json",
        )
    if "pokeguardian" in sources:
        indices["pokeguardian"] = discover_pokeguardian_sets(
            client, cache_dir / "pokeguardian" / ".set_index.json",
        )
    if "artofpkm" in sources:
        indices["artofpkm"] = discover_artofpkm_sets(
            client, cache_dir / "artofpkm" / ".set_index.json",
        )

    rows = db.execute("""
        SELECT set_id, MIN(name_en) AS name_en, COUNT(*) AS missing
          FROM cards_master
         WHERE name_jp = '' OR name_jp IS NULL
         GROUP BY set_id
         ORDER BY COUNT(*) DESC
    """).fetchall()
    if filter_set_id:
        rows = [r for r in rows if r["set_id"] == filter_set_id]

    log.info("Sets with missing Japanese names: %d", len(rows))

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

        log.info("[%s] %d missing  pl=%s  pg=%s  ap=%s",
                 set_id, r["missing"],
                 per_source_url.get("pokellector")  or "—",
                 per_source_url.get("pokeguardian") or "—",
                 per_source_url.get("artofpkm")    or "—")
        stats["sets_attempted"] += 1

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

        missing = db.execute("""
            SELECT card_number FROM cards_master
             WHERE set_id = ? AND (name_jp = '' OR name_jp IS NULL)
        """, (set_id,)).fetchall()

        for c in missing:
            cn = c["card_number"]
            num_only = parse_card_number(cn) or cn
            jp = ""
            chosen_src = ""
            for src in sources:
                cards = per_source_cards.get(src, {})
                jp_candidate = cards.get(num_only) or cards.get(cn)
                if jp_candidate and looks_japanese(jp_candidate):
                    jp = jp_candidate.strip()
                    chosen_src = src
                    break

            if not jp:
                stats["names_unmatched"] += 1
                continue

            stats["names_filled"] += 1
            stats["by_source"][chosen_src] = stats["by_source"].get(chosen_src, 0) + 1

            if dry_run:
                if stats["names_filled"] <= 20:
                    log.info("  [dry-run] %s/%s → %s  (%s)", set_id, cn, jp, chosen_src)
                continue

            try:
                db.execute(
                    "UPDATE cards_master SET name_jp = ?, last_built = ? "
                    "WHERE set_id = ? AND card_number = ? "
                    "  AND (name_jp = '' OR name_jp IS NULL)",
                    (jp, int(time.time() * 1000), set_id, cn),
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
                    choices=["auto", "pokellector", "pokeguardian", "artofpkm"],
                    default="auto",
                    help="Which source(s) to consult. "
                         "'auto' = pokellector → pokeguardian → artofpkm chain.")
    ap.add_argument("--debug", action="store_true",
                    help="Verbose parser logging (for selector tuning).")
    args = ap.parse_args()

    if args.debug:
        log.setLevel(logging.DEBUG)

    sources = (
        ["pokellector", "pokeguardian", "artofpkm"] if args.source == "auto"
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
    stats = fill_jp_names(
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
