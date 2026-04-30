"""
zh_sources.py — typed registry of Chinese-language Pokémon TCG sources.

Drives Phase D of sync_card_mirror.py. The decision matrix that produced
this registry lives in pi-setup/docs/ZH_SOURCES.md — read that first if
you need to add a new source or change walking strategy.

Architectural principle: local-mirror-first. Where a Ngansen GitHub fork
already mirrors a region's data (kept fresh by Phase A `git pull`), we
prefer the local clone over web scraping. Web scraping is the FALLBACK,
used only when no local source exists for a language.

Two source kinds:
    LocalMirrorSource  — walks $MIRROR_ROOT/<repo_dir>/<image_path_template>
    RemoteWebSource    — walks <image_url_template> with rate-limiting

Both are frozen dataclasses with no behaviour of their own — Phase D
inspects the kind and dispatches to the appropriate walker. This keeps
the registry trivially serialisable for tests and auditing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Union


class Lang(str, Enum):
    TC = "tc"  # Traditional Chinese (Taiwan, HK, Macau)
    SC = "sc"  # Simplified Chinese  (Mainland China)


class SourceKind(str, Enum):
    LOCAL_MIRROR = "local_mirror"
    REMOTE_WEB = "remote_web"


@dataclass(frozen=True)
class LocalMirrorSource:
    """
    A source backed by a Ngansen fork that Phase A clones into MIRROR_ROOT.

    image_path_template is a forward-slash relative path with `{set_id}`
    and `{card_num}` placeholders, e.g. "img/{set_id}/{card_num}.png".
    Resolved against $MIRROR_ROOT/<repo_dir>/.
    """
    name: str
    lang: Lang
    repo_dir: str
    image_path_template: str
    is_fallback_only: bool = False
    kind: SourceKind = field(default=SourceKind.LOCAL_MIRROR, init=False)

    def local_path(self, mirror_root, set_id: str, card_num: str):
        """Resolve to a Path under mirror_root for this (set, card)."""
        from pathlib import Path
        rel = self.image_path_template.format(set_id=set_id, card_num=card_num)
        return Path(mirror_root) / self.repo_dir / rel


@dataclass(frozen=True)
class RemoteWebSource:
    """
    A source backed by a remote HTTP server that we scrape politely.

    image_url_template uses `{set_id}` and `{card_num}` placeholders, e.g.
    "https://www.ptcg.tw/static/cards/{set_id}/{card_num}.jpg".
    """
    name: str
    lang: Lang
    image_url_template: str
    rate_limit_seconds: float
    user_agent: str = (
        # Browser-style UA — some sites serve different HTML to obvious
        # python-urllib clients. We identify ourselves honestly via the
        # comment, but use a real browser fingerprint as the prefix so
        # dumb bot-detection doesn't trigger on the first request.
        "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) HanryxVault-mirror/1.0"
    )
    extra_headers: tuple[tuple[str, str], ...] = ()
    is_fallback_only: bool = False
    kind: SourceKind = field(default=SourceKind.REMOTE_WEB, init=False)

    def url_for(self, set_id: str, card_num: str) -> str:
        return self.image_url_template.format(set_id=set_id, card_num=card_num)

    @property
    def headers_dict(self) -> dict:
        d = {"User-Agent": self.user_agent}
        for k, v in self.extra_headers:
            d[k] = v
        return d


Source = Union[LocalMirrorSource, RemoteWebSource]


# ── Concrete source registry ──────────────────────────────────────────
#
# Append-only by convention. Removing a source here orphans any data
# previously written under /mnt/cards/zh/<lang>/<source.name>/ — operator
# can clean that up out-of-band but Phase D itself never deletes.

PTCG_CHS_DATASETS = LocalMirrorSource(
    name="PTCG-CHS-Datasets",
    lang=Lang.SC,
    repo_dir="PTCG-CHS-Datasets",
    image_path_template="img/{set_id}/{card_num}.png",
)

PTCG_TW = RemoteWebSource(
    name="ptcg.tw",
    lang=Lang.TC,
    # VERIFY this template each cycle — the Taiwan site has redesigned
    # twice since 2023. The set-index page at /expansions is the source
    # of truth for set-slug values; keep canonical_sets/zh_tc.json in
    # sync with what the index publishes.
    image_url_template="https://www.ptcg.tw/static/cards/{set_id}/{card_num}.jpg",
    rate_limit_seconds=1.0,
)

MYCARDART_TC = RemoteWebSource(
    name="mycardart-tc",
    lang=Lang.TC,
    # VERIFY domain — community site has changed hands. Treat any HTTP
    # response with a redirect to a non-mycardart domain as a sign the
    # site moved and pause walking until the URL is updated here.
    image_url_template="https://mycardart.com/cards/tc/{set_id}/{card_num}.jpg",
    rate_limit_seconds=2.0,
    is_fallback_only=True,  # only walked when zh_set_audit reports a gap
)


_REGISTRY: tuple[Source, ...] = (
    PTCG_CHS_DATASETS,
    PTCG_TW,
    MYCARDART_TC,
)


def all_sources() -> tuple[Source, ...]:
    """Every registered source, in declaration order."""
    return _REGISTRY


def sources_for(lang: Lang, *, include_fallback: bool = False) -> list[Source]:
    """
    All sources for `lang`, in declaration order.

    `include_fallback` defaults False because audit-driven fallbacks
    (MyCardArt) should not be bulk-walked — Phase D skips them and
    ZH-4's zh_set_audit invokes them on demand.
    """
    return [
        s for s in _REGISTRY
        if s.lang == lang and (include_fallback or not s.is_fallback_only)
    ]


def source_by_name(name: str) -> Optional[Source]:
    """Lookup by `name`. Returns None if unknown."""
    for s in _REGISTRY:
        if s.name == name:
            return s
    return None
