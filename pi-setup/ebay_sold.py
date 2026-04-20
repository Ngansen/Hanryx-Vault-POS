"""
eBay sold-listings scraper (no API key required).

eBay's "Completed/Sold" search page is the gold standard for trading-card
pricing — it's actual transaction prices, not asking prices. The Browse API
needs OAuth + an app key; the public search page does not, and it returns
the same sold-comps data buyers/sellers actually use to set prices.

URL pattern (public, paginated):
    https://www.ebay.com/sch/i.html
        ?_nkw=<query>           ← URL-encoded card query
        &LH_Sold=1              ← only sold listings
        &LH_Complete=1          ← include completed (sold AND ended)
        &_ipg=120               ← items per page (max 240, 120 is safe)
        &_sop=13                ← sort by ended-most-recent
        &_pgn=<n>               ← page number (1-indexed)

Returns the same uniform listing shape as price_scrapers.py:
    { title, price, currency, url, image, source: "ebay_sold", sold_at }

Never raises — failures return [] so callers can fan-out safely.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

try:
    from scrape_cache import cached
except Exception:  # pragma: no cover
    def cached(_source, **_):
        def deco(fn): return fn
        return deco

log = logging.getLogger("ebay_sold")

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_HDR = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_TIMEOUT = 15

_RE_PRICE = re.compile(r"([\$£€¥₩])\s*([\d,]+(?:\.\d+)?)")
_CUR_BY_SYMBOL = {"$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY", "₩": "KRW"}


def _parse_price(text: str) -> tuple[float, str] | None:
    if not text:
        return None
    # eBay sometimes shows ranges like "$3.50 to $5.00" — take the lower bound
    m = _RE_PRICE.search(text)
    if not m:
        return None
    try:
        return (float(m.group(2).replace(",", "")),
                _CUR_BY_SYMBOL.get(m.group(1), "USD"))
    except ValueError:
        return None


def _parse_date(text: str) -> int | None:
    """Parse 'Sold  Apr 12, 2026' → unix-ms. Returns None on failure."""
    if not text:
        return None
    m = re.search(r"Sold\s+([A-Za-z]{3,9})\s+(\d{1,2}),\s+(\d{4})", text)
    if not m:
        return None
    try:
        dt = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}",
                               "%b %d %Y").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        try:
            dt = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}",
                                   "%B %d %Y").replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            return None


@cached("ebay_sold")
def ebay_sold(query: str, *, limit: int = 60, pages: int = 1) -> list[dict]:
    """Scrape up to `limit` eBay sold listings across `pages` result pages."""
    if not query:
        return []
    out: list[dict] = []
    for page in range(1, max(1, pages) + 1):
        url = ("https://www.ebay.com/sch/i.html"
               f"?_nkw={quote_plus(query)}"
               f"&LH_Sold=1&LH_Complete=1&_ipg=120&_sop=13&_pgn={page}")
        try:
            r = requests.get(url, headers=_HDR, timeout=_TIMEOUT)
            if r.status_code != 200:
                log.info("[ebay_sold] %s → HTTP %s", url, r.status_code)
                break
        except requests.RequestException as exc:
            log.info("[ebay_sold] %s → %s", url, exc)
            break

        soup = BeautifulSoup(r.text, "lxml")
        items = soup.select("li.s-item")
        if not items:
            break  # eBay returned an empty / blocked page

        page_added = 0
        for li in items:
            if len(out) >= limit:
                break
            # eBay's first <li> is a hidden template — skip it
            tit_el = li.select_one(".s-item__title")
            link_el = li.select_one("a.s-item__link")
            price_el = li.select_one(".s-item__price")
            if not (tit_el and link_el and price_el):
                continue
            title = tit_el.get_text(" ", strip=True)
            if title.lower() in ("shop on ebay", ""):
                continue

            parsed = _parse_price(price_el.get_text(" ", strip=True))
            if not parsed:
                continue
            price, currency = parsed

            # Skip outliers eBay surfaces from "shop other categories"
            if price <= 0 or price > 100_000:
                continue

            # Sold-date is in a small <span> near the price
            sold_el = li.select_one(".s-item__caption--signal, .POSITIVE, "
                                    ".s-item__title--tag, .s-item__caption")
            sold_at = _parse_date(sold_el.get_text(" ", strip=True)
                                  if sold_el else "")

            img_el = li.select_one(".s-item__image-img, img")
            img = ""
            if img_el:
                img = (img_el.get("src") or img_el.get("data-src")
                       or img_el.get("data-defer-load") or "")

            out.append({
                "title":    title,
                "price":    price,
                "currency": currency,
                "url":      link_el.get("href", "").split("?")[0],
                "image":    img,
                "source":   "ebay_sold",
                "sold_at":  sold_at,
            })
            page_added += 1

        log.info("[ebay_sold] %r page %d → %d added (total %d)",
                 query, page, page_added, len(out))
        if page_added == 0 or len(out) >= limit:
            break

    return out[:limit]
