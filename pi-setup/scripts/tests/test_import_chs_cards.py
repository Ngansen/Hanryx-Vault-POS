"""
Tests for import_chs_cards (Slice E — importer test coverage seed).

The CHS importer is the most-used path for Simplified-Chinese cards
and the largest catalogue (~21 MB JSON). Bugs in its pure functions
(_walk_cards / _looks_like_card / _build_row / _safe_int) silently
drop or mangle thousands of rows — the kind of corruption that's
invisible until the booth, when a customer hands you a card and the
lookup misses.

Hermetic: no network, no clone, no DB. Targets the pure functions
plus a FakeCursor-backed _flush_batch UPSERT shape check.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from import_chs_cards import (
    _walk_cards, _looks_like_card, _build_row, _safe_int,
    _flush_batch, IMG_RAW_BASE,
)


# ── FakeConn / FakeCursor (executemany-aware) ───────────────────


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.executed: list[tuple[str, object]] = []
        self.executed_many: list[tuple[str, list]] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def executemany(self, sql, batch):
        # Capture both the SQL and the full batch for later assertions.
        self.executed_many.append((sql, list(batch)))


class FakeConn:
    def __init__(self):
        self.cursors: list[FakeCursor] = []
        self.commits = 0

    def cursor(self):
        c = FakeCursor(self)
        self.cursors.append(c)
        return c

    def commit(self):
        self.commits += 1


# ── _safe_int ──────────────────────────────────────────────────


class SafeIntTests(unittest.TestCase):

    def test_passthrough_on_int(self):
        self.assertEqual(_safe_int(7), 7)
        self.assertEqual(_safe_int(0), 0)
        self.assertEqual(_safe_int(-3), -3)

    def test_string_int_coerced(self):
        self.assertEqual(_safe_int("120"), 120)
        # A leading '0' is fine — int() handles it.
        self.assertEqual(_safe_int("042"), 42)

    def test_none_and_empty_become_none(self):
        self.assertIsNone(_safe_int(None))
        self.assertIsNone(_safe_int(""))

    def test_garbage_becomes_none_not_exception(self):
        # The importer must NEVER raise on a malformed HP value
        # — that would abort an import of 15K cards because one
        # row had `"hp": "—"`.
        self.assertIsNone(_safe_int("not-a-number"))
        self.assertIsNone(_safe_int("∞"))
        self.assertIsNone(_safe_int(object()))


# ── _looks_like_card ────────────────────────────────────────────


class LooksLikeCardTests(unittest.TestCase):

    def test_minimum_shape_with_details_name(self):
        # Modern shape: cardName lives under details.
        self.assertTrue(_looks_like_card({
            "id": 1, "details": {"cardName": "皮卡丘"}
        }))

    def test_minimum_shape_with_top_level_name(self):
        # Some legacy rows had `name` at the top level instead.
        # The importer accepts either.
        self.assertTrue(_looks_like_card({
            "id": 1, "name": "Pikachu", "details": {}
        }))

    def test_string_id_rejected(self):
        # `id` must be int — string ids belong to enum/dict nodes,
        # not card records, and accepting them would yield phantom
        # cards from the upstream `dict` enum block.
        self.assertFalse(_looks_like_card({
            "id": "1", "details": {"cardName": "x"}
        }))

    def test_missing_details_rejected(self):
        self.assertFalse(_looks_like_card({"id": 1, "name": "x"}))

    def test_details_must_be_dict(self):
        self.assertFalse(_looks_like_card({
            "id": 1, "name": "x", "details": []
        }))

    def test_no_name_anywhere_rejected(self):
        # Without a card name there's no useful row to insert.
        self.assertFalse(_looks_like_card({
            "id": 1, "details": {"hp": 100}
        }))


# ── _walk_cards ────────────────────────────────────────────────


class WalkCardsTests(unittest.TestCase):

    def _card(self, cid: int, name: str = "x") -> dict:
        return {"id": cid, "details": {"cardName": name}}

    def test_walks_top_level_list(self):
        node = [self._card(1), self._card(2)]
        ids = [c["id"] for c in _walk_cards(node)]
        self.assertEqual(ids, [1, 2])

    def test_walks_nested_dicts(self):
        # Top-level CHS shape: {"dict": {...enums...}, "cards": [...]}
        node = {
            "dict": {"rarity": ["C", "U", "R"]},   # not card-shaped
            "cards": [self._card(1), self._card(2)],
        }
        ids = sorted(c["id"] for c in _walk_cards(node))
        self.assertEqual(ids, [1, 2])

    def test_walks_deeply_nested(self):
        # The walker must not require cards to be at any particular depth.
        node = {"a": {"b": {"c": [self._card(99)]}}}
        ids = [c["id"] for c in _walk_cards(node)]
        self.assertEqual(ids, [99])

    def test_card_records_are_not_descended_into(self):
        # If a card record happens to contain a child object that
        # ALSO looks card-shaped (e.g. an evolveTo block), we must
        # not double-count it. The walker yields the parent and
        # stops — early return on _looks_like_card match.
        nested_lookalike = {
            "id": 1,
            "details": {
                "cardName": "Charizard",
                "evolveFrom": {"id": 2, "details": {"cardName": "Charmeleon"}},
            },
        }
        ids = [c["id"] for c in _walk_cards(nested_lookalike)]
        self.assertEqual(ids, [1])

    def test_non_card_dicts_ignored(self):
        node = {"meta": {"version": "1.0"}, "cards": []}
        self.assertEqual(list(_walk_cards(node)), [])


# ── _build_row ─────────────────────────────────────────────────


class BuildRowTests(unittest.TestCase):

    def _record(self, **overrides) -> dict:
        base = {
            "id": 12345,
            "name": "Pikachu",
            "image": "images/sv2/pikachu.png",
            "hash": "deadbeef",
            "details": {
                "cardName": "皮卡丘",
                "collectionNumber": "008/207",
                "yorenCode": "SV1",
                "cardType": "Pokemon",
                "cardTypeText": "宝可梦",
                "rarity": "C",
                "rarityText": "普通",
                "regulationMarkText": "G",
                "hp": "70",
                "attribute": "Lightning",
                "evolveText": "基础",
                "pokedexCode": "025",
                "pokedexText": "电气类宝可梦。",
                "illustratorName": ["Atsuko Nishida", "Mitsuhiro Arita"],
                "commodityList": [{"commodityName": "猩红/紫罗兰"}],
            },
            "commodityCode": "SV1-008",
        }
        for k, v in overrides.items():
            if k in base["details"]:
                base["details"][k] = v
            else:
                base[k] = v
        return base

    def test_happy_path_full_row(self):
        row = _build_row(self._record())
        self.assertIsNotNone(row)
        # 21 columns — count must match the INSERT in _flush_batch.
        self.assertEqual(len(row), 21)
        # Spot-check core fields.
        self.assertEqual(row[0], 12345)            # card_id
        self.assertEqual(row[1], "SV1-008")        # commodity_code
        self.assertEqual(row[2], "008/207")        # collection_number
        self.assertEqual(row[3], "猩红/紫罗兰")     # commodity_name
        self.assertEqual(row[4], "皮卡丘")          # name_chs
        self.assertEqual(row[5], "SV1")            # yoren_code
        self.assertEqual(row[6], "Pokemon")        # card_type
        self.assertEqual(row[7], "宝可梦")         # card_type_text
        self.assertEqual(row[8], "C")              # rarity
        self.assertEqual(row[9], "普通")           # rarity_text
        self.assertEqual(row[10], "G")             # regulation_mark
        self.assertEqual(row[11], 70)              # hp
        self.assertEqual(row[12], "Lightning")     # attribute
        self.assertEqual(row[16], "Atsuko Nishida, Mitsuhiro Arita")  # illustrators
        self.assertTrue(row[17].startswith("https://raw.githubusercontent.com/"))
        self.assertIn("images/sv2/pikachu.png", row[17])
        self.assertEqual(row[18], "deadbeef")      # hash
        # raw_json round-trips
        self.assertIsInstance(row[19], str)
        self.assertEqual(json.loads(row[19])["id"], 12345)

    def test_non_int_id_returns_none(self):
        # Defends against dict-shaped enum nodes that slip past the
        # walker due to a future schema change.
        rec = self._record()
        rec["id"] = "12345"
        self.assertIsNone(_build_row(rec))

    def test_empty_name_returns_none(self):
        # No cardName AND no top-level name → no useful row.
        rec = self._record()
        rec["name"] = ""
        rec["details"]["cardName"] = ""
        self.assertIsNone(_build_row(rec))

    def test_top_level_name_used_when_details_cardname_blank(self):
        # cardName missing but top-level name present — the row is
        # still useful (for older snapshots).
        rec = self._record()
        rec["details"]["cardName"] = ""
        rec["name"] = "Charizard"
        row = _build_row(rec)
        self.assertIsNotNone(row)
        self.assertEqual(row[4], "Charizard")

    def test_image_already_absolute_kept_verbatim(self):
        # If upstream ever switches to absolute URLs, we must not
        # double-prefix to https://...https://...
        rec = self._record()
        rec["image"] = "https://cdn.example.com/abc.png"
        row = _build_row(rec)
        self.assertEqual(row[17], "https://cdn.example.com/abc.png")

    def test_image_missing_yields_empty_string(self):
        rec = self._record()
        rec["image"] = ""
        row = _build_row(rec)
        self.assertEqual(row[17], "")

    def test_hp_garbage_does_not_crash(self):
        rec = self._record()
        rec["details"]["hp"] = "—"   # u+2014 em dash from upstream
        row = _build_row(rec)
        self.assertIsNotNone(row)
        self.assertIsNone(row[11])

    def test_illustrators_truncated_to_200_chars(self):
        # Defensive cap matches the schema column width assumption;
        # without it we'd get psycopg2 StringDataRightTruncation on
        # weird upstream rows.
        rec = self._record()
        rec["details"]["illustratorName"] = ["x" * 50] * 10  # ~ 510 chars joined
        row = _build_row(rec)
        self.assertLessEqual(len(row[16]), 200)

    def test_pokedex_text_truncated_to_600_chars(self):
        rec = self._record()
        rec["details"]["pokedexText"] = "y" * 1000
        row = _build_row(rec)
        self.assertLessEqual(len(row[15]), 600)

    def test_non_string_illustrators_skipped_not_stringified(self):
        # If upstream ever shoves a stray int or dict into the
        # illustrator list, the row must still build (skip the bad
        # element) — never str(d) it into the column.
        rec = self._record()
        rec["details"]["illustratorName"] = ["Atsuko Nishida", 42, {"name": "x"}]
        row = _build_row(rec)
        self.assertEqual(row[16], "Atsuko Nishida")

    def test_commodity_list_empty_yields_empty_commodity_name(self):
        rec = self._record()
        rec["details"]["commodityList"] = []
        row = _build_row(rec)
        self.assertEqual(row[3], "")

    def test_commodity_list_non_dict_first_element_does_not_crash(self):
        rec = self._record()
        rec["details"]["commodityList"] = ["junk-string"]
        row = _build_row(rec)
        self.assertEqual(row[3], "")


# ── _flush_batch ───────────────────────────────────────────────


class FlushBatchTests(unittest.TestCase):

    def test_empty_batch_no_op(self):
        conn = FakeConn()
        _flush_batch(conn, [])
        # No cursor allocation, no SQL.
        self.assertEqual(conn.cursors, [])

    def test_uses_executemany_with_upsert(self):
        conn = FakeConn()
        # Two minimal rows — column count must match _build_row (21).
        row = (1,) + ("",) * 19 + (123,)
        _flush_batch(conn, [row, row])
        self.assertEqual(len(conn.cursors), 1)
        emany = conn.cursors[0].executed_many
        self.assertEqual(len(emany), 1)
        sql, batch = emany[0]
        # Critical contracts the importer relies on:
        self.assertIn("INSERT INTO cards_chs", sql)
        self.assertIn("ON CONFLICT (card_id) DO UPDATE SET", sql)
        # The placeholder count matches the row width — psycopg2
        # would reject any mismatch but better to surface it here.
        self.assertEqual(sql.count("%s"), len(row))
        # And the batch is forwarded verbatim, not de-duplicated by
        # the helper (callers are expected to dedup upstream if they
        # want to — _flush_batch is a transport, not a policy layer).
        self.assertEqual(len(batch), 2)


# ── Module-level constant sanity ────────────────────────────────


class ImageBaseTests(unittest.TestCase):

    def test_image_raw_base_is_https_and_no_trailing_slash(self):
        # _build_row concatenates as f"{IMG_RAW_BASE}/{image}". A
        # trailing slash here would yield '//' in the URL which a
        # surprising number of CDNs treat as an error.
        self.assertTrue(IMG_RAW_BASE.startswith("https://"),
                        f"IMG_RAW_BASE must be https: {IMG_RAW_BASE}")
        self.assertFalse(IMG_RAW_BASE.endswith("/"),
                         f"IMG_RAW_BASE must not end with /: {IMG_RAW_BASE}")


if __name__ == "__main__":
    unittest.main()
