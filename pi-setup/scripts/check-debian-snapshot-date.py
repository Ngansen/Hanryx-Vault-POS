#!/usr/bin/env python3
"""
check-debian-snapshot-date.py
=============================

Repository guard that verifies every ``APT_SNAPSHOT_DATE`` build arg in
``pi-setup/`` actually exists on
`snapshot.debian.org <https://snapshot.debian.org/>`_ for both the
``debian`` and ``debian-security`` archives. Fails with a non-zero exit
code if any URL returns a non-2xx HTTP status.

Why
---
The three Debian-based images (``pi-setup/Dockerfile``,
``pi-setup/recognizer/Dockerfile``,
``pi-setup/services/storefront/Dockerfile``) pin all ``apt-get install``
calls to ``snapshot.debian.org`` at a specific point in time via the
``APT_SNAPSHOT_DATE`` build arg (see
``pi-setup/docs/REPRODUCIBILITY.md`` §2 for the bump procedure). The
date is chosen by hand when bumping, and a typo, an off-by-one weekend,
or a future snapshot.debian.org pruning of the chosen date will silently
break **every** ``docker compose build`` of those images on the Pi with
a 404 from the snapshot mirror — and the maintainer only finds out once
they're SSH'd into the Pi trying to deploy.

This guard catches the bad date in CI before it ships.

What the rule is
----------------
Each Dockerfile under ``pi-setup/`` is parsed for an
``ARG APT_SNAPSHOT_DATE=<YYYYMMDDTHHMMSSZ>`` line. Each unique date is
verified by a `HEAD` request against:

* ``https://snapshot.debian.org/archive/debian/<DATE>/``
* ``https://snapshot.debian.org/archive/debian-security/<DATE>/``

Both must return HTTP 2xx. Anything else (404, 5xx, network error after
retries) is a finding.

Run from the repository root::

    python3 pi-setup/scripts/check-debian-snapshot-date.py

Notes
-----
* Dockerfiles that don't define ``APT_SNAPSHOT_DATE`` (e.g. the Alpine
  ``pi-setup/pokeapi/Dockerfile``) are silently ignored — the pin only
  applies to the Debian-based images.
* The check is network-dependent. Each URL is retried with exponential
  backoff before being declared a failure, so a transient
  snapshot.debian.org hiccup doesn't fail CI.
* If snapshot.debian.org is fully unreachable from CI (DNS or network
  outage), the script also fails — that's the safe default. Override
  with ``--skip-on-network-error`` if a maintainer needs to bypass for a
  known mirror outage (use sparingly; this is the only thing standing
  between us and a broken Pi rebuild).
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from typing import NamedTuple

# Match `ARG APT_SNAPSHOT_DATE=<value>` (with optional surrounding whitespace
# and an optional inline `# comment`). Dockerfile ARG values may be quoted,
# but in practice ours never are; we accept either form for safety.
_ARG_RE = re.compile(
    r"^\s*ARG\s+APT_SNAPSHOT_DATE\s*=\s*([^\s#]+)\s*(?:#.*)?$",
    re.IGNORECASE,
)

# snapshot.debian.org dates are `YYYYMMDDTHHMMSSZ` (RFC 3339 basic-format
# UTC, no separators). Anything else is almost certainly a typo and would
# 404 anyway — fail fast with a clearer message.
_DATE_RE = re.compile(r"^\d{8}T\d{6}Z$")

SNAPSHOT_URLS = (
    "https://snapshot.debian.org/archive/debian/{date}/",
    "https://snapshot.debian.org/archive/debian-security/{date}/",
)

# Per-request HTTP timeout (seconds). snapshot.debian.org is occasionally
# slow under load; this is generous enough to absorb that without making
# CI hang for minutes if the host is genuinely down.
HTTP_TIMEOUT_S = 30.0

# Retry budget per URL. Total worst-case wall time per URL is roughly
# HTTP_TIMEOUT_S * MAX_ATTEMPTS + sum(BACKOFF_SCHEDULE_S).
MAX_ATTEMPTS = 4
BACKOFF_SCHEDULE_S = (2.0, 5.0, 10.0)  # waits between attempts 1→2, 2→3, 3→4

USER_AGENT = (
    "hanryx-pi-setup-snapshot-check/1.0 "
    "(+https://github.com/Ngansen/hanryx; CI guard for APT_SNAPSHOT_DATE)"
)


class DateUse(NamedTuple):
    date: str
    dockerfile_rel: str
    lineno: int


class Finding(NamedTuple):
    date: str
    url: str
    detail: str
    used_by: list[DateUse]


def _find_dockerfiles(root: str) -> list[str]:
    """Return absolute paths of every Dockerfile under ``root``."""
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip vendored / build / cache trees the same way the other
        # pi-setup checkers do.
        dirnames[:] = [
            d for d in dirnames
            if d not in {"__pycache__", "node_modules", ".git", "dist",
                          "build", "data", "storefront-src"}
        ]
        for fn in filenames:
            lower = fn.lower()
            if (
                lower == "dockerfile"
                or lower.startswith("dockerfile.")
                or lower.startswith("dockerfile-")
                or lower.endswith(".dockerfile")
            ):
                out.append(os.path.join(dirpath, fn))
    out.sort()
    return out


def _extract_dates(dockerfile_path: str, repo_root: str) -> list[DateUse]:
    """Find every ``ARG APT_SNAPSHOT_DATE=<value>`` line in the file.

    A multi-stage Dockerfile re-declares the ARG in each stage (so each
    stage gets its own default). We surface every occurrence so a stage
    that drifted from the others is flagged too.
    """
    rel = os.path.relpath(dockerfile_path, repo_root)
    try:
        with open(dockerfile_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except (OSError, UnicodeDecodeError):
        return []
    out: list[DateUse] = []
    for idx, line in enumerate(lines):
        m = _ARG_RE.match(line)
        if not m:
            continue
        out.append(DateUse(date=m.group(1), dockerfile_rel=rel, lineno=idx + 1))
    return out


def _http_head_ok(url: str) -> tuple[bool, str]:
    """HEAD ``url`` and return ``(ok, detail)``.

    ``ok`` is True iff the server returned a 2xx status. ``detail`` is a
    short human-readable description used in failure output (status code
    + reason, or the network error). Retries with exponential backoff.
    """
    last_detail = "no attempts made"
    for attempt in range(MAX_ATTEMPTS):
        if attempt > 0:
            time.sleep(BACKOFF_SCHEDULE_S[min(attempt - 1, len(BACKOFF_SCHEDULE_S) - 1)])
        req = urllib.request.Request(
            url,
            method="HEAD",
            headers={"User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                status = resp.status
                if 200 <= status < 300:
                    return True, f"HTTP {status}"
                last_detail = f"HTTP {status} {resp.reason or ''}".strip()
        except urllib.error.HTTPError as e:
            # 4xx (incl. 404 for a missing snapshot date) and 5xx land here.
            # Retry 5xx; 4xx is terminal — a missing date won't appear later.
            last_detail = f"HTTP {e.code} {e.reason or ''}".strip()
            if 400 <= e.code < 500:
                return False, last_detail
        except urllib.error.URLError as e:
            last_detail = f"network error: {e.reason}"
        except (TimeoutError, socket.timeout):
            last_detail = f"timeout after {HTTP_TIMEOUT_S:.0f}s"
        except OSError as e:
            last_detail = f"network error: {e}"
    return False, last_detail


def _format_uses(uses: list[DateUse]) -> str:
    return ", ".join(f"{u.dockerfile_rel}:{u.lineno}" for u in uses)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify every APT_SNAPSHOT_DATE in pi-setup/ Dockerfiles "
            "actually exists on snapshot.debian.org."
        ),
    )
    parser.add_argument(
        "--skip-on-network-error",
        action="store_true",
        help=(
            "Pass (with a warning) if snapshot.debian.org is unreachable "
            "for ALL probes (DNS / total network outage). A specific 4xx "
            "for a date still fails. Use sparingly — this guard is the "
            "only thing that catches a bad date before the Pi rebuild."
        ),
    )
    args = parser.parse_args(argv)

    here = os.path.dirname(os.path.abspath(__file__))
    pi_setup = os.path.abspath(os.path.join(here, ".."))
    repo_root = os.path.abspath(os.path.join(pi_setup, ".."))
    if not os.path.isdir(pi_setup):
        print(f"error: pi-setup/ not found at {pi_setup}", file=sys.stderr)
        return 2

    dockerfiles = _find_dockerfiles(pi_setup)
    if not dockerfiles:
        print(f"error: no Dockerfiles under {pi_setup}", file=sys.stderr)
        return 2

    # Collect every (date, dockerfile, line) and group by unique date so we
    # only hit each URL once even if multiple stages / Dockerfiles share it.
    all_uses: list[DateUse] = []
    for df in dockerfiles:
        all_uses.extend(_extract_dates(df, repo_root))

    if not all_uses:
        print(
            "OK: no APT_SNAPSHOT_DATE pins found in pi-setup/ "
            "(nothing to verify).",
        )
        return 0

    by_date: dict[str, list[DateUse]] = {}
    for u in all_uses:
        by_date.setdefault(u.date, []).append(u)

    findings: list[Finding] = []
    network_errors_only = True

    for date in sorted(by_date):
        uses = by_date[date]
        if not _DATE_RE.match(date):
            findings.append(Finding(
                date=date,
                url="(local validation)",
                detail=(
                    f"APT_SNAPSHOT_DATE={date!r} is not in YYYYMMDDTHHMMSSZ "
                    "format (e.g. 20260601T000000Z)"
                ),
                used_by=uses,
            ))
            network_errors_only = False
            continue
        for tmpl in SNAPSHOT_URLS:
            url = tmpl.format(date=date)
            print(f"checking {url} ... ", end="", flush=True)
            ok, detail = _http_head_ok(url)
            print(detail if ok else f"FAIL ({detail})")
            if not ok:
                if not detail.startswith("network error") and not detail.startswith("timeout"):
                    network_errors_only = False
                findings.append(Finding(
                    date=date, url=url, detail=detail, used_by=uses,
                ))

    if not findings:
        print("")
        print(
            f"OK: all {len(by_date)} unique APT_SNAPSHOT_DATE value(s) "
            "verified on snapshot.debian.org.",
        )
        return 0

    if network_errors_only and args.skip_on_network_error:
        print("", file=sys.stderr)
        print(
            "WARN: snapshot.debian.org appears unreachable; passing because "
            "--skip-on-network-error is set. The following probes failed:",
            file=sys.stderr,
        )
        for f in findings:
            print(
                f"  - {f.url}: {f.detail} (used by {_format_uses(f.used_by)})",
                file=sys.stderr,
            )
        return 0

    print("", file=sys.stderr)
    print(
        "FAIL: APT_SNAPSHOT_DATE verification failed. The Pi's Debian-based "
        "images pin apt to snapshot.debian.org at this date — a missing or "
        "unreachable snapshot will break every `docker compose build` of "
        "pos / recognizer / storefront on the Pi. Pick a different date "
        "(see `pi-setup/docs/REPRODUCIBILITY.md` §2 for the bump procedure) "
        "or, if this is a transient mirror outage you've confirmed by hand, "
        "re-run with --skip-on-network-error.",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    for f in findings:
        print(
            f"  date {f.date}  ->  {f.url}",
            file=sys.stderr,
        )
        print(f"      {f.detail}", file=sys.stderr)
        print(
            f"      used by: {_format_uses(f.used_by)}",
            file=sys.stderr,
        )
    print("", file=sys.stderr)
    print(f"{len(findings)} finding(s).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
