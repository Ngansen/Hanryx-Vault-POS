"""
Tests for workers/set_mapping_import.

Hermetic: no DB, no network. The TCGdex /sets calls are replaced with
a canned `fetch_fn`; the DB is replaced with a FakeConn that records
every executed SQL + params for assertion.

What gets pinned here:

  * Multi-language merge collapses to one row per set_id and writes
    each non-empty name into the matching name_* column.
  * Empty / missing names per language don't clobber other languages.
  * The UPSERT preserves operator-curated state — name_*  on conflict
    falls back to the existing row when EXCLUDED is empty (COALESCE +
    NULLIF), and aliases / era / region are simply never SET, so they
    keep whatever the operator typed in.
  * EMPTY_SOURCE short-circuits: when every language returns nothing
    we write zero rows (don't blow away yesterday's mapping with a
    transient outage).
  * Seed is idempotent under the orchestrator's polling tick (ON
    CONFLICT collapses re-seeds to a no-op).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workers.set_mapping_import import (   # noqa: E402
    SetMappingImportWorker,
    _LANG_TO_COL,
    _UPSERT_SQL,
    _merge_languages,
)


# ── FakeConn / FakeCursor ─────────────────────────────────────────────────


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0
        self.executed: list[tuple[str, object]] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self.conn.all_sql.append((sql, params))
        if self.conn.rowcount_queue:
            self.rowcount = self.conn.rowcount_queue.pop(0)


class FakeConn:
    def __init__(self):
        self.commits = 0
        self.cursors: list[FakeCursor] = []
        self.all_sql: list[tuple[str, object]] = []
        self.rowcount_queue: list[int] = []

    def cursor(self):
        c = FakeCursor(self)
        self.cursors.append(c)
        return c

    def commit(self):
        self.commits += 1


# ── Reusable canned fetch helpers ─────────────────────────────────────────


def make_fetch_fn(per_lang: dict[str, list[dict]]):
    """Return a fetch_fn that serves the canned list per language and
    records which languages were asked for. Missing keys yield []."""
    asked: list[str] = []

    def fetch(lang: str) -> list[dict]:
        asked.append(lang)
        return per_lang.get(lang, [])

    fetch.asked = asked  # type: ignore[attr-defined]
    return fetch


# ── Sanity: language-to-column map ────────────────────────────────────────


class LangToColTests(unittest.TestCase):
    """Pin the contract — anything that touches this map needs a schema
    review (each col must exist on ref_set_mapping). Failing this test
    is a code-review trigger."""

    def test_expected_languages_and_columns(self):
        self.assertEqual(_LANG_TO_COL, {
            "en":    "name_en",
            "ko":    "name_kr",
            "ja":    "name_jp",
            "zh-cn": "name_chs",
            "zh-tw": "name_cht",
        })


# ── _merge_languages ──────────────────────────────────────────────────────


class MergeLanguagesTests(unittest.TestCase):

    def test_collapses_to_one_row_per_set_id_with_all_names(self):
        fetch = make_fetch_fn({
            "en":    [{"id": "sv2", "name": "Paldea Evolved"}],
            "ko":    [{"id": "sv2", "name": "팔데아의 진화"}],
            "ja":    [{"id": "sv2", "name": "ポケモンカードゲーム sv2"}],
            "zh-cn": [{"id": "sv2", "name": "帕底亚进化"}],
            "zh-tw": [{"id": "sv2", "name": "帕底亞進化"}],
        })
        merged, counts = _merge_languages(fetch)
        self.assertEqual(set(merged.keys()), {"sv2"})
        self.assertEqual(merged["sv2"], {
            "name_en":  "Paldea Evolved",
            "name_kr":  "팔데아의 진화",
            "name_jp":  "ポケモンカードゲーム sv2",
            "name_chs": "帕底亚进化",
            "name_cht": "帕底亞進化",
        })
        self.assertEqual(counts, {"en": 1, "ko": 1, "ja": 1,
                                  "zh-cn": 1, "zh-tw": 1})

    def test_each_language_queried_exactly_once(self):
        # Cheap defence against future refactors that loop multiple
        # times — would silently quintuple the API load.
        fetch = make_fetch_fn({})
        _merge_languages(fetch)
        self.assertEqual(sorted(fetch.asked),
                         sorted(_LANG_TO_COL.keys()))
        self.assertEqual(len(fetch.asked), len(_LANG_TO_COL))

    def test_empty_language_response_does_not_create_partial_rows(self):
        # If TCGdex hasn't published a Korean translation for a set
        # yet, that set's merged row simply lacks the 'name_kr' key —
        # it doesn't show up as an empty string. The UPSERT branch
        # then sends '' for name_kr, and the COALESCE/NULLIF preserves
        # whatever was already there.
        fetch = make_fetch_fn({
            "en": [{"id": "sv2", "name": "Paldea Evolved"}],
            "ko": [],   # nothing yet
        })
        merged, _ = _merge_languages(fetch)
        self.assertIn("sv2", merged)
        self.assertEqual(merged["sv2"].get("name_en"), "Paldea Evolved")
        self.assertNotIn("name_kr", merged["sv2"])

    def test_blank_name_does_not_overwrite_real_one(self):
        # Same set appearing in multiple language passes; later passes
        # with empty/whitespace names must not blank out an earlier
        # non-empty entry for a different column.
        fetch = make_fetch_fn({
            "en": [{"id": "sv2", "name": "Paldea Evolved"}],
            "ko": [{"id": "sv2", "name": "   "}],  # whitespace-only
        })
        merged, _ = _merge_languages(fetch)
        self.assertEqual(merged["sv2"].get("name_en"), "Paldea Evolved")
        self.assertNotIn("name_kr", merged["sv2"])

    def test_rows_without_id_are_skipped(self):
        # TCGdex has at least one historical row with id="" — would
        # otherwise create a phantom set_id="" row in ref_set_mapping
        # that breaks the PK display in the admin UI.
        fetch = make_fetch_fn({
            "en": [
                {"id": "",       "name": "ghost"},
                {"id": "   ",    "name": "ghost-ws"},
                {"id": "sv2",    "name": "Paldea Evolved"},
                # no 'id' key at all
                {"name": "no-id"},
            ],
        })
        merged, counts = _merge_languages(fetch)
        self.assertEqual(set(merged.keys()), {"sv2"})
        # per_lang_counts still reflects the raw fetch length, not the
        # post-skip count — that's diagnostic-honest (operator can see
        # "EN returned 4 but only 1 had an id" if needed).
        self.assertEqual(counts["en"], 4)

    def test_names_are_trimmed(self):
        fetch = make_fetch_fn({
            "en": [{"id": "sv2", "name": "  Paldea Evolved  "}],
        })
        merged, _ = _merge_languages(fetch)
        self.assertEqual(merged["sv2"]["name_en"], "Paldea Evolved")

    def test_set_ids_are_trimmed(self):
        # Whitespace-padded ids would create distinct rows from clean
        # ones in ref_set_mapping; trim before keying the dict.
        fetch = make_fetch_fn({
            "en": [{"id": "  sv2  ", "name": "Paldea Evolved"}],
        })
        merged, _ = _merge_languages(fetch)
        self.assertIn("sv2", merged)
        self.assertNotIn("  sv2  ", merged)

    def test_returns_empty_when_all_languages_fail(self):
        # All-empty signal. The worker uses this to skip the UPSERT
        # entirely so a transient outage doesn't blow away yesterday's
        # rows.
        fetch = make_fetch_fn({})
        merged, counts = _merge_languages(fetch)
        self.assertEqual(merged, {})
        self.assertEqual(set(counts.keys()), set(_LANG_TO_COL.keys()))
        self.assertTrue(all(v == 0 for v in counts.values()))


# ── UPSERT SQL contract ───────────────────────────────────────────────────


class UpsertSqlContractTests(unittest.TestCase):
    """The UPSERT must preserve operator-curated state across re-imports.
    These tests pin the literal SQL clauses so a future "clean up the
    UPSERT" pass can't accidentally start clobbering aliases."""

    def test_upsert_targets_ref_set_mapping(self):
        self.assertIn("INSERT INTO ref_set_mapping", _UPSERT_SQL)

    def test_upsert_uses_set_id_as_conflict_key(self):
        self.assertIn("ON CONFLICT (set_id)", _UPSERT_SQL)

    def test_upsert_preserves_existing_name_kr_when_excluded_blank(self):
        # COALESCE(NULLIF(EXCLUDED.name_kr, ''), ref_set_mapping.name_kr)
        # — TCGdex sometimes lacks Korean for new sets, must not wipe.
        self.assertIn(
            "COALESCE(NULLIF(EXCLUDED.name_kr,  ''), ref_set_mapping.name_kr)",
            _UPSERT_SQL,
        )

    def test_upsert_preserves_existing_names_for_all_5_languages(self):
        # Same protection applied uniformly across en / kr / jp / chs / cht.
        for col in ("name_en", "name_kr", "name_jp", "name_chs", "name_cht"):
            with self.subTest(col=col):
                self.assertRegex(
                    _UPSERT_SQL,
                    rf"COALESCE\(NULLIF\(EXCLUDED\.{col},\s+''\),"
                    rf" ref_set_mapping\.{col}\)",
                )

    def test_upsert_does_not_touch_aliases(self):
        # Critical: operator-curated aliases (e.g. "PAL" → sv2) must
        # survive every re-import. The alias branch in en_match is the
        # only recourse for codes TCGdex has never heard of.
        self.assertNotIn("aliases     =",   _UPSERT_SQL)
        self.assertNotIn("aliases =",       _UPSERT_SQL)
        self.assertNotIn("EXCLUDED.aliases", _UPSERT_SQL)

    def test_upsert_does_not_touch_era_or_region(self):
        # Operator-tagged grouping; TCGdex doesn't expose either on the
        # /sets list endpoint, so this re-import has no business
        # overwriting them.
        self.assertNotIn("era         =",   _UPSERT_SQL)
        self.assertNotIn("era =",           _UPSERT_SQL)
        self.assertNotIn("region      =",   _UPSERT_SQL)
        self.assertNotIn("region =",        _UPSERT_SQL)


# ── SetMappingImportWorker.seed ───────────────────────────────────────────


class SeedTests(unittest.TestCase):

    def test_first_seed_enqueues_one_task_keyed_by_today(self):
        conn = FakeConn()
        conn.rowcount_queue = [1]
        w = SetMappingImportWorker(
            conn,
            today_fn=lambda: "2026-04-30",
            fetch_fn=lambda lang: [],   # unused by seed
        )
        n = w.seed()
        self.assertEqual(n, 1)
        self.assertEqual(conn.commits, 1)
        sql, params = conn.all_sql[0]
        self.assertIn("INSERT INTO bg_task_queue", sql)
        # task_type pinned, task_key from today_fn
        self.assertIn("set_mapping_import", sql)
        self.assertEqual(params[0], "2026-04-30")

    def test_repeat_seed_collapses_via_on_conflict(self):
        # Orchestrator polls every few minutes; seed must be a no-op
        # the second time within the same UTC day.
        conn = FakeConn()
        conn.rowcount_queue = [0]   # ON CONFLICT DO NOTHING → 0 rows
        w = SetMappingImportWorker(
            conn, today_fn=lambda: "2026-04-30",
            fetch_fn=lambda lang: [],
        )
        n = w.seed()
        self.assertEqual(n, 0)
        sql, _ = conn.all_sql[0]
        self.assertIn("ON CONFLICT", sql)
        self.assertIn("DO NOTHING", sql)


# ── SetMappingImportWorker.process ────────────────────────────────────────


class ProcessTests(unittest.TestCase):

    def test_happy_path_writes_one_upsert_per_set_and_commits(self):
        conn = FakeConn()
        fetch = make_fetch_fn({
            "en": [{"id": "sv2",  "name": "Paldea Evolved"},
                   {"id": "sv8p", "name": "Paradox Rift"}],
            "ko": [{"id": "sv2",  "name": "팔데아의 진화"}],
        })
        w = SetMappingImportWorker(conn, fetch_fn=fetch)
        out = w.process({})
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["sets_imported"], 2)
        # Per-language diagnostic counts surfaced for the operator.
        self.assertEqual(out["per_lang_counts"]["en"], 2)
        self.assertEqual(out["per_lang_counts"]["ko"], 1)
        self.assertEqual(out["per_lang_counts"]["zh-cn"], 0)
        # Two UPSERTs (one per set), one commit.
        upsert_calls = [s for s in conn.all_sql
                        if "INSERT INTO ref_set_mapping" in s[0]]
        self.assertEqual(len(upsert_calls), 2)
        self.assertEqual(conn.commits, 1)

    def test_upsert_param_order_matches_sql(self):
        # Defensive — getting the param tuple out of order would silently
        # write Korean names into the chs column. The SQL pins the
        # column order; this test pins that the call passes them in the
        # matching order.
        conn = FakeConn()
        fetch = make_fetch_fn({
            "en":    [{"id": "sv2", "name": "EN-NAME"}],
            "ko":    [{"id": "sv2", "name": "KR-NAME"}],
            "ja":    [{"id": "sv2", "name": "JP-NAME"}],
            "zh-cn": [{"id": "sv2", "name": "CHS-NAME"}],
            "zh-tw": [{"id": "sv2", "name": "CHT-NAME"}],
        })
        w = SetMappingImportWorker(conn, fetch_fn=fetch)
        w.process({})
        upsert = next(s for s in conn.all_sql
                      if "INSERT INTO ref_set_mapping" in s[0])
        _, params = upsert
        # Layout: (set_id, name_en, name_kr, name_jp, name_chs, name_cht,
        #          raw_json, imported_at)
        self.assertEqual(params[0], "sv2")
        self.assertEqual(params[1], "EN-NAME")
        self.assertEqual(params[2], "KR-NAME")
        self.assertEqual(params[3], "JP-NAME")
        self.assertEqual(params[4], "CHS-NAME")
        self.assertEqual(params[5], "CHT-NAME")
        # raw is JSON; imported_at is an int.
        self.assertIn("tcgdex_sets", params[6])
        self.assertIsInstance(params[7], int)

    def test_missing_language_passes_empty_string_param(self):
        # COALESCE/NULLIF on the SQL side handles the empty — but the
        # parameter itself must be ''  (not None — psycopg2 would bind
        # NULL and the NULLIF would fail to fire).
        conn = FakeConn()
        fetch = make_fetch_fn({
            "en": [{"id": "sv2", "name": "EN-only"}],
        })
        w = SetMappingImportWorker(conn, fetch_fn=fetch)
        w.process({})
        _, params = next(s for s in conn.all_sql
                         if "INSERT INTO ref_set_mapping" in s[0])
        self.assertEqual(params[1], "EN-only")
        # Korean / JP / CHS / CHT all empty strings, never None.
        for i in (2, 3, 4, 5):
            with self.subTest(idx=i):
                self.assertEqual(params[i], "")

    def test_empty_source_short_circuits_with_no_writes(self):
        # Booth on a captive portal — every fetch returns []. The
        # worker must not start writing zero-name rows over the live
        # mapping; it must skip and report.
        conn = FakeConn()
        fetch = make_fetch_fn({})
        w = SetMappingImportWorker(conn, fetch_fn=fetch)
        out = w.process({})
        self.assertEqual(out["status"], "EMPTY_SOURCE")
        self.assertEqual(out["sets_imported"], 0)
        # No UPSERTs, no commit.
        self.assertEqual(
            [s for s in conn.all_sql if "INSERT INTO ref_set_mapping" in s[0]],
            [],
        )
        self.assertEqual(conn.commits, 0)
        # Per-language counts still surfaced so the operator can see
        # WHICH language(s) failed when investigating.
        self.assertIn("per_lang_counts", out)

    def test_raw_blob_includes_source_tag(self):
        # The raw column is the breadcrumb the admin UI shows when a
        # mapping looks suspicious — it must be obvious where it came
        # from.
        conn = FakeConn()
        fetch = make_fetch_fn({
            "en": [{"id": "sv2", "name": "Paldea Evolved"}],
        })
        w = SetMappingImportWorker(conn, fetch_fn=fetch)
        w.process({})
        _, params = next(s for s in conn.all_sql
                         if "INSERT INTO ref_set_mapping" in s[0])
        raw_json = params[6]
        self.assertIn('"source"', raw_json)
        self.assertIn('"tcgdex_sets"', raw_json)

    def test_raw_blob_is_unicode_safe(self):
        # ensure_ascii=False keeps Korean / Japanese / Chinese names
        # readable in psql / pgAdmin without \uXXXX escaping. Mirrors
        # how en_match's alias lookup binds non-ASCII needles verbatim.
        conn = FakeConn()
        fetch = make_fetch_fn({
            "en": [{"id": "sv2", "name": "Paldea Evolved"}],
            "ko": [{"id": "sv2", "name": "팔데아의 진화"}],
        })
        w = SetMappingImportWorker(conn, fetch_fn=fetch)
        w.process({})
        _, params = next(s for s in conn.all_sql
                         if "INSERT INTO ref_set_mapping" in s[0])
        raw_json = params[6]
        # Verbatim Hangul, not \uXXXX escapes.
        self.assertIn("팔데아의 진화", raw_json)
        self.assertNotIn("\\u", raw_json)


# ── Worker registration ──────────────────────────────────────────────────


class RegistrationTests(unittest.TestCase):
    """Belt-and-suspenders: the new worker must be addressable by its
    CLI key. A typo here would only surface when the operator tries to
    actually run it. Inspect run.py as text rather than importing it —
    workers/run.py imports psycopg2 at module top, which isn't always
    available in CI sandboxes."""

    def test_set_mapping_import_registered_in_run_dispatch(self):
        run_py = ROOT / "workers" / "run.py"
        src = run_py.read_text(encoding="utf-8")
        # Both halves must be present:
        #   1) the import line that pulls the class in
        self.assertIn(
            "from workers.set_mapping_import import SetMappingImportWorker",
            src,
            "SetMappingImportWorker not imported in workers/run.py",
        )
        #   2) the WORKERS-dict entry the operator types on the CLI
        self.assertRegex(
            src,
            r'"set_mapping_import"\s*:\s*SetMappingImportWorker',
            'WORKERS dict missing "set_mapping_import": SetMappingImportWorker',
        )


if __name__ == "__main__":
    unittest.main()
