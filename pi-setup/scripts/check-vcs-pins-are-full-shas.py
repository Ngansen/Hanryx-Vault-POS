#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# check-vcs-pins-are-full-shas.py
#
# Why this exists
# ───────────────
# `pi-setup/requirements-vcs.txt` holds the git+ URLs that `uv pip compile`
# can't hash (currently just openai/CLIP). The whole reproducibility story
# for those deps rests on the URL ending in `@<full-40-char-commit-sha>` —
# a commit SHA IS a content hash, but ONLY if it's the full SHA. A branch
# name (`@main`), a tag (`@v1.0`), or a short SHA (`@dcba3cb`) all silently
# drift when upstream moves and break the byte-for-byte rebuild guarantee.
#
# This script enforces that every git+ pin in `requirements-vcs.txt` ends in
# a full 40-char lowercase hex SHA. It's wired into CI alongside the lockfile
# drift check so a sloppy bump fails the PR instead of the Pi rebuild weeks
# later.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VCS_FILE = REPO_ROOT / "pi-setup" / "requirements-vcs.txt"

# Matches the `@<ref>` suffix on any `git+http(s)://.../repo.git@<ref>` URL.
# We deliberately capture greedily up to whitespace, `#` (egg fragment), or
# end-of-line so that branch names with slashes, tags, and short SHAs are
# all caught and rejected.
GIT_PIN_RE = re.compile(r"git\+https?://\S+?\.git@([^\s#]+)")
FULL_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")


def main() -> int:
    if not VCS_FILE.is_file():
        print(f"[check-vcs-pins] FATAL: {VCS_FILE} not found", file=sys.stderr)
        return 1

    text = VCS_FILE.read_text()
    pins: list[str] = []
    for line in text.splitlines():
        # Skip comments — a `#` after the URL marks an inline comment, but
        # full-line comments shouldn't be scanned at all (they may legitimately
        # contain example branch names like `@main`).
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        for match in GIT_PIN_RE.finditer(line):
            pins.append(match.group(1))

    if not pins:
        # Nothing to check is a green result — `requirements-vcs.txt` may be
        # empty after a future cleanup. We don't want CI to start failing then.
        print(f"[check-vcs-pins] OK: no git+ pins found in {VCS_FILE.name}")
        return 0

    bad: list[str] = []
    for pin in pins:
        if FULL_SHA_RE.match(pin):
            print(f"[check-vcs-pins] OK: {pin} is a full 40-char hex SHA")
        else:
            print(
                f"[check-vcs-pins] FAIL: {pin!r} is not a full 40-char "
                f"lowercase hex commit SHA",
                file=sys.stderr,
            )
            bad.append(pin)

    if bad:
        print(
            f"\n[check-vcs-pins] {len(bad)} bad pin(s) in {VCS_FILE}.\n"
            "Replace each `@<ref>` with the full 40-char commit SHA — a "
            "branch name, tag, or short SHA cannot give us byte-for-byte "
            "reproducibility on the Pi.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
