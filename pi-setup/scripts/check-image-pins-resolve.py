#!/usr/bin/env python3
"""
check-image-pins-resolve.py
===========================

Repository / CI guard that catches retired Docker Hub tags **and**
silent digest drift on every ``name:tag@sha256:<digest>`` pin in
``pi-setup/`` before the next ``docker compose pull`` on a Pi sees the
404. Companion to ``check-no-floating-tags.py`` (which forbids floating
tags) and ``refresh-image-digests.py`` (which proposes new digests when
a maintainer is intentionally bumping a tag).

Why
---
Every ``FROM`` line in the four in-tree Dockerfiles and every
``image:`` line in ``pi-setup/docker-compose.yml`` is pinned as
``name:tag@sha256:<digest>`` (Tasks #9 / #11). The digest is what
``docker pull`` actually fetches, so an existing Pi that already has
the image cached keeps working forever — the digest is content-
addressable.

A fresh Pi (or a Pi after ``docker system prune``) is a different
story: ``docker compose pull`` first resolves the **tag** and only then
verifies the digest. If the upstream maintainer has retired the tag
(Docker Hub returns 404), the pull fails on the Pi with a confusing
manifest-not-found error and the operator can't bring up the stack.

This is exactly what bit us on ``edoburu/pgbouncer:1.21.0`` while
working Task #14 — the unsuffixed ``1.21.0`` tag had been replaced
upstream by ``1.21.0-pN`` patch tags, the digest still pulled (because
it was content-addressable), but a fresh pull would 404. Nothing in CI
caught it; the bug shipped to the Pi.

This guard catches the bad pin in CI before it ships. For each pin it
verifies, against ``registry-1.docker.io``, BOTH:

1. The tag still resolves to a manifest (no 404 — i.e. it hasn't been
   retired or garbage-collected).
2. The resolved manifest digest still matches the digest pinned in the
   file (i.e. the upstream maintainer hasn't re-pushed the same tag
   with different bits — supply-chain tamper-evidence).

What this is NOT
----------------
* It's not a tag-syntax check — that's ``check-no-floating-tags.py``.
* It's not a "propose new digests" tool — that's
  ``refresh-image-digests.py --write``. This script never edits files.
* It doesn't check ``snapshot.debian.org`` — that's
  ``check-debian-snapshot-date.py``. Different upstream registry,
  different failure mode.

Run from the repository root::

    python3 pi-setup/scripts/check-image-pins-resolve.py

Exit codes:
    0 — every pin's tag resolves AND its digest still matches.
    1 — at least one pin's tag was retired or its digest drifted. The
        offending image is named in the error message.
    2 — setup error (target file missing, malformed pin, etc.).

Network
-------
All lookups go to ``registry-1.docker.io`` via anonymous bearer tokens
from ``auth.docker.io`` — no credentials needed. Each request is
retried with exponential backoff so a transient Docker Hub hiccup does
not fail CI. A persistent network outage (DNS down, registry
unreachable for the full retry budget) IS reported as a failure so
that the maintainer notices — silently passing on "couldn't check"
would defeat the point of the guard.

The check asks for the **multi-arch image-index digest** (``Accept:
application/vnd.oci.image.index.v1+json,
application/vnd.docker.distribution.manifest.list.v2+json``) — that's
the digest format we pin (see ``refresh-image-digests.py`` docstring),
so digests are compared on a like-for-like basis. A registry that has
retired the multi-arch index but kept a per-arch manifest is treated
as a tag retirement (we'd silently drop multi-arch pulls if we
accepted that digest).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from typing import NamedTuple


# ── Pin syntax ───────────────────────────────────────────────────────────────
#
# Match a ``<repo>:<tag>@sha256:<64 hex>`` reference anywhere in a line.
# Same regex as ``refresh-image-digests.py`` — the two scripts must agree
# on what counts as a pin or one of them will silently miss something.
# Repo may include a registry / namespace prefix with slashes; tag is
# the usual Docker tag charset; digest is exactly 64 lower-case hex
# chars. The leading lookbehind keeps us from matching the middle of a
# longer token.
PIN_RE = re.compile(
    r"(?:(?<=[\s=:'\"])|(?<=^))"
    r"(?P<repo>[a-zA-Z0-9][a-zA-Z0-9._\-/]*)"
    r":(?P<tag>[a-zA-Z0-9_][a-zA-Z0-9._\-]*)"
    r"@sha256:(?P<digest>[a-fA-F0-9]{64})"
)


# ── Files to scan ────────────────────────────────────────────────────────────
#
# Same list as ``refresh-image-digests.py``. Adding a new pinned
# Dockerfile? Add it to BOTH scripts so refresh and check stay in sync.
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

# Multi-arch image-index media types. Same Accept header as the
# refresher, so the digest we get back is comparable to the digest
# pinned in the file (the refresher only ever writes index digests).
MANIFEST_INDEX_ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
))

USER_AGENT = (
    "hanryx-pi-setup-check-image-pins-resolve/1.0 "
    "(+https://github.com/Ngansen/hanryx; CI guard for FROM/image: pins)"
)

HTTP_TIMEOUT_S = 30.0
MAX_ATTEMPTS = 4
BACKOFF_S = (2.0, 5.0, 10.0)  # waits between attempts 1→2, 2→3, 3→4


# ── Data ─────────────────────────────────────────────────────────────────────

class Pin(NamedTuple):
    """One occurrence of a ``name:tag@sha256:<digest>`` reference."""
    file: str            # repo-root-relative path
    lineno: int          # 1-indexed
    repo_raw: str        # as written in the file (e.g. ``python``)
    repo: str            # canonicalized for registry (e.g. ``library/python``)
    tag: str
    digest: str          # current digest (lower-case 64-hex, no ``sha256:`` prefix)


class Finding(NamedTuple):
    """One pin that failed verification, plus a human-readable reason."""
    pin: Pin
    reason: str


# ── Parsing ──────────────────────────────────────────────────────────────────

def canonicalize_repo(repo_raw: str) -> str:
    """Docker Hub official images live under the implicit ``library/``
    prefix; anything with a ``/`` already is namespaced (user/org or a
    full ``registry.example.com/foo`` path). Same convention as the
    refresher."""
    return repo_raw if "/" in repo_raw else f"library/{repo_raw}"


def parse_file(rel_path: str, repo_root: str) -> list[Pin]:
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
                repo_raw=repo_raw,
                repo=canonicalize_repo(repo_raw),
                tag=m.group("tag"),
                digest=m.group("digest").lower(),
            ))
    return out


# ── Registry lookups ─────────────────────────────────────────────────────────

class TagRetiredError(RuntimeError):
    """Raised when the registry says the tag no longer exists (4xx)."""


class RegistryError(RuntimeError):
    """Raised when the registry call itself fails (network, 5xx after
    retries, malformed response). Distinct from ``TagRetiredError`` so
    callers can decide whether to fail the build (we always do, but
    the distinction shows up in the error message)."""


def _http_with_retries(
    req: urllib.request.Request,
) -> tuple[int, list[tuple[str, str]], bytes]:
    """``urlopen`` with retry / backoff. Returns ``(status, headers,
    body)`` on a 2xx response. Raises ``TagRetiredError`` on a terminal
    4xx (other than 429), or ``RegistryError`` on persistent transient
    failures.

    The split is important: a 404 on a manifest URL is a definitive
    "this tag does not exist" answer and means the pin is broken — the
    caller should report it as such and not retry. A network blip or a
    503 is "couldn't ask" and gets retried before being surfaced as a
    different kind of failure.
    """
    last_err = "no attempts made"
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
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code} {e.reason or ''}".strip()
            # 4xx (except rate-limit) is terminal — won't fix itself.
            # That's the "tag retired" signal we care about.
            if 400 <= e.code < 500 and e.code != 429:
                raise TagRetiredError(last_err) from e
        except urllib.error.URLError as e:
            last_err = f"network error: {e.reason}"
        except (TimeoutError, socket.timeout):
            last_err = f"timeout after {HTTP_TIMEOUT_S:.0f}s"
        except OSError as e:
            last_err = f"network error: {e}"
    raise RegistryError(last_err)


def fetch_token(repo: str) -> str:
    """Anonymous bearer token for the given repo. ``auth.docker.io``
    happily issues these for public images — no credentials required."""
    url = AUTH_TOKEN_URL.format(repo=repo)
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        _status, _headers, body = _http_with_retries(req)
    except TagRetiredError as e:
        # auth.docker.io returns 401 for repos that genuinely don't
        # exist, but it's not strictly a "tag retired" — re-frame as a
        # registry error so the message makes sense.
        raise RegistryError(f"could not fetch auth token: {e}") from e
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
    server's casing."""
    name_lc = name.lower()
    for k, v in headers:
        if k.lower() == name_lc:
            return v.strip()
    return ""


def resolve_digest(repo: str, tag: str) -> str:
    """Return the lower-case 64-hex sha256 digest of the multi-arch
    image index for ``<repo>:<tag>`` on registry-1.docker.io.

    Raises ``TagRetiredError`` if the registry says the tag does not
    exist (404 on the manifest URL, or a 200 but with a per-arch
    response — we treat that as "the multi-arch index this pin needs is
    gone"). Raises ``RegistryError`` for transient or malformed-response
    failures.
    """
    token = fetch_token(repo)
    url = MANIFEST_URL.format(registry=REGISTRY, repo=repo, tag=tag)
    req = urllib.request.Request(
        url,
        method="HEAD",
        headers={
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {token}",
            "Accept": MANIFEST_INDEX_ACCEPT,
        },
    )
    _status, headers, _body = _http_with_retries(req)

    digest = _header(headers, "Docker-Content-Digest")
    if not digest:
        raise RegistryError(
            "registry response missing Docker-Content-Digest header"
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

    # Multi-arch guardrail: a registry that has dropped the multi-arch
    # index but kept a single-arch manifest may respond 200 with a
    # per-arch Content-Type. The digest we'd get is NOT comparable to
    # what we have pinned (the refresher only ever writes index
    # digests), so report it as if the tag was retired — the maintainer
    # needs to bump.
    content_type = _header(headers, "Content-Type").lower()
    if content_type and (
        "manifest.list" not in content_type
        and "image.index" not in content_type
    ):
        raise TagRetiredError(
            f"registry returned a per-arch manifest ({content_type!r}), "
            "not the multi-arch index. The pinned multi-arch index "
            "appears to have been retired upstream."
        )
    return hex_part


# ── Main ─────────────────────────────────────────────────────────────────────

def verify_pins(pins: list[Pin]) -> list[Finding]:
    """Resolve each unique ``(repo, tag)`` exactly once and return a
    list of pins that failed verification. Empty list = all good."""
    # Cache by (repo, tag) — the same image is often pinned twice (e.g.
    # builder + runtime stage in a multi-stage Dockerfile), and we
    # don't want to hit the registry twice for the same answer.
    resolved: dict[tuple[str, str], str] = {}
    retired: dict[tuple[str, str], str] = {}     # (repo, tag) -> reason
    transient: dict[tuple[str, str], str] = {}   # (repo, tag) -> reason

    unique_keys = sorted({(p.repo, p.tag) for p in pins})
    for repo, tag in unique_keys:
        print(f"[check] {repo}:{tag} ...", file=sys.stderr)
        try:
            digest = resolve_digest(repo, tag)
        except TagRetiredError as e:
            retired[(repo, tag)] = str(e)
            print(f"[check]   RETIRED: {e}", file=sys.stderr)
            continue
        except RegistryError as e:
            transient[(repo, tag)] = str(e)
            print(f"[check]   ERROR:   {e}", file=sys.stderr)
            continue
        resolved[(repo, tag)] = digest
        print(f"[check]   sha256:{digest}", file=sys.stderr)

    findings: list[Finding] = []
    for p in pins:
        key = (p.repo, p.tag)
        if key in retired:
            findings.append(Finding(
                pin=p,
                reason=(
                    f"tag retired upstream ({retired[key]}). The pinned "
                    "digest still pulls from caches that already have "
                    "the image, but a fresh `docker compose pull` on "
                    "the Pi will 404. Bump the pin (see "
                    "pi-setup/docs/REPRODUCIBILITY.md §6)."
                ),
            ))
        elif key in transient:
            findings.append(Finding(
                pin=p,
                reason=(
                    f"could not verify against {REGISTRY} after "
                    f"{MAX_ATTEMPTS} attempts ({transient[key]}). "
                    "Re-run the check; if it keeps failing, the "
                    "registry may be down."
                ),
            ))
        else:
            actual = resolved[key]
            if actual != p.digest:
                findings.append(Finding(
                    pin=p,
                    reason=(
                        f"digest drift: the multi-arch index for this "
                        f"tag is now sha256:{actual} upstream, but the "
                        f"file pins sha256:{p.digest}. Either the "
                        "upstream maintainer re-pushed the tag with "
                        "different bits (supply-chain warning — "
                        "investigate before bumping), or this tag is "
                        "expected to be mutable and the pin is stale. "
                        "Run `python3 pi-setup/scripts/refresh-image-"
                        "digests.py` to inspect, then `--write` to "
                        "apply once you've confirmed the change is "
                        "intentional."
                    ),
                ))
    return findings


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify every name:tag@sha256:<digest> pin in pi-setup/ "
            "still resolves on Docker Hub and the digest still matches."
        ),
    )
    parser.parse_args(argv)

    here = os.path.dirname(os.path.abspath(__file__))
    pi_setup = os.path.abspath(os.path.join(here, ".."))
    repo_root = os.path.abspath(os.path.join(pi_setup, ".."))

    # Parse all pins from all target files.
    all_pins: list[Pin] = []
    for rel in TARGET_FILES:
        abs_path = os.path.join(repo_root, rel)
        if not os.path.isfile(abs_path):
            print(f"error: {rel} not found at {abs_path}", file=sys.stderr)
            return 2
        all_pins.extend(parse_file(rel, repo_root))

    if not all_pins:
        # No pins is suspicious for pi-setup/ — there should always be
        # at least the compose file's pgvector / redis / pgbouncer
        # entries. Treat as a setup error so a parser regression in
        # PIN_RE can't silently green-light CI.
        print(
            "error: no `name:tag@sha256:<digest>` pins found in "
            "pi-setup/. Either every pin was removed (unexpected) or "
            "the pin parser regressed.",
            file=sys.stderr,
        )
        return 2

    findings = verify_pins(all_pins)

    if not findings:
        unique_count = len({(p.repo, p.tag) for p in all_pins})
        print("")
        print(
            f"OK: all {len(all_pins)} pin(s) ({unique_count} unique "
            f"tag(s)) still resolve on {REGISTRY} and the digests "
            "match what is pinned in pi-setup/."
        )
        return 0

    # Group findings by file for readable output.
    print("", file=sys.stderr)
    print(
        f"FAIL: {len(findings)} pin(s) in pi-setup/ no longer verify "
        f"against {REGISTRY}. A fresh `docker compose pull` on the Pi "
        "would fail or load unexpected bits.",
        file=sys.stderr,
    )
    by_file: dict[str, list[Finding]] = {}
    for f in findings:
        by_file.setdefault(f.pin.file, []).append(f)
    for rel_path in sorted(by_file):
        print("", file=sys.stderr)
        print(f"  {rel_path}:", file=sys.stderr)
        for f in sorted(by_file[rel_path], key=lambda x: x.pin.lineno):
            p = f.pin
            print(
                f"    line {p.lineno}: {p.repo_raw}:{p.tag}"
                f"@sha256:{p.digest[:12]}…",
                file=sys.stderr,
            )
            print(f"      → {f.reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
