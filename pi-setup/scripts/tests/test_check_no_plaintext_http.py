"""
Unit tests for ``pi-setup/scripts/check-no-plaintext-http.py``.

The scanner makes a lot of fine-grained classification decisions (RFC 1918,
CGNAT, ``.local`` / ``.ts.net`` / ``.internal`` / ``.lan`` / ``.home.arpa``
suffixes, bare Docker hostnames, shell/template placeholders, XML
namespaces, two allow-marker spellings, marker-on-line vs marker-above).
A future edit to any of those branches could silently widen the allow-list
and the regression would never show up on CI of a clean tree.

These tests pin the contract end-to-end:

* ``_is_internal_host`` — every internal-host shape in the allow-list
  must classify as internal, and several genuinely external hosts must
  NOT classify as internal (including a few that look superficially
  internal, e.g. ``evil.local.attacker.com``, ``localhost.evil.com``).
* ``_is_xml_namespace_host`` — known XML namespace identifiers skip,
  arbitrary external hosts do not.
* ``_line_is_allowed`` — both marker spellings are honoured both on the
  same line and on the line immediately above, but NOT two lines above.
* ``_scan_file`` (end-to-end) — exercises the regex (``http``, ``ws``,
  ``mqtt``, ``ftp``), confirms the TLS-protected variants (``https``,
  ``wss``, ``mqtts``, ``ftps``, ``sftp``) are never flagged, and walks
  through real-looking lines for each interesting allow / deny case.

Runnable both with ``pytest`` and with ``python -m unittest`` so the CI
job does not need any third-party dependency installed.

Note on self-hosting
--------------------
This file lives under ``pi-setup/`` and so is itself scanned by the very
script it tests. If we wrote URLs as plain literals (e.g.
``http`` + ``://example.com``) the static scan would (correctly!) flag
every test fixture as an external plaintext call. To avoid that
bootstrap problem we construct every URL at runtime via
``_url(scheme, rest)`` — the source file therefore contains no literal
``scheme :// host`` substring at all, the scanner's cheap
``"://" not in line`` pre-filter short-circuits, and the runtime strings
remain exactly what the scanner would see in real source.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


# Build URL strings at runtime so no literal ``scheme://...`` substring
# appears in this file. See module docstring for why.
_SEP = ":" + "//"


def _url(scheme: str, rest: str) -> str:
    return scheme + _SEP + rest


# The script's filename has hyphens in it, so a normal ``import`` won't
# work. Load it as a module via importlib so we can call the private
# helpers directly with no subprocess / no synthetic-file plumbing.
_HERE = Path(__file__).resolve().parent
_SCANNER_PATH = _HERE.parent / "check-no-plaintext-http.py"

_spec = importlib.util.spec_from_file_location("check_no_plaintext_http", _SCANNER_PATH)
assert _spec is not None and _spec.loader is not None, f"cannot load {_SCANNER_PATH}"
scanner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scanner)


class IsInternalHostTests(unittest.TestCase):
    """Every shape on the documented allow-list must classify as internal."""

    # ---- loopback / unspecified ----

    def test_localhost_literal(self) -> None:
        self.assertTrue(scanner._is_internal_host("localhost"))

    def test_localhost_uppercase(self) -> None:
        # Hostnames are case-insensitive — make sure casing doesn't bypass.
        self.assertTrue(scanner._is_internal_host("LOCALHOST"))

    def test_ip6_localhost_alias(self) -> None:
        self.assertTrue(scanner._is_internal_host("ip6-localhost"))

    def test_ip6_loopback_alias(self) -> None:
        self.assertTrue(scanner._is_internal_host("ip6-loopback"))

    def test_ipv4_loopback(self) -> None:
        self.assertTrue(scanner._is_internal_host("127.0.0.1"))

    def test_ipv4_loopback_high(self) -> None:
        # Whole 127/8 is loopback, not just .0.0.1.
        self.assertTrue(scanner._is_internal_host("127.255.255.254"))

    def test_ipv4_unspecified(self) -> None:
        self.assertTrue(scanner._is_internal_host("0.0.0.0"))

    def test_ipv6_loopback_bracketed(self) -> None:
        # IPv6 in URLs is bracketed.
        self.assertTrue(scanner._is_internal_host("[::1]"))

    def test_ipv6_loopback_unbracketed(self) -> None:
        self.assertTrue(scanner._is_internal_host("::1"))

    def test_ipv6_link_local(self) -> None:
        self.assertTrue(scanner._is_internal_host("[fe80::1]"))

    def test_ipv6_unique_local(self) -> None:
        # fc00::/7 is the IPv6 equivalent of RFC 1918.
        self.assertTrue(scanner._is_internal_host("[fd12:3456:789a::1]"))

    # ---- RFC 1918 ----

    def test_rfc1918_ten(self) -> None:
        self.assertTrue(scanner._is_internal_host("10.0.0.1"))

    def test_rfc1918_ten_high(self) -> None:
        self.assertTrue(scanner._is_internal_host("10.255.255.254"))

    def test_rfc1918_172_16(self) -> None:
        self.assertTrue(scanner._is_internal_host("172.16.0.1"))

    def test_rfc1918_172_31_boundary(self) -> None:
        # 172.16.0.0/12 ends at 172.31.255.255 — still private.
        self.assertTrue(scanner._is_internal_host("172.31.255.254"))

    def test_172_32_is_not_private(self) -> None:
        # First address outside 172.16.0.0/12 — must be external.
        self.assertFalse(scanner._is_internal_host("172.32.0.1"))

    def test_rfc1918_192_168(self) -> None:
        self.assertTrue(scanner._is_internal_host("192.168.1.1"))

    # ---- link-local ----

    def test_ipv4_link_local(self) -> None:
        # 169.254/16 — DHCP-failed APIPA range.
        self.assertTrue(scanner._is_internal_host("169.254.1.1"))

    # ---- CGNAT (Tailscale) ----

    def test_cgnat_low(self) -> None:
        # 100.64.0.0/10 — Tailscale's address pool.
        self.assertTrue(scanner._is_internal_host("100.64.0.1"))

    def test_cgnat_high(self) -> None:
        self.assertTrue(scanner._is_internal_host("100.127.255.254"))

    def test_just_below_cgnat_is_not_internal(self) -> None:
        # 100.63.x.x is publicly routable — must NOT be allow-listed.
        self.assertFalse(scanner._is_internal_host("100.63.255.254"))

    def test_just_above_cgnat_is_not_internal(self) -> None:
        # 100.128.x.x is publicly routable — must NOT be allow-listed.
        self.assertFalse(scanner._is_internal_host("100.128.0.1"))

    # ---- mDNS / internal DNS / Tailscale magic DNS / LAN / home.arpa ----

    def test_mdns_local_suffix(self) -> None:
        self.assertTrue(scanner._is_internal_host("printer.local"))

    def test_mdns_local_multi_label(self) -> None:
        self.assertTrue(scanner._is_internal_host("kiosk-3.satellites.local"))

    def test_internal_suffix(self) -> None:
        self.assertTrue(scanner._is_internal_host("api.internal"))

    def test_tailscale_magic_dns(self) -> None:
        self.assertTrue(scanner._is_internal_host("mainpi.tailnet-abc.ts.net"))

    def test_lan_suffix(self) -> None:
        self.assertTrue(scanner._is_internal_host("router.lan"))

    def test_home_arpa_suffix(self) -> None:
        # RFC 8375 — the standardised home-network suffix.
        self.assertTrue(scanner._is_internal_host("nas.home.arpa"))

    def test_bare_suffix_is_internal(self) -> None:
        # A host literally equal to the suffix without the leading dot
        # (e.g. ``local`` itself) is also internal — pin this so a future
        # edit doesn't accidentally drop the equality branch.
        self.assertTrue(scanner._is_internal_host("local"))

    def test_trailing_dot_is_stripped(self) -> None:
        # Fully-qualified DNS names end in a literal dot. Make sure that
        # doesn't defeat the suffix check.
        self.assertTrue(scanner._is_internal_host("printer.local."))

    # ---- bare Docker service names ----

    def test_bare_hostname_is_internal(self) -> None:
        # No-dot hostnames are Docker service names like ``pos``, ``db``,
        # ``redis``, ``pgbouncer``, ``mainpi``.
        for h in ("pos", "storefront", "db", "redis", "pgbouncer", "mainpi"):
            with self.subTest(host=h):
                self.assertTrue(scanner._is_internal_host(h))

    # ---- shell / template placeholders ----

    def test_placeholder_dollar_brace(self) -> None:
        self.assertTrue(scanner._is_internal_host("${HOST}"))

    def test_placeholder_dollar_var(self) -> None:
        self.assertTrue(scanner._is_internal_host("$HOST"))

    def test_placeholder_command_substitution(self) -> None:
        self.assertTrue(scanner._is_internal_host("$(hostname)"))

    def test_placeholder_angle_brackets(self) -> None:
        self.assertTrue(scanner._is_internal_host("<PLACEHOLDER>"))

    def test_placeholder_curly(self) -> None:
        self.assertTrue(scanner._is_internal_host("{host}"))

    def test_placeholder_printf_style(self) -> None:
        # Has ``%`` somewhere in it — printf / Python %-format / URL-encoded.
        self.assertTrue(scanner._is_internal_host("api.%s.example"))

    def test_placeholder_glob_star(self) -> None:
        # ``*`` is in PLACEHOLDER_CHARS — pin that branch.
        self.assertTrue(scanner._is_internal_host("*.svc.cluster"))

    # ---- genuinely external hosts (must NOT be internal) ----

    def test_external_dns(self) -> None:
        self.assertFalse(scanner._is_internal_host("api.example.com"))

    def test_external_github(self) -> None:
        self.assertFalse(scanner._is_internal_host("github.com"))

    def test_external_dotted_subdomain(self) -> None:
        self.assertFalse(scanner._is_internal_host("connectivity-check.ubuntu.com"))

    def test_external_public_ip(self) -> None:
        self.assertFalse(scanner._is_internal_host("8.8.8.8"))

    def test_external_public_ip_cloudflare(self) -> None:
        self.assertFalse(scanner._is_internal_host("1.1.1.1"))

    # NOTE: We deliberately do NOT assert on bracketed *public* IPv6
    # literals here. The current scanner falls through to its "bare
    # hostname (no dots)" branch for ``[2001:db8::1]``-shaped inputs and
    # so classifies them as internal. That's a real (separate) gap, not
    # something this regression-pin test should lock in either way.

    # ---- tricky negatives — superficially "internal", actually external ----

    def test_local_in_middle_is_not_internal(self) -> None:
        # The whole point of this test file: a future edit could replace
        # ``endswith(".local")`` with something laxer (e.g. ``in``) and
        # accidentally treat ``evil.local.attacker.com`` as internal.
        self.assertFalse(scanner._is_internal_host("evil.local.attacker.com"))

    def test_localhost_as_subdomain_is_not_internal(self) -> None:
        # ``localhost.evil.com`` looks like loopback at a glance but is
        # actually an attacker-controlled DNS name — must be flagged.
        self.assertFalse(scanner._is_internal_host("localhost.evil.com"))

    def test_dotted_localhost_suffix_is_not_internal(self) -> None:
        # Must not match the bare ``localhost`` exact-name branch.
        self.assertFalse(scanner._is_internal_host("evil.localhost"))

    def test_ts_net_in_middle_is_not_internal(self) -> None:
        self.assertFalse(scanner._is_internal_host("evil.ts.net.attacker.com"))

    def test_internal_in_middle_is_not_internal(self) -> None:
        self.assertFalse(scanner._is_internal_host("evil.internal.attacker.com"))

    def test_lan_in_middle_is_not_internal(self) -> None:
        self.assertFalse(scanner._is_internal_host("evil.lan.attacker.com"))

    def test_substring_local_without_dot_is_not_internal(self) -> None:
        # ``mylocal.com`` must NOT be matched by the ``.local`` suffix.
        self.assertFalse(scanner._is_internal_host("mylocal.com"))

    def test_empty_host_is_treated_as_internal(self) -> None:
        # Documented behaviour: malformed/empty matches are let through
        # rather than treated as findings. Pin it so a refactor doesn't
        # silently invert this.
        self.assertTrue(scanner._is_internal_host(""))


class IsXmlNamespaceHostTests(unittest.TestCase):
    """XML/XSD namespace identifiers are not network calls — they skip."""

    def test_w3_org(self) -> None:
        self.assertTrue(scanner._is_xml_namespace_host("www.w3.org"))

    def test_w3_org_uppercase(self) -> None:
        self.assertTrue(scanner._is_xml_namespace_host("WWW.W3.ORG"))

    def test_openxmlformats(self) -> None:
        self.assertTrue(scanner._is_xml_namespace_host("schemas.openxmlformats.org"))

    def test_xmlsoap(self) -> None:
        self.assertTrue(scanner._is_xml_namespace_host("schemas.xmlsoap.org"))

    def test_microsoft_schemas(self) -> None:
        self.assertTrue(scanner._is_xml_namespace_host("schemas.microsoft.com"))

    def test_purl(self) -> None:
        self.assertTrue(scanner._is_xml_namespace_host("purl.org"))

    def test_adobe_ns(self) -> None:
        self.assertTrue(scanner._is_xml_namespace_host("ns.adobe.com"))

    def test_arbitrary_external_is_not_xml_ns(self) -> None:
        # These are real network calls that look superficially similar.
        for h in ("api.w3.org", "example.com", "schemas.example.com"):
            with self.subTest(host=h):
                self.assertFalse(scanner._is_xml_namespace_host(h))


class LineIsAllowedTests(unittest.TestCase):
    """Both marker spellings, on this line and the line immediately above."""

    def test_no_marker_anywhere(self) -> None:
        lines = ["uri = " + _url("http", "api.example.com")]
        self.assertFalse(scanner._line_is_allowed(lines, 0))

    def test_plaintext_marker_same_line(self) -> None:
        lines = ["uri = " + _url("http", "api.example.com")
                 + "  # hanryx-allow-plaintext: reason"]
        self.assertTrue(scanner._line_is_allowed(lines, 0))

    def test_insecure_marker_same_line(self) -> None:
        # The TLS-checker marker is also honoured here — pin it so a
        # future edit doesn't drop one of the two.
        lines = ["uri = " + _url("http", "api.example.com")
                 + "  # hanryx-allow-insecure: reason"]
        self.assertTrue(scanner._line_is_allowed(lines, 0))

    def test_plaintext_marker_line_above(self) -> None:
        lines = [
            "# hanryx-allow-plaintext: NM captive-portal probe must be HTTP",
            "uri = " + _url("http", "connectivity-check.ubuntu.com"),
        ]
        self.assertTrue(scanner._line_is_allowed(lines, 1))

    def test_insecure_marker_line_above(self) -> None:
        lines = [
            "# hanryx-allow-insecure: documented reason",
            "uri = " + _url("http", "api.example.com"),
        ]
        self.assertTrue(scanner._line_is_allowed(lines, 1))

    def test_marker_two_lines_above_is_not_enough(self) -> None:
        # Pin the "immediately above" contract — a marker that's two
        # lines up is too far away and must NOT carry over.
        lines = [
            "# hanryx-allow-plaintext: stale marker, two lines up",
            "",
            "uri = " + _url("http", "api.example.com"),
        ]
        self.assertFalse(scanner._line_is_allowed(lines, 2))

    def test_marker_on_line_below_is_not_enough(self) -> None:
        # Markers above only — never below.
        lines = [
            "uri = " + _url("http", "api.example.com"),
            "# hanryx-allow-plaintext: too late, this is below",
        ]
        self.assertFalse(scanner._line_is_allowed(lines, 0))

    def test_marker_on_first_line_no_index_underflow(self) -> None:
        # Make sure idx==0 with no marker doesn't try to read lines[-1].
        lines = ["uri = " + _url("http", "api.example.com")]
        self.assertFalse(scanner._line_is_allowed(lines, 0))


def _write_and_scan(content: str) -> list[scanner.Finding]:
    """Write ``content`` to a temp file and return the scanner's findings."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(content)
        path = fh.name
    try:
        return list(scanner._scan_file(path, os.path.basename(path)))
    finally:
        os.unlink(path)


def _src(line: str) -> str:
    """Wrap a single source line in a trailing newline for the scanner."""
    return line + "\n"


class ScanFileEndToEndTests(unittest.TestCase):
    """End-to-end: feed lines of source through the actual scan path."""

    # ---- schemes that MUST be flagged when external ----

    def test_external_http_is_flagged(self) -> None:
        findings = _write_and_scan(_src(
            'url = "' + _url("http", "api.example.com/v1") + '"'
        ))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].scheme, "http")
        self.assertEqual(findings[0].host, "api.example.com")

    def test_external_ws_is_flagged(self) -> None:
        findings = _write_and_scan(_src(
            'ws = "' + _url("ws", "stream.example.com/socket") + '"'
        ))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].scheme, "ws")

    def test_external_mqtt_is_flagged(self) -> None:
        findings = _write_and_scan(_src(
            'broker = "' + _url("mqtt", "broker.example.com:1883") + '"'
        ))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].scheme, "mqtt")

    def test_external_ftp_is_flagged(self) -> None:
        findings = _write_and_scan(_src(
            'mirror = "' + _url("ftp", "files.example.com/pub") + '"'
        ))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].scheme, "ftp")

    # ---- TLS-protected siblings must NEVER be flagged ----

    def test_https_is_not_flagged(self) -> None:
        self.assertEqual(
            _write_and_scan(_src('url = "' + _url("https", "api.example.com") + '"')),
            [],
        )

    def test_wss_is_not_flagged(self) -> None:
        self.assertEqual(
            _write_and_scan(_src('ws = "' + _url("wss", "stream.example.com") + '"')),
            [],
        )

    def test_mqtts_is_not_flagged(self) -> None:
        self.assertEqual(
            _write_and_scan(_src('b = "' + _url("mqtts", "broker.example.com") + '"')),
            [],
        )

    def test_ftps_is_not_flagged(self) -> None:
        self.assertEqual(
            _write_and_scan(_src('m = "' + _url("ftps", "files.example.com") + '"')),
            [],
        )

    def test_sftp_is_not_flagged(self) -> None:
        # ``\b`` at the start of the regex prevents ``sftp`` from being
        # read as ``ftp`` mid-token.
        self.assertEqual(
            _write_and_scan(_src('m = "' + _url("sftp", "files.example.com") + '"')),
            [],
        )

    # ---- internal hosts must not be flagged ----

    def test_internal_localhost(self) -> None:
        self.assertEqual(
            _write_and_scan(_src('u = "' + _url("http", "localhost:3000/api") + '"')),
            [],
        )

    def test_internal_rfc1918(self) -> None:
        self.assertEqual(
            _write_and_scan(_src('u = "' + _url("http", "10.10.0.1/") + '"')),
            [],
        )

    def test_internal_cgnat_tailscale(self) -> None:
        self.assertEqual(
            _write_and_scan(_src('u = "' + _url("http", "100.64.0.5/") + '"')),
            [],
        )

    def test_internal_local_suffix(self) -> None:
        self.assertEqual(
            _write_and_scan(_src('u = "' + _url("http", "printer.local/cups") + '"')),
            [],
        )

    def test_internal_ts_net_suffix(self) -> None:
        self.assertEqual(
            _write_and_scan(_src(
                'u = "' + _url("http", "mainpi.tailnet-abc.ts.net/") + '"'
            )),
            [],
        )

    def test_internal_internal_suffix(self) -> None:
        self.assertEqual(
            _write_and_scan(_src('u = "' + _url("http", "api.internal/") + '"')),
            [],
        )

    def test_internal_bare_docker_service(self) -> None:
        for host in ("pos", "storefront", "db", "redis", "pgbouncer", "mainpi"):
            with self.subTest(host=host):
                self.assertEqual(
                    _write_and_scan(_src(
                        'u = "' + _url("http", host + ":8080/health") + '"'
                    )),
                    [],
                )

    def test_internal_placeholder_dollar_brace(self) -> None:
        self.assertEqual(
            _write_and_scan(_src('u = "' + _url("http", "${HOST}/api") + '"')),
            [],
        )

    def test_internal_placeholder_angle(self) -> None:
        self.assertEqual(
            _write_and_scan(_src('u = "' + _url("http", "<HOST>/api") + '"')),
            [],
        )

    # ---- allow markers ----

    def test_allow_marker_same_line_plaintext(self) -> None:
        src = _src(
            'uri = "' + _url("http", "api.example.com") + '"'
            "  # hanryx-allow-plaintext: documented"
        )
        self.assertEqual(_write_and_scan(src), [])

    def test_allow_marker_same_line_insecure(self) -> None:
        src = _src(
            'uri = "' + _url("http", "api.example.com") + '"'
            "  # hanryx-allow-insecure: documented"
        )
        self.assertEqual(_write_and_scan(src), [])

    def test_allow_marker_line_above_plaintext(self) -> None:
        src = (
            "# hanryx-allow-plaintext: NM captive-portal probe must be HTTP\n"
            + _src('uri = "' + _url("http", "connectivity-check.ubuntu.com") + '"')
        )
        self.assertEqual(_write_and_scan(src), [])

    def test_allow_marker_line_above_insecure(self) -> None:
        src = (
            "# hanryx-allow-insecure: documented reason\n"
            + _src('uri = "' + _url("http", "api.example.com") + '"')
        )
        self.assertEqual(_write_and_scan(src), [])

    def test_allow_marker_two_lines_above_does_not_carry(self) -> None:
        # Pins the "immediately above" rule end-to-end — a marker further
        # up than one line must NOT silently allow-list the URL.
        src = (
            "# hanryx-allow-plaintext: stale, two lines above\n"
            "\n"
            + _src('uri = "' + _url("http", "api.example.com") + '"')
        )
        findings = _write_and_scan(src)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].host, "api.example.com")

    # ---- XML namespaces ----

    def test_xml_namespace_w3(self) -> None:
        # Canonical SVG namespace declaration — not a network call.
        src = _src(
            '<svg xmlns="' + _url("http", "www.w3.org/2000/svg") + '"></svg>'
        )
        self.assertEqual(_write_and_scan(src), [])

    def test_xml_namespace_openxmlformats(self) -> None:
        src = _src(
            'rel = "'
            + _url("http", "schemas.openxmlformats.org/officeDocument/2006/relationships")
            + '"'
        )
        self.assertEqual(_write_and_scan(src), [])

    # ---- tricky negatives — the "silent regression" cases ----

    def test_local_in_middle_is_flagged(self) -> None:
        # Pin the documented contract from the task: a future edit could
        # accidentally widen ``endswith(".local")`` to a substring match
        # and start treating ``evil.local.attacker.com`` as internal.
        # This test would catch that regression.
        src = _src('uri = "' + _url("http", "evil.local.attacker.com/") + '"')
        findings = _write_and_scan(src)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].host, "evil.local.attacker.com")

    def test_localhost_as_subdomain_is_flagged(self) -> None:
        src = _src('uri = "' + _url("http", "localhost.evil.com/") + '"')
        findings = _write_and_scan(src)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].host, "localhost.evil.com")

    def test_external_just_outside_cgnat_is_flagged(self) -> None:
        # 100.128.0.1 is publicly routable — must NOT be allow-listed by
        # the Tailscale-CGNAT branch.
        src = _src('uri = "' + _url("http", "100.128.0.1/") + '"')
        findings = _write_and_scan(src)
        self.assertEqual(len(findings), 1)

    # ---- multiple findings on one line ----

    def test_two_external_urls_on_one_line(self) -> None:
        # The regex is finditer-based, so multiple URLs on one line must
        # each produce their own finding.
        src = _src(
            'a = "' + _url("http", "a.example.com") + '"; '
            'b = "' + _url("http", "b.example.com") + '"'
        )
        findings = _write_and_scan(src)
        self.assertEqual(len(findings), 2)
        hosts = sorted(f.host for f in findings)
        self.assertEqual(hosts, ["a.example.com", "b.example.com"])

    def test_mixed_internal_and_external_on_one_line(self) -> None:
        # Internal one is filtered; external one is reported.
        src = _src(
            'a = "' + _url("http", "localhost") + '"; '
            'b = "' + _url("http", "api.example.com") + '"'
        )
        findings = _write_and_scan(src)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].host, "api.example.com")


if __name__ == "__main__":
    unittest.main()
