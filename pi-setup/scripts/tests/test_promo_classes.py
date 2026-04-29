#!/usr/bin/env python3
"""
test_promo_classes.py — unit tests for the Slice 8 ref_promo_class
helpers in build_cards_master.py.

Coverage:
  * _read_promo_class_index:
      - indexes a class under each non-empty region code (uppercased)
      - skips an empty/None code field cleanly
      - DEDUPLICATES: a class with EN=KR=CN='SM-P' appears only ONCE
        under 'SM-P', not three times
      - multi-class JP catch-all 'プロモ' yields all classes under
        the same key
  * _enrich_promo_class:
      - empty index → guaranteed no-op (deploy-safety: the slice is
        safe to ship before the importer runs)
      - existing promo_source from per-card ref_promo lookup wins
      - 0 matches → no write, no source_refs entry
      - exactly 1 match → 'Category: Name' + class_id reference
      - multi-match → 'Promo bucket (N candidate classes)' + every
        class_id concatenated, sorted, into source_refs
      - case-insensitive on set_id ('sm-p' matches 'SM-P')
      - empty/None set_id is safe (no crash, no enrichment)
      - lowercase JP kana 'プロモ' matches uppercase index lookup
        (Japanese kana have no case so .upper() is a no-op — verifying
        we don't break on non-ASCII codes)
      - missing promo_category falls back to 'Promo'
      - missing promo_name uses class_id; missing both yields just cat
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

sys.path.insert(0, str(PROJECT_ROOT))


def _stub_module(name: str, **attrs) -> types.ModuleType:
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


# Sample fixtures matching the user's CSV structure.
# Use camelCase fields exactly as ref_promo_class columns.
P001_PIKACHU = {
    "class_id":       "P-001",
    "promo_name":     "Pikachu Promo",
    "promo_category": "Movie Promo",
    "code_en":        "SM234",
    "code_jp":        "プロモ",
    "code_kr":        "SM-P",
    "code_chs":       "SM-P",        # CN reuses the SM-P bucket
}
P002_EEVEE = {
    "class_id":       "P-002",
    "promo_name":     "Eevee Promo",
    "promo_category": "Product Promo",
    "code_en":        "SWSH promo",
    "code_jp":        "プロモ",       # same JP catch-all as P-001
    "code_kr":        "S-P",
    "code_chs":       "S-P",
}
P011_WCS = {
    "class_id":       "P-011",
    "promo_name":     "World Championship Promo",
    "promo_category": "Championship",
    "code_en":        "WCS",
    "code_jp":        "WCS",
    "code_kr":        "WCS",
    "code_chs":       "WCS",
}
P004_MEW = {
    "class_id":       "P-004",
    "promo_name":     "Mew Anniversary",
    "promo_category": "Anniversary Promo",
    "code_en":        "SM-P",        # EN also uses SM-P, collides with P-001's KR
    "code_jp":        "プロモ",
    "code_kr":        "SM-P",
    "code_chs":       "SM-P",
}


class _FakeCursor:
    """Stand-in for a psycopg2 RealDictCursor — only execute() and
    fetchall() are exercised by the helpers under test."""
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        # The helper only issues one SELECT — accept anything.
        pass

    def fetchall(self):
        return list(self._rows)


class ReadPromoClassIndexTests(unittest.TestCase):
    """_read_promo_class_index unions the four region-code columns into
    a single CODE_UPPER → [class_dict, …] index, deduplicating per row."""

    def test_empty_table_returns_empty_dict(self):
        idx = module._read_promo_class_index(_FakeCursor([]))
        self.assertEqual(idx, {})

    def test_undefined_table_returns_empty_dict(self):
        class _Bad(_FakeCursor):
            def execute(self, sql, params=None):
                raise module.psycopg2.errors.UndefinedTable("no table")
        idx = module._read_promo_class_index(_Bad([]))
        self.assertEqual(idx, {})

    def test_single_class_indexed_under_each_region_code(self):
        idx = module._read_promo_class_index(_FakeCursor([P002_EEVEE]))
        # Eevee has 4 codes but KR=CN='S-P' → 3 unique upper-keys.
        self.assertIn("SWSH PROMO", idx)
        self.assertIn("プロモ",     idx)
        self.assertIn("S-P",       idx)
        self.assertEqual(len(idx["SWSH PROMO"]), 1)
        self.assertEqual(len(idx["プロモ"]),     1)
        self.assertEqual(len(idx["S-P"]),       1)
        # Crucial: the class is NOT double-listed under S-P just because
        # both KR and CN columns spelled it the same way.
        self.assertEqual(idx["S-P"][0]["class_id"], "P-002")

    def test_dedup_when_three_regions_share_one_code(self):
        # Mew Anniversary: EN=KR=CN='SM-P'. Should appear ONCE under
        # SM-P, not three times.
        idx = module._read_promo_class_index(_FakeCursor([P004_MEW]))
        self.assertEqual(len(idx.get("SM-P", [])), 1)
        self.assertEqual(idx["SM-P"][0]["class_id"], "P-004")

    def test_jp_catch_all_collects_multiple_classes(self):
        # The whole point of the multi-list design: 'プロモ' should
        # accumulate every class that uses it.
        idx = module._read_promo_class_index(_FakeCursor(
            [P001_PIKACHU, P002_EEVEE, P004_MEW, P011_WCS]))
        promo_classes = idx.get("プロモ", [])
        ids = sorted(c["class_id"] for c in promo_classes)
        # P-011 has JP='WCS' not 'プロモ', so it's NOT under this key.
        self.assertEqual(ids, ["P-001", "P-002", "P-004"])

    def test_skips_empty_code_columns(self):
        partial = dict(P001_PIKACHU)
        partial["code_chs"] = ""
        partial["code_en"]  = None  # type: ignore[assignment]
        idx = module._read_promo_class_index(_FakeCursor([partial]))
        # Only JP and KR codes survive.
        self.assertIn("プロモ", idx)
        self.assertIn("SM-P",  idx)
        self.assertNotIn("",   idx)
        self.assertNotIn("SM234", idx)  # was code_en, now None


class EnrichPromoClassTests(unittest.TestCase):
    """_enrich_promo_class performs in-place enrichment of the per-card
    `out` dict's promo_source field, with multi-match awareness."""

    def setUp(self):
        # Build the index once; each test starts from a fresh `out` dict.
        self.idx = module._read_promo_class_index(_FakeCursor(
            [P001_PIKACHU, P002_EEVEE, P004_MEW, P011_WCS]))

    def test_empty_index_is_noop(self):
        out, refs = {}, {}
        module._enrich_promo_class(out, refs, "SM-P", {})
        self.assertEqual(out, {})
        self.assertEqual(refs, {})

    def test_existing_promo_source_is_preserved(self):
        # Per-card ref_promo lookup wins — never overwrite.
        out  = {"promo_source": "Black Star Promo (per-card)"}
        refs = {"promo_source": "ref_promo_provenance:abc"}
        module._enrich_promo_class(out, refs, "SM-P", self.idx)
        self.assertEqual(out["promo_source"], "Black Star Promo (per-card)")
        self.assertEqual(refs["promo_source"], "ref_promo_provenance:abc")

    def test_zero_matches_is_noop(self):
        out, refs = {}, {}
        module._enrich_promo_class(out, refs, "definitely-not-a-promo-code",
                                   self.idx)
        self.assertEqual(out, {})
        self.assertEqual(refs, {})

    def test_empty_set_id_is_safe(self):
        out, refs = {}, {}
        module._enrich_promo_class(out, refs, "", self.idx)
        module._enrich_promo_class(out, refs, None, self.idx)  # type: ignore[arg-type]
        self.assertEqual(out, {})
        self.assertEqual(refs, {})

    def test_single_match_uses_category_and_name(self):
        # WCS is unambiguous — single class P-011.
        out, refs = {}, {}
        module._enrich_promo_class(out, refs, "WCS", self.idx)
        self.assertEqual(out["promo_source"],
                         "Championship: World Championship Promo")
        self.assertEqual(refs["promo_source"], "ref_promo_class:P-011")

    def test_single_match_is_case_insensitive(self):
        # Spine canonicaliser sometimes lowercases set_ids — enrichment
        # must still hit because the index is uppercase-keyed.
        out, refs = {}, {}
        module._enrich_promo_class(out, refs, "wcs", self.idx)
        self.assertEqual(refs["promo_source"], "ref_promo_class:P-011")

    def test_single_match_with_jp_kana_set_id(self):
        # The CSV pulls in non-ASCII codes ('プロモ', 'コロコロ').
        # .upper() on Japanese kana is a no-op — verify we don't crash
        # and the lookup still works. With three classes sharing 'プロモ'
        # in the fixture, this is actually a multi-match path.
        out, refs = {}, {}
        module._enrich_promo_class(out, refs, "プロモ", self.idx)
        self.assertEqual(
            out["promo_source"],
            "Promo bucket (3 candidate classes)",
        )
        self.assertEqual(
            refs["promo_source"],
            "ref_promo_class:P-001,P-002,P-004",
        )

    def test_multi_match_records_all_candidate_ids_sorted(self):
        # SM-P matches P-001 (KR/CN), P-004 (EN/KR/CN). Sorted ASC.
        out, refs = {}, {}
        module._enrich_promo_class(out, refs, "SM-P", self.idx)
        self.assertEqual(
            out["promo_source"],
            "Promo bucket (2 candidate classes)",
        )
        self.assertEqual(
            refs["promo_source"],
            "ref_promo_class:P-001,P-004",
        )

    def test_falls_back_when_category_missing(self):
        weird = {
            "class_id":       "P-X",
            "promo_name":     "Mystery Promo",
            "promo_category": "",          # empty
            "code_en":        "MYSTERY",
        }
        idx = module._read_promo_class_index(_FakeCursor([weird]))
        out, refs = {}, {}
        module._enrich_promo_class(out, refs, "MYSTERY", idx)
        self.assertEqual(out["promo_source"], "Promo: Mystery Promo")
        self.assertEqual(refs["promo_source"], "ref_promo_class:P-X")

    def test_falls_back_when_name_missing(self):
        weird = {
            "class_id":       "P-Y",
            "promo_name":     "",
            "promo_category": "Staff Only",
            "code_en":        "STAFF-ONLY",
        }
        idx = module._read_promo_class_index(_FakeCursor([weird]))
        out, refs = {}, {}
        module._enrich_promo_class(out, refs, "STAFF-ONLY", idx)
        # No name → just the category, no trailing colon.
        self.assertEqual(out["promo_source"], "Staff Only: P-Y")
        self.assertEqual(refs["promo_source"], "ref_promo_class:P-Y")


if __name__ == "__main__":
    unittest.main(verbosity=2)
