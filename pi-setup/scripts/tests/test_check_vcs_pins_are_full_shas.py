"""
Unit tests for ``pi-setup/scripts/check-vcs-pins-are-full-shas.py``.

The checker is a CI gate that protects the byte-for-byte rebuild
guarantee for every git+ pin in ``pi-setup/requirements-vcs.txt``
(currently OpenAI CLIP, plus any future git+ deps). ``uv pip compile``
cannot hash git+ URLs, so the only thing standing between us and silent
upstream drift is that each URL ends in a full 40-char lowercase
hex commit SHA. A branch name (``@main``), a tag (``@v1.0``), a short
SHA (``@dcba3cb``), or an uppercase SHA (``@DCBA...``) all *look*
plausible in a diff but quietly break reproducibility — the operator
only finds out weeks later when the Pi rebuild produces a different
binary.

The whole guarantee rests on two regexes:

* ``GIT_PIN_RE`` — extracts the ``@<ref>`` suffix from each
  ``git+http(s)://.../repo.git@<ref>`` line. Must capture every pin in
  the file but NOT pick up text that merely contains an ``@`` (an
  inline comment, a different scheme, etc.).
* ``FULL_SHA_RE`` — accepts only ``[0-9a-f]{40}`` anchored to the
  whole captured ref. Anything else fails.

Plus the comment-skip rule in ``main``: full-line comments may
legitimately contain example branch names like ``@main`` in
documentation, and must NOT trigger a false positive.

A sloppy edit to either regex (e.g. dropping ``\\A``/``\\Z`` anchors,
or accepting ``[0-9a-fA-F]``) would silently start letting bad pins
through, and the only signal would be a broken Pi rebuild. These tests
lock the contract end-to-end.

Runnable both with ``pytest`` and with ``python -m unittest`` so the
CI job needs no third-party dependency installed.
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


# The script's filename has hyphens, so a normal ``import`` won't work.
# Load it via importlib so we can call ``main`` directly and exercise
# the regexes without subprocess plumbing.
_HERE = Path(__file__).resolve().parent
_CHECKER_PATH = _HERE.parent / "check-vcs-pins-are-full-shas.py"

_spec = importlib.util.spec_from_file_location(
    "check_vcs_pins_are_full_shas", _CHECKER_PATH
)
assert _spec is not None and _spec.loader is not None, (
    f"cannot load {_CHECKER_PATH}"
)
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)


# A real-shaped 40-char lowercase hex SHA used across the tests.
GOOD_SHA = "dcba3cb2e2827b402d2701e7e1c7d9fed8a20ef1"
GOOD_SHA_2 = "0123456789abcdef0123456789abcdef01234567"


def _git_line(ref: str, repo: str = "https://github.com/openai/CLIP.git") -> str:
    """Build a realistic ``clip @ git+<repo>@<ref>`` requirements line."""
    return f"clip @ git+{repo}@{ref}\n"


# ---------------------------------------------------------------------------
# FULL_SHA_RE — the actual gatekeeper
# ---------------------------------------------------------------------------


class FullShaRegexTests(unittest.TestCase):
    """``FULL_SHA_RE`` must accept ONLY a 40-char lowercase hex string."""

    def test_canonical_full_sha_accepted(self) -> None:
        # The exact pin currently in requirements-vcs.txt — the
        # baseline this test suite is anchored around.
        self.assertIsNotNone(checker.FULL_SHA_RE.match(GOOD_SHA))

    def test_all_zeros_full_length_accepted(self) -> None:
        # All-zero is structurally a valid SHA shape; the checker
        # cares about the *form*, not whether it's a real commit.
        self.assertIsNotNone(checker.FULL_SHA_RE.match("0" * 40))

    def test_short_sha_rejected(self) -> None:
        # The most common drift: someone abbreviates to 7 chars (the
        # default ``git rev-parse --short`` length). Short SHAs are
        # NOT content hashes — they're prefix-collision-prone and
        # let upstream silently drift.
        self.assertIsNone(checker.FULL_SHA_RE.match("dcba3cb"))
        self.assertIsNone(checker.FULL_SHA_RE.match("dcba3cb2e"))

    def test_uppercase_sha_rejected(self) -> None:
        # Git itself will accept uppercase, but the regex is
        # deliberately lowercase-only so we have a single canonical
        # form in the file. Otherwise a diff between ``DEADBEEF...``
        # and ``deadbeef...`` would look like a real change when it
        # isn't, and reviewers would tune out.
        self.assertIsNone(checker.FULL_SHA_RE.match(GOOD_SHA.upper()))

    def test_mixed_case_sha_rejected(self) -> None:
        # Defensive: even one uppercase char means it's not the
        # canonical form.
        self.assertIsNone(
            checker.FULL_SHA_RE.match("Dcba3cb2e2827b402d2701e7e1c7d9fed8a20ef1")
        )

    def test_branch_name_main_rejected(self) -> None:
        # The whole reason this script exists.
        self.assertIsNone(checker.FULL_SHA_RE.match("main"))
        self.assertIsNone(checker.FULL_SHA_RE.match("master"))

    def test_tag_v_prefix_rejected(self) -> None:
        # ``v1.0`` — looks specific, drifts silently when upstream
        # force-moves the tag.
        self.assertIsNone(checker.FULL_SHA_RE.match("v1.0"))
        self.assertIsNone(checker.FULL_SHA_RE.match("v1.0.0"))

    def test_branch_name_with_slash_rejected(self) -> None:
        # ``release/2024-07`` style branch names contain a `/` which
        # the URL regex still captures as part of the ref.
        self.assertIsNone(checker.FULL_SHA_RE.match("release/2024-07"))

    def test_empty_string_rejected(self) -> None:
        self.assertIsNone(checker.FULL_SHA_RE.match(""))

    def test_too_long_rejected(self) -> None:
        # 41 chars is not a SHA — the ``\Z`` anchor must reject it
        # rather than matching the leading 40 and ignoring the tail.
        self.assertIsNone(checker.FULL_SHA_RE.match(GOOD_SHA + "a"))

    def test_non_hex_char_rejected(self) -> None:
        # ``g`` is not a hex digit. Catches a typo where someone
        # mistypes a SHA character.
        bad = "g" + GOOD_SHA[1:]
        self.assertIsNone(checker.FULL_SHA_RE.match(bad))


# ---------------------------------------------------------------------------
# GIT_PIN_RE — the extractor
# ---------------------------------------------------------------------------


class GitPinRegexTests(unittest.TestCase):
    """``GIT_PIN_RE`` must find every ``@<ref>`` suffix on a git+ URL."""

    def test_extracts_ref_after_dot_git(self) -> None:
        m = checker.GIT_PIN_RE.search(_git_line(GOOD_SHA))
        assert m is not None
        self.assertEqual(m.group(1), GOOD_SHA)

    def test_stops_at_egg_fragment_marker(self) -> None:
        # ``#egg=clip`` is a legacy pip URL fragment. The capture
        # must stop at the ``#`` so the egg fragment doesn't pollute
        # the captured ref (which would then fail SHA validation as
        # malformed even when the SHA itself is fine).
        line = f"git+https://github.com/openai/CLIP.git@{GOOD_SHA}#egg=clip\n"
        m = checker.GIT_PIN_RE.search(line)
        assert m is not None
        self.assertEqual(m.group(1), GOOD_SHA)

    def test_stops_at_whitespace(self) -> None:
        # Trailing whitespace / a comment after the URL must not be
        # captured as part of the ref.
        line = f"clip @ git+https://github.com/openai/CLIP.git@{GOOD_SHA}  # comment\n"
        m = checker.GIT_PIN_RE.search(line)
        assert m is not None
        self.assertEqual(m.group(1), GOOD_SHA)

    def test_finds_all_pins_on_separate_lines(self) -> None:
        # finditer over a multi-line blob must produce one match per
        # pin — the file is read line-by-line in main(), but the
        # regex itself should also work on a multi-line string.
        text = _git_line(GOOD_SHA) + _git_line(GOOD_SHA_2)
        refs = [m.group(1) for m in checker.GIT_PIN_RE.finditer(text)]
        self.assertEqual(refs, [GOOD_SHA, GOOD_SHA_2])

    def test_does_not_match_non_git_url(self) -> None:
        # A regular https URL (e.g. a doc link in a comment) must
        # NOT be picked up — only ``git+`` URLs are pins.
        self.assertIsNone(
            checker.GIT_PIN_RE.search("https://github.com/openai/CLIP@main\n")
        )

    def test_matches_git_plus_http_too(self) -> None:
        # Defensive: the regex allows http as well as https. We
        # don't expect an http pin in practice (a separate check
        # blocks plaintext HTTP), but the SHA gate must still catch
        # one if it slipped through — defense in depth.
        line = f"git+http://example.invalid/repo.git@{GOOD_SHA}\n"
        m = checker.GIT_PIN_RE.search(line)
        assert m is not None
        self.assertEqual(m.group(1), GOOD_SHA)


# ---------------------------------------------------------------------------
# main() — end-to-end against a synthetic requirements-vcs.txt
# ---------------------------------------------------------------------------


class MainEndToEndTests(unittest.TestCase):
    """End-to-end: parser + comment-skip + SHA validator + exit code."""

    def _run_main(self, body: str) -> int:
        """Run ``main()`` against an in-memory ``requirements-vcs.txt``.

        ``checker.VCS_FILE`` is patched to point at a tempfile so the
        script reads our synthetic content instead of the real repo
        file — the same content the real check would see in CI.
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "requirements-vcs.txt"
            tmp.write_text(body, encoding="utf-8")
            with mock.patch.object(checker, "VCS_FILE", tmp):
                return checker.main()

    # --- the cases enumerated in the task -----------------------------------

    def test_valid_full_sha_passes(self) -> None:
        # The current real-world pin shape — must exit 0.
        rc = self._run_main(_git_line(GOOD_SHA))
        self.assertEqual(rc, 0)

    def test_short_sha_fails(self) -> None:
        # Short SHAs are prefix-collision-prone and not content
        # hashes — must be rejected.
        rc = self._run_main(_git_line("dcba3cb"))
        self.assertEqual(rc, 1)

    def test_branch_name_at_main_fails(self) -> None:
        # ``@main`` is the canonical "drifts silently" pin.
        rc = self._run_main(_git_line("main"))
        self.assertEqual(rc, 1)

    def test_tag_at_v1_0_fails(self) -> None:
        # Tags can be force-moved upstream → not reproducible.
        rc = self._run_main(_git_line("v1.0"))
        self.assertEqual(rc, 1)

    def test_uppercase_sha_fails(self) -> None:
        # Same hex value, wrong case — rejected to keep one
        # canonical form in the file.
        rc = self._run_main(_git_line(GOOD_SHA.upper()))
        self.assertEqual(rc, 1)

    def test_empty_file_passes(self) -> None:
        # A future cleanup might empty requirements-vcs.txt entirely
        # (no more git+ deps). That's a green result — there's
        # nothing to validate, so CI must NOT start failing.
        rc = self._run_main("")
        self.assertEqual(rc, 0)

    def test_file_with_only_comments_passes(self) -> None:
        # Same idea: a file that's all banner / docs and no actual
        # pins must pass. Importantly, the example ``@main`` text in
        # a full-line comment must NOT be misread as a real pin —
        # otherwise the file's own documentation would fail the
        # check. The comment-skip in main() exists for exactly this.
        body = (
            "# requirements-vcs.txt\n"
            "# Example bad pin (do NOT do this):\n"
            "#   clip @ git+https://github.com/openai/CLIP.git@main\n"
            "# Example bad pin (do NOT do this):\n"
            "#   clip @ git+https://github.com/openai/CLIP.git@v1.0\n"
        )
        rc = self._run_main(body)
        self.assertEqual(rc, 0)

    def test_multiple_pins_one_bad_fails(self) -> None:
        # The first pin is a perfectly valid full SHA; the second is
        # a branch name. The whole run must fail — a single bad pin
        # is enough to break the rebuild guarantee, so we can't let
        # the good pin mask it.
        body = _git_line(GOOD_SHA) + _git_line("main", repo="https://github.com/x/y.git")
        rc = self._run_main(body)
        self.assertEqual(rc, 1)

    # --- additional safety nets --------------------------------------------

    def test_multiple_pins_all_good_passes(self) -> None:
        # Future-proofing: when a second git+ dep is added, two
        # full SHAs in the same file must still pass cleanly. This
        # is the "happy path" for the multi-pin case.
        body = (
            _git_line(GOOD_SHA)
            + _git_line(GOOD_SHA_2, repo="https://github.com/x/y.git")
        )
        rc = self._run_main(body)
        self.assertEqual(rc, 0)

    def test_inline_comment_with_at_main_does_not_fail_a_good_pin(self) -> None:
        # An inline comment after a perfectly valid pin must NOT be
        # captured as part of the ref. Because the regex stops at
        # whitespace, the trailing ``# bumped from @main`` text
        # shouldn't introduce a phantom bad pin.
        line = (
            f"clip @ git+https://github.com/openai/CLIP.git@{GOOD_SHA}"
            "  # bumped from @main on 2024-07\n"
        )
        rc = self._run_main(line)
        self.assertEqual(rc, 0)

    def test_missing_file_fails_loudly(self) -> None:
        # If requirements-vcs.txt is renamed / deleted by accident,
        # the script must fail rather than silently exit 0 — the
        # "no pins is a pass" branch only applies when the file
        # exists and is empty.
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "does-not-exist.txt"
            with mock.patch.object(checker, "VCS_FILE", missing):
                rc = checker.main()
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
