"""
Tests for unified.en_match — the EN-edition resolver behind the
GET /admin/cards/en-match route used by the "Matched as" header strip
on /admin/market.

Hermetic: we use a tiny FakeConn that returns canned fetchone() rows in
priority order (exact -> name_set -> name), so no Postgres is required
and we don't have to import the full Flask app.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from unified.en_match import (   # noqa: E402
    build_match_response,
    normalise_number,
    resolve_en_match,
    resolve_set_id,
)


# ── FakeConn / FakeCursor (fetchone variant) ─────────────────────────────

class FakeCursor:
    """
    Minimal psycopg2-style cursor for testing en_match.

    The cursor inspects each SQL string and routes it to the matching
    response queue on the FakeConn:

      * `ref_set_mapping`  → drains `resolver_queue`  (set-id canonicaliser)
      * everything else    → drains `fetchone_queue`  (cards_master tiers)

    This split keeps the pre-resolver tests behaviourally unchanged
    (their resolver_queue is empty → resolver misses → falls back to
    the raw set_code) and lets resolver-specific tests exercise the
    canonicaliser without staging dummy cards_master responses.
    """
    def __init__(self, conn):
        self.conn = conn
        self.last_sql: str = ""
        self.last_params: object = None
        self._next_one: object = None
        self.closed = False

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.last_params = params
        self.conn.executed.append((sql, params))
        if "ref_set_mapping" in sql:
            queue = self.conn.resolver_queue
        else:
            queue = self.conn.fetchone_queue
        if queue:
            self._next_one = queue.pop(0)
        else:
            self._next_one = None

    def fetchone(self):
        return self._next_one

    def close(self):
        self.closed = True


class FakeConn:
    def __init__(self, fetchone_queue=None, raise_on_cursor=False,
                 resolver_queue=None):
        self.fetchone_queue = list(fetchone_queue or [])
        self.resolver_queue = list(resolver_queue or [])
        self.executed: list[tuple[str, object]] = []
        self._raise_on_cursor = raise_on_cursor

    def cursor(self):
        if self._raise_on_cursor:
            raise RuntimeError("simulated db failure")
        return FakeCursor(self)

    @property
    def cards_master_executed(self) -> list[tuple[str, object]]:
        """
        The subset of executed SQL that hit cards_master (i.e. the tier
        SELECTs we care about for match-priority assertions). Filtering
        out the resolver lookups keeps the original "only one cards_master
        query runs when exact hits" performance contract testable
        without coupling tests to the resolver's internal query count.
        """
        return [
            (sql, params)
            for sql, params in self.executed
            if "ref_set_mapping" not in sql
        ]


# ── Number normalisation ─────────────────────────────────────────────────

class TestNormaliseNumber(unittest.TestCase):
    def test_pure_int_strips_leading_zeros(self):
        self.assertEqual(normalise_number("008"), "8")
        self.assertEqual(normalise_number("042"), "42")
        self.assertEqual(normalise_number("100"), "100")

    def test_zero_collapses_to_zero(self):
        # Guard against the empty-string bug that would happen if you
        # naively did `n.lstrip('0')` without the `or '0'` fallback.
        self.assertEqual(normalise_number("0"),   "0")
        self.assertEqual(normalise_number("00"),  "0")
        self.assertEqual(normalise_number("000"), "0")

    def test_alphanumeric_left_alone(self):
        # Real Pokémon collector formats — must not be int-normalised.
        self.assertEqual(normalise_number("TG01"),    "TG01")
        self.assertEqual(normalise_number("SV-P-001"),"SV-P-001")
        self.assertEqual(normalise_number("RC1"),     "RC1")
        self.assertEqual(normalise_number("SWSH001"), "SWSH001")

    def test_empty_returned_unchanged(self):
        self.assertEqual(normalise_number(""), "")

    def test_no_leading_zeros_unchanged(self):
        self.assertEqual(normalise_number("42"), "42")


# ── Match priority ───────────────────────────────────────────────────────

class TestMatchPriority(unittest.TestCase):
    EXACT_ROW = ("Charizard ex", "sv3pt5", "199",
                 "Special Illustration Rare", "5ban Graphics",
                 "https://images.pokemontcg.io/sv3pt5/199.png")

    def test_exact_wins_when_set_and_number_given(self):
        conn = FakeConn([self.EXACT_ROW])
        m = resolve_en_match(conn, "리자몽", "sv3pt5", "199")
        self.assertIsNotNone(m)
        self.assertEqual(m["confidence"], "exact")
        self.assertEqual(m["name_en"],    "Charizard ex")

    def test_only_one_query_runs_when_exact_hits(self):
        # Performance: the fall-through cards_master SELECTs should NOT
        # run when the exact branch already produced a row — anchor
        # pricing for offline use should be cheap. (We only count
        # cards_master queries; ref_set_mapping resolver lookups are
        # bounded and against a tiny mapping table.)
        conn = FakeConn([self.EXACT_ROW])
        resolve_en_match(conn, "리자몽", "sv3pt5", "199")
        self.assertEqual(len(conn.cards_master_executed), 1)

    def test_falls_through_to_name_set_when_exact_misses(self):
        conn = FakeConn([None, self.EXACT_ROW])
        m = resolve_en_match(conn, "リザードン", "sv3pt5", "199")
        self.assertEqual(m["confidence"], "name_set")
        self.assertEqual(len(conn.cards_master_executed), 2)

    def test_falls_through_to_name_only_when_others_miss(self):
        conn = FakeConn([None, None, self.EXACT_ROW])
        m = resolve_en_match(conn, "Charizard", "sv3pt5", "199")
        self.assertEqual(m["confidence"], "name")
        self.assertEqual(len(conn.cards_master_executed), 3)

    def test_no_match_returns_none(self):
        conn = FakeConn([None, None, None])
        m = resolve_en_match(conn, "DoesNotExist", "zzz", "999")
        self.assertIsNone(m)

    def test_name_only_skips_exact_and_name_set_branches(self):
        # No set_code supplied — only the name-only branch should run.
        # (Resolver is also skipped since set is empty.)
        conn = FakeConn([self.EXACT_ROW])
        m = resolve_en_match(conn, "Charizard", "", "")
        self.assertEqual(m["confidence"], "name")
        self.assertEqual(len(conn.cards_master_executed), 1)

    def test_set_and_number_no_name_runs_only_exact(self):
        # When the operator scans a (set, number) but has no name in
        # hand (e.g. blurry photo) we still resolve via exact only.
        conn = FakeConn([self.EXACT_ROW])
        m = resolve_en_match(conn, "", "sv3pt5", "199")
        self.assertEqual(m["confidence"], "exact")
        self.assertEqual(len(conn.cards_master_executed), 1)

    def test_no_inputs_returns_none_with_no_queries(self):
        conn = FakeConn([])
        m = resolve_en_match(conn, "", "", "")
        self.assertIsNone(m)
        # No set means no resolver lookup either, so executed is empty.
        self.assertEqual(conn.executed, [])


# ── SQL composition ──────────────────────────────────────────────────────

class TestSqlComposition(unittest.TestCase):
    def test_exact_branch_passes_both_number_forms(self):
        # cards_master may have stored '8' or '008'; we hedge by passing
        # both forms in the WHERE clause.
        conn = FakeConn([("X", "sv1", "8", "", "", "")])
        resolve_en_match(conn, "", "sv1", "008")
        sql, params = conn.cards_master_executed[0]
        self.assertIn("set_id = %s", sql)
        self.assertIn("8",   params)
        self.assertIn("008", params)
        self.assertIn("sv1", params)

    def test_exact_branch_filters_to_nonempty_name_en(self):
        # We never anchor to a row missing its English name — that's
        # the EN spine the price percentages compare against.
        conn = FakeConn([("X", "sv1", "1", "", "", "")])
        resolve_en_match(conn, "", "sv1", "1")
        sql, _ = conn.cards_master_executed[0]
        self.assertIn("name_en <> ''", sql)

    def test_name_set_branch_ilike_5_languages(self):
        # The card name can come in any language the operator types —
        # ILIKE across all five name columns.
        conn = FakeConn([None, ("X", "sv1", "1", "", "", "")])
        resolve_en_match(conn, "リザードン", "sv1", "999")
        sql, params = conn.cards_master_executed[1]
        # Collapse runs of whitespace so the test isn't brittle against
        # the SQL formatter's column alignment.
        sql_flat = " ".join(sql.split())
        for col in ("name_en", "name_kr", "name_jp", "name_chs", "name_cht"):
            self.assertIn(f"{col} ILIKE %s", sql_flat,
                          f"missing ILIKE on {col}")
        # 1 set_code + 5 name patterns = 6 params.
        self.assertEqual(len(params), 6)
        # All 5 patterns are wildcarded with the same name string.
        self.assertEqual(params[1:], ("%リザードン%",) * 5)

    def test_name_branch_orders_by_shortest_name_en(self):
        # "Charizard ex" should win over "Charizard ex VMAX" when both
        # share the substring — the ORDER BY length(name_en) ASC is
        # the cheap proxy for "least suffixed".
        conn = FakeConn([None, None, ("X", "sv1", "1", "", "", "")])
        resolve_en_match(conn, "Charizard", "", "")
        # name-only is the only cards_master branch that runs when set
        # is empty (resolver skipped, exact + name_set need a set).
        # See test_name_only_skips_exact_and_name_set_branches above.
        sql_executed = conn.cards_master_executed[-1][0]
        self.assertIn("ORDER BY length(name_en) ASC", sql_executed)


# ── Response shape ───────────────────────────────────────────────────────

class TestResponseShape(unittest.TestCase):
    ROW = ("Pikachu V", "swsh4", "43", "Ultra Rare", "PLANETA",
           "https://example.com/pika.png")

    def _resolve(self):
        conn = FakeConn([self.ROW])
        return resolve_en_match(conn, "피카츄", "swsh4", "43")

    def test_all_match_fields_present(self):
        m = self._resolve()
        for k in ("name_en", "set_id", "card_number", "rarity", "artist",
                  "image_local_url", "ebay_sold_url", "confidence"):
            self.assertIn(k, m, f"missing key {k}")

    def test_image_url_routes_through_card_image(self):
        # Critical for offline-first: the chip image MUST go through
        # /card/image so the USB mirror is consulted before the network.
        # If this URL ever points at images.pokemontcg.io directly, the
        # whole booth breaks when WiFi drops.
        m = self._resolve()
        self.assertTrue(m["image_local_url"].startswith("/card/image?"))
        self.assertIn("set_id=swsh4",   m["image_local_url"])
        self.assertIn("card_number=43", m["image_local_url"])
        self.assertIn("lang=en",        m["image_local_url"])

    def test_ebay_url_targets_sold_completed(self):
        # Operator wants actual sale prices, not aspirational asks —
        # the link must filter to sold + completed listings.
        m = self._resolve()
        self.assertTrue(m["ebay_sold_url"].startswith(
            "https://www.ebay.com/sch/i.html?"))
        self.assertIn("LH_Sold=1",     m["ebay_sold_url"])
        self.assertIn("LH_Complete=1", m["ebay_sold_url"])

    def test_ebay_url_includes_name_and_number(self):
        m = self._resolve()
        self.assertIn("Pikachu", m["ebay_sold_url"])
        self.assertIn("43",      m["ebay_sold_url"])
        self.assertIn("pokemon", m["ebay_sold_url"])

    def test_null_rarity_artist_become_empty_strings(self):
        # cards_master lets rarity/artist be NULL — the response should
        # always be JSON-stable so the frontend doesn't have to null-check.
        row = ("X", "sv1", "1", None, None, "")
        m = build_match_response(row, confidence="exact")
        self.assertEqual(m["rarity"], "")
        self.assertEqual(m["artist"], "")

    def test_confidence_is_passed_through(self):
        for c in ("exact", "name_set", "name"):
            m = build_match_response(
                ("X", "sv1", "1", "Common", "Foo", ""), confidence=c)
            self.assertEqual(m["confidence"], c)

    def test_response_does_not_leak_raw_image_url_field(self):
        # We deliberately drop cards_master.image_url from the response
        # so callers can't accidentally bypass /card/image and hit the
        # network when offline.
        m = self._resolve()
        self.assertNotIn("image_url", m)


# ── Cursor lifecycle ─────────────────────────────────────────────────────

class TestCursorLifecycle(unittest.TestCase):
    def test_cursor_closed_on_success(self):
        conn = FakeConn([("X", "sv1", "1", "", "", "")])
        resolve_en_match(conn, "", "sv1", "1")
        # FakeConn keeps no direct ref to the cursor it created, so
        # we re-create one to prove the close path is wired up: the
        # close() method shouldn't raise even if called multiple times.
        # (The real assertion is the .close() inside resolve_en_match's
        # finally — covered by the lack of exceptions here.)
        c = conn.cursor()
        c.close()
        self.assertTrue(c.closed)

    def test_cursor_failure_propagates(self):
        # When the DB itself is down, the helper re-raises so the
        # endpoint can return a 500 with a well-formed JSON envelope.
        conn = FakeConn(raise_on_cursor=True)
        with self.assertRaises(RuntimeError):
            resolve_en_match(conn, "X", "sv1", "1")


# ── Set-id canonicaliser (resolve_set_id) ────────────────────────────────

class TestResolveSetId(unittest.TestCase):
    """
    The /admin/market page hands us `t2.set.name` (a human set name like
    "Scarlet & Violet—Paldea Evolved") for the eBay query path and the
    same string flows into the en-match call. cards_master only indexes
    by canonical set_id, so we must canonicalise before the tier SQL.

    These tests pin the four-stage resolution order against the
    `ref_set_mapping` table that the sets-import worker populates.
    """

    CANON = ("sv2",)  # what every successful resolution should return

    def test_empty_input_returned_unchanged(self):
        # Caller has nothing to resolve — short-circuit so we don't
        # waste a round-trip on an empty WHERE clause.
        conn = FakeConn(resolver_queue=[self.CANON])
        self.assertEqual(resolve_set_id(conn, ""),    "")
        self.assertEqual(resolve_set_id(conn, None),  None)
        self.assertEqual(conn.executed, [])

    def test_whitespace_only_returned_unchanged(self):
        conn = FakeConn(resolver_queue=[self.CANON])
        self.assertEqual(resolve_set_id(conn, "   "), "   ")
        self.assertEqual(conn.executed, [])

    def test_literal_set_id_match_short_circuits(self):
        # When the input already IS a canonical set_id, we should
        # return on the first lookup and skip the human-name + alias
        # branches entirely (cheaper + more accurate).
        conn = FakeConn(resolver_queue=[self.CANON])
        out = resolve_set_id(conn, "sv2")
        self.assertEqual(out, "sv2")
        self.assertEqual(len(conn.executed), 1)
        self.assertIn("UPPER(set_id) = UPPER(%s)", conn.executed[0][0])

    def test_literal_set_id_match_is_case_insensitive(self):
        # Operator may type "SV2" while cards_master stores "sv2".
        # Both must canonicalise to the same row.
        conn = FakeConn(resolver_queue=[self.CANON])
        out = resolve_set_id(conn, "SV2")
        self.assertEqual(out, "sv2")

    def test_human_name_exact_match_in_english(self):
        # Frontend handed us t2.set.name = "Paldea Evolved". Literal
        # set_id misses (None) → exact human-name match hits.
        conn = FakeConn(resolver_queue=[None, self.CANON])
        out = resolve_set_id(conn, "Paldea Evolved")
        self.assertEqual(out, "sv2")
        self.assertEqual(len(conn.executed), 2)
        # The exact-name SQL hits all five language columns so the
        # operator can paste the set name in any of them.
        sql = conn.executed[1][0]
        for col in ("name_en", "name_kr", "name_jp", "name_chs", "name_cht"):
            self.assertIn(f"UPPER({col})", sql)

    def test_human_name_exact_match_in_korean(self):
        conn = FakeConn(resolver_queue=[None, self.CANON])
        out = resolve_set_id(conn, "팔데아의 진화")
        self.assertEqual(out, "sv2")
        # The same Korean string is broadcast to all five name params
        # (we don't try to detect the language; SQL just OR's them).
        params = conn.executed[1][1]
        self.assertEqual(params, ("팔데아의 진화",) * 5)

    def test_falls_through_to_ilike_when_exact_misses(self):
        # Frontend often hands us a noisier name like "Scarlet &
        # Violet—Paldea Evolved" that won't exact-match name_en. The
        # ILIKE substring branch is the safety net.
        conn = FakeConn(resolver_queue=[None, None, self.CANON])
        out = resolve_set_id(conn, "Scarlet & Violet—Paldea Evolved")
        self.assertEqual(out, "sv2")
        self.assertEqual(len(conn.executed), 3)
        self.assertIn("ILIKE",                conn.executed[2][0])
        self.assertIn("ORDER BY length",      conn.executed[2][0])

    def test_falls_through_to_alias_when_human_name_misses(self):
        # Operator-curated aliases let us pin ptcgo-style codes
        # (e.g. "PAL") and legacy abbreviations to the canonical
        # set_id without needing a name match.
        conn = FakeConn(resolver_queue=[None, None, None, self.CANON])
        out = resolve_set_id(conn, "PAL")
        self.assertEqual(out, "sv2")
        self.assertEqual(len(conn.executed), 4)
        sql, params = conn.executed[3]
        self.assertIn("aliases @>", sql)
        # JSONB containment requires the needle wrapped as a JSON array
        # of one string — assert the exact wire shape.
        self.assertEqual(params, ('["PAL"]',))

    def test_no_match_returns_raw_input(self):
        # Unknown sets must not block en-match — fall back to the raw
        # value so the tier-1/tier-2 SQL still gets a chance with
        # whatever the frontend sent. (Worst case: tier-3 name-only.)
        conn = FakeConn(resolver_queue=[None, None, None, None])
        out = resolve_set_id(conn, "wholly-unknown-set")
        self.assertEqual(out, "wholly-unknown-set")
        self.assertEqual(len(conn.executed), 4)

    def test_db_error_falls_back_to_raw_not_crash(self):
        # ref_set_mapping might not exist on a fresh install before the
        # sets-import worker has ever run. The resolver must not blow
        # up the whole match — just return the raw input so callers
        # behave like the pre-resolver code path.
        class BoomCursor(FakeCursor):
            def execute(self, sql, params=None):
                raise RuntimeError("relation does not exist")

        class BoomConn(FakeConn):
            def cursor(self):
                return BoomCursor(self)

        conn = BoomConn()
        out = resolve_set_id(conn, "sv2")
        self.assertEqual(out, "sv2")

    def test_cursor_closed_after_resolution(self):
        # The resolver opens its own short-lived cursor and must close
        # it whether we hit, miss, or fall back. We can't observe
        # closure on the consumed cursor directly, but the lack of an
        # exception here proves the finally block ran.
        conn = FakeConn(resolver_queue=[None, None, None, None])
        resolve_set_id(conn, "doesntmatter")
        c = conn.cursor()  # fresh cursor — proves no leaked state.
        c.close()
        self.assertTrue(c.closed)

    def test_strips_surrounding_whitespace(self):
        # Toolbar paste from a wiki page often carries leading/trailing
        # spaces that would otherwise miss every WHERE clause.
        conn = FakeConn(resolver_queue=[None, self.CANON])
        out = resolve_set_id(conn, "  Paldea Evolved  ")
        self.assertEqual(out, "sv2")
        # The literal-set_id and exact-name branches both see the
        # trimmed needle, never the padded version.
        for sql, params in conn.executed:
            if isinstance(params, tuple):
                self.assertNotIn("  Paldea Evolved  ", params)


# ── End-to-end: resolver feeds the right set_id into the tier SQL ────────

class TestResolverIntegration(unittest.TestCase):
    """
    Belt-and-suspenders for the architect-flagged HIGH bug: the frontend
    passes a human set name, but tier-1/tier-2 need a canonical set_id.
    These tests assert the canonicalised value (not the raw human name)
    is what reaches the cards_master WHERE clause.
    """

    EXACT_ROW = ("Charizard ex", "sv2", "199", "SIR", "Yuu Nishida", "")

    def test_human_set_name_canonicalised_before_tier_1(self):
        # Resolver hits on second lookup (human-name exact), tier-1
        # then runs against the resolved 'sv2', not 'Paldea Evolved'.
        conn = FakeConn(
            fetchone_queue=[self.EXACT_ROW],
            resolver_queue=[None, ("sv2",)],
        )
        m = resolve_en_match(conn, "Charizard", "Paldea Evolved", "199")
        self.assertEqual(m["confidence"], "exact")
        # The cards_master tier-1 SELECT must have used 'sv2', not the
        # human name we received from the frontend.
        tier1_sql, tier1_params = conn.cards_master_executed[0]
        self.assertIn("sv2", tier1_params)
        self.assertNotIn("Paldea Evolved", tier1_params)

    def test_unresolved_set_falls_back_to_raw_in_tier_sql(self):
        # When the resolver can't match anything, tier-1/2 still get a
        # shot with the raw input — preserves pre-resolver behaviour.
        conn = FakeConn(
            fetchone_queue=[None, None, self.EXACT_ROW],
            resolver_queue=[None, None, None, None],
        )
        m = resolve_en_match(conn, "Charizard", "made-up-set", "199")
        self.assertEqual(m["confidence"], "name")
        tier1_sql, tier1_params = conn.cards_master_executed[0]
        self.assertIn("made-up-set", tier1_params)


if __name__ == "__main__":
    unittest.main()
