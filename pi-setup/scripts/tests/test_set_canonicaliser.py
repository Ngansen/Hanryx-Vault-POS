#!/usr/bin/env python3
"""Tests for the spine-canonicaliser helpers in build_cards_master.py.

Purely tests the in-memory logic: builds a fake canon_map, then verifies
that _build_canonicaliser, _lookup_one, and _lookup_multi behave as
documented. No database, no real CSV — just mathematical contracts that
must hold so a sloppy edit doesn't silently break the consolidator.

Run with:
    python3 -m unittest pi-setup.scripts.tests.test_set_canonicaliser -v

The script is loaded by name (build_cards_master.py is at the project
root, not under a package) using importlib so we don't depend on
PYTHONPATH layout.
"""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
TARGET = PROJECT_ROOT / "build_cards_master.py"

# Make `unified.*` resolvable when build_cards_master imports it.
sys.path.insert(0, str(PROJECT_ROOT))


def _stub_module(name: str, **attrs) -> types.ModuleType:
    """Register a placeholder module so import succeeds in environments
    that don't have the real driver installed (CI, dev laptops). The
    helpers under test never call any of these — they only touch dict
    primitives — so stubs are safe."""
    mod = sys.modules.get(name) or types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


try:
    import psycopg2  # type: ignore  # noqa: F401
except ImportError:
    psycopg2_mod    = _stub_module("psycopg2")
    psycopg2_errors = _stub_module("psycopg2.errors", UndefinedTable=Exception)
    psycopg2_extras = _stub_module("psycopg2.extras", RealDictCursor=object,
                                    execute_values=lambda *a, **kw: None)
    setattr(psycopg2_mod, "errors", psycopg2_errors)
    setattr(psycopg2_mod, "extras", psycopg2_extras)
    setattr(psycopg2_mod, "Error",  Exception)

# unified.local_images / unified.priority / unified.schema may also be
# missing dependencies on a bare CI box. They're imported by name from
# build_cards_master.py — stub the symbols the consolidator references.
try:
    from unified import local_images  # type: ignore  # noqa: F401
except Exception:
    _stub_module("unified")
    _stub_module("unified.local_images",
                 local_path_for=lambda *a, **kw: "",
                 SOURCE_LANG={})
    _stub_module("unified.priority", PRIORITY={}, AGGREGATES={})
    _stub_module("unified.schema",
                 init_unified_schema=lambda *a, **kw: None)

spec = importlib.util.spec_from_file_location("build_cards_master_under_test",
                                              TARGET)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


class CanonicaliserTests(unittest.TestCase):
    """_build_canonicaliser must be a SAFE, case-tolerant identity for
    unknown codes and a lowercase-EN rewriter for known ones."""

    def setUp(self):
        # Mirrors what _read_set_canonicaliser_map would produce from a
        # ref_set_alias row {EN=SV2, JP=SV2, KR=S2, CHS=CS2}.
        self.canon_map = {
            "SV2":  "sv2",
            "S2":   "sv2",
            "CS2":  "sv2",
            # JP-only set (no EN abbrev): canonical falls back to JP code
            "SV2A": "sv2a",
            # EN-only set: canonical = EN itself
            "PR":   "pr",
        }
        self.canon = module._build_canonicaliser(self.canon_map)

    def test_known_code_lowercases_to_canonical(self):
        self.assertEqual(self.canon("SV2"), "sv2")
        self.assertEqual(self.canon("sv2"), "sv2")  # already canonical
        self.assertEqual(self.canon("Sv2"), "sv2")  # mixed case

    def test_known_kr_chs_codes_canonicalise_to_en(self):
        self.assertEqual(self.canon("S2"),  "sv2")
        self.assertEqual(self.canon("s2"),  "sv2")
        self.assertEqual(self.canon("CS2"), "sv2")

    def test_jp_only_set_canonicalises_to_jp_code(self):
        self.assertEqual(self.canon("SV2A"), "sv2a")
        self.assertEqual(self.canon("sv2a"), "sv2a")

    def test_unknown_code_passes_through_unchanged(self):
        # JP-only sets not in the CSV (e.g. older promos) must NOT get
        # rewritten — pass-through preserves whatever spine key they had.
        self.assertEqual(self.canon("MYSTERYSET"), "MYSTERYSET")
        self.assertEqual(self.canon("p-a"),        "p-a")

    def test_empty_inputs_round_trip(self):
        self.assertEqual(self.canon(""),    "")
        self.assertEqual(self.canon(None),  None)  # type: ignore[arg-type]
        self.assertEqual(self.canon("   "), "   ")  # whitespace-only

    def test_strips_surrounding_whitespace_for_lookup(self):
        # Lookup is case-insensitive AND whitespace-tolerant.
        self.assertEqual(self.canon("  SV2  "), "sv2")

    def test_empty_canon_map_makes_canonicaliser_a_noop(self):
        # Fresh DB before ref_set_alias is populated → canon_map is {}
        # → canonicaliser must be a strict pass-through. This is what
        # makes the slice safe to deploy before importing the CSV.
        noop = module._build_canonicaliser({})
        self.assertEqual(noop("SV2"),       "SV2")
        self.assertEqual(noop("anything"),  "anything")


class LookupHelpersTests(unittest.TestCase):
    """_lookup_one and _lookup_multi must walk every candidate key
    against a source dict and return the first match (or all matches
    for the multi variant)."""

    def setUp(self):
        # Single-row source — keyed by ORIGINAL set_id, never canonical.
        self.single_src = {
            ("SV2",  "001"): {"name": "EN-row"},
            ("S2",   "002"): {"name": "KR-row"},
            ("sv2a", "010"): {"name": "JP-row"},
        }
        # Multi-row source (eng_ex/jp_ex shape).
        self.multi_src = {
            ("SV2", "001"): [{"code": "EN-A"}, {"code": "EN-B"}],
            ("S2",  "001"): [{"code": "KR-A"}],
        }
        # Stub _alt_keys_for_number — return nothing extra for tests.
        self.no_alts = lambda n: ()

    def test_lookup_one_finds_first_candidate(self):
        candidates = [("SV2", "001"), ("S2", "001")]
        row, key = module._lookup_one(self.single_src, candidates,
                                       self.no_alts)
        self.assertEqual(row, {"name": "EN-row"})
        self.assertEqual(key, ("SV2", "001"))

    def test_lookup_one_falls_through_to_second_candidate(self):
        # First candidate misses → try the second
        candidates = [("MISS", "001"), ("S2", "002")]
        row, key = module._lookup_one(self.single_src, candidates,
                                       self.no_alts)
        self.assertEqual(row, {"name": "KR-row"})
        self.assertEqual(key, ("S2", "002"))

    def test_lookup_one_returns_none_when_nothing_matches(self):
        row, key = module._lookup_one(self.single_src,
                                       [("X", "999")], self.no_alts)
        self.assertIsNone(row)
        self.assertIsNone(key)

    def test_lookup_one_uses_alt_card_number_forms(self):
        alt_fn = lambda n: ("001",) if n == "1" else ()
        row, key = module._lookup_one(self.single_src,
                                       [("SV2", "1")], alt_fn)
        self.assertEqual(row, {"name": "EN-row"})
        self.assertEqual(key, ("SV2", "001"))

    def test_lookup_one_handles_empty_candidate_list(self):
        row, key = module._lookup_one(self.single_src, [], self.no_alts)
        self.assertIsNone(row)
        self.assertIsNone(key)

    def test_lookup_multi_concatenates_across_candidates(self):
        # Both candidates have rows → both lists get concatenated, so a
        # canonicalised card collects EX codes from BOTH its EN and KR
        # source rows in one pass.
        candidates = [("SV2", "001"), ("S2", "001")]
        rows = module._lookup_multi(self.multi_src, candidates)
        self.assertEqual(len(rows), 3)
        codes = [r["code"] for r in rows]
        self.assertIn("EN-A", codes)
        self.assertIn("EN-B", codes)
        self.assertIn("KR-A", codes)

    def test_lookup_multi_returns_empty_for_misses(self):
        rows = module._lookup_multi(self.multi_src,
                                     [("MISS", "999")])
        self.assertEqual(rows, [])

    def test_lookup_multi_handles_empty_candidate_list(self):
        self.assertEqual(module._lookup_multi(self.multi_src, []), [])


class CanonMapEndToEndTests(unittest.TestCase):
    """Wire the canonicaliser into the candidate-grouping logic the way
    _run does, and verify cross-region duplicate collapse end-to-end."""

    def test_two_regional_sources_collapse_into_one_canonical_spine_row(self):
        """The whole point of Slice 7: the same physical card appearing
        under both its EN and KR set_ids (e.g. SV2/001 and S2/001) must
        end up as ONE canonical spine row, with both originals available
        in the candidates list for downstream lookups."""
        from collections import defaultdict
        canon_map = {"SV2": "sv2", "S2": "sv2"}
        canon = module._build_canonicaliser(canon_map)
        sources = {
            "tcg_api":     {("SV2", "001"): {"name_en": "Mew"}},
            "kr_official": {("S2",  "001"): {"name_kr": "뮤"}},
        }
        candidates_by_canon: dict = defaultdict(list)
        for src in sources.values():
            for orig_key in src.keys():
                osid, onum = orig_key
                candidates_by_canon[(canon(osid), onum)].append(orig_key)
        self.assertEqual(len(candidates_by_canon), 1)
        self.assertIn(("sv2", "001"), candidates_by_canon)
        self.assertEqual(set(candidates_by_canon[("sv2", "001")]),
                         {("SV2", "001"), ("S2", "001")})

    def test_unknown_set_does_not_collapse_with_known_set(self):
        """A spine row from a JP-only set (not in canon_map) must NOT
        accidentally merge with anything else — pass-through identity."""
        from collections import defaultdict
        canon_map = {"SV2": "sv2"}
        canon = module._build_canonicaliser(canon_map)
        sources = {
            "tcg_api":  {("SV2",       "001"): {"x": 1}},
            "jp_pokell": {("UNKNOWNJP", "001"): {"y": 2}},
        }
        candidates_by_canon: dict = defaultdict(list)
        for src in sources.values():
            for orig_key in src.keys():
                osid, onum = orig_key
                candidates_by_canon[(canon(osid), onum)].append(orig_key)
        self.assertEqual(len(candidates_by_canon), 2)
        self.assertIn(("sv2",       "001"), candidates_by_canon)
        self.assertIn(("UNKNOWNJP", "001"), candidates_by_canon)


if __name__ == "__main__":
    unittest.main(verbosity=2)
