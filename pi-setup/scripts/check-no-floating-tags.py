#!/usr/bin/env python3
"""
check-no-floating-tags.py
=========================

Repository guard that scans ``pi-setup/`` for accidentally-reintroduced
floating Docker tags (the build-reproducibility analog of
``check-no-insecure-tls.py``). Fails with a non-zero exit code if any
``FROM`` line in a ``pi-setup/`` Dockerfile or any ``image:`` line in a
``pi-setup/`` docker-compose file uses a tag that is not a full point
release.

Why
---
Task #11 pinned every ``FROM`` line in the Pi's four custom-built
Dockerfiles, and Task #9 did the same for the ``image:`` lines in
``pi-setup/docker-compose.yml``. Without a guardrail, a copy-paste from a
Stack Overflow answer or a quick "I'll just bump it" can silently put us
back on a floating tag like ``python:3.11-slim``, ``node:20-slim``, or
``nginx:alpine`` — and the next Pi rebuild stops being reproducible with
no warning. Floating tags are also a supply-chain risk: an upstream tag is
mutable and can be re-pushed with different bits.

What the rule is
----------------
Every base image referenced by ``FROM`` or compose ``image:`` must have a
tag that includes at least two dots (a full point release such as
``3.11.10``, ``7.4.1-alpine`` or ``0.7.4-pg16``). The optional
``@sha256:…`` digest is ignored for this check — Task #11 / Task #9 own
the digest-pinning policy.

The following ``FROM`` shapes are allowed without a tag:

* ``FROM scratch``
* ``FROM <stage-name>`` where ``<stage-name>`` matched an earlier
  ``FROM … AS <stage-name>`` in the same Dockerfile (multi-stage build
  cross-reference, not an external image).

Run from the repository root::

    python3 pi-setup/scripts/check-no-floating-tags.py

Allow-listing an audited exception
----------------------------------
If a base image legitimately has no point-release tag (rare — most
official images do), put the marker ``hanryx-allow-floating-tag`` in a
comment on the **same line** or the line **immediately above** the
``FROM`` / ``image:`` line. Optionally append a short reason after a
colon::

    FROM example/legacy:latest  # hanryx-allow-floating-tag: vendor only publishes :latest

Pinning by ``@sha256:…`` digest is *still safer* than an allow marker,
even when the tag is floating, because it makes the pull tamper-evident.
Prefer that over the allow marker whenever the registry exposes a digest.

Extending
---------
This guard is intentionally narrow — only ``FROM`` and ``image:`` lines.
If we adopt other forms of base-image reference in the future (e.g.
``BaseImage =`` in a Buildah script), add another parser here rather than
broadening the existing regexes.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Iterable, NamedTuple

ALLOW_MARKER = "hanryx-allow-floating-tag"

# Directories under pi-setup/ that are not our source code (vendored, build
# output, cached external clones, runtime data, etc.) and so must be skipped
# wholesale. The path components are matched anywhere in the relative path.
SKIP_DIR_NAMES: frozenset[str] = frozenset({
    "__pycache__",
    "node_modules",
    ".git",
    "dist",
    "build",
    "data",
    "storefront-src",  # external repo cloned at build time
})

# Compose-style filenames we scan for `image:` lines. We deliberately do NOT
# scan arbitrary YAML — `image:` is a common key name (Helm values, GitHub
# Actions matrix, etc.) and there's no Helm/etc. content under pi-setup/
# today, but limiting to compose filenames keeps the rule's scope obvious
# and avoids false positives if someone adds one.
COMPOSE_FILENAME_RE = re.compile(
    r"^(docker-)?compose([.-][^.]+)?\.ya?ml$",
    re.IGNORECASE,
)


# ── FROM / image:  parsers ────────────────────────────────────────────────────

# Match a Dockerfile FROM line. Case-insensitive because Dockerfile keywords
# are conventionally upper-case but technically case-insensitive.
_FROM_RE = re.compile(r"^\s*FROM\s+(.+?)\s*$", re.IGNORECASE)

# Match a YAML `image:` line at any indentation. The capture group is the
# raw value (which may be quoted, may contain a comment, may be a YAML
# anchor reference, etc. — handled below).
_IMAGE_RE = re.compile(r"^\s*image\s*:\s*(.+?)\s*$")


def _strip_inline_comment(value: str) -> str:
    """Strip a trailing `# …` comment, preserving the value text. We do NOT
    try to be clever about `#` inside quoted strings — Dockerfile / compose
    image references never legitimately contain a `#`."""
    if "#" in value:
        return value.split("#", 1)[0].rstrip()
    return value


def _strip_yaml_quotes(value: str) -> str:
    """`image: "foo:1.2.3"` and `image: 'foo:1.2.3'` are both legal."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_from_line(line: str) -> tuple[str, str | None] | None:
    """Parse a ``FROM`` line.

    Returns ``(image_ref, stage_name_or_none)`` if the line is a FROM line,
    or ``None`` otherwise. ``image_ref`` is the raw image reference token
    (``image[:tag][@digest]`` or a stage name or ``scratch``); flags like
    ``--platform=…`` are stripped.
    """
    m = _FROM_RE.match(line)
    if not m:
        return None
    rest = _strip_inline_comment(m.group(1)).strip()
    if not rest:
        return None
    tokens = rest.split()
    # Drop leading flags (e.g. `--platform=linux/arm64`).
    while tokens and tokens[0].startswith("--"):
        tokens.pop(0)
    if not tokens:
        return None
    image_ref = tokens[0]
    stage_name: str | None = None
    # `FROM image AS name` — case-insensitive `AS`.
    if len(tokens) >= 3 and tokens[1].upper() == "AS":
        stage_name = tokens[2]
    return image_ref, stage_name


def _parse_image_line(line: str) -> str | None:
    """Parse a compose ``image:`` line, returning the image reference or
    ``None`` if the line is not an `image:` value (or carries an unusable
    value like a YAML anchor reference / variable)."""
    m = _IMAGE_RE.match(line)
    if not m:
        return None
    raw = _strip_yaml_quotes(_strip_inline_comment(m.group(1)).strip())
    if not raw:
        return None
    # YAML anchor reference — cannot statically verify the target. Skip it
    # and let the anchor's defining line carry the check instead.
    if raw.startswith("*"):
        return None
    # Pure variable reference like `${IMAGE}` — out of scope, this guard
    # only checks literal pins.
    if raw.startswith("${") and raw.endswith("}"):
        return None
    return raw


# ── Tag validation ────────────────────────────────────────────────────────────

def _extract_tag(image_ref: str) -> str | None:
    """Given ``image[:tag][@digest]``, return the tag string (or ``None`` if
    no tag is present). Handles ``registry:port/image:tag`` correctly (the
    port colon is not the tag separator)."""
    # Strip @digest first — the tag is whatever lives between the last `:`
    # in the name part and the `@`.
    name_part = image_ref.split("@", 1)[0]
    if ":" not in name_part:
        return None
    last_colon = name_part.rfind(":")
    after = name_part[last_colon + 1:]
    # If there's a `/` after the colon, that colon was a registry port
    # separator (e.g. `registry.example.com:5000/foo`), not a tag separator.
    if "/" in after:
        return None
    return after if after else None


def _tag_is_pinned(tag: str) -> bool:
    """The pinning rule: the tag must contain at least two ``.`` characters,
    encoding a full point release (``3.11.10``, ``7.4.1-alpine``, etc.)."""
    return tag.count(".") >= 2


# ── Scanning ──────────────────────────────────────────────────────────────────

class Finding(NamedTuple):
    path: str
    lineno: int
    kind: str          # "FROM" or "image:"
    reason: str
    line: str


def _line_is_allowed(lines: list[str], idx: int) -> bool:
    """The marker may sit on the same line or the line immediately above."""
    if ALLOW_MARKER in lines[idx]:
        return True
    if idx > 0 and ALLOW_MARKER in lines[idx - 1]:
        return True
    return False


def _read_text(path: str) -> list[str] | None:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text.splitlines()


def _is_dockerfile(filename: str) -> bool:
    name = filename.lower()
    # Matches `Dockerfile`, `Dockerfile.foo`, `Dockerfile-bar`, `foo.dockerfile`.
    if name == "dockerfile" or name.startswith("dockerfile.") or name.startswith("dockerfile-"):
        return True
    if name.endswith(".dockerfile"):
        return True
    return False


def _is_compose_file(filename: str) -> bool:
    return bool(COMPOSE_FILENAME_RE.match(filename))


def _scan_dockerfile(path: str, rel_path: str) -> Iterable[Finding]:
    lines = _read_text(path)
    if lines is None:
        return ()
    out: list[Finding] = []
    known_stages: set[str] = set()
    for idx, line in enumerate(lines):
        parsed = _parse_from_line(line)
        if parsed is None:
            continue
        image_ref, stage_name = parsed
        # Multi-stage cross-reference (e.g. `FROM builder`) — allowed.
        if image_ref == "scratch" or image_ref in known_stages:
            if stage_name:
                known_stages.add(stage_name)
            continue
        tag = _extract_tag(image_ref)
        if tag is None:
            reason = (
                f"FROM {image_ref!r} has no tag (defaults to :latest, which "
                "floats). Pin to a full point release like image:1.2.3."
            )
            ok = False
        elif not _tag_is_pinned(tag):
            reason = (
                f"FROM tag {tag!r} is not a full point release "
                "(needs at least two dots, e.g. 3.11.10 or 7.4.1-alpine)."
            )
            ok = False
        else:
            ok = True
            reason = ""
        if not ok and not _line_is_allowed(lines, idx):
            out.append(Finding(
                path=rel_path,
                lineno=idx + 1,
                kind="FROM",
                reason=reason,
                line=line.rstrip(),
            ))
        if stage_name:
            known_stages.add(stage_name)
    return out


def _scan_compose_file(path: str, rel_path: str) -> Iterable[Finding]:
    lines = _read_text(path)
    if lines is None:
        return ()
    out: list[Finding] = []
    for idx, line in enumerate(lines):
        image_ref = _parse_image_line(line)
        if image_ref is None:
            continue
        tag = _extract_tag(image_ref)
        if tag is None:
            reason = (
                f"image: {image_ref!r} has no tag (defaults to :latest, "
                "which floats). Pin to a full point release like "
                "image:1.2.3@sha256:…"
            )
            ok = False
        elif not _tag_is_pinned(tag):
            reason = (
                f"image: tag {tag!r} is not a full point release "
                "(needs at least two dots, e.g. 7.4.1-alpine or 0.7.4-pg16)."
            )
            ok = False
        else:
            ok = True
            reason = ""
        if not ok and not _line_is_allowed(lines, idx):
            out.append(Finding(
                path=rel_path,
                lineno=idx + 1,
                kind="image:",
                reason=reason,
                line=line.rstrip(),
            ))
    return out


def _should_skip_dir(rel_path: str) -> bool:
    parts = rel_path.split(os.sep)
    return any(p in SKIP_DIR_NAMES for p in parts)


SELF_PATH = os.path.abspath(__file__)


def scan(root: str) -> list[Finding]:
    findings: list[Finding] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir != "." and _should_skip_dir(rel_dir):
            dirnames[:] = []
            continue
        # Prune skipped subdirs in-place so os.walk doesn't descend.
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            # The checker itself documents bad tag shapes in docstrings —
            # skip it explicitly rather than littering it with allow markers.
            if os.path.abspath(full) == SELF_PATH:
                continue
            rel = os.path.relpath(full, start=os.getcwd())
            if _is_dockerfile(fn):
                findings.extend(_scan_dockerfile(full, rel))
            elif _is_compose_file(fn):
                findings.extend(_scan_compose_file(full, rel))
    return findings


def main(argv: list[str]) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    pi_setup = os.path.abspath(os.path.join(here, ".."))
    if not os.path.isdir(pi_setup):
        print(f"error: pi-setup/ not found at {pi_setup}", file=sys.stderr)
        return 2

    findings = scan(pi_setup)

    if not findings:
        print("OK: no floating Docker tags found in pi-setup/.")
        return 0

    print(
        "FAIL: floating Docker tag(s) detected in pi-setup/. Tasks #11 (FROM "
        "lines) and #9 (compose image: lines) require every base image to "
        "be pinned to a full point release (and a @sha256 digest). If a "
        "base image legitimately has no point-release tag, add "
        f"`# {ALLOW_MARKER}: <reason>` on the same or preceding line. See "
        "`Reproducible builds (pi-setup/)` in replit.md and "
        "`pi-setup/docs/REPRODUCIBILITY.md` for the bump procedure.",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    for f in findings:
        print(
            f"  {f.path}:{f.lineno}: [{f.kind}] {f.reason}",
            file=sys.stderr,
        )
        print(f"      {f.line}", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"{len(findings)} finding(s).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
