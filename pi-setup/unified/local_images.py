"""
unified/local_images.py — derive local file paths for card images.

Every per-language importer stores `image_url` as a network URL (mostly
raw.githubusercontent.com), which means the kiosk shows blank squares the
moment booth Wi-Fi flakes. The Pi already has the actual image bytes on
the USB drive at /mnt/cards/<fork>/..., we just never looked them up.

This module turns (source_id, network_url) into the local file path on
the USB drive — IF the file actually exists. Used by:
    1. build_cards_master.py — to populate cards_master.image_url_alt with
       {src, url, local} candidates so the consolidator records every
       image we know about per logical card.
    2. server.py /card/image endpoint — to walk those candidates at
       request time, preferring local files over network URLs.

The discovery rules below are intentionally defensive: every per-source
function returns "" if the URL doesn't match the expected pattern OR the
derived file doesn't exist on disk. We never fabricate a path.

USB root is configurable via env var USB_CARDS_ROOT (default /mnt/cards)
so dev workstations can point at a synthetic fixtures dir.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, unquote

USB_ROOT = Path(os.environ.get("USB_CARDS_ROOT", "/mnt/cards"))


# raw.githubusercontent.com/Ngansen/<repo>/<branch>/<rest…>
#   →  <USB_ROOT>/<repo>/<rest…>
_RAW_GH_RX = re.compile(
    r"^https?://raw\.githubusercontent\.com/Ngansen/([^/]+)/[^/]+/(.+)$"
)


def _from_github(url: str) -> str:
    """Translate a raw.githubusercontent.com Ngansen URL into the
    matching path under USB_ROOT. Returns "" if URL doesn't match or
    the file isn't on disk."""
    if not url:
        return ""
    m = _RAW_GH_RX.match(url)
    if not m:
        return ""
    repo, rest = m.group(1), unquote(m.group(2))
    candidate = USB_ROOT / repo / rest
    return str(candidate) if candidate.is_file() else ""


def _kr_local(url: str) -> str:
    """Korean cards: ptcg-kr-db/card_img/<basename>. The official KR API
    serves cardImgURL from pokemoncard.co.kr, but the fork mirrors the
    files under card_img/ keyed by the URL's basename. We try a couple
    of basename variants because the fork's exact layout isn't fully
    documented."""
    if not url:
        return ""
    base = os.path.basename(unquote(urlparse(url).path))
    if not base:
        return ""
    root = USB_ROOT / "ptcg-kr-db" / "card_img"
    # Direct hit at the top of card_img/
    direct = root / base
    if direct.is_file():
        return str(direct)
    # Some forks bucket images by set under card_img/<set>/<basename>.
    # We don't know the set here so we can't do a directed lookup —
    # leave that to the consolidator which has set_id in scope.
    return ""


def _kr_local_with_set(url: str, set_id: Optional[str]) -> str:
    """Same as _kr_local but also tries `card_img/<set_id>/<basename>`
    when the consolidator knows the set."""
    direct = _kr_local(url)
    if direct:
        return direct
    if not url or not set_id:
        return ""
    base = os.path.basename(unquote(urlparse(url).path))
    if not base:
        return ""
    candidate = USB_ROOT / "ptcg-kr-db" / "card_img" / set_id / base
    return str(candidate) if candidate.is_file() else ""


def _from_cdn(url: str) -> str:
    """Resolve any HTTP(S) URL via the generic CDN mirror at
    <USB_ROOT>/cdn/<host>/<path>. This is what makes JP / TCGdex /
    pokemoncard.co.kr / etc. fully offline once `sync_card_mirror.py`
    has run with Phase C."""
    if not url:
        return ""
    try:
        p = urlparse(url)
    except Exception:
        return ""
    host = (p.netloc or "").lower()
    rel = unquote(p.path or "").lstrip("/")
    if not host or not rel:
        return ""
    candidate = USB_ROOT / "cdn" / host / rel
    return str(candidate) if candidate.is_file() else ""


# Map of source_id → resolver. Each resolver takes (url, hints_dict)
# and returns a local path string ("" if not found). Resolvers try the
# source-specific layout first, then fall back to the generic CDN mirror
# so a freshly-downloaded URL becomes available without code changes.
def _kr_resolver(u, h):
    return _kr_local_with_set(u, h.get("set_id")) or _from_cdn(u)

def _gh_or_cdn(u, h):
    return _from_github(u) or _from_cdn(u)

_RESOLVERS = {
    "kr_official":  _kr_resolver,
    "chs_official": _gh_or_cdn,
    "pocket_off":   _gh_or_cdn,
    "pocket_lt":    _gh_or_cdn,
    # JP + TCGdex are CDN-hosted: served by the cdn/ mirror once
    # sync_card_mirror.py Phase C has downloaded them.
    "tcgdex":       lambda u, h: _from_cdn(u),
    "tcg_api":      lambda u, h: _from_cdn(u),
    "jp_pokell":    lambda u, h: _from_cdn(u),
    "jp_pcc":       lambda u, h: _from_cdn(u),
    "eng_xlsx":     lambda u, h: _from_cdn(u),
    "jp_xlsx":      lambda u, h: _from_cdn(u),
}


def local_path_for(source_id: str, image_url: str, **hints) -> str:
    """Return the on-disk path for `image_url` from `source_id`, or ""
    if no mirror exists. `hints` may include set_id (used by KR)."""
    if not image_url:
        return ""
    fn = _RESOLVERS.get(source_id)
    return fn(image_url, hints) if fn else ""


# Source → preferred display language. Drives the /card/image resolver's
# language-match priority (e.g. ?lang=kr should pick a kr_official local
# file before a chs_official local file).
SOURCE_LANG = {
    "kr_official":  "kr",
    "chs_official": "chs",
    "jp_pokell":    "jp",
    "jp_pcc":       "jp",
    "pocket_off":   "jp",
    "pocket_lt":    "jp",
    "eng_xlsx":     "en",
    "tcgdex":       "en",   # TCGdex returns the EN art by default
    "tcg_api":      "en",
}
