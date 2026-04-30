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

    # CI escape hatch for a total Docker Hub outage. Passes (with a
    # WARN) only if EVERY failed lookup is a network/timeout error;
    # a 4xx for a specific tag still fails, and any digest drift on
    # tags that DID resolve still fails. See ``--help``.
    python3 pi-setup/scripts/refresh-image-digests.py --skip-on-network-error

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

Shared registry-lookup code lives in ``_docker_registry.py``: the
``PIN_RE`` regex, the ``TARGET_FILES`` list, ``canonicalize_repo`` /
``parse_file``, and the bearer-token + multi-arch-index HEAD-request
flow are deliberately one-and-the-same for this script and for the
``check-image-pins-resolve.py`` CI guard. See the docstring on that
module for the lock-step contract.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import NamedTuple

# Hyphenated filenames in this directory aren't importable as Python
# modules, so the shared module is named with underscores. Adding the
# script's own directory to sys.path lets us ``import _docker_registry``
# regardless of how the script is invoked (``pi-setup/scripts/...``,
# ``./refresh-image-digests.py`` from inside the directory, etc.).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _docker_registry import (  # noqa: E402
    MAX_ATTEMPTS,
    PIN_RE,
    REGISTRY,
    TARGET_FILES,
    Pin,
    RegistryError,
    canonicalize_repo,
    fetch_token,
    parse_file,
    resolve_digest as _shared_resolve_digest,
)


# ── Caller-specific constants ────────────────────────────────────────────────

# Each script identifies itself separately in the User-Agent so a
# registry operator can tell which tool made the request.
USER_AGENT = (
    "hanryx-pi-setup-refresh-image-digests/1.0 "
    "(+https://github.com/Ngansen/hanryx; refresh helper for FROM/image: pins)"
)


def resolve_digest(repo: str, tag: str) -> str:
    """Thin wrapper that pins the User-Agent for this script. Defined
    at module scope (rather than inlined) so unit tests / future
    callers can ``mock.patch`` it directly the way they could before
    the registry helpers moved to ``_docker_registry.py``."""
    return _shared_resolve_digest(repo, tag, user_agent=USER_AGENT)


# ── Data ─────────────────────────────────────────────────────────────────────

class ProposedUpdate(NamedTuple):
    pin: Pin
    new_digest: str      # lower-case 64-hex


class LookupFailure(NamedTuple):
    """One ``(repo, tag)`` lookup that the registry refused or that we
    couldn't reach. ``network_only`` carries the classification from
    ``RegistryError`` so ``main()`` can decide whether the
    ``--skip-on-network-error`` escape hatch applies."""
    key: str             # human-readable "repo:tag"
    detail: str          # short error description for the operator
    network_only: bool


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


# ── Apply ────────────────────────────────────────────────────────────────────

def compute_updates(
    pins: list[Pin],
) -> tuple[list[ProposedUpdate], list[LookupFailure]]:
    """Resolve each unique ``(repo, tag)`` exactly once and build the
    list of pins whose digest needs refreshing. Returns
    ``(updates, lookup_failures)`` — failures preserve the
    ``network_only`` classification so the caller can decide whether
    ``--skip-on-network-error`` applies."""
    resolved: dict[tuple[str, str], str] = {}
    failures: list[LookupFailure] = []
    unique_keys = sorted({(p.repo, p.tag) for p in pins})
    for repo, tag in unique_keys:
        print(f"[refresh] resolving {repo}:{tag} ...", file=sys.stderr)
        try:
            digest = resolve_digest(repo, tag)
        except RegistryError as e:
            failures.append(LookupFailure(
                key=f"{repo}:{tag}",
                detail=str(e),
                network_only=e.network_only,
            ))
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
    return updates, failures


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
    parser.add_argument(
        "--skip-on-network-error",
        action="store_true",
        help=(
            "Pass (with a warning) if registry-1.docker.io is unreachable "
            "for ALL failed lookups (DNS / total network outage / timeout). "
            "A 4xx for a specific tag still fails, and any digest drift on "
            "tags that DID resolve still fails. Use sparingly — this guard "
            "is what catches half-bumped pins and silent supply-chain "
            "re-pushes before the Pi rebuild."
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
    updates, lookup_failures = compute_updates(all_pins)

    suppressed_network_outage = False
    if lookup_failures:
        all_network = all(f.network_only for f in lookup_failures)
        if args.skip_on_network_error and all_network:
            # Total registry outage (DNS / connection / timeout) on every
            # failed lookup. The maintainer asked to bypass — emit a loud
            # WARN and fall through. We must still process whatever DID
            # resolve, because real digest drift on the resolved tags is
            # exactly what we're here to catch and a registry hiccup must
            # not silently mask it.
            print("", file=sys.stderr)
            print(
                "WARN: registry-1.docker.io appears unreachable for one or "
                "more tag(s); passing those because --skip-on-network-error "
                "is set. The following lookups failed:",
                file=sys.stderr,
            )
            for f in lookup_failures:
                print(f"  - {f.key}: {f.detail}", file=sys.stderr)
            suppressed_network_outage = True
        else:
            print("", file=sys.stderr)
            print(
                "FAIL: could not resolve one or more tags against the registry. "
                "Either the tag was deleted upstream (Task #19 catches that on "
                "every PR), there's a transient network problem, or the tag "
                "was typo'd in the Dockerfile / compose file. If this looks "
                "like a total Docker Hub outage, re-run with "
                "--skip-on-network-error to pass with a warning. For a "
                "deleted tag, the fix is to bump to a still-published tag — "
                "see pi-setup/docs/REPRODUCIBILITY.md §6.",
                file=sys.stderr,
            )
            for f in lookup_failures:
                print(f"  {f.key}  ->  {f.detail}", file=sys.stderr)
            return 1

    if not updates:
        if suppressed_network_outage:
            # Don't claim "all tags verified" — some weren't reachable.
            # The WARN above already conveyed the partial state.
            return 0
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
        f"{len(updates)} digest(s) need refreshing. Re-run locally with "
        "--write to apply (then `docker compose build --no-cache` and "
        "smoke-test before committing), or copy the new digests in by "
        "hand. Full bump procedure: pi-setup/docs/REPRODUCIBILITY.md §6. "
        "(No files were modified.)",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
