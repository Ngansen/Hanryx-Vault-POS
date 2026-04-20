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

# ── Lot / bundle / sealed-product exclusions ────────────────────────────────
# When searching for a single-card price we must keep lots, playsets, sealed
# product, and bulk wholesale out of the median — they live on a totally
# different price curve and skew the result high (sealed) or low (bulk).
#
# Two-layer defence:
#   1. Append `-keyword` exclusions to the eBay search URL itself. eBay
#      supports `-term` as NOT in the search box, so most matching listings
#      never come back over the wire. Cheap and dramatic.
#   2. Client-side regex on the title for anything eBay let through. Belt
#      and suspenders — eBay's NOT operator is occasionally inconsistent on
#      compound phrases.
#
# The list is opinionated for trading-card searches; pass `include_lots=True`
# to disable both layers when you actually do want lots (e.g. wholesale
# pricing intelligence).
EXCLUDE_KEYWORDS: tuple[str, ...] = (
    "lot", "lots", "playset", "bundle", "sealed", "complete set",
    "booster box", "booster pack", "etb", "elite trainer box", "case",
    "x2", "x3", "x4", "x5", "x6", "x10", "x20", "x50", "x100",
    "factory sealed", "wholesale", "repack", "mystery box", "graded lot",
    "collection", "binder",
)
_RE_TITLE_LOT = re.compile(
    r"\b("
    r"lots?|playsets?|bundles?|sealed|complete\s*sets?|"
    r"booster\s*box(?:es)?|booster\s*packs?|etb|elite\s*trainer\s*box(?:es)?|"
    r"cases?|x\s*\d{1,3}\b|\b\d{2,3}\s*cards?|"
    r"factory\s*sealed|wholesale|repacks?|mystery\s*box(?:es)?|"
    r"graded\s*lots?|collections?|binders?"
    r")\b",
    re.IGNORECASE,
)

# ── Shipping cost extraction ────────────────────────────────────────────────
# eBay shows shipping as a sibling element ("+$4.99 shipping" / "Free
# shipping" / "Free International Shipping" / "+£3.50 postage"). When the
# shipping is a numeric add-on we subtract it from the headline price so
# the median reflects the *item* cost, not item+shipping. "Free shipping"
# means the seller absorbed it — keep the headline price as-is.
_RE_SHIPPING = re.compile(
    r"([\$£€¥₩])\s*([\d,]+(?:\.\d+)?)\s*(?:shipping|postage|delivery)",
    re.IGNORECASE,
)
_RE_FREE_SHIP = re.compile(
    r"\bfree\s*(?:international\s+)?(?:shipping|postage|delivery)\b",
    re.IGNORECASE,
)


def _build_search_query(query: str, include_lots: bool) -> str:
    """Append `-keyword` exclusions to the search string for eBay's NOT op."""
    if include_lots:
        return query
    return query + " " + " ".join(f"-{kw}" for kw in EXCLUDE_KEYWORDS)


def _parse_shipping(text: str) -> float:
    """Return shipping cost as float, or 0.0 for free / unparseable / missing."""
    if not text:
        return 0.0
    if _RE_FREE_SHIP.search(text):
        return 0.0
    m = _RE_SHIPPING.search(text)
    if not m:
        return 0.0
    try:
        return float(m.group(2).replace(",", ""))
    except ValueError:
        return 0.0


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
def ebay_sold(query: str, *, limit: int = 60, pages: int = 1,
              include_lots: bool = False) -> list[dict]:
    """
    Scrape up to `limit` eBay sold listings across `pages` result pages.

    By default, lots / playsets / sealed product / wholesale lots are
    excluded both server-side (via eBay's `-keyword` NOT operator in the
    search URL) and client-side (regex on the title). Pass
    `include_lots=True` to disable both layers.

    Each returned listing includes:
      - `price`         : the *item* price after shipping subtraction
      - `price_raw`     : the headline price as eBay displayed it
      - `shipping`      : numeric shipping cost subtracted (0 = free / N/A)
      - `currency`, `url`, `image`, `source`, `sold_at`, `title`

    Subtracting shipping matters because eBay's "sold" price field shows
    the buyer's item-only payment, but the `s-item__price` element on the
    search page often shows the same price *plus* a separate "+$X.XX
    shipping" sibling — including that pollutes the median upward.
    """
    if not query:
        return []
    search_q = _build_search_query(query, include_lots)
    out: list[dict] = []
    skipped_lots = 0
    for page in range(1, max(1, pages) + 1):
        url = ("https://www.ebay.com/sch/i.html"
               f"?_nkw={quote_plus(search_q)}"
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

            # Belt-and-suspenders: drop lot/bundle/sealed/wholesale even when
            # eBay's NOT operator let one slip through. Cheap regex check.
            if not include_lots and _RE_TITLE_LOT.search(title):
                skipped_lots += 1
                continue

            parsed = _parse_price(price_el.get_text(" ", strip=True))
            if not parsed:
                continue
            price_raw, currency = parsed

            # Skip outliers eBay surfaces from "shop other categories"
            if price_raw <= 0 or price_raw > 100_000:
                continue

            # Subtract shipping when the listing splits it out separately.
            # "Free shipping" → keep the headline price (seller absorbed it).
            # Numeric shipping in the same currency → subtract, but cap at
            # 90% of the headline so a malformed parse can never zero out
            # the row (e.g. shipping accidentally read as the item price).
            ship_el = li.select_one(
                ".s-item__shipping, .s-item__logisticsCost, "
                ".s-item__dynamic.s-item__logisticsCost"
            )
            shipping = _parse_shipping(
                ship_el.get_text(" ", strip=True) if ship_el else ""
            )
            if shipping > 0 and shipping < price_raw * 0.9:
                price = round(price_raw - shipping, 2)
            else:
                price = price_raw

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
                "title":     title,
                "price":     price,
                "price_raw": price_raw,
                "shipping":  shipping,
                "currency":  currency,
                "url":       link_el.get("href", "").split("?")[0],
                "image":     img,
                "source":    "ebay_sold",
                "sold_at":   sold_at,
            })
            page_added += 1

        log.info("[ebay_sold] %r page %d → %d added (total %d, "
                 "%d lot/bundle skipped)",
                 query, page, page_added, len(out), skipped_lots)
        if page_added == 0 or len(out) >= limit:
            break

    return out[:limit]
