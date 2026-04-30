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

Shared registry-lookup code lives in ``_docker_registry.py``: the
``PIN_RE`` regex, the ``TARGET_FILES`` list, ``canonicalize_repo`` /
``parse_file``, and the bearer-token + multi-arch-index HEAD-request
flow are deliberately one-and-the-same for this script and for
``refresh-image-digests.py``. See the docstring on that module for
the lock-step contract.
"""

from __future__ import annotations

import argparse
import os
import sys
import time  # re-exported for tests that mock.patch(checker.time, "sleep")  # noqa: F401
import urllib.error  # re-exported for test fixtures   # noqa: F401
import urllib.request  # re-exported for tests that patch urlopen  # noqa: F401
from typing import NamedTuple

# Hyphenated filenames in this directory aren't importable as Python
# modules, so the shared module is named with underscores. Adding the
# script's own directory to sys.path lets us ``import _docker_registry``
# regardless of how the script is invoked.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Re-export the shared parser surface verbatim so callers (and the
# existing unit tests in ``tests/test_check_image_pins_resolve.py``)
# can keep referring to ``checker.PIN_RE`` / ``checker.parse_file`` /
# ``checker.canonicalize_repo`` / ``checker.Pin`` / ``checker.TARGET_FILES``
# without caring that the implementation moved.
from _docker_registry import (  # noqa: E402
    MAX_ATTEMPTS,
    PIN_RE,
    REGISTRY,
    TARGET_FILES,
    Pin,
    _header,
    canonicalize_repo,
    parse_file,
)
from _docker_registry import RegistryError as _SharedRegistryError  # noqa: E402
from _docker_registry import _http_with_retries as _shared_http_with_retries  # noqa: E402
from _docker_registry import fetch_token as _shared_fetch_token  # noqa: E402
from _docker_registry import resolve_digest as _shared_resolve_digest  # noqa: E402


# ── Caller-specific constants ────────────────────────────────────────────────

# Each script identifies itself separately in the User-Agent so a
# registry operator can tell which tool made the request.
USER_AGENT = (
    "hanryx-pi-setup-check-image-pins-resolve/1.0 "
    "(+https://github.com/Ngansen/hanryx; CI guard for FROM/image: pins)"
)


# ── Local exception types ────────────────────────────────────────────────────

# Both subclass the shared ``RegistryError`` so existing ``except`` /
# ``isinstance`` chains keep working. The split into two types is a
# **caller-specific** concern (the refresher doesn't care about the
# distinction; this script does), so it lives here, not in the shared
# module. ``verify_pins`` below relies on the split to render the
# more actionable "tag retired upstream" message instead of the
# generic transient-failure one.
class TagRetiredError(_SharedRegistryError):
    """Raised when the registry says the tag no longer exists.

    Surfaces both an HTTP 4xx on the manifest URL ("the tag is gone")
    and the per-arch-only manifest case ("the multi-arch index this
    pin needs is gone"). In both cases a fresh ``docker compose pull``
    on the Pi would fail.
    """


class RegistryError(_SharedRegistryError):
    """Raised when the registry call itself fails (network, 5xx after
    retries, malformed response). Distinct from ``TagRetiredError``
    so callers can decide whether to fail the build (we always do,
    but the distinction shows up in the error message).
    """


def _translate(exc: _SharedRegistryError) -> "RegistryError | TagRetiredError":
    """Wrap a shared ``RegistryError`` in this script's local subclass
    that mirrors its classification. ``terminal_4xx`` is the "this
    tag is gone" signal; everything else is a transient/malformed
    failure that we surface as ``RegistryError``."""
    cls: type[_SharedRegistryError]
    cls = TagRetiredError if exc.terminal_4xx else RegistryError
    new = cls(str(exc))
    new.network_only = exc.network_only
    new.terminal_4xx = exc.terminal_4xx
    return new


# ── Registry helpers (thin wrappers over the shared module) ──────────────────
#
# These exist as module-level functions so the existing unit tests can
# call ``checker._http_with_retries`` / ``checker.fetch_token`` /
# ``checker.resolve_digest`` directly and assert against the local
# exception subclasses.

def _http_with_retries(
    req: urllib.request.Request,
) -> tuple[int, list[tuple[str, str]], bytes]:
    """``urlopen`` with retry / backoff. Returns ``(status, headers,
    body)`` on a 2xx response. Raises ``TagRetiredError`` on a
    terminal 4xx (other than 429), or ``RegistryError`` on persistent
    transient failures.

    The split is important: a 404 on a manifest URL is a definitive
    "this tag does not exist" answer and means the pin is broken — the
    caller should report it as such and not retry. A network blip or a
    503 is "couldn't ask" and gets retried before being surfaced as a
    different kind of failure.
    """
    try:
        return _shared_http_with_retries(req)
    except _SharedRegistryError as e:
        if isinstance(e, (TagRetiredError, RegistryError)):
            raise
        raise _translate(e) from e


def fetch_token(repo: str) -> str:
    """Anonymous bearer token for the given repo. ``auth.docker.io``
    happily issues these for public images — no credentials required.
    """
    try:
        return _shared_fetch_token(repo, user_agent=USER_AGENT)
    except _SharedRegistryError as e:
        if isinstance(e, (TagRetiredError, RegistryError)):
            raise
        raise _translate(e) from e


def resolve_digest(repo: str, tag: str) -> str:
    """Return the lower-case 64-hex sha256 digest of the multi-arch
    image index for ``<repo>:<tag>`` on registry-1.docker.io.

    Raises ``TagRetiredError`` if the registry says the tag does not
    exist (404 on the manifest URL, or a 200 but with a per-arch
    response — we treat that as "the multi-arch index this pin needs
    is gone"). Raises ``RegistryError`` for transient or
    malformed-response failures.
    """
    try:
        return _shared_resolve_digest(repo, tag, user_agent=USER_AGENT)
    except _SharedRegistryError as e:
        if isinstance(e, (TagRetiredError, RegistryError)):
            raise
        raise _translate(e) from e


# ── Findings ─────────────────────────────────────────────────────────────────

class Finding(NamedTuple):
    """One pin that failed verification, plus a human-readable reason."""
    pin: Pin
    reason: str


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
