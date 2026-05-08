"""
Asian + EU price scrapers
=========================

Best-effort marketplace price lookups for the languages the POS supports
beyond eBay (which is handled separately in server.py).

Sources
-------
  - naver       → openapi.naver.com           (Korean general marketplace,
                                               via Naver Open Search API)
  - tcgkorea    → tcgkorea.com                (Korean Pokémon-card store)
  - snkrdunk    → snkrdunk.com                (Japanese marketplace, large
                                               Pokémon-card section)
  - cardmarket  → api.tcgdex.net              (EU TCG marketplace EUR pricing
                                               via tcgdex.net, Pokemon-only)

  Reachable but not populated (kept in code, not in active SCRAPERS):
  - tcgplayer   → api.tcgdex.net              (US TCG marketplace USD pricing
                                               via tcgdex.net — wrapper exists
                                               in `tcgdex_api.py` but tcgdex's
                                               tcgplayer field is empirically
                                               null on essentially every card,
                                               so it's not in SCRAPERS to
                                               avoid wasted detail fetches and
                                               drift-canary noise. Re-enable
                                               by re-adding to the dict if
                                               tcgdex ever backfills.)

Every scraper returns a UNIFORM list of dicts:
    {
        "title":     "<listing title>",
        "price":     <float in source currency>,
        "currency":  "KRW" | "JPY" | "EUR",
        "url":       "https://...",
        "image":     "https://..." | "",
        "source":    "naver" | "tcgkorea" | "snkrdunk" | "cardmarket",
    }

…and never raises — failures return [].  Network is wrapped with a short
timeout and a real-browser User-Agent because every one of these sites
silently blocks default `python-requests` clients.

Anti-bot strategy
-----------------
We maintain a per-domain `requests.Session()` so cookies (Cloudflare
clearance, CSRF tokens, locale prefs) persist across requests. Before the
first search hit on a domain we issue a ONE-TIME warmup GET against the
homepage so the session collects whatever cookies the site issues to a
fresh visitor. Headers mimic a recent Chrome on macOS including
client-hint headers (`sec-ch-ua*`) and fetch-mode hints (`sec-fetch-*`)
that anti-bot WAFs use to distinguish browsers from scripts.

Playwright fallback
-------------------
For sites that block requests-based access entirely (naver returns HTTP
418, cardmarket Cloudflare-403, snkrdunk client-rendered SPA), each
scraper FIRST tries `playwright_scraper.fetch_html()` — a real chromium
browser kept warm in a background thread. If Playwright is disabled
(`ENABLE_PLAYWRIGHT_SCRAPER=0`), not installed, or fails to launch, we
silently fall back to the requests-based `_safe_get` path. The parsing
below is identical for both fetch backends — Playwright only solves the
HTML-acquisition problem; selectors and currency handling stay the same.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Callable
from urllib.parse import quote_plus, urlsplit

import requests
from bs4 import BeautifulSoup

try:
    from scrape_cache import cached  # Redis-backed cache + drift detector
except Exception:  # pragma: no cover — bare-script execution fallback
    def cached(_source, **_):  # type: ignore[no-redef]
        def deco(fn): return fn
        return deco

try:
    import playwright_scraper as _pw  # real-browser fallback for blocked sites
except Exception:  # pragma: no cover — module not present at all
    _pw = None  # type: ignore[assignment]

log = logging.getLogger("price_scrapers")

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Headers that mimic Chrome 124 on macOS — including client hints
# (sec-ch-ua*) and fetch metadata (sec-fetch-*) that anti-bot WAFs check.
_HDR = {
    "User-Agent": _UA,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8,"
               "application/signed-exchange;v=b3;q=0.7"),
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8,ja;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "max-age=0",
    "sec-ch-ua": '"Chromium";v="124", "Not-A.Brand";v="99", "Google Chrome";v="124"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}
_TIMEOUT = 12

# Per-domain Session + warmup tracker. Sessions persist Cloudflare and
# locale cookies across requests; warmup ensures we hit the homepage
# before any search endpoint so cookies are seeded properly.
_sessions: dict[str, requests.Session] = {}
_warmed:   set[str] = set()
_session_lock = threading.Lock()


def _domain_for(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _session_for(url: str) -> requests.Session:
    domain = _domain_for(url)
    with _session_lock:
        sess = _sessions.get(domain)
        if sess is None:
            sess = requests.Session()
            sess.headers.update(_HDR)
            _sessions[domain] = sess
        # First time we touch this domain: warmup with a homepage GET so
        # the session collects Cloudflare clearance / locale / consent
        # cookies before any search endpoint is hit.
        if domain not in _warmed:
            _warmed.add(domain)
            try:
                sess.get(domain + "/", timeout=_TIMEOUT, allow_redirects=True)
            except requests.RequestException as exc:
                log.info("[scrape:warmup] %s → %s", domain, exc)
    return sess


def _money(s: str) -> float | None:
    """Pull the first numeric value out of a price string."""
    if not s:
        return None
    s = s.replace(",", "").replace("\u00a0", " ").strip()
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    return float(m.group(1)) if m else None


def _fetch_html_smart(url: str, *, params: dict | None = None,
                      referer: str | None = None,
                      locale: str = "en-US",
                      wait_selector: str | None = None,
                      stealth: bool = False) -> str | None:
    """Try Playwright first, fall back to the requests-based path.

    Playwright is the only reliable fetcher for sites that TLS-fingerprint
    (cardmarket via Cloudflare) or load results via XHR/$.ajax (tcgkorea
    Cafe24, snkrdunk SPA). When it's disabled, not installed, or has
    crashed, we silently fall back to the requests path — which today
    returns nothing useful for the four blocked sites but at least won't
    crash on cooperative endpoints.

    Optional `wait_selector` and `stealth` are forwarded to the Playwright
    fetch — see `playwright_scraper.fetch_html` for semantics. Both are
    silently ignored on the requests fallback.
    """
    # IMPORTANT: do NOT pre-gate on `_pw.is_available()` here — that flag
    # is only set True INSIDE fetch_html() via lazy init, so a pre-check
    # would mean the browser is never started in the first place
    # (chicken-and-egg). Just call fetch_html() unconditionally; it
    # handles disabled/not-installed/launch-failed internally and
    # returns "" cheaply when Playwright isn't usable.
    if _pw is not None:
        # Playwright doesn't take query params separately — bake them into
        # the URL for the browser fetch (the requests path uses `params=`).
        full_url = url
        if params:
            from urllib.parse import urlencode
            sep = "&" if ("?" in url) else "?"
            full_url = f"{url}{sep}{urlencode(params)}"
        # Defensive kwargs forwarding: if the deployed playwright_scraper.py
        # is older than this price_scrapers.py (e.g. mid-deploy state),
        # `wait_selector` / `stealth` won't exist as parameters → TypeError.
        # Retry without them so we don't strand callers on the requests
        # path during a partial deploy.
        try:
            html = _pw.fetch_html(full_url, locale=locale,
                                  wait_selector=wait_selector,
                                  stealth=stealth)
        except TypeError:
            html = _pw.fetch_html(full_url, locale=locale)
        if html:
            return html
        # Empty result: either Playwright is disabled/uninstalled (cheap
        # no-op after first call), or the fetch genuinely failed. Either
        # way, fall through to the requests path so cooperative endpoints
        # still work and tests don't go silent.
    return _safe_get(url, params=params, referer=referer)


def _safe_get(url: str, *, params: dict | None = None,
              referer: str | None = None) -> str | None:
    """GET with per-domain Session, warmup, and Chrome-like headers.

    `referer` should be set to the page the search query would naturally
    navigate from (usually the site's homepage) — anti-bot filters often
    reject requests with no Referer or with a Referer from a different
    origin than the request URL.
    """
    sess = _session_for(url)
    headers: dict[str, str] = {}
    if referer:
        headers["Referer"] = referer
        # Sec-Fetch-Site flips when navigating from same origin
        if _domain_for(referer) == _domain_for(url):
            headers["Sec-Fetch-Site"] = "same-origin"
        else:
            headers["Sec-Fetch-Site"] = "cross-site"
    try:
        r = sess.get(url, headers=headers, params=params, timeout=_TIMEOUT,
                     allow_redirects=True)
        if r.status_code != 200:
            log.info("[scrape] %s → HTTP %s", url, r.status_code)
            return None
        return r.text
    except requests.RequestException as exc:
        log.info("[scrape] %s → %s", url, exc)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Naver Shopping (Korean) — official Open API
# ──────────────────────────────────────────────────────────────────────────────
# Naver's HTML shopping page IP-bans the trade-show egress (the response is
# a styled "쇼핑 서비스 접속이 일시적으로 제한되었습니다" page — see
# replit.md gotchas) and even Playwright + stealth can't defeat it because
# the block is at the network layer. The Open API at openapi.naver.com is
# authenticated and exempt from that classifier — 25,000 free calls/day,
# returns clean JSON, no scraping. Register an app at
#   https://developers.naver.com/apps/#/register
# (tick "검색" / Search; pick WEB; any URL works) and put the keys in
# pi-setup/.env as NAVER_CLIENT_ID + NAVER_CLIENT_SECRET. Without keys
# this falls back to returning [] (with an info log) so the rest of the
# language-pricing response still renders.
_NAVER_CLIENT_ID     = os.environ.get("NAVER_CLIENT_ID", "").strip()
_NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "").strip()
# Naver's API wraps matched query terms in <b>...</b> inside `title`;
# strip those for clean display.
_NAVER_TAG_RE = re.compile(r"</?b>", re.IGNORECASE)


@cached("naver")
def naver_shopping(query: str, *, limit: int = 20) -> list[dict]:
    if not query:
        return []
    if not (_NAVER_CLIENT_ID and _NAVER_CLIENT_SECRET):
        log.info("[naver] NAVER_CLIENT_ID/SECRET not set in env — skipping. "
                 "Register an Open API app at developers.naver.com/apps and "
                 "put both values in pi-setup/.env to enable.")
        return []
    try:
        r = requests.get(
            "https://openapi.naver.com/v1/search/shop.json",
            params={
                "query":   query,
                "display": min(max(limit, 1), 100),  # API max 100/page
                "sort":    "sim",                    # similarity-ranked
            },
            headers={
                "X-Naver-Client-Id":     _NAVER_CLIENT_ID,
                "X-Naver-Client-Secret": _NAVER_CLIENT_SECRET,
                "User-Agent":            _UA,
                "Accept":                "application/json",
            },
            timeout=_TIMEOUT,
        )
        if r.status_code == 401:
            log.warning("[naver] 401 — check NAVER_CLIENT_ID/SECRET values "
                        "and that the app has '검색' (Search) API enabled")
            return []
        if r.status_code == 429:
            log.warning("[naver] 429 — quota exhausted "
                        "(25k req/day per app). Backing off.")
            return []
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as exc:
        log.info("[naver] api error: %s", exc)
        return []
    except ValueError as exc:  # JSON decode
        log.info("[naver] non-JSON response: %s", exc)
        return []
    out: list[dict] = []
    for item in (data.get("items") or [])[:limit]:
        title = _NAVER_TAG_RE.sub("", item.get("title") or "").strip()
        try:
            price = float(item.get("lprice") or 0)
        except (TypeError, ValueError):
            price = 0.0
        if not (title and price > 0):
            continue
        out.append({
            "title":    title,
            "price":    price,
            "currency": "KRW",
            "url":      item.get("link") or "",
            "image":    item.get("image") or "",
            "mall":     item.get("mallName") or "",
            "source":   "naver",
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
    # Cafe24's `/product/search.html` route returns the homepage shell with
    # the keyword echoed into the search bar, then loads results via $.ajax
    # into one of several xans-product-* containers AFTER `domcontentloaded`.
    # Without `wait_selector` the snapshot fires before the AJAX response and
    # we see a "no results" page even when there ARE matches. The selector
    # union covers Cafe24's three common result-container conventions.
    html = _fetch_html_smart(
        url,
        referer="https://www.tcgkorea.com/",
        locale="ko-KR",
        wait_selector=("li[id^='anchorBoxId_'], "
                       ".xans-product-listmain li, "
                       ".xans-product-normalpackage li, "
                       ".ec-base-product .item"),
    )
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    # Cafe24 product cards live in <li> inside .xans-product-listmain* etc.
    for li in soup.select("li[id^='anchorBoxId_'], .xans-product-normalpackage li, "
                          ".xans-product-listmain li, .ec-base-product .item"):
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
_SNKR_LABEL_RE = re.compile(r"^(.+?)\s*-\s*¥\s*([\d,]+)\s*$")


@cached("snkrdunk")
def snkrdunk(query: str, *, limit: int = 20) -> list[dict]:
    """SnkrDunk JP — uses the root /search route, NOT /en/search.

    Empirically (2026-05): /en/search?keyword=... silently redirects to
    the homepage when the keyword doesn't match an English product slug
    (which is true for ~all Pokemon card names). The root /search route
    with `ja-JP` locale handles both katakana (e.g. "メガニウム") and
    English transliterations, returning a server-rendered productGrid.

    Caller responsibility: best results require the Japanese (katakana)
    Pokemon name. The ja-JP→Pokemon name map lives in
    `pokeapi_canonical.py` upstream; this scraper just runs whatever
    keyword it receives. English names return zero matches on most cards.

    Product tile selector: each result is `<a href="/apparels/<id>"
    aria-label="<name> - ¥<price>" class="...productTile">`. The wrapper
    class hash (currently `jAqS3W`) is build-time-generated and rotates
    on every Snkrdunk deploy; href + aria-label format are stable.
    """
    if not query:
        return []
    url = f"https://snkrdunk.com/search?keyword={quote_plus(query)}"
    html = _fetch_html_smart(
        url,
        referer="https://snkrdunk.com/",
        locale="ja-JP",
        # Wait for at least one product tile to attach — snkrdunk's
        # productGrid hydrates from a JSON island in <head>, usually <1s.
        wait_selector="a[href*='/apparels/'][aria-label]",
    )
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    # Snkrdunk silently shows the "おすすめアイテム" (Recommended Items) feed
    # as a fallback whenever a search has zero exact matches OR when the
    # /search route can't resolve the keyword to a product category. The
    # page title flips to "おすすめアイテム | 通販・相場はスニーカーダンク"
    # — distinct from a real result page which echoes the query. Returning
    # this carousel as-is would poison every chip with the same unrelated
    # featured items (we saw the same Pokemon GO Special Set + Fukuoka
    # Pikachu surface for メガニウム / ピカチュウ / Meganium in one run).
    title_el = soup.find("title")
    if title_el and "おすすめアイテム" in title_el.get_text():
        log.info("[snkrdunk] %r → 0 results "
                 "(recommendation carousel returned, ignoring)", query)
        return []
    out: list[dict] = []
    for a in soup.select("a[href*='/apparels/'][aria-label]"):
        if len(out) >= limit:
            break
        label = (a.get("aria-label") or "").strip()
        m = _SNKR_LABEL_RE.match(label)
        if not m:
            # Sold-out tiles have aria-label without "- ¥<price>" suffix;
            # skip them because we have no price.
            continue
        title = m.group(1).strip()
        try:
            price = float(m.group(2).replace(",", ""))
        except ValueError:
            continue
        href = a["href"]
        if href.startswith("/"):
            href = "https://snkrdunk.com" + href
        img_el = a.find("img")
        img = ""
        if img_el:
            img = img_el.get("src") or img_el.get("data-src") or ""
        out.append({
            "title": title, "price": price, "currency": "JPY",
            "url": href, "image": img, "source": "snkrdunk",
        })
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Cardmarket + TCGplayer — both via tcgdex.net public API
# ──────────────────────────────────────────────────────────────────────────────
# We previously scraped cardmarket.com directly. By 2026-05 it sits behind
# Cloudflare bot-fight from our trade-show egress IP — header-spoofed
# requests get HTTP 403, playwright + stealth + 15s IUAM wait still gets
# the challenge page, and each query cost 30s of timeouts before [].
#
# tcgdex.dev publishes a free public REST API that EMBEDS Cardmarket EUR
# pricing AND TCGplayer USD pricing in every Pokemon card response — the
# same data we were trying to scrape, pre-aggregated, no auth, ~1s per
# query. As a bonus we get TCGplayer (USD) which we couldn't reach before.
# See `tcgdex_api.py` for the full rationale and JSON shape mapping.
#
# tcgplayer is imported but NOT registered in SCRAPERS — see module docstring.
# The wrapper works correctly (parses tcgdex's `pricing.tcgplayer` shape), but
# tcgdex's actual tcgplayer coverage is empirically null on essentially every
# card (probed 30+ Pikachu hits + 10+ Charizard hits + modern English-set
# spot checks like swsh3-3 Darkness Ablaze: 0% had tcgplayer populated, 60-90%
# had cardmarket). Registering it would cost ~1s/query for guaranteed [].
from tcgdex_api import cardmarket, tcgplayer  # noqa: E402,F401

# Multilingual species-name lookup. Used by search_all() to translate English
# card-name queries into the native language each marketplace indexes in.
# Import is wrapped so a missing/broken species_names.py degrades to the
# pre-translation behaviour (queries are passed through verbatim) instead of
# 500-ing the entire /card/price endpoint.
try:
    from species_names import translate as _species_translate  # type: ignore
except Exception as _e:  # pragma: no cover
    log.warning("[price_scrapers] species_names unavailable: %s "
                "(snkrdunk/tcgkorea will use raw English queries)", _e)
    def _species_translate(query: str, target_lang: str):  # type: ignore[no-redef]
        return None

# Per-source translation policy: which target language to translate the
# user's query into before handing it to that scraper. Sources not listed
# here receive the original query unchanged.
#   snkrdunk → katakana    (snkrdunk's product titles are Japanese-only;
#                          English transliterations return ~0 hits)
#   tcgkorea → hangul      (Cafe24-based store, search index is Korean-only)
#   naver    → no rewrite  (Naver's Open Search API handles English+Korean
#                          fine, and translating loses brand-name matches
#                          like 'Pokemon Center' that work in either lang)
#   cardmarket → no rewrite (tcgdex.net REST API is English-keyed)
_TRANSLATE_LANG: dict[str, str] = {
    "snkrdunk": "ja_kana",
    "tcgkorea": "ko",
}


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
    a single thread for low-volume POS use).

    Each source receives the language it indexes in: snkrdunk gets the
    katakana form of the species name (e.g. 'Charizard' → 'リザードン'),
    tcgkorea gets hangul ('Charizard' → '리자몽'). Translation is done
    via species_names.translate(); when the query isn't a recognised
    Pokémon species, the original (English) query is passed through.

    Returns:
        {
          "results":    {source: [..]},
          "errors":     {source: "msg"},
          "query":      <original user query>,
          "query_used": {source: <actual string sent to that scraper>}
            ↑ debugging aid: shows whether translation fired per source.
              Same as `query` for sources without a translation policy.
        }
    """
    out: dict = {
        "results":    {},
        "errors":     {},
        "query":      query,
        "query_used": {},
    }
    for name in (sources or list(SCRAPERS)):
        fn = SCRAPERS.get(name)
        if not fn:
            out["errors"][name] = "unknown source"
            continue

        # ── per-source query translation ────────────────────────────────
        target_lang = _TRANSLATE_LANG.get(name)
        if target_lang:
            translated = _species_translate(query, target_lang)
            q_for_source = translated if translated else query
            if translated and translated != query:
                log.info("[scrape:%s] query translated %r → %r (%s)",
                         name, query, translated, target_lang)
        else:
            q_for_source = query
        out["query_used"][name] = q_for_source

        try:
            if name == "cardmarket":
                # cardmarket() accepts game= for backward compat; tcgdex
                # only covers Pokemon, but other games used to be valid
                # against the prior scrape implementation.
                out["results"][name] = fn(q_for_source,
                                          limit=limit_per_source, game=game)
            else:
                out["results"][name] = fn(q_for_source, limit=limit_per_source)
        except Exception as exc:  # belt-and-suspenders; scrapers swallow most
            log.warning("[scrape:%s] %s", name, exc)
            out["errors"][name] = str(exc)
            out["results"][name] = []
    return out
