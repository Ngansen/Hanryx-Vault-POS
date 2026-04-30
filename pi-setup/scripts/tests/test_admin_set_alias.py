"""
Tests for the operator-curated set-alias workflow added in Commit F:

  GET  /admin/cards/set-alias-suggest?needle=<text>
  POST /admin/cards/set-alias-add  {set_id, alias}

Hermetic by file-text inspection of server.py — same convention as the
JS/CSS strip alignment tests in test_admin_cards_en_match.py. The
production codebase imports psycopg2 and Flask which aren't in the dev
environment, so we verify SQL composition, route wiring, and error
handling by reading the source.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SERVER_PY = (ROOT / "server.py").read_text(encoding="utf-8")
SCHEMA_PY = (ROOT / "unified" / "schema.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers — pull out the suggest/add code blocks once so each test can
#           assert against just the section it cares about.
# ---------------------------------------------------------------------------

def _slice(src: str, start_marker: str, end_marker: str) -> str:
    """Return the substring between two markers (inclusive of start)."""
    s = src.index(start_marker)
    e = src.index(end_marker, s + len(start_marker))
    return src[s:e]


SUGGEST_SECTION = _slice(
    SERVER_PY,
    '@app.route("/admin/cards/set-alias-suggest", methods=["GET"])',
    '@app.route("/admin/cards/set-alias-add"',
)
ADD_SECTION = _slice(
    SERVER_PY,
    '@app.route("/admin/cards/set-alias-add", methods=["POST"])',
    "# ------",   # next horizontal rule
)
SUGGEST_SQL = _slice(SERVER_PY, "_ALIAS_SUGGEST_SQL = \"\"\"", "\"\"\"\n\n# ")
ADD_SQL     = _slice(SERVER_PY, "_ALIAS_ADD_SQL = \"\"\"",     "\"\"\"\n\n\n@app")


# ---------------------------------------------------------------------------
# Bootstrap — fuzzystrmatch extension
# ---------------------------------------------------------------------------

class TestFuzzystrmatchBootstrap(unittest.TestCase):
    """The init_db bootstrap must best-effort enable fuzzystrmatch so
    the suggest endpoint works on the Pi's main Postgres. Failure must
    not block startup (some Postgres images strip contrib extensions)."""

    def test_extension_create_present(self):
        self.assertIn("CREATE EXTENSION IF NOT EXISTS fuzzystrmatch", SERVER_PY)

    def test_extension_wrapped_in_try_except(self):
        # The bootstrap block must be inside a try/except so a missing
        # extension just disables the suggester instead of crashing init_db.
        block_start = SERVER_PY.index("CREATE EXTENSION IF NOT EXISTS fuzzystrmatch")
        # Look backwards a few lines for `try:` and forwards for `except`
        prefix = SERVER_PY[max(0, block_start - 200):block_start]
        suffix = SERVER_PY[block_start:block_start + 600]
        self.assertIn("try:", prefix, "fuzzystrmatch CREATE EXTENSION not wrapped in try:")
        self.assertIn("except Exception", suffix,
                      "fuzzystrmatch CREATE EXTENSION not wrapped in except")

    def test_extension_rolls_back_on_failure(self):
        block_start = SERVER_PY.index("CREATE EXTENSION IF NOT EXISTS fuzzystrmatch")
        suffix = SERVER_PY[block_start:block_start + 600]
        self.assertIn("rollback", suffix,
                      "fuzzystrmatch except branch must rollback the failed txn")

    def test_extension_logs_helpful_message(self):
        block_start = SERVER_PY.index("CREATE EXTENSION IF NOT EXISTS fuzzystrmatch")
        suffix = SERVER_PY[block_start:block_start + 600]
        # Operator should know why the suggester is dead if they grep logs.
        self.assertIn("alias suggester", suffix.lower())


# ---------------------------------------------------------------------------
# SQL composition — suggester
# ---------------------------------------------------------------------------

class TestSuggestSqlComposition(unittest.TestCase):
    """The suggest SQL must:
      * compute levenshtein() over all 5 name_* columns
      * cap the input to lower(left(%s, 64)) on both sides
      * skip empty/NULL columns (999 sentinel) so they don't dominate LEAST
      * order by distance ASC then set_id ASC for stable tie-break
      * limit to 3 rows, distance threshold parameterised
    """

    def test_uses_with_dist_cte(self):
        self.assertIn("WITH dist AS", SUGGEST_SQL)

    def test_levenshtein_called_for_each_of_5_languages(self):
        # Five levenshtein() calls in the WITH (one per language).
        self.assertEqual(SUGGEST_SQL.count("levenshtein("), 5,
                         "expected exactly 5 levenshtein() calls — one per name_* column")

    def test_each_language_column_referenced(self):
        for col in ("name_en", "name_kr", "name_jp", "name_chs", "name_cht"):
            self.assertIn(col, SUGGEST_SQL, f"{col} missing from suggest SQL")

    def test_byte_length_capped_to_64(self):
        # left(%s, 64) keeps fuzzystrmatch under its 255-byte input limit
        # even for multi-byte Korean/Japanese/Chinese characters.
        # Should appear at least 10 times (5 needles + 5 columns).
        self.assertGreaterEqual(SUGGEST_SQL.count("left("), 10,
                                "left(..., 64) byte cap missing on inputs")
        self.assertIn(", 64)", SUGGEST_SQL,
                      "byte cap must be 64 to stay under fuzzystrmatch 255-byte limit")

    def test_lower_applied_to_both_sides(self):
        # Both the needle and the column must be lower()'d for case-insensitive
        # matching consistent with the rest of the resolver (en_match.py).
        # 5 needles + 5 columns = 10 lower() calls inside levenshtein args.
        self.assertGreaterEqual(SUGGEST_SQL.count("lower("), 10)

    def test_empty_column_returns_999_sentinel(self):
        # An all-blank language column must NOT contribute to LEAST(),
        # otherwise distance would always be 0 against any empty needle.
        self.assertIn("ELSE 999", SUGGEST_SQL)
        self.assertIn("COALESCE(name_en", SUGGEST_SQL)

    def test_least_used_for_min_distance(self):
        self.assertIn("LEAST(d_en, d_kr, d_jp, d_chs, d_cht)", SUGGEST_SQL)

    def test_matched_lang_case_resolution(self):
        # The CASE expression that maps the winning distance back to a
        # language tag must enumerate all 5 languages plus a fallback.
        for lang in ("'en'", "'kr'", "'jp'", "'chs'", "'cht'"):
            self.assertIn(f"THEN {lang}", SUGGEST_SQL)
        self.assertIn("'unknown'", SUGGEST_SQL)

    def test_distance_threshold_parameterised(self):
        # The Levenshtein cap (default 2) must be a bound param, not
        # hard-coded into the SQL — that lets the constant change without
        # editing the SQL string.
        self.assertIn("LEAST(d_en, d_kr, d_jp, d_chs, d_cht) <= %s",
                      SUGGEST_SQL)

    def test_orders_by_distance_then_set_id(self):
        self.assertRegex(SUGGEST_SQL,
                         r"ORDER\s+BY\s+distance\s+ASC,\s+set_id\s+ASC")

    def test_limits_to_3(self):
        self.assertRegex(SUGGEST_SQL, r"LIMIT\s+3\b")

    def test_total_param_count(self):
        # 5 needles in WITH dist + 1 distance cap = 6 bound params.
        self.assertEqual(SUGGEST_SQL.count("%s"), 6,
                         "suggest SQL must have exactly 6 %s placeholders "
                         "(5 needles + 1 distance cap)")

    def test_constants_have_expected_values(self):
        self.assertIn("_ALIAS_NEEDLE_MAX = 100", SERVER_PY)
        self.assertIn("_ALIAS_LEV_CAP    = 2", SERVER_PY)


# ---------------------------------------------------------------------------
# SQL composition — alias add
# ---------------------------------------------------------------------------

class TestAddSqlComposition(unittest.TestCase):
    """The add SQL must:
      * UPDATE ref_set_mapping (the canonical set table)
      * append the new alias to the JSONB array
      * dedupe case-insensitively (so 'PAL' and 'pal' don't both get stored)
      * bind the alias as ::text (no JSONB literal injection risk)
      * return rowcount=0 for both already-present and unknown set_id
    """

    def test_targets_ref_set_mapping(self):
        self.assertIn("UPDATE ref_set_mapping", ADD_SQL)

    def test_appends_via_jsonb_concat(self):
        # The append uses JSONB concat (||) with the aliases column
        # (wrapped in COALESCE so a NULL column doesn't propagate). The
        # appended element is bound as ::text so the param goes through
        # psycopg2's normal text adapter — no JSONB literal injection.
        self.assertIn("|| jsonb_build_array(%s::text)", ADD_SQL)
        self.assertRegex(
            ADD_SQL,
            r"COALESCE\(aliases,\s*'\[\]'::jsonb\)\s*\|\|\s*jsonb_build_array\(%s::text\)",
        )

    def test_handles_null_aliases_via_coalesce(self):
        # ref_set_mapping.aliases defaults to '[]' but a hand-edited row
        # could be NULL — COALESCE keeps the concat safe.
        self.assertIn("COALESCE(aliases, '[]'::jsonb)", ADD_SQL)

    def test_idempotent_via_not_exists_lookup(self):
        self.assertIn("NOT EXISTS", ADD_SQL)
        self.assertIn("jsonb_array_elements_text", ADD_SQL)
        self.assertIn("lower(a) = lower(%s)", ADD_SQL)

    def test_filters_by_set_id(self):
        self.assertIn("WHERE set_id = %s", ADD_SQL)

    def test_total_param_count(self):
        # alias for append + set_id for WHERE + alias for dedupe = 3 params.
        self.assertEqual(ADD_SQL.count("%s"), 3,
                         "add SQL must have exactly 3 %s placeholders "
                         "(alias, set_id, alias)")


# ---------------------------------------------------------------------------
# Endpoint wiring — suggest
# ---------------------------------------------------------------------------

class TestSuggestEndpointWiring(unittest.TestCase):
    """The suggest route must validate inputs, bind the right number of
    params, detect missing-extension errors, and return JSON shapes the
    booth UI can render."""

    def test_route_registered_with_get(self):
        self.assertIn(
            '@app.route("/admin/cards/set-alias-suggest", methods=["GET"])',
            SERVER_PY,
        )

    def test_function_named_admin_cards_set_alias_suggest(self):
        self.assertIn("def admin_cards_set_alias_suggest():", SERVER_PY)

    def test_rejects_empty_needle_with_400(self):
        self.assertIn('"error": "needle required"', SUGGEST_SECTION)
        self.assertIn(", 400", SUGGEST_SECTION)

    def test_rejects_overlong_needle_with_400(self):
        # The length check must reference the constant so changing it in
        # one place updates both the validator and the SQL byte cap docs.
        self.assertIn("len(needle) > _ALIAS_NEEDLE_MAX", SUGGEST_SECTION)

    def test_passes_needle_5_times_plus_cap(self):
        # The cur.execute call must bind: 5 needles for the 5 levenshtein
        # cells in the CTE, then the distance cap. Order matters.
        self.assertIn(
            "(needle, needle, needle, needle, needle, _ALIAS_LEV_CAP)",
            SUGGEST_SECTION,
        )

    def test_uses_fetchall_not_fetchone(self):
        # We want up to 3 rows back, not just the top one.
        self.assertIn("cur.fetchall()", SUGGEST_SECTION)

    def test_missing_extension_returns_503_not_500(self):
        # A 500 would surface as a generic "server error" toast in the
        # booth UI — 503 lets the frontend hide the Curate button instead.
        self.assertIn(", 503", SUGGEST_SECTION)
        self.assertIn("fuzzystrmatch extension missing", SUGGEST_SECTION)

    def test_missing_extension_path_rolls_back_txn(self):
        # psycopg2 connections enter aborted state after a failed query;
        # a rollback is required before the connection can be reused or
        # closed cleanly.
        # Find the fetchall except branch and verify rollback() is called.
        try_idx = SUGGEST_SECTION.index("cur.fetchall()")
        except_idx = SUGGEST_SECTION.index("except Exception", try_idx)
        # Look at the next ~400 chars after `except` for the rollback.
        branch = SUGGEST_SECTION[except_idx:except_idx + 400]
        self.assertIn("db.rollback()", branch,
                      "missing-extension branch must rollback aborted txn")

    def test_response_shape_includes_required_keys(self):
        # Each suggestion dict must carry every key the JS renderer expects.
        for key in ("set_id", "name_en", "name_kr", "name_jp",
                    "name_chs", "name_cht", "current_aliases",
                    "distance", "matched_lang"):
            self.assertIn(f'"{key}"', SUGGEST_SECTION,
                          f"{key} missing from suggest response shape")

    def test_aliases_normalised_to_list(self):
        # psycopg2's JSONB adapter returns a Python list, but a NULL
        # column comes back as None — wrap so the client always gets [].
        self.assertIn("list(aliases) if aliases else []", SUGGEST_SECTION)

    def test_distance_coerced_to_int(self):
        # Some Postgres drivers return integer aggregates as Decimal;
        # int() ensures the JSON value is a native number.
        self.assertIn("int(distance)", SUGGEST_SECTION)

    def test_closes_db_connection_in_finally(self):
        self.assertIn("finally:", SUGGEST_SECTION)
        self.assertIn("db.close()", SUGGEST_SECTION)


# ---------------------------------------------------------------------------
# Endpoint wiring — alias add
# ---------------------------------------------------------------------------

class TestAddEndpointWiring(unittest.TestCase):
    """The add route must validate inputs, commit on success, surface
    `added: bool` so the UI can distinguish add-vs-already-curated, and
    accept either JSON or form bodies (some booth scripts use curl -d)."""

    def test_route_registered_with_post(self):
        self.assertIn(
            '@app.route("/admin/cards/set-alias-add", methods=["POST"])',
            SERVER_PY,
        )

    def test_function_named_admin_cards_set_alias_add(self):
        self.assertIn("def admin_cards_set_alias_add():", SERVER_PY)

    def test_accepts_json_or_form(self):
        # request.get_json(silent=True) won't 415 on missing Content-Type;
        # the `or request.form` fallback lets curl -d / form posts work.
        self.assertIn("request.get_json(silent=True) or request.form",
                      ADD_SECTION)

    def test_rejects_missing_set_id_or_alias_with_400(self):
        self.assertIn('"error": "set_id and alias required"', ADD_SECTION)
        self.assertIn(", 400", ADD_SECTION)

    def test_rejects_overlong_alias_with_400(self):
        self.assertIn("len(alias) > _ALIAS_NEEDLE_MAX", ADD_SECTION)

    def test_passes_3_params_alias_setid_alias(self):
        # The first %s feeds jsonb_build_array (the appended alias),
        # the second is set_id (WHERE clause), the third is the alias
        # again for the dedupe lookup. Order matters.
        self.assertIn("(alias, set_id, alias)", ADD_SECTION)

    def test_uses_rowcount_to_compute_added(self):
        # rowcount==1 means the UPDATE actually fired (alias was new and
        # set_id existed); rowcount==0 means either the alias was already
        # curated or the set_id is unknown.
        self.assertIn("bool(cur.rowcount)", ADD_SECTION)

    def test_commits_on_success(self):
        self.assertIn("db.commit()", ADD_SECTION)

    def test_rolls_back_on_failure(self):
        # The except branch must rollback so the connection can be closed
        # cleanly by the finally block.
        self.assertIn("db.rollback()", ADD_SECTION)

    def test_response_carries_set_id_alias_and_added(self):
        for key in ('"set_id":', '"alias":', '"added":'):
            self.assertIn(key, ADD_SECTION)

    def test_closes_db_connection_in_finally(self):
        self.assertIn("finally:", ADD_SECTION)
        self.assertIn("db.close()", ADD_SECTION)


# ---------------------------------------------------------------------------
# Schema — confirm aliases column still JSONB on ref_set_mapping
# ---------------------------------------------------------------------------

class TestRefSetMappingAliasesColumn(unittest.TestCase):
    """Sanity: the alias-add SQL assumes ref_set_mapping.aliases is a
    JSONB array with default '[]'. If someone migrates that column to
    text[] or removes the default, the add UPDATE silently corrupts data."""

    def test_aliases_column_is_jsonb_with_default(self):
        # Match across whitespace so column-comment indentation doesn't
        # break the assertion.
        self.assertRegex(
            SCHEMA_PY,
            r"aliases\s+JSONB\s+DEFAULT\s+'\[\]'",
            "ref_set_mapping.aliases must remain JSONB DEFAULT '[]' "
            "for the add endpoint to be safe",
        )


# ---------------------------------------------------------------------------
# Cross-cutting — both endpoints share helpers
# ---------------------------------------------------------------------------

class TestSharedConventions(unittest.TestCase):

    def test_both_use_direct_db_helper(self):
        # The same _direct_db() factory used by /admin/cards/en-match —
        # keeps connection-pool semantics consistent across admin endpoints.
        self.assertIn("db = _direct_db()", SUGGEST_SECTION)
        self.assertIn("db = _direct_db()", ADD_SECTION)

    def test_both_log_db_errors_with_endpoint_prefix(self):
        # Grep-friendly log lines so operators can filter by endpoint.
        self.assertIn("[admin/cards/set-alias-suggest]", SUGGEST_SECTION)
        self.assertIn("[admin/cards/set-alias-add]", ADD_SECTION)

    def test_module_constants_block_documents_byte_cap(self):
        # The constants section above the SQLs explains why we cap at
        # 64 source chars — keeps the next reader from "optimising" it.
        constants_zone = _slice(
            SERVER_PY,
            "# Cap needle length before levenshtein()",
            "_ALIAS_SUGGEST_SQL",
        )
        self.assertIn("255-byte input limit", constants_zone)
        self.assertIn("Korean/Japanese", constants_zone)


if __name__ == "__main__":
    unittest.main()
