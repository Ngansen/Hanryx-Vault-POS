#!/usr/bin/env python3
"""
check-no-insecure-tls.py
========================

Repository guard that scans ``pi-setup/`` for known patterns that disable
TLS / SSL certificate verification (the same class of issues Tasks #5, #8 and
#10 cleaned up). Fails with a non-zero exit code if any new occurrence
appears that is not explicitly approved with an allow-list marker.

Run from the repository root::

    python3 pi-setup/scripts/check-no-insecure-tls.py

Allow-listing an audited bypass
-------------------------------
If a debug-only bypass is genuinely required (it must be gated behind a
``HANRYX_DEBUG_INSECURE_*`` env var and log a warning per ``replit.md``), put
the marker ``hanryx-allow-insecure`` in a comment on the **same line** as the
match or on the line **immediately above** it. Optionally append a short
reason after a colon, e.g.::

    env["GIT_SSL_NO_VERIFY"] = "1"  # hanryx-allow-insecure: gated by HANRYX_DEBUG_INSECURE_GIT

Extending the patterns
----------------------
Add to ``INSECURE_PATTERNS`` below. Keep each pattern narrow enough to avoid
false positives but broad enough to catch the obvious shapes
(``verify=False``, ``curl -k``, ``--no-check-certificate``, etc.).
"""

from __future__ import annotations

import os
import re
import sys
from typing import Iterable, NamedTuple

ALLOW_MARKER = "hanryx-allow-insecure"

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

# File extensions we will scan. We deliberately stay text-only so we don't
# choke on binaries; files with no extension are scanned only if they look
# like text (best-effort UTF-8 decode).
TEXT_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".sh", ".bash", ".zsh",
    ".yml", ".yaml",
    ".conf", ".cfg", ".ini", ".toml",
    ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".json",
    ".md", ".rst", ".txt",
    ".service", ".env", ".env.example",
    ".html", ".htm",
    ".dockerfile",
})

# Filenames (case-insensitive) we always scan even without one of the above
# extensions.
ALWAYS_SCAN_FILENAMES: frozenset[str] = frozenset({
    "dockerfile",
    "makefile",
})


class Pattern(NamedTuple):
    name: str
    regex: re.Pattern[str]
    description: str


# Each entry is a (name, compiled regex, human description). Patterns are
# applied per-line so they can't span lines. Keep them surgical.
INSECURE_PATTERNS: tuple[Pattern, ...] = (
    Pattern(
        "verify=False",
        re.compile(r"\bverify\s*=\s*False\b"),
        "requests/httpx call with TLS verification disabled",
    ),
    Pattern(
        "curl --insecure / -k",
        # Catches `curl -k`, `curl --insecure`, and clustered short options
        # like `curl -fsSLk` / `curl -skLO`. To avoid false-positives on
        # English words like `-knock` / `-keep`, the cluster form requires
        # either a bare `-k` or a cluster that also contains an uppercase
        # letter (real curl clusters almost always do, e.g. -L, -S, -O).
        re.compile(
            r"\bcurl\b[^\n#]*?(?:\s|=)"
            r"(?:--insecure\b|-(?:k|(?=[A-Za-z]*[A-Z])[A-Za-z]*k[A-Za-z]*)\b)"
        ),
        "curl invoked with TLS verification disabled",
    ),
    Pattern(
        "wget --no-check-certificate",
        re.compile(r"--no-check-certificate\b"),
        "wget (or compatible) with TLS verification disabled",
    ),
    Pattern(
        "pip --trusted-host",
        re.compile(r"--trusted-host\b"),
        "pip install bypassing TLS for a host",
    ),
    Pattern(
        "docker --insecure-registry",
        re.compile(r"--insecure-registry\b"),
        "docker daemon configured to trust an insecure registry",
    ),
    Pattern(
        "GIT_SSL_NO_VERIFY",
        re.compile(r"\bGIT_SSL_NO_VERIFY\b"),
        "git configured to skip TLS verification",
    ),
    Pattern(
        "urllib3.disable_warnings",
        re.compile(r"\bdisable_warnings\s*\("),
        "urllib3 InsecureRequestWarning silenced (usually paired with verify=False)",
    ),
    Pattern(
        "ssl._create_unverified_context",
        re.compile(r"\b_create_unverified_context\b"),
        "ssl context created without certificate verification",
    ),
    Pattern(
        "check_hostname=False",
        re.compile(r"\bcheck_hostname\s*=\s*False\b"),
        "ssl context with hostname checking disabled",
    ),
    Pattern(
        "NODE_TLS_REJECT_UNAUTHORIZED",
        re.compile(r"\bNODE_TLS_REJECT_UNAUTHORIZED\b"),
        "Node.js TLS rejection disabled",
    ),
    Pattern(
        "PYTHONHTTPSVERIFY=0",
        re.compile(r"\bPYTHONHTTPSVERIFY\s*=\s*0\b|\bPYTHONHTTPSVERIFY\s*=\s*[\"']0[\"']"),
        "Python global HTTPS verification disabled",
    ),
)


class Finding(NamedTuple):
    path: str
    lineno: int
    pattern: str
    description: str
    line: str


def _should_skip_dir(rel_path: str) -> bool:
    parts = rel_path.split(os.sep)
    return any(p in SKIP_DIR_NAMES for p in parts)


def _is_scannable(filename: str) -> bool:
    name = filename.lower()
    if name in ALWAYS_SCAN_FILENAMES:
        return True
    # Dockerfile.foo / Dockerfile-bar style
    if name.startswith("dockerfile"):
        return True
    _, ext = os.path.splitext(name)
    return ext in TEXT_EXTENSIONS


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


def _line_is_allowed(lines: list[str], idx: int) -> bool:
    """The marker may sit on the same line or the line immediately above."""
    if ALLOW_MARKER in lines[idx]:
        return True
    if idx > 0 and ALLOW_MARKER in lines[idx - 1]:
        return True
    return False


def _scan_file(path: str, rel_path: str) -> Iterable[Finding]:
    lines = _read_text(path)
    if lines is None:
        return ()
    out: list[Finding] = []
    for idx, line in enumerate(lines):
        # Cheap filter: skip lines that obviously can't contain any of our
        # patterns to keep the inner loop fast on large files (server.py is
        # ~24k lines).
        for pat in INSECURE_PATTERNS:
            if pat.regex.search(line):
                if _line_is_allowed(lines, idx):
                    continue
                out.append(Finding(
                    path=rel_path,
                    lineno=idx + 1,
                    pattern=pat.name,
                    description=pat.description,
                    line=line.rstrip(),
                ))
    return out


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
            if not _is_scannable(fn):
                continue
            full = os.path.join(dirpath, fn)
            # The checker itself contains every pattern as a string literal —
            # skip it explicitly rather than littering it with allow markers.
            if os.path.abspath(full) == SELF_PATH:
                continue
            rel = os.path.relpath(full, start=os.getcwd())
            findings.extend(_scan_file(full, rel))
    return findings


def main(argv: list[str]) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    pi_setup = os.path.abspath(os.path.join(here, ".."))
    if not os.path.isdir(pi_setup):
        print(f"error: pi-setup/ not found at {pi_setup}", file=sys.stderr)
        return 2

    findings = scan(pi_setup)

    if not findings:
        print("OK: no insecure-TLS patterns found in pi-setup/.")
        return 0

    print(
        "FAIL: insecure-TLS pattern(s) detected in pi-setup/. "
        "If genuinely needed, gate behind a HANRYX_DEBUG_INSECURE_* env var, "
        f"log a warning, and add `# {ALLOW_MARKER}: <reason>` on the same or "
        "preceding line. See `Security Policy — TLS verification` in replit.md.",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    for f in findings:
        print(
            f"  {f.path}:{f.lineno}: [{f.pattern}] {f.description}",
            file=sys.stderr,
        )
        print(f"      {f.line}", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"{len(findings)} finding(s).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
