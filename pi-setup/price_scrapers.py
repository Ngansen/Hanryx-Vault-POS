"""
Asian + EU price scrapers
=========================

Best-effort marketplace price lookups for the languages the POS supports
beyond eBay (which is handled separately in server.py).

Sources
-------
  - naver       → search.shopping.naver.com   (Korean general marketplace)
  - tcgkorea    → tcgkorea.com                (Korean Pokémon-card store)
  - snkrdunk    → snkrdunk.com/en             (Japanese card+sneakers
                                               marketplace, popular for cards)
  - cardmarket  → www.cardmarket.com          (EU TCG marketplace — Pokémon,
                                               Magic, Lorcana, One Piece)

Every scraper returns a UNIFORM list of dicts:
    {
        "title":     "<listing title>",
        "price":     <float in source currency>,
        "currency":  "KRW" | "JPY" | "EUR" | "USD",
        "url":       "https://...",
        "image":     "https://..." | "",
        "source":    "naver" | "tcgkorea" | "snkrdunk" | "cardmarket",
    }

…and never raises — failures return [].  Network is wrapped with a short
timeout and a real-browser User-Agent because every one of these sites
silently blocks default `python-requests` clients.

This file is intentionally dependency-light: only `requests` + `bs4` are
required, and both are already in the POS image.
"""
from __future__ import annotations

import logging
import re
from typing import Callable
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

try:
    from scrape_cache import cached  # Redis-backed cache + drift detector
except Exception:  # pragma: no cover — bare-script execution fallback
    def cached(_source, **_):  # type: ignore[no-redef]
        def deco(fn): return fn
        return deco

log = logging.getLogger("price_scrapers")

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_HDR = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.7,ja;q=0.6",
}
_TIMEOUT = 12


def _money(s: str) -> float | None:
    """Pull the first numeric value out of a price string."""
    if not s:
        return None
    s = s.replace(",", "").replace("\u00a0", " ").strip()
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    return float(m.group(1)) if m else None


def _safe_get(url: str, *, params: dict | None = None) -> str | None:
    try:
        r = requests.get(url, headers=_HDR, params=params, timeout=_TIMEOUT)
        if r.status_code != 200:
            log.info("[scrape] %s → HTTP %s", url, r.status_code)
            return None
        return r.text
    except requests.RequestException as exc:
        log.info("[scrape] %s → %s", url, exc)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Naver Shopping (Korean) — search.shopping.naver.com
# ──────────────────────────────────────────────────────────────────────────────
@cached("naver")
def naver_shopping(query: str, *, limit: int = 20) -> list[dict]:
    if not query:
        return []
    # Naver's HTML page is the most reliable surface — their REST API needs
    # an Open API client_id/secret.  The HTML works without auth.
    url = f"https://search.shopping.naver.com/search/all?query={quote_plus(query)}"
    html = _safe_get(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    # Naver's class names are randomised — we look for any <a> linking to a
    # product page that has a sibling <span> containing a price.
    for a in soup.select("a[href*='/catalog/'], a[href*='shopping.naver.com']"):
        if len(out) >= limit:
            break
        title = (a.get("title") or a.get_text(" ", strip=True) or "").strip()
        if len(title) < 4:
            continue
        # Find the closest price-looking sibling
        scope = a.find_parent() or a
        price_el = scope.find(string=re.compile(r"\d{1,3}(?:,\d{3})+\s*원"))
        price = _money(price_el) if price_el else None
        if not price:
            continue
        href = a.get("href", "")
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = "https://search.shopping.naver.com" + href
        img = ""
        img_el = scope.find("img")
        if img_el:
            img = img_el.get("src") or img_el.get("data-src") or ""
        out.append({
            "title": title, "price": price, "currency": "KRW",
            "url": href, "image": img, "source": "naver",
        })
    return out


# ──────────────────────────────────────────────────────────────────────────────
# TCGkorea — tcgkorea.com   (Korean Pokémon-card focused store)
# ──────────────────────────────────────────────────────────────────────────────
@cached("tcgkorea")
def tcgkorea(query: str, *, limit: int = 20) -> list[dict]:
    if not query:
        return []
    # tcgkorea uses Cafe24's standard search route
    url = f"https://www.tcgkorea.com/product/search.html?keyword={quote_plus(query)}"
    html = _safe_get(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    # Cafe24 product cards live in <li> inside .xans-product-listmain* etc.
    for li in soup.select("li[id^='anchorBoxId_'], .xans-product-normalpackage li"):
        if len(out) >= limit:
            break
        a = li.select_one("a[href*='/product/']")
        if not a:
            continue
        title = (a.get("title") or a.get_text(" ", strip=True) or "").strip()
        # Cafe24 wraps the price in <strong> with the currency suffix '원' or '$'
        price_el = li.find(string=re.compile(r"\d[\d,]*\s*원"))
        price = _money(price_el) if price_el else None
        if not (title and price):
            continue
        href = a["href"]
        if href.startswith("/"):
            href = "https://www.tcgkorea.com" + href
        img_el = li.find("img")
        img = ""
        if img_el:
            img = img_el.get("src") or img_el.get("data-original") or ""
            if img.startswith("//"):
                img = "https:" + img
        out.append({
            "title": title, "price": price, "currency": "KRW",
            "url": href, "image": img, "source": "tcgkorea",
        })
    return out


# ──────────────────────────────────────────────────────────────────────────────
# SnkrDunk — snkrdunk.com (Japanese marketplace, large Pokémon-card section)
# ──────────────────────────────────────────────────────────────────────────────
@cached("snkrdunk")
def snkrdunk(query: str, *, limit: int = 20) -> list[dict]:
    if not query:
        return []
    # English product search route
    url = f"https://snkrdunk.com/en/search?keyword={quote_plus(query)}"
    html = _safe_get(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    for a in soup.select("a[href*='/products/']"):
        if len(out) >= limit:
            break
        title = (a.get("aria-label") or a.get_text(" ", strip=True) or "").strip()
        if len(title) < 3:
            continue
        # SnkrDunk shows price as "¥12,800" or "$83"
        price_el = a.find(string=re.compile(r"[¥$€]\s*\d"))
        if not price_el:
            scope = a.find_parent() or a
            price_el = scope.find(string=re.compile(r"[¥$€]\s*\d"))
        if not price_el:
            continue
        currency = "JPY" if "¥" in price_el else ("USD" if "$" in price_el
                                                   else "EUR")
        price = _money(price_el)
        if not price:
            continue
        href = a["href"]
        if href.startswith("/"):
            href = "https://snkrdunk.com" + href
        img_el = a.find("img")
        img = ""
        if img_el:
            img = img_el.get("src") or img_el.get("data-src") or ""
        out.append({
            "title": title, "price": price, "currency": currency,
            "url": href, "image": img, "source": "snkrdunk",
        })
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Cardmarket — cardmarket.com (EU TCG marketplace, all four supported games)
# ──────────────────────────────────────────────────────────────────────────────
@cached("cardmarket")
def cardmarket(query: str, *, game: str = "Pokemon", limit: int = 20) -> list[dict]:
    """
    game ∈ Pokemon | Magic | Lorcana | OnePiece | DragonBallSuperCG …
    Cardmarket maps each game to a slug under /en/<Game>/Cards
    """
    if not query:
        return []
    slug = {
        "pokemon":  "Pokemon",
        "mtg":      "Magic",
        "magic":    "Magic",
        "lorcana":  "Lorcana",
        "onepiece": "OnePiece",
        "one-piece":"OnePiece",
        "dbs":      "DragonBallSuperCG",
    }.get(game.lower().strip(), game)
    url = (f"https://www.cardmarket.com/en/{slug}/Products/Search"
           f"?searchString={quote_plus(query)}")
    html = _safe_get(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    # Cardmarket lists products as table rows or link cards depending on filters
    rows = soup.select("div.row.no-gutters[role='row']") or soup.select("table tr")
    for row in rows:
        if len(out) >= limit:
            break
        a = row.select_one("a[href*='/Products/']")
        if not a:
            continue
        title = (a.get_text(" ", strip=True) or "").strip()
        if not title:
            continue
        # Price formatted like "12,34 €" or "1.234,56 €"
        price_el = row.find(string=re.compile(r"\d[\d.,]*\s*€"))
        price_raw = (price_el or "").strip()
        # Cardmarket uses "1.234,56" — convert to "1234.56"
        m = re.search(r"(\d[\d.]*),(\d{1,2})\s*€", price_raw)
        if m:
            price = float(m.group(1).replace(".", "") + "." + m.group(2))
        else:
            price = _money(price_raw)
        if not price:
            continue
        href = a["href"]
        if href.startswith("/"):
            href = "https://www.cardmarket.com" + href
        img_el = row.find("img")
        img = ""
        if img_el:
            img = img_el.get("src") or img_el.get("data-echo") or ""
        out.append({
            "title": title, "price": price, "currency": "EUR",
            "url": href, "image": img, "source": "cardmarket",
            "game": slug,
        })
    return out


# ──────────────────────────────────────────────────────────────────────────────
SCRAPERS: dict[str, Callable[..., list[dict]]] = {
    "naver":      naver_shopping,
    "tcgkorea":   tcgkorea,
    "snkrdunk":   snkrdunk,
    "cardmarket": cardmarket,
}


def search_all(query: str, *, sources: list[str] | None = None,
               limit_per_source: int = 10, game: str = "Pokemon") -> dict:
    """
    Fan-out search across every scraper sequentially (HTTP-bound, fine on
    a single thread for low-volume POS use).  Returns:
        {"results": {source: [..]}, "errors": {source: "msg"}, "query": ...}
    """
    out: dict = {"results": {}, "errors": {}, "query": query}
    for name in (sources or list(SCRAPERS)):
        fn = SCRAPERS.get(name)
        if not fn:
            out["errors"][name] = "unknown source"
            continue
        try:
            if name == "cardmarket":
                out["results"][name] = fn(query, limit=limit_per_source, game=game)
            else:
                out["results"][name] = fn(query, limit=limit_per_source)
        except Exception as exc:  # belt-and-suspenders; scrapers swallow most
            log.warning("[scrape:%s] %s", name, exc)
            out["errors"][name] = str(exc)
            out["results"][name] = []
    return out
