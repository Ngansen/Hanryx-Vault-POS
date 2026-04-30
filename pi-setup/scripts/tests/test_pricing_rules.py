#!/usr/bin/env python3
"""
test_pricing_rules.py — pin the canonical language-price multipliers in
server.py against the spec so they cannot silently drift again.

Background:
  The spec is KR=0.55 (Korean cards sell at 55% of EN), JP=0.80, EN=1.00.
  On 2026-04-30 we caught the in-tree code shipping KR=0.40 / JP=0.55,
  which would under-price every non-EN card by a meaningful margin
  (~$11 on a $100 EN card for KR alone). This test exists so any future
  drift is caught by `unittest discover` before it ships.

Why AST instead of import:
  pi-setup/server.py is a 23k+ line monolith whose top-level code opens
  Postgres connections, registers Flask routes, and starts background
  threads. Importing it from a unit test is slow at best and impossible
  in CI without a live DB. Parsing the dict literal with `ast` is fast,
  side-effect free, and just as accurate for pinning a constant.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path


PI_SETUP = Path(__file__).resolve().parent.parent.parent
SERVER_PY = PI_SETUP / "server.py"


def _extract_language_price_rules() -> dict:
    """Parse server.py and return the literal value of _LANGUAGE_PRICE_RULES.

    Raises AssertionError if the assignment is missing, is no longer a
    plain dict literal, or contains non-literal values — any of those
    means the regression contract this test is enforcing has changed
    shape and a human needs to re-bless it.
    """
    src = SERVER_PY.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(SERVER_PY))
    for node in tree.body:
        # Match both `_LANGUAGE_PRICE_RULES = {...}` and the annotated
        # form `_LANGUAGE_PRICE_RULES: dict = {...}` actually used in
        # server.py.
        target_name = None
        value_node = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            target_name = node.targets[0].id
            value_node = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_name = node.target.id
            value_node = node.value
        if target_name != "_LANGUAGE_PRICE_RULES":
            continue
        assert value_node is not None, \
            "_LANGUAGE_PRICE_RULES has no value (bare annotation?)"
        # ast.literal_eval rejects anything that isn't a literal — perfect.
        return ast.literal_eval(value_node)
    raise AssertionError(
        "_LANGUAGE_PRICE_RULES not found in server.py — was it renamed "
        "or moved? Update test_pricing_rules.py if so."
    )


class LanguagePriceRulesSpecTest(unittest.TestCase):
    """Pin every spec'd multiplier. Adding new languages is fine; changing
    an existing one requires updating both server.py AND this test, which
    is the whole point."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = _extract_language_price_rules()

    def test_english_full_price(self) -> None:
        # EN is the reference currency for the multiplier — must be 1.0.
        self.assertEqual(self.rules.get("English"), 1.0)
        self.assertEqual(self.rules.get("EN"), 1.0)

    def test_japanese_eighty_percent(self) -> None:
        # Spec: JP cards sell at 80% of EN.
        self.assertEqual(self.rules.get("Japanese"), 0.80)
        self.assertEqual(self.rules.get("JP"), 0.80)

    def test_korean_fifty_five_percent(self) -> None:
        # Spec: KR cards sell at 55% of EN. Catches the 0.40 regression
        # that shipped before 2026-04-30.
        self.assertEqual(self.rules.get("Korean"), 0.55)
        self.assertEqual(self.rules.get("KR"), 0.55)

    def test_long_form_and_short_form_agree(self) -> None:
        # "Korean" and "KR" must always resolve to the same multiplier;
        # likewise "Japanese"/"JP" and "English"/"EN". Operators have
        # been bitten in the past by code that hits one alias but not
        # the other, producing two different prices for the same card.
        for long_form, short_form in (
            ("English", "EN"),
            ("Japanese", "JP"),
            ("Korean", "KR"),
        ):
            self.assertIn(long_form, self.rules,
                          f"missing long-form key {long_form!r}")
            self.assertIn(short_form, self.rules,
                          f"missing short-form key {short_form!r}")
            self.assertEqual(
                self.rules[long_form], self.rules[short_form],
                f"{long_form!r}={self.rules[long_form]} disagrees with "
                f"{short_form!r}={self.rules[short_form]}",
            )

    def test_all_multipliers_are_sensible(self) -> None:
        # Defensive sanity: every multiplier must be a positive float
        # between 0 and 2 (we don't currently sell any language at a
        # premium > 2x EN; if that ever changes, bump this bound and
        # document why).
        for key, value in self.rules.items():
            self.assertIsInstance(value, float,
                                  f"{key!r} multiplier must be a float")
            self.assertGreater(value, 0.0,
                               f"{key!r} multiplier must be > 0")
            self.assertLessEqual(value, 2.0,
                                 f"{key!r} multiplier must be <= 2.0")


if __name__ == "__main__":
    unittest.main()
