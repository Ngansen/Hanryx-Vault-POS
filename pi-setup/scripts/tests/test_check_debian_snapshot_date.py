"""
Unit tests for ``pi-setup/scripts/check-debian-snapshot-date.py``.

The checker is a CI guard against a bad ``APT_SNAPSHOT_DATE`` pin slipping
into one of the Debian-based images (pos / recognizer / storefront). It
makes several silent classification decisions:

* The Dockerfile parser (``_extract_dates``) — must catch every
  ``ARG APT_SNAPSHOT_DATE=...`` line in single-stage and multi-stage
  Dockerfiles, including inline ``#`` comments and quoted values, AND
  must ignore Dockerfiles that don't pin the date at all (e.g. the
  Alpine pokeapi image).
* The date-format validator (``_DATE_RE``) — must reject anything that
  isn't ``YYYYMMDDTHHMMSSZ`` *before* a network call goes out, so a
  hand-typo like ``2026-04-15`` fails fast with a clear message rather
  than a confusing 404.
* The HTTP layer (``_http_head_ok``) — must succeed on 2xx, fail
  *immediately* on 4xx (no retries — a missing snapshot date won't
  appear later), and retry on 5xx / network errors up to
  ``MAX_ATTEMPTS``.
* The ``--skip-on-network-error`` escape hatch — must pass with a
  warning when *every* probe failed with a network/timeout error, but
  must NOT pass if any probe got a real 4xx/5xx response (those are
  genuine bad-date signals, not mirror outages).

A future refactor that broke any of those branches would silently stop
catching bad dates, and we'd only find out the next time a build 404'd
on the Pi. These tests pin the contract end-to-end.

Runnable both with ``pytest`` and with ``python -m unittest`` so the CI
job needs no third-party dependency installed.
"""

from __future__ import annotations

import importlib.util
import io
import os
import socket
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


# The script's filename has hyphens, so a normal ``import`` won't work.
# Load it via importlib so we can call the private helpers directly with
# no subprocess plumbing.
_HERE = Path(__file__).resolve().parent
_CHECKER_PATH = _HERE.parent / "check-debian-snapshot-date.py"

_spec = importlib.util.spec_from_file_location(
    "check_debian_snapshot_date", _CHECKER_PATH
)
assert _spec is not None and _spec.loader is not None, (
    f"cannot load {_CHECKER_PATH}"
)
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)


# A valid snapshot.debian.org timestamp shape used across the tests.
GOOD_DATE = "20260415T000000Z"
GOOD_DATE_2 = "20260601T120000Z"


def _write_dockerfile(dirpath: str, name: str, body: str) -> str:
    """Write ``body`` into ``dirpath/name`` and return the absolute path."""
    p = os.path.join(dirpath, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(body)
    return p


# ---------------------------------------------------------------------------
# Dockerfile parser
# ---------------------------------------------------------------------------


class ExtractDatesTests(unittest.TestCase):
    """``_extract_dates`` must find every ARG line, skip everything else."""

    def test_single_stage_dockerfile(self) -> None:
        body = (
            "FROM debian:bookworm-slim\n"
            f"ARG APT_SNAPSHOT_DATE={GOOD_DATE}\n"
            "RUN apt-get update\n"
        )
        with tempfile.TemporaryDirectory() as td:
            df = _write_dockerfile(td, "Dockerfile", body)
            uses = checker._extract_dates(df, td)
        self.assertEqual([u.date for u in uses], [GOOD_DATE])
        self.assertEqual(uses[0].lineno, 2)
        self.assertEqual(uses[0].dockerfile_rel, "Dockerfile")

    def test_multi_stage_dockerfile_picks_up_every_stage(self) -> None:
        # Multi-stage files re-declare the ARG in each stage so each stage
        # gets its own default; we want EVERY occurrence so a stage that
        # drifted from the others gets flagged.
        body = (
            "FROM debian:bookworm-slim AS builder\n"
            f"ARG APT_SNAPSHOT_DATE={GOOD_DATE}\n"
            "RUN apt-get update\n"
            "\n"
            "FROM debian:bookworm-slim AS runtime\n"
            f"ARG APT_SNAPSHOT_DATE={GOOD_DATE_2}\n"
            "RUN apt-get update\n"
        )
        with tempfile.TemporaryDirectory() as td:
            df = _write_dockerfile(td, "Dockerfile", body)
            uses = checker._extract_dates(df, td)
        self.assertEqual([u.date for u in uses], [GOOD_DATE, GOOD_DATE_2])
        self.assertEqual([u.lineno for u in uses], [2, 6])

    def test_inline_comment_after_value_is_stripped(self) -> None:
        body = (
            "FROM debian:bookworm-slim\n"
            f"ARG APT_SNAPSHOT_DATE={GOOD_DATE}  # bumped 2026-04-15\n"
        )
        with tempfile.TemporaryDirectory() as td:
            df = _write_dockerfile(td, "Dockerfile", body)
            uses = checker._extract_dates(df, td)
        self.assertEqual(len(uses), 1)
        # The captured date must NOT include the trailing comment.
        self.assertEqual(uses[0].date, GOOD_DATE)

    def test_leading_indentation_is_tolerated(self) -> None:
        # We don't normally indent ARG, but the regex tolerates leading
        # whitespace so a stray space doesn't make the guard miss a pin.
        body = (
            "FROM debian:bookworm-slim\n"
            f"   ARG APT_SNAPSHOT_DATE={GOOD_DATE}\n"
        )
        with tempfile.TemporaryDirectory() as td:
            df = _write_dockerfile(td, "Dockerfile", body)
            uses = checker._extract_dates(df, td)
        self.assertEqual(len(uses), 1)
        self.assertEqual(uses[0].date, GOOD_DATE)

    def test_double_quoted_value_strips_quotes(self) -> None:
        # Per `man dockerfile`, ARG values may be quoted. The parser
        # must strip the surrounding quotes so the captured date is the
        # bare YYYYMMDDTHHMMSSZ string — otherwise downstream date
        # validation would (wrongly) flag a perfectly valid quoted pin
        # as malformed.
        body = (
            "FROM debian:bookworm-slim\n"
            f'ARG APT_SNAPSHOT_DATE="{GOOD_DATE}"\n'
        )
        with tempfile.TemporaryDirectory() as td:
            df = _write_dockerfile(td, "Dockerfile", body)
            uses = checker._extract_dates(df, td)
        self.assertEqual(len(uses), 1)
        self.assertEqual(uses[0].date, GOOD_DATE)

    def test_single_quoted_value_strips_quotes(self) -> None:
        # Same as above but with single quotes — also legal per the
        # Dockerfile spec.
        body = (
            "FROM debian:bookworm-slim\n"
            f"ARG APT_SNAPSHOT_DATE='{GOOD_DATE}'\n"
        )
        with tempfile.TemporaryDirectory() as td:
            df = _write_dockerfile(td, "Dockerfile", body)
            uses = checker._extract_dates(df, td)
        self.assertEqual(len(uses), 1)
        self.assertEqual(uses[0].date, GOOD_DATE)

    def test_double_quoted_value_with_inline_comment(self) -> None:
        # Belt-and-braces: quoting + inline comment together. The value
        # group must capture only what's inside the quotes; the trailing
        # comment must be discarded.
        body = (
            "FROM debian:bookworm-slim\n"
            f'ARG APT_SNAPSHOT_DATE="{GOOD_DATE}"  # bumped 2026-04-15\n'
        )
        with tempfile.TemporaryDirectory() as td:
            df = _write_dockerfile(td, "Dockerfile", body)
            uses = checker._extract_dates(df, td)
        self.assertEqual(len(uses), 1)
        self.assertEqual(uses[0].date, GOOD_DATE)

    def test_quoted_value_passes_end_to_end_date_validation(self) -> None:
        # End-to-end sanity: a perfectly valid date that happens to be
        # double-quoted must NOT trigger the date-format finding. This
        # is the regression test for the bug where the parser captured
        # the quotes as part of the value, which then failed `_DATE_RE`
        # validation as malformed.
        body = (
            "FROM debian:bookworm-slim\n"
            f'ARG APT_SNAPSHOT_DATE="{GOOD_DATE}"\n'
        )
        with tempfile.TemporaryDirectory() as td:
            df = _write_dockerfile(td, "Dockerfile", body)
            uses = checker._extract_dates(df, td)
        self.assertEqual(len(uses), 1)
        # The captured date must be in the canonical form so the
        # downstream `_DATE_RE` check passes.
        self.assertIsNotNone(checker._DATE_RE.match(uses[0].date))

    def test_arg_keyword_is_case_insensitive(self) -> None:
        # Dockerfile instructions are case-insensitive per the spec; the
        # regex sets ``re.IGNORECASE`` so a lowercase ``arg`` still
        # counts.
        body = f"arg APT_SNAPSHOT_DATE={GOOD_DATE}\n"
        with tempfile.TemporaryDirectory() as td:
            df = _write_dockerfile(td, "Dockerfile", body)
            uses = checker._extract_dates(df, td)
        self.assertEqual(len(uses), 1)
        self.assertEqual(uses[0].date, GOOD_DATE)

    def test_dockerfile_with_no_snapshot_date_is_ignored(self) -> None:
        # Mirrors the real Alpine pokeapi image: pins no date, must not
        # appear in the parsed output (and so won't trigger any HTTP
        # check downstream).
        body = (
            "FROM alpine:3.19\n"
            "RUN apk add --no-cache curl\n"
            "ARG SOMETHING_ELSE=hello\n"
        )
        with tempfile.TemporaryDirectory() as td:
            df = _write_dockerfile(td, "Dockerfile", body)
            uses = checker._extract_dates(df, td)
        self.assertEqual(uses, [])

    def test_similarly_named_arg_is_not_matched(self) -> None:
        # Defensive: ``APT_SNAPSHOT_DATE_OVERRIDE`` (or any longer name
        # that starts with the same prefix) must NOT be picked up as a
        # snapshot-date pin — different semantic.
        body = (
            "FROM debian:bookworm-slim\n"
            f"ARG APT_SNAPSHOT_DATE_OVERRIDE={GOOD_DATE}\n"
        )
        with tempfile.TemporaryDirectory() as td:
            df = _write_dockerfile(td, "Dockerfile", body)
            uses = checker._extract_dates(df, td)
        self.assertEqual(uses, [])

    def test_unreadable_file_returns_empty_list(self) -> None:
        # A malformed (non-utf8) file should not crash the whole scan;
        # the function returns [] and the file is silently skipped.
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "Dockerfile")
            with open(p, "wb") as fh:
                # 0xFF is not valid utf-8 anywhere in the byte stream.
                fh.write(b"\xff\xfe\x00bad\n")
            uses = checker._extract_dates(p, td)
        self.assertEqual(uses, [])


# ---------------------------------------------------------------------------
# Date-format validation (the cheap pre-flight check)
# ---------------------------------------------------------------------------


class DateFormatValidationTests(unittest.TestCase):
    """``_DATE_RE`` rejects malformed values before any network call."""

    def test_canonical_form_accepted(self) -> None:
        self.assertIsNotNone(checker._DATE_RE.match(GOOD_DATE))
        self.assertIsNotNone(checker._DATE_RE.match("20260601T235959Z"))

    def test_iso_dashes_rejected(self) -> None:
        # The most common typo when bumping by hand.
        self.assertIsNone(checker._DATE_RE.match("2026-04-15"))
        self.assertIsNone(checker._DATE_RE.match("2026-04-15T00:00:00Z"))

    def test_date_only_no_time_rejected(self) -> None:
        # ``20260415`` would 404 anyway; reject locally for a clearer
        # error message than "HTTP 404".
        self.assertIsNone(checker._DATE_RE.match("20260415"))

    def test_empty_string_rejected(self) -> None:
        self.assertIsNone(checker._DATE_RE.match(""))

    def test_missing_trailing_z_rejected(self) -> None:
        # snapshot.debian.org URLs require the trailing Z.
        self.assertIsNone(checker._DATE_RE.match("20260415T000000"))

    def test_lowercase_separators_rejected(self) -> None:
        # ``t`` instead of ``T`` would 404 on the mirror.
        self.assertIsNone(checker._DATE_RE.match("20260415t000000z"))

    def test_extra_trailing_garbage_rejected(self) -> None:
        self.assertIsNone(checker._DATE_RE.match(GOOD_DATE + "/"))


# ---------------------------------------------------------------------------
# HTTP layer (mocked)
# ---------------------------------------------------------------------------


def _http_response(status: int, reason: str = "OK"):
    """Build a context-manager-compatible fake urlopen response."""
    fake = mock.MagicMock()
    fake.status = status
    fake.reason = reason
    cm = mock.MagicMock()
    cm.__enter__.return_value = fake
    cm.__exit__.return_value = False
    return cm


def _http_error(code: int, reason: str = "Not Found") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://example.invalid/",
        code=code,
        msg=reason,
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b""),
    )


class HttpHeadOkTests(unittest.TestCase):
    """``_http_head_ok`` is the only thing that touches the network."""

    def test_200_passes_on_first_attempt(self) -> None:
        with mock.patch.object(checker.urllib.request, "urlopen") as urlopen, \
             mock.patch.object(checker.time, "sleep") as sleep:
            urlopen.return_value = _http_response(200, "OK")
            ok, detail = checker._http_head_ok("https://snapshot.debian.org/x/")
        self.assertTrue(ok)
        self.assertIn("200", detail)
        self.assertEqual(urlopen.call_count, 1)
        # No retry → no backoff sleep.
        sleep.assert_not_called()

    def test_404_fails_immediately_without_retry(self) -> None:
        # A missing snapshot date won't appear later; retrying would
        # just waste CI time. The script must give up after the first
        # 4xx response.
        with mock.patch.object(checker.urllib.request, "urlopen") as urlopen, \
             mock.patch.object(checker.time, "sleep") as sleep:
            urlopen.side_effect = _http_error(404, "Not Found")
            ok, detail = checker._http_head_ok("https://snapshot.debian.org/x/")
        self.assertFalse(ok)
        self.assertIn("404", detail)
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_503_retries_then_fails(self) -> None:
        # 5xx is potentially transient → retry up to MAX_ATTEMPTS.
        with mock.patch.object(checker.urllib.request, "urlopen") as urlopen, \
             mock.patch.object(checker.time, "sleep") as sleep:
            urlopen.side_effect = _http_error(503, "Service Unavailable")
            ok, detail = checker._http_head_ok("https://snapshot.debian.org/x/")
        self.assertFalse(ok)
        self.assertIn("503", detail)
        self.assertEqual(urlopen.call_count, checker.MAX_ATTEMPTS)
        # One sleep between each pair of attempts.
        self.assertEqual(sleep.call_count, checker.MAX_ATTEMPTS - 1)

    def test_503_then_200_recovers(self) -> None:
        # If the mirror flakes once and then comes back, we must NOT
        # surface a finding — that would be CI noise on a fine date.
        with mock.patch.object(checker.urllib.request, "urlopen") as urlopen, \
             mock.patch.object(checker.time, "sleep"):
            urlopen.side_effect = [
                _http_error(503, "Service Unavailable"),
                _http_response(200, "OK"),
            ]
            ok, detail = checker._http_head_ok("https://snapshot.debian.org/x/")
        self.assertTrue(ok)
        self.assertIn("200", detail)
        self.assertEqual(urlopen.call_count, 2)

    def test_url_error_retries_then_fails_with_network_error_marker(self) -> None:
        # A DNS failure / TCP refusal surfaces as URLError; the detail
        # string MUST start with "network error" because the
        # ``--skip-on-network-error`` branch in main() classifies
        # findings by that prefix.
        with mock.patch.object(checker.urllib.request, "urlopen") as urlopen, \
             mock.patch.object(checker.time, "sleep") as sleep:
            urlopen.side_effect = urllib.error.URLError("Name or service not known")
            ok, detail = checker._http_head_ok("https://snapshot.debian.org/x/")
        self.assertFalse(ok)
        self.assertTrue(
            detail.startswith("network error"),
            f"detail must start with 'network error' for the "
            f"--skip-on-network-error classifier; got {detail!r}",
        )
        self.assertEqual(urlopen.call_count, checker.MAX_ATTEMPTS)
        self.assertEqual(sleep.call_count, checker.MAX_ATTEMPTS - 1)

    def test_socket_timeout_classified_as_timeout(self) -> None:
        # Same idea as URLError: the ``timeout`` prefix is what main()
        # uses to decide network-only outage vs real failure.
        with mock.patch.object(checker.urllib.request, "urlopen") as urlopen, \
             mock.patch.object(checker.time, "sleep"):
            urlopen.side_effect = socket.timeout("timed out")
            ok, detail = checker._http_head_ok("https://snapshot.debian.org/x/")
        self.assertFalse(ok)
        self.assertTrue(
            detail.startswith("timeout"),
            f"detail must start with 'timeout' for the "
            f"--skip-on-network-error classifier; got {detail!r}",
        )


# ---------------------------------------------------------------------------
# main() — end-to-end with mocked HTTP and a synthetic Dockerfile tree
# ---------------------------------------------------------------------------


class _MainHarness:
    """Run ``main()`` against a temp tree of Dockerfiles with HTTP mocked."""

    def __init__(self, dockerfiles: dict[str, str]):
        self._dockerfiles = dockerfiles
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        self._files: list[str] = []

    def __enter__(self) -> "_MainHarness":
        self._tmp = tempfile.TemporaryDirectory()
        for name, body in self._dockerfiles.items():
            self._files.append(_write_dockerfile(self._tmp.name, name, body))
        return self

    def __exit__(self, *exc: object) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()

    @property
    def files(self) -> list[str]:
        return list(self._files)

    @property
    def root(self) -> str:
        assert self._tmp is not None
        return self._tmp.name


class MainEndToEndTests(unittest.TestCase):
    """End-to-end: parser + validator + HTTP retry + escape hatch."""

    def _run_main(
        self,
        dockerfiles: dict[str, str],
        urlopen_side_effect,
        argv: list[str] | None = None,
    ) -> tuple[int, mock.MagicMock]:
        """Execute ``main(argv)`` against a synthetic Dockerfile tree.

        ``_find_dockerfiles`` is patched so the checker scans our temp
        tree instead of the real ``pi-setup/`` directory; ``urlopen`` is
        patched with the supplied side-effect; ``time.sleep`` is patched
        so retry tests run instantly.
        """
        if argv is None:
            argv = []
        with _MainHarness(dockerfiles) as h, \
             mock.patch.object(checker, "_find_dockerfiles", return_value=h.files), \
             mock.patch.object(checker.urllib.request, "urlopen") as urlopen, \
             mock.patch.object(checker.time, "sleep"):
            urlopen.side_effect = urlopen_side_effect
            rc = checker.main(argv)
        return rc, urlopen

    def test_happy_path_all_good(self) -> None:
        # Two distinct dates, both resolve. We expect 2 dates × 2 URLs
        # (debian + debian-security) = 4 HTTP probes, all 200.
        rc, urlopen = self._run_main(
            {
                "Dockerfile": (
                    f"FROM debian:bookworm-slim\nARG APT_SNAPSHOT_DATE={GOOD_DATE}\n"
                ),
                "recognizer.Dockerfile": (
                    f"FROM debian:bookworm-slim\nARG APT_SNAPSHOT_DATE={GOOD_DATE_2}\n"
                ),
            },
            urlopen_side_effect=lambda *a, **kw: _http_response(200, "OK"),
        )
        self.assertEqual(rc, 0)
        self.assertEqual(urlopen.call_count, 4)

    def test_no_pins_anywhere_is_a_pass(self) -> None:
        # Mirrors the Alpine pokeapi case: nothing to verify, exit 0,
        # zero network calls.
        rc, urlopen = self._run_main(
            {"Dockerfile": "FROM alpine:3.19\nRUN apk add curl\n"},
            urlopen_side_effect=lambda *a, **kw: _http_response(200, "OK"),
        )
        self.assertEqual(rc, 0)
        self.assertEqual(urlopen.call_count, 0)

    def test_malformed_date_fails_before_any_http_call(self) -> None:
        # ``2026-04-15`` is the canonical hand-typo; it must be rejected
        # locally (no network call) so the operator sees a clear error
        # rather than an opaque 404.
        rc, urlopen = self._run_main(
            {
                "Dockerfile": (
                    "FROM debian:bookworm-slim\n"
                    "ARG APT_SNAPSHOT_DATE=2026-04-15\n"
                ),
            },
            urlopen_side_effect=AssertionError("must not hit the network"),
        )
        self.assertEqual(rc, 1)
        self.assertEqual(urlopen.call_count, 0)

    def test_404_fails_run(self) -> None:
        rc, urlopen = self._run_main(
            {
                "Dockerfile": (
                    f"FROM debian:bookworm-slim\nARG APT_SNAPSHOT_DATE={GOOD_DATE}\n"
                ),
            },
            urlopen_side_effect=_http_error(404, "Not Found"),
        )
        self.assertEqual(rc, 1)
        # 4xx is terminal: 1 attempt per URL × 2 URLs = 2 calls, no retries.
        self.assertEqual(urlopen.call_count, 2)

    def test_total_outage_with_skip_flag_passes(self) -> None:
        # Every probe gets a URLError → all findings are network errors
        # → with --skip-on-network-error we exit 0 with a warning.
        rc, urlopen = self._run_main(
            {
                "Dockerfile": (
                    f"FROM debian:bookworm-slim\nARG APT_SNAPSHOT_DATE={GOOD_DATE}\n"
                ),
            },
            urlopen_side_effect=urllib.error.URLError("Name or service not known"),
            argv=["--skip-on-network-error"],
        )
        self.assertEqual(rc, 0)
        # 2 URLs × MAX_ATTEMPTS retries each.
        self.assertEqual(urlopen.call_count, 2 * checker.MAX_ATTEMPTS)

    def test_total_outage_without_skip_flag_fails(self) -> None:
        # Same outage, but without the escape hatch we MUST fail — the
        # safe default is to refuse to ship an unverified pin.
        rc, _ = self._run_main(
            {
                "Dockerfile": (
                    f"FROM debian:bookworm-slim\nARG APT_SNAPSHOT_DATE={GOOD_DATE}\n"
                ),
            },
            urlopen_side_effect=urllib.error.URLError("Name or service not known"),
            argv=[],
        )
        self.assertEqual(rc, 1)

    def test_skip_flag_does_not_swallow_real_404(self) -> None:
        # The escape hatch only kicks in if EVERY finding is a network
        # error. A real 404 mixed in must still fail the run, even with
        # --skip-on-network-error — that 404 is a genuine bad-date
        # signal we mustn't paper over.
        responses = [
            _http_error(404, "Not Found"),                       # 1st URL: real 404
            urllib.error.URLError("Name or service not known"),  # 2nd URL: outage
            urllib.error.URLError("Name or service not known"),
            urllib.error.URLError("Name or service not known"),
            urllib.error.URLError("Name or service not known"),
        ]
        rc, _ = self._run_main(
            {
                "Dockerfile": (
                    f"FROM debian:bookworm-slim\nARG APT_SNAPSHOT_DATE={GOOD_DATE}\n"
                ),
            },
            urlopen_side_effect=responses,
            argv=["--skip-on-network-error"],
        )
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
