#!/usr/bin/env python3
"""
check-no-plaintext-http.py
==========================

Repository guard that scans ``pi-setup/`` for plaintext ``http://`` URLs that
target *external* hosts. Complements ``check-no-insecure-tls.py`` (Task #12),
which only catches code that explicitly disables TLS verification — this
checker catches code that never attempts TLS in the first place
(``requests.get("http://api.example.com")`` and friends), which is just as
exploitable on a hostile network like trade-show Wi-Fi.

Run from the repository root::

    python3 pi-setup/scripts/check-no-plaintext-http.py

Internal hosts are allow-listed by default
------------------------------------------
The pi-setup deliberately talks to its own services over the Docker network
and the LAN/VPN using plaintext (e.g. ``http://storefront:3000``,
``http://pos:8080``, ``http://localhost``, ``http://127.0.0.1``,
``http://10.10.0.1``). Those internal URLs are *not* findings. A host is
considered internal when its hostname matches any of:

* loopback / unspecified — ``localhost``, ``127.0.0.1``, ``0.0.0.0``, ``::1``
* RFC 1918 private IPv4 — ``10/8``, ``172.16/12``, ``192.168/16``
* link-local IPv4 — ``169.254/16``
* CGNAT (Tailscale) IPv4 — ``100.64/10``
* mDNS — anything ending in ``.local``
* Internal DNS — anything ending in ``.internal``
* Tailscale magic DNS — anything ending in ``.ts.net``
* a bare hostname with no dots (treated as a Docker service name like
  ``pos``, ``storefront``, ``db``, ``redis``, ``pgbouncer``, ``mainpi``)
* a host that contains a shell / template variable (``${VAR}``, ``$(cmd)``,
  ``$VAR``, ``<PLACEHOLDER>``, ``{var}``, ``%s``) — these are dynamic and
  almost always populated with internal values in this codebase

XML namespace URLs (``xmlns="http://www.w3.org/..."`` etc.) are not network
calls and are also skipped.

Allow-listing an audited bypass
-------------------------------
If a *real* external plaintext URL is genuinely required (e.g. NetworkManager's
captive-portal probe, which must be HTTP by design), put either of these
markers in a comment on the **same line** as the match or on the line
**immediately above** it:

* ``hanryx-allow-plaintext`` (preferred — specific to this checker)
* ``hanryx-allow-insecure`` (the marker used by ``check-no-insecure-tls.py``;
  honoured here so a single comment can satisfy both checks)

Optionally append a short reason after a colon, e.g.::

    uri=http://connectivity-check.ubuntu.com  # hanryx-allow-plaintext: NM captive-portal probe
"""

from __future__ import annotations

import ipaddress
import os
import re
import sys
from typing import Iterable, NamedTuple

ALLOW_MARKERS: tuple[str, ...] = ("hanryx-allow-plaintext", "hanryx-allow-insecure")

# Directories under pi-setup/ that are not our source code (vendored, build
# output, cached external clones, runtime data, etc.) and so must be skipped
# wholesale. Mirrors the sibling ``check-no-insecure-tls.py`` skip list.
SKIP_DIR_NAMES: frozenset[str] = frozenset({
    "__pycache__",
    "node_modules",
    ".git",
    "dist",
    "build",
    "data",
    "storefront-src",  # external repo cloned at build time
})

# File extensions we will scan. Same set as the sibling TLS checker so the
# two guards have identical coverage.
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

ALWAYS_SCAN_FILENAMES: frozenset[str] = frozenset({
    "dockerfile",
    "makefile",
})

# Hostnames (lowercase, exact match) that are never findings.
INTERNAL_HOSTNAMES: frozenset[str] = frozenset({
    "localhost",
    "ip6-localhost",
    "ip6-loopback",
})

# Hostname suffixes (lowercase, must match end of host) that are internal.
# Note: a leading dot is required so e.g. ``.local`` does not match ``cool``.
INTERNAL_SUFFIXES: tuple[str, ...] = (
    ".local",
    ".internal",
    ".ts.net",       # Tailscale magic DNS
    ".lan",          # common home-router suffix
    ".home.arpa",    # RFC 8375
)

# URL-host prefixes that are XML / XSD namespace identifiers, not real
# network calls. ``xmlns="http://www.w3.org/2000/svg"`` is the canonical
# example. Match is on the host portion only.
XML_NAMESPACE_HOSTS: frozenset[str] = frozenset({
    "www.w3.org",
    "schemas.xmlsoap.org",
    "schemas.openxmlformats.org",
    "schemas.microsoft.com",
    "purl.org",
    "ns.adobe.com",
})

# Characters that, if present in the host portion, mark it as a dynamic
# placeholder that we cannot statically resolve. We treat all such URLs as
# internal because in this codebase every templated URL we found expands to
# an internal host (see ``setup-satellite-kiosk-boot.sh`` etc.).
PLACEHOLDER_CHARS: frozenset[str] = frozenset("${}<>%()*")

# URL extractor. We look for ``http://`` (case-insensitive) followed by host
# characters, optionally a port, and stop at any character that cannot
# appear in a host:port. The character class deliberately includes ``$ { }
# < > % ( )`` so we can detect templated hosts and treat them as
# placeholders rather than truncating mid-variable.
URL_RE = re.compile(
    r"\bhttp://"
    r"(?P<host>[A-Za-z0-9._\-\$\{\}<>%\(\)\*]+)"
    r"(?::(?P<port>[0-9A-Za-z\$\{\}<>%\(\)\*_]+))?",
    re.IGNORECASE,
)


class Finding(NamedTuple):
    path: str
    lineno: int
    host: str
    line: str


def _should_skip_dir(rel_path: str) -> bool:
    parts = rel_path.split(os.sep)
    return any(p in SKIP_DIR_NAMES for p in parts)


def _is_scannable(filename: str) -> bool:
    name = filename.lower()
    if name in ALWAYS_SCAN_FILENAMES:
        return True
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
    """Either marker may sit on the same line or the line immediately above."""
    line = lines[idx]
    if any(m in line for m in ALLOW_MARKERS):
        return True
    if idx > 0:
        prev = lines[idx - 1]
        if any(m in prev for m in ALLOW_MARKERS):
            return True
    return False


def _is_private_ipv4(host: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(host)
    except (ipaddress.AddressValueError, ValueError):
        return False
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified:
        return True
    # Tailscale CGNAT block 100.64.0.0/10 — Python flags this as ``is_global``
    # = False / ``is_private`` = False, so check explicitly.
    if ipaddress.IPv4Address("100.64.0.0") <= ip <= ipaddress.IPv4Address("100.127.255.255"):
        return True
    return False


def _is_ipv6_loopback_or_private(host: str) -> bool:
    # IPv6 literals in URLs are wrapped in ``[]``; strip them before parsing.
    h = host.strip("[]")
    try:
        ip = ipaddress.IPv6Address(h)
    except (ipaddress.AddressValueError, ValueError):
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified


def _is_internal_host(host: str) -> bool:
    if not host:
        return True  # malformed match — let it through, not our concern
    h = host.lower().rstrip(".")
    if any(c in PLACEHOLDER_CHARS for c in h):
        return True
    if h in INTERNAL_HOSTNAMES:
        return True
    if any(h == suf.lstrip(".") or h.endswith(suf) for suf in INTERNAL_SUFFIXES):
        return True
    if _is_private_ipv4(h):
        return True
    if _is_ipv6_loopback_or_private(h):
        return True
    # Bare hostname with no dots — treated as a Docker service name / LAN
    # short hostname (``pos``, ``storefront``, ``db``, ``mainpi``, etc.).
    if "." not in h:
        return True
    return False


def _is_xml_namespace_host(host: str) -> bool:
    h = host.lower()
    return h in XML_NAMESPACE_HOSTS


SELF_PATH = os.path.abspath(__file__)


def _scan_file(path: str, rel_path: str) -> Iterable[Finding]:
    lines = _read_text(path)
    if lines is None:
        return ()
    out: list[Finding] = []
    for idx, line in enumerate(lines):
        # Cheap pre-filter — most lines have no http:// at all.
        if "http://" not in line and "HTTP://" not in line:
            continue
        for match in URL_RE.finditer(line):
            host = match.group("host") or ""
            if _is_internal_host(host):
                continue
            if _is_xml_namespace_host(host):
                continue
            if _line_is_allowed(lines, idx):
                continue
            out.append(Finding(
                path=rel_path,
                lineno=idx + 1,
                host=host,
                line=line.rstrip(),
            ))
    return out


def scan(root: str) -> list[Finding]:
    findings: list[Finding] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir != "." and _should_skip_dir(rel_dir):
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        for fn in filenames:
            if not _is_scannable(fn):
                continue
            full = os.path.join(dirpath, fn)
            # Skip self — this file contains every example pattern as a
            # string literal which would otherwise produce noise.
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
        print("OK: no plaintext-http external URLs found in pi-setup/.")
        return 0

    print(
        "FAIL: plaintext http:// call(s) to external host(s) detected in "
        "pi-setup/. Switch the URL to https:// (preferred) or, if the host "
        "genuinely cannot speak TLS, add a `# hanryx-allow-plaintext: "
        "<reason>` marker on the same or preceding line. See "
        "`Security Policy — TLS verification` in replit.md.",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    for f in findings:
        print(
            f"  {f.path}:{f.lineno}: external plaintext http:// → {f.host}",
            file=sys.stderr,
        )
        print(f"      {f.line}", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"{len(findings)} finding(s).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
