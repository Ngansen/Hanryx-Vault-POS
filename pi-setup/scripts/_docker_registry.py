"""
_docker_registry.py
===================

Shared internals for the two ``pi-setup/scripts/`` tools that look up
``name:tag@sha256:<digest>`` base-image pins on
``registry-1.docker.io``:

* ``refresh-image-digests.py`` — proposes new digests when a maintainer
  is intentionally bumping a tag.
* ``check-image-pins-resolve.py`` — CI guard (Task #21) that catches
  retired tags and silent digest drift on every pin.

The leading underscore in the filename marks this as private to the
``pi-setup/scripts/`` package; nothing outside should import it.

Lock-step contract
------------------
Both scripts must agree, byte-for-byte, on:

* ``PIN_RE`` — the regex defining what counts as a pin. If the two
  scripts disagree on what a pin looks like, one will silently miss a
  bad pin (or treat a comment-glued junk string as a real pin) and the
  refresh-able vs verify-able guarantees stop matching reality.
* ``TARGET_FILES`` — the set of files in the repo that contain pins.
  If the refresher doesn't know about a file the checker checks (or
  vice versa), a maintainer who bumps a tag can't refresh its digest
  but CI still expects it to verify, or — worse — a stale pin lives
  in a file the checker never reads.
* ``canonicalize_repo`` and ``parse_file`` — how a raw pin token in a
  ``FROM`` / ``image:`` line maps to a canonical
  ``(library/...|user/repo)`` registry path. A divergence here means
  one script hits ``library/python`` and the other hits ``python``,
  one of which 404s and one of which doesn't.
* The bearer-token + multi-arch-index HEAD-request flow against
  ``registry-1.docker.io`` (``fetch_token``, ``resolve_digest``,
  ``_http_with_retries``, the ``HTTP_TIMEOUT_S`` / ``MAX_ATTEMPTS`` /
  ``BACKOFF_S`` budget, the ``MANIFEST_INDEX_ACCEPT`` media types).
  If one script tightens the retry budget (or one starts asking for
  a per-arch manifest while the other still asks for the index),
  the answers the two get back are no longer comparable.

Caller-specific (intentionally NOT shared)
------------------------------------------
* ``USER_AGENT`` — each script identifies itself separately so a
  registry operator (or a Docker Hub abuse review) can tell which
  tool made the request. Pass the script's user-agent string into
  ``fetch_token`` / ``resolve_digest`` as a kwarg.
* Exit-code policy and error formatting — ``refresh-image-digests.py``
  has a ``--skip-on-network-error`` escape hatch for a total Docker
  Hub outage; ``check-image-pins-resolve.py`` deliberately doesn't.
* The "is this 4xx the same thing as a tag retirement?" decision —
  the refresher treats every lookup failure uniformly; the checker
  surfaces "tag retired" as a distinct, more actionable message.
  ``RegistryError`` exposes ``network_only`` and ``terminal_4xx`` so
  each caller can decide independently.

Adding a new pinned image to ``pi-setup/``? Add the file path to
``TARGET_FILES`` below — not to either script. Tightening the pin
regex? Update ``PIN_RE`` here. The whole point of this module is that
neither change should ever land in just one of the two scripts.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from typing import NamedTuple


# ── Pin syntax ───────────────────────────────────────────────────────────────

# Match a ``<repo>:<tag>@sha256:<64 hex>`` reference anywhere in a line.
# Repo may include a registry / namespace prefix with slashes; tag is
# the usual Docker tag charset; digest is exactly 64 lower-case hex
# chars. The leading lookbehind keeps us from matching the middle of
# a longer token (e.g. ``some-prefix-pgvector/pgvector:…``) — pins are
# always preceded by whitespace, ``=``, ``:`` (compose ``image:`` colon
# and space), ``"`` / ``'`` (quoted compose values), or start-of-line.
# The trailing negative lookahead anchors the digest to EXACTLY 64 hex
# chars: without it, a malformed 65+ hex char digest would silently
# match the leading 64 and pass the parser as if it were a real pin —
# defeating the whole point of a pin check.
PIN_RE = re.compile(
    r"(?:(?<=[\s=:'\"])|(?<=^))"
    r"(?P<repo>[a-zA-Z0-9][a-zA-Z0-9._\-/]*)"
    r":(?P<tag>[a-zA-Z0-9_][a-zA-Z0-9._\-]*)"
    r"@sha256:(?P<digest>[a-fA-F0-9]{64})"
    r"(?![a-fA-F0-9])"
)


# ── Files to scan ────────────────────────────────────────────────────────────

# Order is stable so the refresher's diff output is deterministic.
# Adding a new pinned Dockerfile or compose file? Add it here so BOTH
# the refresher and the checker pick it up. Anything not listed here
# is silently skipped by both tools.
TARGET_FILES: tuple[str, ...] = (
    "pi-setup/Dockerfile",
    "pi-setup/recognizer/Dockerfile",
    "pi-setup/pokeapi/Dockerfile",
    "pi-setup/services/storefront/Dockerfile",
    "pi-setup/docker-compose.yml",
)


# ── Registry constants ───────────────────────────────────────────────────────

REGISTRY = "registry-1.docker.io"

# auth.docker.io issues anonymous pull tokens for any public repo.
AUTH_TOKEN_URL = (
    "https://auth.docker.io/token"
    "?service=registry.docker.io&scope=repository:{repo}:pull"
)

MANIFEST_URL = "https://{registry}/v2/{repo}/manifests/{tag}"

# Multi-arch image-index media types. We deliberately do NOT advertise
# the single-arch manifest types — we want the index digest, which is
# what supports ``docker pull`` on both the maintainer's laptop (x86)
# and the Pi's arm64 hardware. A registry that has only a per-arch
# manifest is detected from the response Content-Type and rejected by
# ``resolve_digest``: pinning a per-arch digest would silently break
# the build on the other architecture.
MANIFEST_INDEX_ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
))

HTTP_TIMEOUT_S = 30.0
MAX_ATTEMPTS = 4
BACKOFF_S = (2.0, 5.0, 10.0)  # waits between attempts 1→2, 2→3, 3→4


# ── Data ─────────────────────────────────────────────────────────────────────

class Pin(NamedTuple):
    """One occurrence of a ``name:tag@sha256:<digest>`` reference.

    ``line`` is the original line text (no trailing newline). The
    refresher uses it to render diff hunks; the checker doesn't need
    it but carrying the field is harmless and keeps the parser
    single-purpose.
    """
    file: str            # repo-root-relative path
    lineno: int          # 1-indexed
    line: str            # original line text (no trailing newline)
    repo_raw: str        # as written in the file (e.g. ``python``)
    repo: str            # canonicalized for registry (e.g. ``library/python``)
    tag: str
    digest: str          # current digest (lower-case 64-hex, no ``sha256:`` prefix)


# ── Parsing ──────────────────────────────────────────────────────────────────

def canonicalize_repo(repo_raw: str) -> str:
    """Docker Hub official images live under the implicit ``library/``
    prefix; anything with a ``/`` already is namespaced (user/org or a
    full ``registry.example.com/foo`` path)."""
    return repo_raw if "/" in repo_raw else f"library/{repo_raw}"


def parse_file(rel_path: str, repo_root: str) -> list[Pin]:
    """Return every pin found in ``<repo_root>/<rel_path>``. ``lineno``
    is 1-indexed; ``digest`` is normalised to lower-case so the
    comparison against a digest we got back from the registry is
    case-insensitive (some registries echo upper-case)."""
    abs_path = os.path.join(repo_root, rel_path)
    with open(abs_path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    out: list[Pin] = []
    for idx, line in enumerate(lines):
        for m in PIN_RE.finditer(line):
            repo_raw = m.group("repo")
            out.append(Pin(
                file=rel_path,
                lineno=idx + 1,
                line=line,
                repo_raw=repo_raw,
                repo=canonicalize_repo(repo_raw),
                tag=m.group("tag"),
                digest=m.group("digest").lower(),
            ))
    return out


# ── Registry lookups ─────────────────────────────────────────────────────────

class RegistryError(RuntimeError):
    """Raised when a lookup against ``registry-1.docker.io`` fails.

    The two callers want to classify failures differently, so this
    one exception type carries both pieces of information instead of
    forcing every caller to catch a hierarchy:

    ``network_only``
        True iff the failure is a transient network / timeout problem
        (DNS lookup failure, connection reset, request timed out, 5xx
        after exhausting the retry budget) rather than a definitive
        HTTP response from the registry. ``refresh-image-digests.py``
        uses this for its ``--skip-on-network-error`` escape hatch:
        only failures with ``network_only=True`` are eligible to be
        bypassed; a 4xx on a specific tag still fails the run because
        a real bad pin won't fix itself by waiting.

    ``terminal_4xx``
        True iff the failure was a definitive 4xx response on the
        manifest URL (i.e. "this tag does not exist") OR the registry
        returned a per-arch manifest where we asked for the multi-arch
        index (i.e. "the index this pin needs is gone upstream").
        ``check-image-pins-resolve.py`` uses this to surface a
        "tag retired upstream" message instead of the generic
        "couldn't verify" one. For the refresher the distinction is
        irrelevant — every lookup failure is reported uniformly.

    Note that the auth-token endpoint can also return 4xx (e.g. 401
    on a private/non-existent repo). ``fetch_token`` re-frames that
    as ``terminal_4xx=False`` on purpose: "auth refused for the repo"
    is fundamentally different from "the tag was retired", and the
    checker's "tag retired upstream" message would be misleading for
    an auth failure.
    """

    def __init__(
        self,
        msg: str,
        *,
        network_only: bool = False,
        terminal_4xx: bool = False,
    ) -> None:
        super().__init__(msg)
        self.network_only = network_only
        self.terminal_4xx = terminal_4xx


def _http_with_retries(
    req: urllib.request.Request,
) -> tuple[int, list[tuple[str, str]], bytes]:
    """``urlopen`` with retry / backoff. Returns ``(status, headers,
    body)`` on a 2xx response.

    Raises ``RegistryError`` with:

    * ``terminal_4xx=True`` on a terminal 4xx (other than 429 — that's
      rate-limiting and gets retried). 4xx on a manifest URL is the
      definitive "this tag does not exist" answer; retrying will not
      help.
    * ``network_only=True`` after exhausting retries on transient
      failures (URLError, timeout, generic OSError).
    * neither flag set after exhausting retries on a persistent 5xx.

    The User-Agent and Accept headers must already be on the Request;
    this helper does not inject them so the same retry budget can be
    reused for both the auth-token GET and the manifest HEAD.
    """
    last_err = "no attempts made"
    # Default to non-network until we actually see a network-style
    # exception — "made no attempts" should not be silently skipped
    # by --skip-on-network-error in the refresher.
    last_network_only = False
    for attempt in range(MAX_ATTEMPTS):
        if attempt > 0:
            time.sleep(BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)])
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                status = resp.status
                headers = list(resp.getheaders())
                body = resp.read()
                if 200 <= status < 300:
                    return status, headers, body
                last_err = f"HTTP {status} {resp.reason or ''}".strip()
                last_network_only = False
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code} {e.reason or ''}".strip()
            last_network_only = False
            # 4xx (except rate-limit) is terminal — won't fix itself.
            # That's the "tag retired" signal the checker cares about.
            if 400 <= e.code < 500 and e.code != 429:
                raise RegistryError(last_err, terminal_4xx=True) from e
        except urllib.error.URLError as e:
            last_err = f"network error: {e.reason}"
            last_network_only = True
        except (TimeoutError, socket.timeout):
            last_err = f"timeout after {HTTP_TIMEOUT_S:.0f}s"
            last_network_only = True
        except OSError as e:
            last_err = f"network error: {e}"
            last_network_only = True
    raise RegistryError(last_err, network_only=last_network_only)


def fetch_token(repo: str, *, user_agent: str) -> str:
    """Anonymous bearer token for ``repo``. ``auth.docker.io`` issues
    these for any public image — no credentials required.

    A terminal 4xx from the auth endpoint (e.g. 401 on a
    private/non-existent repo) is re-framed as a non-``terminal_4xx``
    ``RegistryError`` because "auth refused" is not the same shape of
    failure as "tag retired" — see the ``RegistryError`` docstring.
    """
    url = AUTH_TOKEN_URL.format(repo=repo)
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": user_agent, "Accept": "application/json"},
    )
    try:
        _status, _headers, body = _http_with_retries(req)
    except RegistryError as e:
        if e.terminal_4xx:
            raise RegistryError(
                f"could not fetch auth token: {e}",
                network_only=False,
                terminal_4xx=False,
            ) from e
        raise
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise RegistryError(
            f"auth.docker.io returned non-JSON token body: {e}"
        ) from e
    # Docker Hub returns ``token``; some other registries that share
    # the same v2 protocol return ``access_token``. Accept either.
    token = data.get("token") or data.get("access_token")
    if not isinstance(token, str) or not token:
        raise RegistryError(
            "auth.docker.io returned no token in response body"
        )
    return token


def _header(headers: list[tuple[str, str]], name: str) -> str:
    """Case-insensitive header lookup. HTTP header names are
    case-insensitive but ``HTTPResponse.getheaders()`` preserves the
    server's casing — be defensive."""
    name_lc = name.lower()
    for k, v in headers:
        if k.lower() == name_lc:
            return v.strip()
    return ""


def resolve_digest(repo: str, tag: str, *, user_agent: str) -> str:
    """Return the lower-case 64-hex sha256 digest of the multi-arch
    image index for ``<repo>:<tag>`` on ``registry-1.docker.io``.

    Raises ``RegistryError`` with:

    * ``terminal_4xx=True`` if the registry says the tag does not
      exist (404 on the manifest URL) OR returned a per-arch manifest
      where we asked for the multi-arch index. The latter is treated
      as "the index this pin needs is retired upstream": its digest
      is not comparable to the multi-arch index digest the refresher
      writes, so accepting it would silently drop multi-arch pulls
      and break the build on the other architecture.
    * ``network_only=True`` for transient network/timeout failures
      after the retry budget is exhausted.
    * neither flag set for malformed registry responses (missing
      ``Docker-Content-Digest`` header, non-sha256 algorithm,
      non-64-hex digest).
    """
    token = fetch_token(repo, user_agent=user_agent)
    url = MANIFEST_URL.format(registry=REGISTRY, repo=repo, tag=tag)
    req = urllib.request.Request(
        url,
        method="HEAD",
        headers={
            "User-Agent": user_agent,
            "Authorization": f"Bearer {token}",
            "Accept": MANIFEST_INDEX_ACCEPT,
        },
    )
    _status, headers, _body = _http_with_retries(req)

    digest = _header(headers, "Docker-Content-Digest")
    if not digest:
        raise RegistryError(
            "registry response missing Docker-Content-Digest header "
            "(was the tag deleted from Docker Hub?)"
        )
    if not digest.startswith("sha256:"):
        raise RegistryError(
            f"unexpected digest algorithm in response: {digest!r}"
        )
    hex_part = digest.split(":", 1)[1].lower()
    if not re.fullmatch(r"[0-9a-f]{64}", hex_part):
        raise RegistryError(
            f"malformed sha256 digest from registry: {digest!r}"
        )

    # Multi-arch guardrail: if the registry doesn't have an index for
    # this tag (e.g. an older single-arch image, or the index was
    # garbage-collected), it may still respond 200 with whatever
    # manifest it has and a per-arch Content-Type. The digest we'd
    # get is NOT comparable to what the refresher writes (which is
    # always an index digest), so refuse it. Marked terminal_4xx so
    # the checker surfaces "tag retired upstream" (which is in
    # practice what has happened) rather than "transient error".
    content_type = _header(headers, "Content-Type").lower()
    if content_type and (
        "manifest.list" not in content_type
        and "image.index" not in content_type
    ):
        raise RegistryError(
            f"registry returned a per-arch manifest ({content_type!r}), "
            "not the multi-arch index. Refusing to use a single-arch "
            "digest — the build would break on the other architecture, "
            "and the multi-arch index this pin needs appears to have "
            "been retired upstream.",
            terminal_4xx=True,
        )
    return hex_part
