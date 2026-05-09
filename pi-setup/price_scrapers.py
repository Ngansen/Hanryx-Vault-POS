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
# Bunjang (번개장터) — Korean C2C marketplace, large Pokémon TCG community
# ──────────────────────────────────────────────────────────────────────────────
# Replaces the previous `tcgkorea` scraper. tcgkorea.com (a Cafe24-based
# wholesale shop) returned ~zero hits for individual Pokemon names — its
# catalog is sealed product (booster boxes, ETBs) and Yu-Gi-Oh / MTG, not
# Pokemon singles. Bunjang's mobile API at api.bunjang.co.kr is a clean
# JSON endpoint with no auth, no anti-bot, and active Pokemon listings.
#
# Probed 2026-05: q='리자몽' returned listings starting with '이상해꽃 리자몽
# 띠부씰북 삽니다 !' @ ₩80,000; q='피카츄' returned 'Pokemon Pikachu apron set'
# @ ₩4,900. Real C2C inventory, served instantly.
@cached("bunjang")
def bunjang(query: str, *, limit: int = 20) -> list[dict]:
    """Bunjang KR — JSON API at api.bunjang.co.kr/api/1/find_v2.json.

    Returns Korean C2C marketplace listings. Best results with a hangul
    query (e.g. '리자몽' for Charizard); English queries also work but
    return fewer hits. Translation is handled upstream by
    `species_names.translate(query, 'ko')` via search_all().

    Response shape:
        {"list": [{"pid": "12345", "name": "...", "price": 80000,
                   "product_image": "https://...", "status": "0",
                   "location": "서울 강남구", ...}]}
    Items are sold-out when status != "0" (we keep them — historic
    asking-price is still useful price signal at trade shows).
    """
    if not query:
        return []
    try:
        r = requests.get(
            "https://api.bunjang.co.kr/api/1/find_v2.json",
            params={
                "q":     query,
                "order": "score",       # similarity-ranked (vs date)
                "page":  0,
                "n":     min(max(limit, 1), 100),
            },
            headers={
                "User-Agent": _UA,
                "Accept":     "application/json",
                "Referer":    "https://m.bunjang.co.kr/",
            },
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as exc:
        log.info("[bunjang] api error: %s", exc)
        return []
    except ValueError as exc:
        log.info("[bunjang] non-JSON response: %s", exc)
        return []
    out: list[dict] = []
    for item in (data.get("list") or [])[:limit]:
        title = (item.get("name") or "").strip()
        try:
            price = float(item.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        if not (title and price > 0):
            continue
        pid = item.get("pid") or ""
        out.append({
            "title":    title,
            "price":    price,
            "currency": "KRW",
            "url":      f"https://m.bunjang.co.kr/products/{pid}" if pid else "",
            "image":    item.get("product_image") or "",
            "location": item.get("location") or "",
            "source":   "bunjang",
        })
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Hareruya2 — Japan's largest Pokémon-card specialty online store
# ──────────────────────────────────────────────────────────────────────────────
# Replaces the previous `snkrdunk` scraper. snkrdunk.com's main /search
# index empirically does not contain Pokemon TCG (probed 2026-05: every
# Pokemon query returns the おすすめアイテム recommendation carousel
# regardless of language; /categories/pokemon-card/ 404s). Mercari was
# the obvious next pick but their HTML is a SPA with no SSR data and
# their JSON API requires DPoP token auth.
#
# Hareruya2 (晴れる屋2) is a Pokémon-singles specialist with a Shopify
# storefront — clean SSR HTML, no anti-bot, real keyword filtering.
#
# Search URL: /search?q=<kw> (Shopify standard). Probed 2026-05:
# `?q=リザードン` returns "検索: 「リザードン」の検索結果573件" with 375
# product anchors; empty query returns prods=1 — confirms real filtering.
#
# DO NOT use /?act=Sch&card_name= — that legacy URL parameter is
# silently ignored by the modern Shopify theme and returns the homepage
# (with featured-products carousels) for every query, regardless of
# keyword. We shipped that bug in C10 and got メガゲッコウガex back when
# searching for Charizard. The fix is the standard Shopify /search route.
#
# Each product anchor's text content has a stable structured format:
#     "<NAME> 販売価格: ¥<PRICE> 単価 / あたり 在庫<N>"
# Splitting on the literal "販売価格:" cleanly separates the card name
# (which can contain unicode brackets like 〈114/083〉 and curly braces
# like {水}, all of which we want to keep) from the price and stock text.
_HARERUYA_SPLIT_RE = re.compile(r"\s*販売価格[:：]\s*[¥￥]\s*([\d,]+)")


@cached("hareruya2")
def hareruya2(query: str, *, limit: int = 20) -> list[dict]:
    """Hareruya2 JP — Shopify-based Pokémon singles specialist."""
    if not query:
        return []
    url = f"https://www.hareruya2.com/search?q={quote_plus(query)}"
    # Pure SSR — no playwright needed, regular requests works fine.
    html = _safe_get(url, referer="https://www.hareruya2.com/")
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    seen_hrefs: set[str] = set()
    # Each product card is a <div class="card-wrapper">. The anchor's
    # full text holds "<NAME> 販売価格: ¥<PRICE> 単価 / あたり 在庫<N>".
    for card in soup.select("div.card-wrapper"):
        if len(out) >= limit:
            break
        a = card.select_one("a[href*='/products/'].full-unstyled-link, "
                            "a[href*='/products/']")
        if not a:
            continue
        href = a["href"]
        if href in seen_hrefs:        # search page repeats anchors (img+text)
            continue
        seen_hrefs.add(href)
        text = a.get_text(" ", strip=True)
        m = _HARERUYA_SPLIT_RE.search(text)
        if not m:
            # Out-of-stock / pre-order tiles render without "販売価格:"
            # (they show "入荷待ち" instead). Skip — no price means no chip.
            continue
        title = text[: m.start()].strip()
        if not title:
            continue
        try:
            price = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if price <= 0:
            continue
        if href.startswith("/"):
            href = "https://www.hareruya2.com" + href
        img_el = card.find("img")
        img = ""
        if img_el:
            img = img_el.get("src") or img_el.get("data-src") or ""
            if img.startswith("//"):
                img = "https:" + img
        out.append({
            "title":    title,
            "price":    price,
            "currency": "JPY",
            "url":      href,
            "image":    img,
            "source":   "hareruya2",
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
    "hareruya2": "ja_kana",
    "bunjang":   "ko",
    "naver":     "ko",   # naver Open API: KR text matches site catalog 5-10x better than EN
}


# ──────────────────────────────────────────────────────────────────────────────
SCRAPERS: dict[str, Callable[..., list[dict]]] = {
    "naver":      naver_shopping,   # KR Open API,    via openapi.naver.com
    "bunjang":    bunjang,          # KR C2C JSON,    via api.bunjang.co.kr
    "hareruya2":  hareruya2,        # JP Pokemon-spec, via hareruya2.com SSR
    "cardmarket": cardmarket,       # EU EUR,         via api.tcgdex.net
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
