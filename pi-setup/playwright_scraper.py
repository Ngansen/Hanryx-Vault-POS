"""
Playwright-backed HTML fetcher for sites that block requests-based scrapers.
============================================================================

Why this exists
---------------
By 2026, every major TCG marketplace we scrape has moved behind one of:

  * naver.com       → returns HTTP 418 to non-browser TLS fingerprints
  * cardmarket.com  → Cloudflare bot-fight mode, HTTP 403
  * snkrdunk.com    → entirely client-rendered SPA (HTML shell has no prices)
  * tcgkorea.com    → mostly server-rendered but increasingly JS-hydrated

Header spoofing alone (Chrome client-hints + warmup + per-domain Sessions —
see `price_scrapers.py`) gets us past the EASIEST WAFs but cannot defeat
TLS fingerprinting (JA3/JA4) or JavaScript challenges. A real browser is
the only reliable answer.

Architecture
------------
ONE chromium process for the whole POS lifetime, kept warm in a dedicated
asyncio loop running in a background thread. Each domain gets ONE persistent
`BrowserContext` that survives between calls — Cloudflare clearance cookies,
locale prefs, and consent banners are paid for ONCE per domain rather than
once per query. Each fetch creates a short-lived `Page` inside that context.

We block heavy resources (images / fonts / CSS / media) at the routing
layer because we only ever need the resulting HTML — JavaScript is left
on so SPAs hydrate and Cloudflare's challenge can complete.

Lifecycle
---------
* Module import does NOTHING expensive — `playwright` itself is imported
  lazily on first `fetch_html()` call. Importing this module does not require
  playwright to be installed (`is_available()` returns False instead).
* The browser is launched on first use and reused thereafter.
* On any catastrophic failure (browser crash, init error, missing
  chromium binary), `is_available()` flips to False and all callers fall
  back to `requests`-based fetches with no further attempts.

Trade-show kill switch
----------------------
Set `ENABLE_PLAYWRIGHT_SCRAPER=0` in the environment to disable Playwright
entirely — useful when running the POS on a battery-constrained Pi where
the ~150 MB chromium RAM cost is unacceptable. With Playwright disabled,
all callers fall back to the requests-based scrapers (which today return
empty results for the four blocked sites, but at least won't crash).

Public API
----------
    is_available() -> bool
    fetch_html(url, *, locale="en-US", ua=..., timeout=30.0) -> str
    shutdown() -> None      # idempotent; called from server.py atexit
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger("playwright_scraper")

# ── Configuration ────────────────────────────────────────────────────────────
_ENABLED = os.environ.get("ENABLE_PLAYWRIGHT_SCRAPER", "1").strip() not in ("0", "", "false", "False")
_NAV_TIMEOUT_MS = int(os.environ.get("PLAYWRIGHT_NAV_TIMEOUT_MS", "12000"))
_SETTLE_MS      = int(os.environ.get("PLAYWRIGHT_SETTLE_MS", "1500"))
_INIT_TIMEOUT_S = float(os.environ.get("PLAYWRIGHT_INIT_TIMEOUT_S", "30"))
# Extra time we'll wait when Cloudflare's "Just a moment..." IUAM challenge
# is detected on the first content snapshot. The challenge JS typically
# auto-solves within 5-10s if our chromium fingerprint passes (i.e. stealth
# is applied AND the egress IP isn't already flagged). Bailing at the
# normal 1.5s settle (which is correct for everything else) means we never
# give CF a chance to clear; 15s is the smallest budget that consistently
# lets it pass on a clean residential/Tailscale egress.
_CLOUDFLARE_WAIT_MS = int(os.environ.get("PLAYWRIGHT_CLOUDFLARE_WAIT_MS", "15000"))

# Default UA — recent Chrome on macOS. Per-call overrides allowed.
_DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Resource types we DROP at the network layer — we only need the final HTML.
# Keeping JS enabled is non-negotiable: Cloudflare challenges and SPAs both
# require JS execution to render anything useful.
_BLOCKED_RESOURCES = {"image", "font", "media", "stylesheet"}

# ── Module state (guarded by _init_lock) ─────────────────────────────────────
_init_lock      = threading.Lock()
_init_attempted = False
_available      = False
_loop: asyncio.AbstractEventLoop | None = None
_thread:  threading.Thread | None = None
_browser: Any = None       # playwright.async_api.Browser
_pw:      Any = None       # playwright.async_api.Playwright (for shutdown)
_contexts: dict[tuple[str, str], Any] = {}    # (domain, locale) -> BrowserContext
_contexts_lock_async: asyncio.Lock | None = None  # created inside the loop


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────
def is_available() -> bool:
    """True iff Playwright is enabled, installed, and the browser is alive."""
    return _ENABLED and _available


# Substrings in exception messages that mean "the browser/loop/context is
# DEAD and won't recover" — when we see one of these we flip _available
# False so subsequent calls skip Playwright and go straight to fallback,
# instead of paying the timeout-and-retry cost on every single request.
_FATAL_ERROR_HINTS = (
    "browser has been closed",
    "browser closed unexpectedly",
    "target page, context or browser has been closed",
    "target closed",
    "connection closed",
    "event loop is closed",
    "browser not initialised",
)


def fetch_html(url: str, *, locale: str = "en-US",
               ua: str = _DEFAULT_UA, timeout: float = 30.0,
               wait_selector: str | None = None,
               stealth: bool = False) -> str:
    """Synchronously fetch `url` with chromium and return the rendered HTML.

    Returns "" on any failure (disabled, not installed, browser crashed,
    navigation timeout). Callers should treat "" as "fall back to the
    requests-based path".

    Optional kwargs (added to defeat 2026-era SPA + bot-WAF scrapes):
      * `wait_selector` — CSS selector to wait for AFTER `domcontentloaded`
        but BEFORE `page.content()`. Use this when the site loads results
        via XHR/$.ajax into a known container (e.g. Cafe24 stores). On
        timeout we still return whatever HTML rendered, so a "no results"
        page doesn't crash the call — the parser just finds zero rows.
      * `stealth` — apply `playwright-stealth` overrides to the page to
        defeat Cloudflare's chromium-fingerprint check. Adds ~200ms per
        page; only enable for sites that actually need it (cardmarket).
        Silently no-ops if the `playwright-stealth` package isn't
        installed, so first deploy before the lockfile regen still runs.

    Self-healing: on any fatal browser/loop error this function flips
    `_available` to False so future calls short-circuit to "" without
    paying the per-request timeout (the requests-based fallback in
    price_scrapers.py picks up immediately).
    """
    global _available
    if not _ENABLED:
        return ""
    _ensure_started()
    if not _available or _loop is None:
        return ""
    try:
        coro = _fetch_html_async(url, locale=locale, ua=ua,
                                 wait_selector=wait_selector,
                                 stealth=stealth)
        fut = asyncio.run_coroutine_threadsafe(coro, _loop)
        return fut.result(timeout=timeout) or ""
    except Exception as exc:
        msg = str(exc).lower()
        log.info("[playwright] fetch %s failed: %s", url, exc)
        if any(hint in msg for hint in _FATAL_ERROR_HINTS):
            log.warning("[playwright] browser appears dead — disabling "
                        "Playwright path for the rest of this process "
                        "(error was: %s)", exc)
            _available = False
            # Drop stale context references; they belong to the dead browser.
            _contexts.clear()
        return ""


def shutdown() -> None:
    """Stop the background loop + close chromium. Idempotent. Safe to call
    multiple times — used at process exit so chromium doesn't linger."""
    global _available
    _available = False
    loop = _loop
    if loop is None:
        return
    try:
        fut = asyncio.run_coroutine_threadsafe(_shutdown_async(), loop)
        fut.result(timeout=10)
    except Exception as exc:
        log.debug("[playwright] shutdown error (non-fatal): %s", exc)
    try:
        loop.call_soon_threadsafe(loop.stop)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────────
def _ensure_started() -> None:
    """Lazily start the background loop + launch chromium. Idempotent."""
    global _init_attempted, _available, _loop, _thread, _browser, _pw
    if _init_attempted:
        return
    with _init_lock:
        if _init_attempted:
            return
        _init_attempted = True

        # Try to import playwright lazily — module import works without it.
        try:
            from playwright.async_api import async_playwright  # noqa: F401
        except ImportError as exc:
            log.warning("[playwright] python package not installed (%s) — "
                        "falling back to requests scrapers", exc)
            return

        ready = threading.Event()
        startup_err: list[BaseException] = []

        def _runner() -> None:
            global _loop, _browser, _pw, _available, _contexts_lock_async
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                _loop = loop
                _contexts_lock_async = asyncio.Lock()

                async def _launch() -> None:
                    global _browser, _pw
                    from playwright.async_api import async_playwright
                    _pw = await async_playwright().start()
                    # --no-sandbox + --disable-dev-shm-usage are required
                    # for chromium under Docker on the Pi (no kernel sandbox
                    # available, and /dev/shm is small).
                    _browser = await _pw.chromium.launch(
                        headless=True,
                        args=[
                            "--no-sandbox",
                            "--disable-dev-shm-usage",
                            "--disable-blink-features=AutomationControlled",
                            "--disable-gpu",
                        ],
                    )

                loop.run_until_complete(_launch())
                _available = True
                ready.set()
                loop.run_forever()
            except BaseException as exc:  # pragma: no cover — startup failure
                startup_err.append(exc)
                ready.set()

        _thread = threading.Thread(target=_runner, daemon=True,
                                    name="playwright-loop")
        _thread.start()

        if not ready.wait(timeout=_INIT_TIMEOUT_S):
            log.error("[playwright] init timed out after %ss — disabling",
                      _INIT_TIMEOUT_S)
            return
        if startup_err:
            log.error("[playwright] launch failed: %r — disabling",
                      startup_err[0])
            return
        log.info("[playwright] chromium ready (headless, JS on, "
                 "image/font/css blocked)")


async def _block_heavy(route: Any) -> None:
    """Network route handler: drop images/fonts/css/media, pass everything else."""
    try:
        if route.request.resource_type in _BLOCKED_RESOURCES:
            await route.abort()
        else:
            await route.continue_()
    except Exception:
        # Page may have closed mid-request; nothing to recover.
        pass


async def _get_context(domain: str, locale: str, ua: str) -> Any:
    """Get-or-create a persistent BrowserContext keyed on (domain, locale).

    Reusing the context across calls means cookies (Cloudflare clearance,
    locale, consent banners) survive — we pay the challenge cost ONCE per
    domain rather than once per query. The context lives until process exit
    or `shutdown()`.
    """
    assert _contexts_lock_async is not None
    key = (domain, locale)
    async with _contexts_lock_async:
        ctx = _contexts.get(key)
        if ctx is not None:
            return ctx
        if _browser is None:
            raise RuntimeError("browser not initialised")
        ctx = await _browser.new_context(
            user_agent=ua,
            locale=locale,
            viewport={"width": 1366, "height": 800},
            java_script_enabled=True,
            ignore_https_errors=False,
        )
        await ctx.route("**/*", _block_heavy)
        _contexts[key] = ctx
        return ctx


async def _fetch_html_async(url: str, *, locale: str, ua: str,
                            wait_selector: str | None = None,
                            stealth: bool = False) -> str:
    """Open a fresh page in the per-domain context, navigate, return HTML."""
    if not _available or _browser is None:
        return ""
    parts = urlsplit(url)
    domain = parts.netloc
    ctx = await _get_context(domain, locale, ua)
    page = await ctx.new_page()
    try:
        if stealth:
            # Per-page stealth — we don't bake it into the cached context
            # because most domains don't need it (and it adds setup cost).
            # Soft-fails so a missing `playwright-stealth` install (e.g.
            # first deploy before lockfile regen) doesn't break the fetch;
            # the page just gets the vanilla chromium fingerprint and
            # Cloudflare-fronted sites return their challenge page → ""
            # → fallback to requests, exactly as before.
            try:
                from playwright_stealth import Stealth  # type: ignore
                await Stealth().apply_stealth_async(page)
            except Exception as exc:  # ImportError or API drift
                log.debug("[playwright] stealth unavailable (%s) — "
                          "continuing without it", exc)
        await page.goto(url, timeout=_NAV_TIMEOUT_MS,
                        wait_until="domcontentloaded")
        if wait_selector:
            # XHR/$.ajax-loaded results: wait for the actual results
            # container. Timeout is non-fatal — we still return the
            # current HTML so the parser can confirm "no results".
            try:
                await page.wait_for_selector(wait_selector,
                                             timeout=_NAV_TIMEOUT_MS,
                                             state="attached")
            except Exception as exc:
                log.debug("[playwright] wait_for_selector(%r) on %s "
                          "timed out: %s", wait_selector, domain, exc)
        # Settle: give SPAs a moment to fetch their JSON and hydrate.
        await page.wait_for_timeout(_SETTLE_MS)
        html = await page.content()
        # Cloudflare IUAM ("Just a moment...") challenge detection.
        # CF serves an interstitial that runs a JS challenge in-page;
        # if our fingerprint passes it auto-redirects/replaces content
        # within 5-10s. We were bailing at the normal _SETTLE_MS (1.5s)
        # — never giving CF a chance to clear. When we detect the
        # challenge title in the head AND stealth was requested
        # (callers that don't ask for stealth aren't expecting CF), we
        # wait up to _CLOUDFLARE_WAIT_MS for the challenge to disappear.
        # Both signals MUST match — naive title-only check would
        # mis-trigger on legit pages that mention "Just a moment" in
        # body content; we constrain to the first 2KB which is just <head>.
        if stealth and ("Just a moment" in html[:2048]
                        or "cf-challenge" in html[:4096]
                        or "challenge-platform" in html[:4096]):
            log.info("[playwright] cloudflare IUAM challenge detected on "
                     "%s — waiting up to %dms for it to clear",
                     domain, _CLOUDFLARE_WAIT_MS)
            try:
                # Wait until the document title no longer matches the
                # challenge string. wait_for_function polls every ~100ms,
                # so this returns quickly once CF redirects.
                await page.wait_for_function(
                    "() => !document.title.includes('Just a moment')",
                    timeout=_CLOUDFLARE_WAIT_MS,
                )
                # CF just navigated us; give the destination page its
                # own settle window before snapshotting.
                await page.wait_for_timeout(_SETTLE_MS)
                html = await page.content()
                log.info("[playwright] cloudflare challenge cleared on %s",
                         domain)
            except Exception as exc:
                log.info("[playwright] cloudflare challenge did NOT clear "
                         "on %s within %dms (%s) — returning challenge "
                         "page as-is, caller will see [] and fall back",
                         domain, _CLOUDFLARE_WAIT_MS, exc)
        return html
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def _shutdown_async() -> None:
    """Close all contexts + the browser, gracefully."""
    global _browser, _pw
    for ctx in list(_contexts.values()):
        try:
            await ctx.close()
        except Exception:
            pass
    _contexts.clear()
    if _browser is not None:
        try:
            await _browser.close()
        except Exception:
            pass
        _browser = None
    if _pw is not None:
        try:
            await _pw.stop()
        except Exception:
            pass
        _pw = None
