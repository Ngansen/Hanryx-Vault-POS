"""Unit tests for ``pi-setup/scripts/check-image-pins-resolve.py``.

Locks PIN_RE, canonicalize_repo, parse_file, verify_pins, and the
HTTP retry / multi-arch-index handling so a regex tweak or HTTP
refactor can't silently green-light CI on a bad pin. Network is
fully stubbed via ``mock.patch`` on ``urllib.request.urlopen``.

Runnable with both ``pytest`` and ``python -m unittest``.
"""

from __future__ import annotations

import importlib.util
import io
import socket
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


# Hyphenated filename → load via importlib so we can call helpers directly.
_HERE = Path(__file__).resolve().parent
_CHECKER_PATH = _HERE.parent / "check-image-pins-resolve.py"
_REPO_ROOT = _HERE.parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "check_image_pins_resolve", _CHECKER_PATH
)
assert _spec is not None and _spec.loader is not None
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)


# Real pins currently in pi-setup/. Using the actual values keeps the
# tests anchored to what the live CI run sees.
DIGEST_PGVECTOR = "7bbb2558b9b73d23c4a7795d7a016cbb531ef5fb1e231115fa1aeac5129caf10"
DIGEST_REDIS = "c1e88455c85225310bbea54816e9c3f4b5295815e6dbf80c34d40afc6df28275"
DIGEST_PGBOUNCER = "6c5f93ba9e4c78fc3d88456b4ab82097b1cc745185f4be891aca70bdac875d8c"
DIGEST_PYTHON = "840e180ebcc6e5c8efab209c43f5e40fd2af98cb49db5c7103c90539c56bb30e"
DIGEST_NGINX = "74175cf34632e88c6cfe206897cbfe2d2fecf9bf033c40e7f9775a3689e8adc7"
DIGEST_NODE = "28fbbb764069c698ead61d6a739a7615e8f0e07a4b8fe1473ceca70c1c3d6aaa"
DIGEST_DRIFTED = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


# ---------------------------------------------------------------------------
# PIN_RE
# ---------------------------------------------------------------------------


class PinRegexAcceptsRealPinsTests(unittest.TestCase):
    def test_pgvector_compose_pin(self) -> None:
        m = checker.PIN_RE.search(
            f"    image: pgvector/pgvector:0.7.4-pg16@sha256:{DIGEST_PGVECTOR}\n"
        )
        assert m is not None
        self.assertEqual(m.group("repo"), "pgvector/pgvector")
        self.assertEqual(m.group("tag"), "0.7.4-pg16")
        self.assertEqual(m.group("digest"), DIGEST_PGVECTOR)

    def test_redis_compose_pin(self) -> None:
        m = checker.PIN_RE.search(
            f"    image: redis:7.4.1-alpine@sha256:{DIGEST_REDIS}\n"
        )
        assert m is not None
        self.assertEqual(m.group("repo"), "redis")
        self.assertEqual(m.group("tag"), "7.4.1-alpine")

    def test_pgbouncer_patch_tag_pin(self) -> None:
        # The post-Task-#14 patch tag — the unsuffixed `1.21.0` was
        # retired upstream, so the `-p2` suffix shape must match.
        m = checker.PIN_RE.search(
            f"    image: edoburu/pgbouncer:1.21.0-p2@sha256:{DIGEST_PGBOUNCER}\n"
        )
        assert m is not None
        self.assertEqual(m.group("tag"), "1.21.0-p2")

    def test_python_from_pin_with_as_alias(self) -> None:
        m = checker.PIN_RE.search(
            f"FROM python:3.11.10-slim-bookworm@sha256:{DIGEST_PYTHON} AS builder\n"
        )
        assert m is not None
        self.assertEqual(m.group("repo"), "python")
        self.assertEqual(m.group("digest"), DIGEST_PYTHON)

    def test_nginx_alpine_pin(self) -> None:
        m = checker.PIN_RE.search(f"FROM nginx:1.27.2-alpine@sha256:{DIGEST_NGINX}\n")
        assert m is not None
        self.assertEqual(m.group("repo"), "nginx")

    def test_node_pin(self) -> None:
        m = checker.PIN_RE.search(
            f"FROM node:20.18.0-bookworm-slim@sha256:{DIGEST_NODE} AS builder\n"
        )
        assert m is not None
        self.assertEqual(m.group("tag"), "20.18.0-bookworm-slim")


class PinRegexRejectsBadShapesTests(unittest.TestCase):
    def test_floating_tag_no_digest(self) -> None:
        # `image: ollama/ollama:0.4.7` shape — no `@sha256:` suffix.
        self.assertIsNone(checker.PIN_RE.search("    image: ollama/ollama:0.4.7\n"))

    def test_bare_image_name(self) -> None:
        self.assertIsNone(checker.PIN_RE.search("FROM python\n"))

    def test_digest_too_short(self) -> None:
        short = DIGEST_PYTHON[:-1]
        self.assertIsNone(checker.PIN_RE.search(f"FROM python:3.11@sha256:{short}\n"))

    def test_digest_too_long(self) -> None:
        # 65 hex chars: the trailing negative lookahead must reject
        # the whole match rather than greedily eating 64 and leaving
        # the 65th in the surrounding text.
        long_digest = DIGEST_PYTHON + "a"
        self.assertIsNone(
            checker.PIN_RE.search(f"FROM python:3.11@sha256:{long_digest}\n")
        )

    def test_digest_way_too_long(self) -> None:
        # Defensive: a digest that's clearly an entire second digest
        # appended must also be rejected outright.
        self.assertIsNone(
            checker.PIN_RE.search(
                f"FROM python:3.11@sha256:{DIGEST_PYTHON}{DIGEST_REDIS}\n"
            )
        )

    def test_non_hex_digest_char(self) -> None:
        bad = "g" + DIGEST_PYTHON[1:]
        self.assertIsNone(checker.PIN_RE.search(f"FROM python:3.11@sha256:{bad}\n"))

    def test_pin_after_non_boundary_char(self) -> None:
        # `*` is neither a boundary char (`\s=:'"`) nor in the repo
        # charset, so a pin glued after `*` must not match.
        self.assertIsNone(
            checker.PIN_RE.search(f"foo*python:3.11@sha256:{DIGEST_PYTHON}\n")
        )

    def test_wrong_digest_algorithm(self) -> None:
        self.assertIsNone(
            checker.PIN_RE.search(f"FROM python:3.11@sha512:{DIGEST_PYTHON}\n")
        )

    def test_missing_at_sign(self) -> None:
        self.assertIsNone(
            checker.PIN_RE.search(f"FROM python:3.11 sha256:{DIGEST_PYTHON}\n")
        )


class PinRegexBoundaryTests(unittest.TestCase):
    def test_after_equals_sign(self) -> None:
        m = checker.PIN_RE.search(f"IMAGE=python:3.11@sha256:{DIGEST_PYTHON}\n")
        assert m is not None
        self.assertEqual(m.group("repo"), "python")

    def test_after_double_quote(self) -> None:
        m = checker.PIN_RE.search(f'image: "python:3.11@sha256:{DIGEST_PYTHON}"\n')
        assert m is not None

    def test_after_single_quote(self) -> None:
        m = checker.PIN_RE.search(f"image: 'python:3.11@sha256:{DIGEST_PYTHON}'\n")
        assert m is not None

    def test_at_start_of_line(self) -> None:
        m = checker.PIN_RE.search(f"python:3.11@sha256:{DIGEST_PYTHON}\n")
        assert m is not None


# ---------------------------------------------------------------------------
# canonicalize_repo
# ---------------------------------------------------------------------------


class CanonicalizeRepoTests(unittest.TestCase):
    def test_official_image_gets_library_prefix(self) -> None:
        self.assertEqual(checker.canonicalize_repo("python"), "library/python")
        self.assertEqual(checker.canonicalize_repo("redis"), "library/redis")
        self.assertEqual(checker.canonicalize_repo("nginx"), "library/nginx")

    def test_namespaced_repo_left_alone(self) -> None:
        # Adding `library/` to a namespaced repo would 404.
        self.assertEqual(
            checker.canonicalize_repo("pgvector/pgvector"), "pgvector/pgvector"
        )
        self.assertEqual(
            checker.canonicalize_repo("edoburu/pgbouncer"), "edoburu/pgbouncer"
        )

    def test_full_registry_path_left_alone(self) -> None:
        self.assertEqual(
            checker.canonicalize_repo("ghcr.io/owner/image"), "ghcr.io/owner/image"
        )

    def test_already_library_prefixed_left_alone(self) -> None:
        self.assertEqual(checker.canonicalize_repo("library/python"), "library/python")


# ---------------------------------------------------------------------------
# parse_file
# ---------------------------------------------------------------------------


def _write(dirpath: str, rel: str, body: str) -> None:
    full = Path(dirpath) / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body, encoding="utf-8")


class ParseFileTests(unittest.TestCase):
    def test_single_dockerfile_pin(self) -> None:
        body = (
            "# Comment\n"
            f"FROM python:3.11.10-slim-bookworm@sha256:{DIGEST_PYTHON}\n"
            "RUN echo hi\n"
        )
        with tempfile.TemporaryDirectory() as td:
            _write(td, "Dockerfile", body)
            pins = checker.parse_file("Dockerfile", td)
        self.assertEqual(len(pins), 1)
        p = pins[0]
        self.assertEqual(p.lineno, 2)
        self.assertEqual(p.repo_raw, "python")
        # Canonicalisation is applied at parse time.
        self.assertEqual(p.repo, "library/python")
        self.assertEqual(p.tag, "3.11.10-slim-bookworm")
        self.assertEqual(p.digest, DIGEST_PYTHON)

    def test_multi_stage_dockerfile_returns_both_pins(self) -> None:
        # Both occurrences must land in the result so a half-bumped
        # multi-stage file is caught.
        body = (
            f"FROM python:3.11.10-slim-bookworm@sha256:{DIGEST_PYTHON} AS builder\n"
            "RUN pip install foo\n"
            "\n"
            f"FROM python:3.11.10-slim-bookworm@sha256:{DIGEST_PYTHON}\n"
        )
        with tempfile.TemporaryDirectory() as td:
            _write(td, "Dockerfile", body)
            pins = checker.parse_file("Dockerfile", td)
        self.assertEqual(len(pins), 2)
        self.assertEqual([p.lineno for p in pins], [1, 4])
        self.assertEqual(pins[0].digest, pins[1].digest)

    def test_compose_file_with_multiple_services(self) -> None:
        body = (
            "services:\n"
            "  db:\n"
            f"    image: pgvector/pgvector:0.7.4-pg16@sha256:{DIGEST_PGVECTOR}\n"
            "  cache:\n"
            f"    image: redis:7.4.1-alpine@sha256:{DIGEST_REDIS}\n"
            "  pool:\n"
            f"    image: edoburu/pgbouncer:1.21.0-p2@sha256:{DIGEST_PGBOUNCER}\n"
        )
        with tempfile.TemporaryDirectory() as td:
            _write(td, "docker-compose.yml", body)
            pins = checker.parse_file("docker-compose.yml", td)
        self.assertEqual(
            [p.repo for p in pins],
            ["pgvector/pgvector", "library/redis", "edoburu/pgbouncer"],
        )
        self.assertEqual([p.tag for p in pins], ["0.7.4-pg16", "7.4.1-alpine", "1.21.0-p2"])
        self.assertEqual([p.lineno for p in pins], sorted(p.lineno for p in pins))

    def test_skips_floating_tag_image(self) -> None:
        # An unpinned image (no `@sha256:`) must be silently skipped,
        # not fabricated into a half-pin.
        body = (
            "services:\n"
            f"  pinned:\n"
            f"    image: redis:7.4.1-alpine@sha256:{DIGEST_REDIS}\n"
            "  floating:\n"
            "    image: ollama/ollama:0.4.7\n"
        )
        with tempfile.TemporaryDirectory() as td:
            _write(td, "docker-compose.yml", body)
            pins = checker.parse_file("docker-compose.yml", td)
        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0].repo, "library/redis")

    def test_returns_empty_for_file_with_no_pins(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _write(td, "Dockerfile", "FROM scratch\nCOPY hello /\n")
            self.assertEqual(checker.parse_file("Dockerfile", td), [])


class RealTargetFilesTests(unittest.TestCase):
    """Dynamic check against the live ``pi-setup/`` source tree.

    Catches the failure mode where the regex still matches synthetic
    fixtures but no longer matches the real pinned files (or vice
    versa: a new pin gets added and the parser silently misses it).
    """

    def test_every_target_file_yields_at_least_one_pin(self) -> None:
        # `main()` treats "no pins anywhere" as a setup error (exit 2),
        # so every target file currently in TARGET_FILES must
        # contribute at least one pin.
        for rel in checker.TARGET_FILES:
            abs_path = _REPO_ROOT / rel
            self.assertTrue(abs_path.is_file(), f"missing target file: {rel}")
            pins = checker.parse_file(rel, str(_REPO_ROOT))
            self.assertGreater(
                len(pins), 0,
                f"PIN_RE found no pins in {rel} — parser may have regressed",
            )

    def test_all_real_pins_have_canonical_shape(self) -> None:
        # Every pin parsed from the real tree must have a 64-char
        # lowercase hex digest, a non-empty tag, and a canonicalised
        # repo. If any of these fails, the registry call would either
        # 404 (wrong repo path) or compare apples to oranges
        # (truncated digest).
        for rel in checker.TARGET_FILES:
            for p in checker.parse_file(rel, str(_REPO_ROOT)):
                self.assertEqual(len(p.digest), 64, f"{rel}:{p.lineno}")
                self.assertEqual(p.digest, p.digest.lower(), f"{rel}:{p.lineno}")
                self.assertTrue(p.tag, f"{rel}:{p.lineno}")
                self.assertIn("/", p.repo, f"{rel}:{p.lineno} not canonicalised")


# ---------------------------------------------------------------------------
# verify_pins — classification
# ---------------------------------------------------------------------------


def _pin(repo_raw: str, tag: str, digest: str, *, lineno: int = 1) -> "checker.Pin":
    return checker.Pin(
        file="pi-setup/docker-compose.yml",
        lineno=lineno,
        repo_raw=repo_raw,
        repo=checker.canonicalize_repo(repo_raw),
        tag=tag,
        digest=digest,
    )


class VerifyPinsClassificationTests(unittest.TestCase):
    def test_matching_digest_produces_no_findings(self) -> None:
        pin = _pin("redis", "7.4.1-alpine", DIGEST_REDIS)
        with mock.patch.object(checker, "resolve_digest", return_value=DIGEST_REDIS):
            self.assertEqual(checker.verify_pins([pin]), [])

    def test_retired_tag_classified_as_retired(self) -> None:
        pin = _pin("edoburu/pgbouncer", "1.21.0", DIGEST_PGBOUNCER)
        with mock.patch.object(
            checker, "resolve_digest",
            side_effect=checker.TagRetiredError("HTTP 404 Not Found"),
        ):
            findings = checker.verify_pins([pin])
        self.assertEqual(len(findings), 1)
        self.assertIn("tag retired upstream", findings[0].reason)
        self.assertIn("404", findings[0].reason)

    def test_digest_drift_classified_as_drift(self) -> None:
        # Both digests must appear in the message so the operator can
        # diff them at a glance.
        pin = _pin("library/python", "3.11.10-slim-bookworm", DIGEST_PYTHON)
        with mock.patch.object(checker, "resolve_digest", return_value=DIGEST_DRIFTED):
            findings = checker.verify_pins([pin])
        self.assertEqual(len(findings), 1)
        self.assertIn("digest drift", findings[0].reason)
        self.assertIn(DIGEST_DRIFTED, findings[0].reason)
        self.assertIn(DIGEST_PYTHON, findings[0].reason)

    def test_transient_registry_error_classified_as_transient(self) -> None:
        pin = _pin("redis", "7.4.1-alpine", DIGEST_REDIS)
        with mock.patch.object(
            checker, "resolve_digest",
            side_effect=checker.RegistryError("HTTP 503 Service Unavailable"),
        ):
            findings = checker.verify_pins([pin])
        self.assertEqual(len(findings), 1)
        self.assertIn("could not verify", findings[0].reason)

    def test_caches_lookups_per_repo_tag(self) -> None:
        # Same (repo, tag) in two stages → exactly one HTTP call.
        pin_a = _pin("python", "3.11.10-slim-bookworm", DIGEST_PYTHON, lineno=12)
        pin_b = _pin("python", "3.11.10-slim-bookworm", DIGEST_PYTHON, lineno=84)
        with mock.patch.object(
            checker, "resolve_digest", return_value=DIGEST_PYTHON
        ) as resolve:
            self.assertEqual(checker.verify_pins([pin_a, pin_b]), [])
        self.assertEqual(resolve.call_count, 1)

    def test_drift_on_cached_lookup_flags_both_pins(self) -> None:
        # A half-bumped multi-stage Dockerfile would otherwise slip
        # through if the cache only flagged the first pin.
        pin_a = _pin("python", "3.11.10-slim-bookworm", DIGEST_PYTHON, lineno=12)
        pin_b = _pin("python", "3.11.10-slim-bookworm", DIGEST_PYTHON, lineno=84)
        with mock.patch.object(checker, "resolve_digest", return_value=DIGEST_DRIFTED):
            findings = checker.verify_pins([pin_a, pin_b])
        self.assertEqual(sorted(f.pin.lineno for f in findings), [12, 84])

    def test_mixed_results_classified_independently(self) -> None:
        good = _pin("redis", "7.4.1-alpine", DIGEST_REDIS)
        retired = _pin("edoburu/pgbouncer", "1.21.0", DIGEST_PGBOUNCER)
        drifted = _pin("library/python", "3.11.10-slim-bookworm", DIGEST_PYTHON)

        def fake_resolve(repo: str, tag: str) -> str:
            if (repo, tag) == ("library/redis", "7.4.1-alpine"):
                return DIGEST_REDIS
            if (repo, tag) == ("edoburu/pgbouncer", "1.21.0"):
                raise checker.TagRetiredError("HTTP 404 Not Found")
            if (repo, tag) == ("library/python", "3.11.10-slim-bookworm"):
                return DIGEST_DRIFTED
            raise AssertionError(f"unexpected lookup: {(repo, tag)}")

        with mock.patch.object(checker, "resolve_digest", side_effect=fake_resolve):
            findings = checker.verify_pins([good, retired, drifted])
        by_pin = {f.pin: f for f in findings}
        self.assertNotIn(good, by_pin)
        self.assertIn("tag retired upstream", by_pin[retired].reason)
        self.assertIn("digest drift", by_pin[drifted].reason)


# ---------------------------------------------------------------------------
# _http_with_retries
# ---------------------------------------------------------------------------


def _http_response(
    status: int,
    *,
    headers: list[tuple[str, str]] | None = None,
    body: bytes = b"",
    reason: str = "OK",
):
    fake = mock.MagicMock()
    fake.status = status
    fake.reason = reason
    fake.getheaders.return_value = list(headers or [])
    fake.read.return_value = body
    cm = mock.MagicMock()
    cm.__enter__.return_value = fake
    cm.__exit__.return_value = False
    return cm


def _http_error(code: int, reason: str = "Not Found") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://example.invalid/", code=code, msg=reason,
        hdrs=None, fp=io.BytesIO(b""),  # type: ignore[arg-type]
    )


def _make_request() -> "checker.urllib.request.Request":
    return checker.urllib.request.Request(
        "https://registry-1.docker.io/v2/library/python/manifests/3.11",
        method="HEAD", headers={"User-Agent": "test"},
    )


class HttpWithRetriesTests(unittest.TestCase):
    def test_200_returns_immediately(self) -> None:
        with mock.patch.object(checker.urllib.request, "urlopen") as urlopen, \
             mock.patch.object(checker.time, "sleep") as sleep:
            urlopen.return_value = _http_response(
                200, headers=[("Docker-Content-Digest", f"sha256:{DIGEST_PYTHON}")],
            )
            status, headers, _ = checker._http_with_retries(_make_request())
        self.assertEqual(status, 200)
        self.assertEqual(
            checker._header(headers, "Docker-Content-Digest"),
            f"sha256:{DIGEST_PYTHON}",
        )
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_404_raises_tag_retired_immediately(self) -> None:
        # 404 is the definitive "tag is gone" signal — must not retry.
        with mock.patch.object(checker.urllib.request, "urlopen") as urlopen, \
             mock.patch.object(checker.time, "sleep") as sleep:
            urlopen.side_effect = _http_error(404, "Not Found")
            with self.assertRaises(checker.TagRetiredError) as ctx:
                checker._http_with_retries(_make_request())
        self.assertIn("404", str(ctx.exception))
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_401_raises_tag_retired_immediately(self) -> None:
        with mock.patch.object(checker.urllib.request, "urlopen") as urlopen, \
             mock.patch.object(checker.time, "sleep"):
            urlopen.side_effect = _http_error(401, "Unauthorized")
            with self.assertRaises(checker.TagRetiredError):
                checker._http_with_retries(_make_request())
        self.assertEqual(urlopen.call_count, 1)

    def test_429_is_retried_not_treated_as_retired(self) -> None:
        # 429 is rate-limiting, not retirement. Must retry, then
        # surface as RegistryError so a fine pin isn't falsely
        # accused during a Docker Hub spike.
        with mock.patch.object(checker.urllib.request, "urlopen") as urlopen, \
             mock.patch.object(checker.time, "sleep") as sleep:
            urlopen.side_effect = _http_error(429, "Too Many Requests")
            with self.assertRaises(checker.RegistryError):
                checker._http_with_retries(_make_request())
        self.assertEqual(urlopen.call_count, checker.MAX_ATTEMPTS)
        self.assertEqual(sleep.call_count, checker.MAX_ATTEMPTS - 1)

    def test_503_retried_then_raises_registry_error(self) -> None:
        with mock.patch.object(checker.urllib.request, "urlopen") as urlopen, \
             mock.patch.object(checker.time, "sleep") as sleep:
            urlopen.side_effect = _http_error(503, "Service Unavailable")
            with self.assertRaises(checker.RegistryError) as ctx:
                checker._http_with_retries(_make_request())
        self.assertIn("503", str(ctx.exception))
        self.assertEqual(urlopen.call_count, checker.MAX_ATTEMPTS)
        self.assertEqual(sleep.call_count, checker.MAX_ATTEMPTS - 1)

    def test_503_then_200_recovers(self) -> None:
        with mock.patch.object(checker.urllib.request, "urlopen") as urlopen, \
             mock.patch.object(checker.time, "sleep"):
            urlopen.side_effect = [
                _http_error(503, "Service Unavailable"),
                _http_response(
                    200, headers=[("Docker-Content-Digest", f"sha256:{DIGEST_PYTHON}")],
                ),
            ]
            status, _h, _b = checker._http_with_retries(_make_request())
        self.assertEqual(status, 200)
        self.assertEqual(urlopen.call_count, 2)

    def test_url_error_retried_then_registry_error(self) -> None:
        with mock.patch.object(checker.urllib.request, "urlopen") as urlopen, \
             mock.patch.object(checker.time, "sleep"):
            urlopen.side_effect = urllib.error.URLError("Name or service not known")
            with self.assertRaises(checker.RegistryError) as ctx:
                checker._http_with_retries(_make_request())
        self.assertIn("network error", str(ctx.exception))
        self.assertEqual(urlopen.call_count, checker.MAX_ATTEMPTS)

    def test_socket_timeout_retried_then_registry_error(self) -> None:
        with mock.patch.object(checker.urllib.request, "urlopen") as urlopen, \
             mock.patch.object(checker.time, "sleep"):
            urlopen.side_effect = socket.timeout("timed out")
            with self.assertRaises(checker.RegistryError) as ctx:
                checker._http_with_retries(_make_request())
        self.assertIn("timeout", str(ctx.exception))
        self.assertEqual(urlopen.call_count, checker.MAX_ATTEMPTS)


# ---------------------------------------------------------------------------
# resolve_digest — multi-arch index parsing
# ---------------------------------------------------------------------------


class ResolveDigestTests(unittest.TestCase):
    def _patches(self):
        # Mock fetch_token at module level so only the manifest urlopen
        # call needs to be queued in each test.
        return (
            mock.patch.object(checker, "fetch_token", return_value="anon-token"),
            mock.patch.object(checker.urllib.request, "urlopen"),
            mock.patch.object(checker.time, "sleep"),
        )

    def test_returns_lowercase_hex_digest(self) -> None:
        # Mixed-case hex from the registry must be normalised so a
        # registry that returns uppercase doesn't cause a false drift.
        token_p, url_p, sleep_p = self._patches()
        with token_p, url_p as urlopen, sleep_p:
            urlopen.return_value = _http_response(
                200,
                headers=[
                    ("Docker-Content-Digest", f"sha256:{DIGEST_PYTHON.upper()}"),
                    ("Content-Type",
                     "application/vnd.docker.distribution.manifest.list.v2+json"),
                ],
            )
            self.assertEqual(
                checker.resolve_digest("library/python", "3.11"), DIGEST_PYTHON
            )

    def test_oci_image_index_content_type_accepted(self) -> None:
        token_p, url_p, sleep_p = self._patches()
        with token_p, url_p as urlopen, sleep_p:
            urlopen.return_value = _http_response(
                200,
                headers=[
                    ("Docker-Content-Digest", f"sha256:{DIGEST_NGINX}"),
                    ("Content-Type", "application/vnd.oci.image.index.v1+json"),
                ],
            )
            self.assertEqual(
                checker.resolve_digest("library/nginx", "1.27.2-alpine"),
                DIGEST_NGINX,
            )

    def test_per_arch_manifest_treated_as_tag_retired(self) -> None:
        # Per-arch digest is not comparable to a pinned index digest —
        # treat as if the multi-arch index was retired.
        token_p, url_p, sleep_p = self._patches()
        with token_p, url_p as urlopen, sleep_p:
            urlopen.return_value = _http_response(
                200,
                headers=[
                    ("Docker-Content-Digest", f"sha256:{DIGEST_PYTHON}"),
                    ("Content-Type",
                     "application/vnd.docker.distribution.manifest.v2+json"),
                ],
            )
            with self.assertRaises(checker.TagRetiredError):
                checker.resolve_digest("library/python", "3.11")

    def test_missing_digest_header_raises_registry_error(self) -> None:
        token_p, url_p, sleep_p = self._patches()
        with token_p, url_p as urlopen, sleep_p:
            urlopen.return_value = _http_response(
                200,
                headers=[("Content-Type", "application/vnd.oci.image.index.v1+json")],
            )
            with self.assertRaises(checker.RegistryError) as ctx:
                checker.resolve_digest("library/python", "3.11")
        self.assertIn("Docker-Content-Digest", str(ctx.exception))

    def test_unexpected_digest_algorithm_raises_registry_error(self) -> None:
        token_p, url_p, sleep_p = self._patches()
        with token_p, url_p as urlopen, sleep_p:
            urlopen.return_value = _http_response(
                200,
                headers=[
                    ("Docker-Content-Digest", f"sha512:{DIGEST_PYTHON}"),
                    ("Content-Type", "application/vnd.oci.image.index.v1+json"),
                ],
            )
            with self.assertRaises(checker.RegistryError) as ctx:
                checker.resolve_digest("library/python", "3.11")
        self.assertIn("sha512", str(ctx.exception))

    def test_malformed_digest_hex_raises_registry_error(self) -> None:
        token_p, url_p, sleep_p = self._patches()
        with token_p, url_p as urlopen, sleep_p:
            urlopen.return_value = _http_response(
                200,
                headers=[
                    ("Docker-Content-Digest", "sha256:not-a-real-digest"),
                    ("Content-Type", "application/vnd.oci.image.index.v1+json"),
                ],
            )
            with self.assertRaises(checker.RegistryError) as ctx:
                checker.resolve_digest("library/python", "3.11")
        self.assertIn("malformed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
