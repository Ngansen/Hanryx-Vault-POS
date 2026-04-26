#!/usr/bin/env python3
"""
refresh-image-digests.py
========================

Re-resolve the ``@sha256:<digest>`` on every pinned base image in
``pi-setup/`` against ``registry-1.docker.io`` and either print a clean
diff for review or update the files in place. Companion helper to the
manual recipe in ``pi-setup/docs/REPRODUCIBILITY.md`` §6.

Why
---
Every ``FROM`` line in the four in-tree Dockerfiles and every ``image:``
line in ``pi-setup/docker-compose.yml`` is pinned as
``name:tag@sha256:<digest>``. The tag is human-readable; the digest is
what ``docker pull`` actually fetches. That's by design — Docker Hub
tags are technically mutable, so the digest makes a substituted image
fail loudly instead of installing silently.

But it's also a footgun for maintainers: if you bump the tag (e.g.
``python:3.11.10-slim-bookworm`` → ``python:3.11.11-slim-bookworm``)
and forget to refresh the digest, every ``docker pull`` refuses the
manifest mismatch and the maintainer has to fish the new digest out of
the registry by hand. This helper does the lookup automatically: edit
the tag, run the script, commit the digest update.

Usage
-----
Run from the repository root::

    # Default — resolve every pinned tag, print a unified diff of what
    # would change to stdout, and exit non-zero if anything is stale.
    # Safe to drop into CI or a pre-push hook.
    python3 pi-setup/scripts/refresh-image-digests.py

    # Same lookup, but rewrite the files in place.
    python3 pi-setup/scripts/refresh-image-digests.py --write

The script never edits a file unless ``--write`` is passed.

Lock-step
---------
A few pins MUST stay byte-identical, and refreshing one but not its
peer would silently break the next build:

* ``pi-setup/Dockerfile`` builder + runtime stages share the same
  Python image. The runtime stage copies in the venv built by the
  builder, so an ABI mismatch (e.g. CPython 3.11.10 → 3.11.11) silently
  breaks extension modules (psycopg2, pillow, …).
* ``pi-setup/services/storefront/Dockerfile`` builder + runtime stages
  share the same Node image (same reason — the runtime image needs the
  same node ABI as the one that ran ``npm ci``).
* ``pi-setup/Dockerfile`` and ``pi-setup/recognizer/Dockerfile`` both
  pin the same Python image. They're separate services but we keep
  their base images in lock-step deliberately so a single bump covers
  both and the recognizer can re-use the POS venv layer cache.

The script enforces this invariant **before** doing any registry
lookups: if any image repo (e.g. ``library/python``) appears in the
scanned files with two different ``(tag, digest)`` pairs, the refresh
aborts with a clear message and asks the maintainer to reconcile the
drift first. That avoids the nasty failure mode where one stale stage
"wins" the refresh and silently propagates to its peer.

Network
-------
All lookups go to ``registry-1.docker.io`` via anonymous bearer tokens
from ``auth.docker.io``. The script asks for the **multi-arch
image-index digest** (via ``Accept:
application/vnd.oci.image.index.v1+json,
application/vnd.docker.distribution.manifest.list.v2+json``) — that's
the digest that supports ``docker pull`` on both the maintainer's
laptop and the Pi's arm64 hardware. A per-architecture manifest digest
would build on one and not the other; the script refuses to pin one
even if the registry returns it.

If you need to point the script at a non-Docker-Hub registry, edit the
``REGISTRY`` constant below — every pin currently in pi-setup/ lives on
Docker Hub so we keep this hard-coded for now.
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

# Match a ``<repo>:<tag>@sha256:<64 hex>`` reference anywhere in a line.
# Repo may include a registry / namespace prefix with slashes; tag is the
# usual Docker tag charset; digest is exactly 64 lower-case hex chars.
# The leading lookbehind keeps us from matching the middle of a longer
# token (e.g. ``some-prefix-pgvector/pgvector:…``) — pins are always
# preceded by whitespace, ``=``, ``:`` (compose ``image:`` colon and
# space), ``"`` / ``'`` (quoted compose values), or start-of-line.
PIN_RE = re.compile(
    r"(?:(?<=[\s=:'\"])|(?<=^))"
    r"(?P<repo>[a-zA-Z0-9][a-zA-Z0-9._\-/]*)"
    r":(?P<tag>[a-zA-Z0-9_][a-zA-Z0-9._\-]*)"
    r"@sha256:(?P<digest>[a-fA-F0-9]{64})"
)


# ── Files to scan ────────────────────────────────────────────────────────────

# Order is stable for diff output. Adding a new pinned Dockerfile? Add it
# here so the refresh covers it. Anything not listed is silently skipped.
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

# Multi-arch image-index media types. We deliberately do NOT advertise the
# single-arch manifest types — we want the index digest, which is what
# supports ``docker pull`` on both x86 and arm64. A registry that has only
# a per-arch manifest will be detected from the response Content-Type and
# rejected (see ``resolve_digest`` below).
MANIFEST_INDEX_ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
))

USER_AGENT = (
    "hanryx-pi-setup-refresh-image-digests/1.0 "
    "(+https://github.com/Ngansen/hanryx; refresh helper for FROM/image: pins)"
)

HTTP_TIMEOUT_S = 30.0
MAX_ATTEMPTS = 4
BACKOFF_S = (2.0, 5.0, 10.0)  # waits between attempts 1→2, 2→3, 3→4


# ── Data ─────────────────────────────────────────────────────────────────────

class Pin(NamedTuple):
    """One occurrence of a ``name:tag@sha256:<digest>`` reference."""
    file: str            # repo-root-relative path
    lineno: int          # 1-indexed
    line: str            # original line text (no trailing newline)
    repo_raw: str        # as written in the file (e.g. ``python``)
    repo: str            # canonicalized for registry (e.g. ``library/python``)
    tag: str
    digest: str          # current digest (lower-case 64-hex, no ``sha256:`` prefix)


class ProposedUpdate(NamedTuple):
    pin: Pin
    new_digest: str      # lower-case 64-hex


# ── Parsing ──────────────────────────────────────────────────────────────────

def canonicalize_repo(repo_raw: str) -> str:
    """Docker Hub official images live under the implicit ``library/``
    prefix; anything with a ``/`` already is namespaced (user/org or a
    full ``registry.example.com/foo`` path)."""
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
                line=line,
                repo_raw=repo_raw,
                repo=canonicalize_repo(repo_raw),
                tag=m.group("tag"),
                digest=m.group("digest").lower(),
            ))
    return out


# ── Lock-step validation ─────────────────────────────────────────────────────

def find_lockstep_drift(pins: list[Pin]) -> list[str]:
    """Return human-readable error messages for any image repo that
    appears in the scanned files with more than one ``(tag, digest)``
    pair. Empty list means everything is in lock-step.

    Why this is a hard error rather than "let the script pick the
    newest": a refresh with drifted sources would silently bump one
    stage to ``latest`` while leaving its lock-step peer behind, which
    is exactly the bug we're trying to prevent. Force the maintainer to
    reconcile by hand first.
    """
    by_repo: dict[str, list[Pin]] = {}
    for p in pins:
        by_repo.setdefault(p.repo, []).append(p)
    errors: list[str] = []
    for repo, group in by_repo.items():
        unique = {(p.tag, p.digest) for p in group}
        if len(unique) <= 1:
            continue
        msg_lines = [
            f"image {repo!r} has {len(unique)} different (tag, digest) "
            "pairs across the scanned files. The pi-setup base images are",
            "kept in lock-step by convention (see docstring of this script);",
            "reconcile the drift by hand before re-running the refresh:",
        ]
        for p in sorted(group, key=lambda x: (x.file, x.lineno)):
            msg_lines.append(
                f"    {p.file}:{p.lineno}  "
                f"{p.repo_raw}:{p.tag}@sha256:{p.digest[:12]}…"
            )
        errors.append("\n  ".join(msg_lines))
    return errors


# ── Registry lookups ─────────────────────────────────────────────────────────

class ResolveError(RuntimeError):
    """Raised when the registry cannot be queried for a given (repo, tag)."""


def _http_with_retries(
    req: urllib.request.Request,
) -> tuple[int, list[tuple[str, str]], bytes]:
    """``urlopen`` with retry / backoff. Returns ``(status, headers, body)``
    on a 2xx response. Raises ``ResolveError`` on a terminal 4xx (other
    than 429) or after exhausting retries on transient failures."""
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
            if 400 <= e.code < 500 and e.code != 429:
                raise ResolveError(last_err) from e
        except urllib.error.URLError as e:
            last_err = f"network error: {e.reason}"
        except (TimeoutError, socket.timeout):
            last_err = f"timeout after {HTTP_TIMEOUT_S:.0f}s"
        except OSError as e:
            last_err = f"network error: {e}"
    raise ResolveError(last_err)


def fetch_token(repo: str) -> str:
    url = AUTH_TOKEN_URL.format(repo=repo)
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    _status, _headers, body = _http_with_retries(req)
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise ResolveError(
            f"auth.docker.io returned non-JSON token body: {e}"
        ) from e
    # Docker Hub returns ``token``; some other registries that share the
    # same v2 protocol return ``access_token``. Accept either.
    token = data.get("token") or data.get("access_token")
    if not isinstance(token, str) or not token:
        raise ResolveError(
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


def resolve_digest(repo: str, tag: str) -> str:
    """Return the lower-case 64-hex sha256 digest of the multi-arch
    image index for ``<repo>:<tag>`` on registry-1.docker.io.

    Raises ``ResolveError`` if:

    * the repo / tag doesn't exist (4xx from the registry),
    * the registry returned no ``Docker-Content-Digest`` header,
    * the digest algorithm isn't sha256 or the hex is malformed,
    * the response is a per-arch manifest (we'd silently lose multi-arch
      pulls if we pinned that).
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
        raise ResolveError(
            "registry response missing Docker-Content-Digest header "
            "(was the tag deleted from Docker Hub?)"
        )
    if not digest.startswith("sha256:"):
        raise ResolveError(
            f"unexpected digest algorithm in response: {digest!r}"
        )
    hex_part = digest.split(":", 1)[1].lower()
    if not re.fullmatch(r"[0-9a-f]{64}", hex_part):
        raise ResolveError(
            f"malformed sha256 digest from registry: {digest!r}"
        )

    # Multi-arch guardrail: if the registry doesn't have an index for
    # this tag (e.g. an older single-arch image), it may still respond
    # with whatever manifest it has and a non-index Content-Type. Pinning
    # that digest would silently drop multi-arch pulls and the next
    # ``docker pull`` on the wrong arch would fail.
    content_type = _header(headers, "Content-Type").lower()
    if content_type and (
        "manifest.list" not in content_type
        and "image.index" not in content_type
    ):
        raise ResolveError(
            f"registry returned a per-arch manifest ({content_type!r}), "
            "not the multi-arch index. Refusing to pin a single-arch "
            "digest — the build would break on the other architecture."
        )
    return hex_part


# ── Apply ────────────────────────────────────────────────────────────────────

def compute_updates(pins: list[Pin]) -> tuple[list[ProposedUpdate], list[str]]:
    """Resolve each unique ``(repo, tag)`` exactly once and build the
    list of pins whose digest needs refreshing. Returns
    ``(updates, lookup_errors)``."""
    resolved: dict[tuple[str, str], str] = {}
    errors: list[str] = []
    unique_keys = sorted({(p.repo, p.tag) for p in pins})
    for repo, tag in unique_keys:
        print(f"[refresh] resolving {repo}:{tag} ...", file=sys.stderr)
        try:
            digest = resolve_digest(repo, tag)
        except ResolveError as e:
            errors.append(f"{repo}:{tag}  ->  {e}")
            print(f"[refresh]   FAIL: {e}", file=sys.stderr)
            continue
        resolved[(repo, tag)] = digest
        print(f"[refresh]   sha256:{digest}", file=sys.stderr)

    updates: list[ProposedUpdate] = []
    for p in pins:
        new = resolved.get((p.repo, p.tag))
        if new is None:
            continue
        if new != p.digest:
            updates.append(ProposedUpdate(pin=p, new_digest=new))
    return updates, errors


def apply_in_place(updates: list[ProposedUpdate], repo_root: str) -> None:
    """Rewrite every affected file in place. Each unique
    ``@sha256:<old>`` token is replaced globally with its new digest.

    A multi-stage Dockerfile (POS, storefront) holds the same pin on
    two different lines; both share the same old digest, so a single
    global ``content.replace(old, new)`` correctly covers both lines —
    we MUST NOT loop the replace per occurrence (the second iteration
    would not find the token because the first already rewrote both).
    Two pins with different old digests in the same file are handled
    by separate global replaces, which don't interfere because their
    search keys differ.

    Lock-step drift is rejected upstream in ``main()``, so we never
    have to worry about two updates with the same ``(file, old_digest)``
    key disagreeing on the new digest.
    """
    by_file: dict[str, list[ProposedUpdate]] = {}
    for u in updates:
        by_file.setdefault(u.pin.file, []).append(u)
    for rel_path, file_updates in by_file.items():
        abs_path = os.path.join(repo_root, rel_path)
        with open(abs_path, "r", encoding="utf-8") as fh:
            content = fh.read()
        # Deduplicate by old_digest within the file. Each unique old
        # digest is rewritten exactly once with a global replace.
        rewrites: dict[str, str] = {}
        for u in file_updates:
            rewrites.setdefault(u.pin.digest, u.new_digest)
        for old_digest, new_digest in rewrites.items():
            old_token = f"@sha256:{old_digest}"
            new_token = f"@sha256:{new_digest}"
            if old_token not in content:
                raise RuntimeError(
                    f"could not locate {old_token} in {rel_path} "
                    "(file changed under us between read and write?)"
                )
            content = content.replace(old_token, new_token)
        with open(abs_path, "w", encoding="utf-8") as fh:
            fh.write(content)


def render_diff(updates: list[ProposedUpdate]) -> str:
    """Render a unified-diff-style preview. We don't shell out to
    ``diff`` because the only thing that ever changes is the digest
    inside one line — easier to format the hunks ourselves."""
    if not updates:
        return ""
    out: list[str] = []
    by_file: dict[str, list[ProposedUpdate]] = {}
    for u in updates:
        by_file.setdefault(u.pin.file, []).append(u)
    for rel_path in sorted(by_file):
        out.append(f"--- a/{rel_path}")
        out.append(f"+++ b/{rel_path}")
        for u in sorted(by_file[rel_path], key=lambda x: x.pin.lineno):
            old_line = u.pin.line
            new_line = old_line.replace(
                f"@sha256:{u.pin.digest}",
                f"@sha256:{u.new_digest}",
            )
            out.append(f"@@ line {u.pin.lineno} @@")
            out.append(f"-{old_line}")
            out.append(f"+{new_line}")
        out.append("")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Re-resolve @sha256:<digest> on every pinned base image in "
            "pi-setup/ and either print a diff (default) or update the "
            "files in place (--write)."
        ),
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help=(
            "Apply the digest updates in place. Default is dry-run: print "
            "a diff to stdout and exit non-zero if any digest would change."
        ),
    )
    args = parser.parse_args(argv)

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
        print("OK: no `name:tag@sha256:<digest>` pins found in pi-setup/.")
        return 0

    # Lock-step pre-validation. If a repo appears with two different
    # (tag, digest) pairs in the source files, refuse before touching
    # the network — refreshing a drifted source would silently propagate
    # one stage's stale tag to the other.
    drift_errors = find_lockstep_drift(all_pins)
    if drift_errors:
        print(
            "FAIL: pi-setup/ base images are not in lock-step. Refusing "
            "to refresh until reconciled (see this script's docstring "
            "for the lock-step rules).",
            file=sys.stderr,
        )
        print("", file=sys.stderr)
        for err in drift_errors:
            print(f"  {err}", file=sys.stderr)
            print("", file=sys.stderr)
        return 1

    # Resolve every unique (repo, tag) and build the list of digest changes.
    updates, lookup_errors = compute_updates(all_pins)

    if lookup_errors:
        print("", file=sys.stderr)
        print(
            "FAIL: could not resolve one or more tags against the registry. "
            "Either the tag was deleted upstream (Task #19 catches that on "
            "every PR), there's a transient network problem, or the tag "
            "was typo'd in the Dockerfile / compose file.",
            file=sys.stderr,
        )
        for e in lookup_errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    if not updates:
        unique_count = len({(p.repo, p.tag) for p in all_pins})
        print("")
        print(
            f"OK: all {unique_count} unique tag(s) already resolve to the "
            "digest pinned in pi-setup/. Nothing to refresh."
        )
        return 0

    print("")
    print(render_diff(updates))

    if args.write:
        apply_in_place(updates, repo_root)
        print(
            f"WROTE: refreshed {len(updates)} digest(s) in place. Re-run "
            "`docker compose build --no-cache` and smoke-test before "
            "committing."
        )
        return 0

    print(
        f"{len(updates)} digest(s) need refreshing. Re-run with --write "
        "to apply, or copy the new digests in by hand. (No files were "
        "modified.)",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
