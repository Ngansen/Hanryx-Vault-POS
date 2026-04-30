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
    _BACKFILL_UPDATE_SQL,
    _LANG_TO_COL,
    _MAX_DETAIL_FETCHES_PER_RUN,
    _UPSERT_SQL,
    _backfill_era_year,
    _extract_era_and_year,
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

    def fetchall(self):
        # Backfill SELECTs use fetchall(); existing tests don't queue
        # any responses and so get []. That's the right default —
        # an empty candidate set means no backfill happens, which
        # keeps the pre-backfill assertions correct.
        if self.conn.fetchall_queue:
            return self.conn.fetchall_queue.pop(0)
        return []


class FakeConn:
    def __init__(self):
        self.commits = 0
        self.cursors: list[FakeCursor] = []
        self.all_sql: list[tuple[str, object]] = []
        self.rowcount_queue: list[int] = []
        self.fetchall_queue: list[list] = []

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


# ── _extract_era_and_year ────────────────────────────────────────────────


class ExtractEraAndYearTests(unittest.TestCase):
    """Pure-function extraction from a TCGdex /sets/{id} document."""

    def test_pulls_serie_name_as_era(self):
        era, year = _extract_era_and_year(
            {"serie": {"id": "sv", "name": "Scarlet & Violet"}})
        self.assertEqual(era, "Scarlet & Violet")
        self.assertEqual(year, "")

    def test_pulls_release_year_from_iso_date(self):
        era, year = _extract_era_and_year({"releaseDate": "2023-06-09"})
        self.assertEqual(era, "")
        self.assertEqual(year, "2023")

    def test_full_payload_returns_both(self):
        era, year = _extract_era_and_year({
            "serie":       {"name": "Sword & Shield"},
            "releaseDate": "2020-02-07",
        })
        self.assertEqual(era, "Sword & Shield")
        self.assertEqual(year, "2020")

    def test_empty_serie_name_yields_empty_era(self):
        era, year = _extract_era_and_year({"serie": {"name": ""}})
        self.assertEqual(era, "")
        self.assertEqual(year, "")

    def test_missing_serie_yields_empty_era(self):
        era, year = _extract_era_and_year({"releaseDate": "2024-01-01"})
        self.assertEqual(era, "")
        self.assertEqual(year, "2024")

    def test_serie_as_string_not_dict_is_tolerated(self):
        # Defensive: TCGdex sometimes returns serie as a bare string.
        # We don't crash, we just leave era empty.
        era, year = _extract_era_and_year({"serie": "Scarlet & Violet"})
        self.assertEqual(era, "")

    def test_garbage_release_date_yields_empty_year(self):
        # 'TBD' / 'soon' / '' / Q3-2026 — anything not YYYY-prefixed.
        for bad in ("", "TBD", "soon", "Q3-2026"):
            with self.subTest(value=bad):
                _, year = _extract_era_and_year({"releaseDate": bad})
                self.assertEqual(year, "")

    def test_release_date_with_time_component_still_works(self):
        _, year = _extract_era_and_year(
            {"releaseDate": "2024-11-08T00:00:00Z"})
        self.assertEqual(year, "2024")

    def test_strips_serie_name_whitespace(self):
        era, _ = _extract_era_and_year(
            {"serie": {"name": "  Scarlet & Violet  "}})
        self.assertEqual(era, "Scarlet & Violet")

    def test_none_releasedate_doesnt_crash(self):
        era, year = _extract_era_and_year({"releaseDate": None})
        self.assertEqual((era, year), ("", ""))


# ── _backfill_era_year ───────────────────────────────────────────────────


def make_detail_fn(by_set_id: dict[str, dict | None]):
    """Return a fetch_detail_fn that serves canned detail dicts and
    records which set_ids were asked for. Missing keys yield None
    (simulating a TCGdex 404 / network error)."""
    asked: list[str] = []

    def fetch(sid: str):
        asked.append(sid)
        return by_set_id.get(sid)

    fetch.asked = asked  # type: ignore[attr-defined]
    return fetch


class BackfillEraYearTests(unittest.TestCase):

    def test_no_blank_rows_does_nothing(self):
        # SELECT returns []; no detail GETs, no UPDATEs.
        conn = FakeConn()
        conn.fetchall_queue = [[]]
        called = make_detail_fn({})
        stats = _backfill_era_year(conn.cursor(), called)
        self.assertEqual(stats["backfill_candidates"], 0)
        self.assertEqual(stats["backfill_fetched"], 0)
        self.assertEqual(stats["backfill_updated"], 0)
        self.assertEqual(called.asked, [])
        # Only the SELECT itself fired — no UPDATE.
        self.assertFalse(any("UPDATE ref_set_mapping" in s
                             for s, _ in conn.all_sql))

    def test_select_uses_blank_predicate_and_max_sets_param(self):
        conn = FakeConn()
        conn.fetchall_queue = [[]]
        _backfill_era_year(conn.cursor(),
                           make_detail_fn({}),
                           max_sets=17)
        sql, params = conn.all_sql[0]
        self.assertIn("FROM ref_set_mapping", sql)
        # Both predicates must be present (era OR release_year blank);
        # missing one would let stale rows fall out of the audit.
        self.assertIn("era = ''", sql)
        self.assertIn("release_year = ''", sql)
        self.assertIn("LIMIT %s", sql)
        # The cap is parameterised, not literal.
        self.assertEqual(params, (17,))

    def test_uses_default_max_sets_when_unspecified(self):
        conn = FakeConn()
        conn.fetchall_queue = [[]]
        _backfill_era_year(conn.cursor(), make_detail_fn({}))
        _, params = conn.all_sql[0]
        self.assertEqual(params, (_MAX_DETAIL_FETCHES_PER_RUN,))

    def test_blank_rows_get_detail_fetched_and_updated(self):
        conn = FakeConn()
        conn.fetchall_queue = [[("sv2",), ("sv8p",)]]
        # Both UPDATEs hit one row each.
        conn.rowcount_queue = [1, 1]
        called = make_detail_fn({
            "sv2":  {"serie": {"name": "Scarlet & Violet"},
                     "releaseDate": "2023-06-09"},
            "sv8p": {"serie": {"name": "Scarlet & Violet"},
                     "releaseDate": "2024-11-08"},
        })
        stats = _backfill_era_year(conn.cursor(), called,
                                   now_fn=lambda: 999)
        self.assertEqual(stats["backfill_candidates"], 2)
        self.assertEqual(stats["backfill_fetched"], 2)
        self.assertEqual(stats["backfill_updated"], 2)
        self.assertEqual(stats["backfill_failed"], 0)
        self.assertEqual(called.asked, ["sv2", "sv8p"])

        updates = [(s, p) for s, p in conn.all_sql
                   if "UPDATE ref_set_mapping" in s]
        self.assertEqual(len(updates), 2)
        # Param order: (era, release_year, now, set_id)
        sql0, p0 = updates[0]
        self.assertEqual(p0, ("Scarlet & Violet", "2023", 999, "sv2"))
        # The SQL preserves operator-curated values via COALESCE.
        self.assertIn("COALESCE(NULLIF(%s, ''), era)", sql0)
        self.assertIn("COALESCE(NULLIF(%s, ''), release_year)", sql0)

    def test_per_set_fetch_failure_counted_as_failed_no_update(self):
        # TCGdex 404s on obscure legacy sets. The whole backfill must
        # not abort — just skip the failed set and keep going.
        conn = FakeConn()
        conn.fetchall_queue = [[("sv2",), ("legacy_set",), ("sv8p",)]]
        conn.rowcount_queue = [1, 1]
        called = make_detail_fn({
            "sv2":  {"serie": {"name": "SV"}, "releaseDate": "2023-01-01"},
            "legacy_set": None,                       # simulated 404
            "sv8p": {"serie": {"name": "SV"}, "releaseDate": "2024-01-01"},
        })
        stats = _backfill_era_year(conn.cursor(), called)
        self.assertEqual(stats["backfill_candidates"], 3)
        self.assertEqual(stats["backfill_fetched"], 3)
        self.assertEqual(stats["backfill_updated"], 2)
        self.assertEqual(stats["backfill_failed"], 1)
        # Exactly two UPDATEs — the failed set is skipped silently.
        updates = [s for s, _ in conn.all_sql
                   if "UPDATE ref_set_mapping" in s]
        self.assertEqual(len(updates), 2)

    def test_detail_with_no_usable_metadata_skips_update(self):
        # TCGdex returned the document but neither serie nor releaseDate
        # gave us anything. Don't UPDATE — that would just churn
        # imported_at without filling any blank.
        conn = FakeConn()
        conn.fetchall_queue = [[("emptyset",)]]
        called = make_detail_fn({
            "emptyset": {"serie": {"name": ""}, "releaseDate": ""},
        })
        stats = _backfill_era_year(conn.cursor(), called)
        self.assertEqual(stats["backfill_candidates"], 1)
        self.assertEqual(stats["backfill_fetched"], 1)
        self.assertEqual(stats["backfill_updated"], 0)
        self.assertEqual(stats["backfill_failed"], 0)
        self.assertFalse(any("UPDATE ref_set_mapping" in s
                             for s, _ in conn.all_sql))

    def test_partial_metadata_still_updates_via_coalesce(self):
        # Only release_year is known; era is blank. The COALESCE in
        # the SQL preserves any operator-curated era while still
        # filling the year column.
        conn = FakeConn()
        conn.fetchall_queue = [[("sv2",)]]
        conn.rowcount_queue = [1]
        called = make_detail_fn({
            "sv2": {"releaseDate": "2023-06-09"},  # no serie key
        })
        stats = _backfill_era_year(conn.cursor(), called)
        self.assertEqual(stats["backfill_updated"], 1)
        update = next((s, p) for s, p in conn.all_sql
                      if "UPDATE ref_set_mapping" in s)
        _, params = update
        # Era is '' (will be NULLIF'd → keeps existing); year is '2023'.
        self.assertEqual(params[0], "")
        self.assertEqual(params[1], "2023")

    def test_max_sets_caps_the_select_not_the_loop(self):
        # The cap is enforced at the SQL level (LIMIT). If SELECT
        # returns 100 anyway (e.g. from a buggy fake), the loop still
        # processes them all — but in production the SQL guarantees
        # we don't oversubscribe TCGdex.
        conn = FakeConn()
        conn.fetchall_queue = [[]]
        _backfill_era_year(conn.cursor(), make_detail_fn({}), max_sets=3)
        sql, params = conn.all_sql[0]
        self.assertIn("LIMIT %s", sql)
        self.assertEqual(params[0], 3)

    def test_skips_blank_set_ids_returned_by_select(self):
        # Defence in depth — if the SELECT ever yields a stray blank,
        # don't pass it to the detail fetch.
        conn = FakeConn()
        conn.fetchall_queue = [[("",), ("sv2",)]]
        conn.rowcount_queue = [1]
        called = make_detail_fn({
            "sv2": {"serie": {"name": "SV"}, "releaseDate": "2023-01-01"},
        })
        stats = _backfill_era_year(conn.cursor(), called)
        self.assertEqual(stats["backfill_candidates"], 1)
        self.assertEqual(called.asked, ["sv2"])


# ── _BACKFILL_UPDATE_SQL contract ────────────────────────────────────────


class BackfillUpdateSqlContractTests(unittest.TestCase):

    def test_only_touches_era_release_year_imported_at(self):
        # Critical: nothing else may be SET — names / aliases / region
        # must keep their operator-curated values.
        for col in ("era", "release_year", "imported_at"):
            self.assertIn(f"{col}", _BACKFILL_UPDATE_SQL)
        for forbidden in ("name_en", "name_kr", "name_jp", "name_chs",
                          "name_cht", "aliases", "region", "raw"):
            self.assertNotIn(f"{forbidden}", _BACKFILL_UPDATE_SQL,
                             f"{forbidden} must not be in the backfill UPDATE")

    def test_uses_coalesce_nullif_to_preserve_operator_values(self):
        # Without NULLIF, an empty-string from the API would overwrite
        # an operator-curated era. With it, '' → NULL → COALESCE
        # falls back to the existing column.
        self.assertIn("COALESCE(NULLIF(%s, ''), era)", _BACKFILL_UPDATE_SQL)
        self.assertIn(
            "COALESCE(NULLIF(%s, ''), release_year)",
            _BACKFILL_UPDATE_SQL)

    def test_updates_by_set_id(self):
        # PK lookup, not full-scan UPDATE.
        self.assertIn("WHERE set_id = %s", _BACKFILL_UPDATE_SQL)

    def test_is_update_not_upsert(self):
        # Backfill must NEVER insert — only fill blanks on rows that
        # already exist (the main UPSERT loop creates them). An
        # accidental INSERT here would create phantom rows.
        self.assertNotIn("INSERT", _BACKFILL_UPDATE_SQL.upper())
        self.assertNotIn("ON CONFLICT", _BACKFILL_UPDATE_SQL.upper())


# ── process integration with backfill ────────────────────────────────────


class ProcessBackfillIntegrationTests(unittest.TestCase):
    """End-to-end: the worker calls the backfill helper after the main
    UPSERT loop, and the backfill stats land in the return dict."""

    def test_process_invokes_backfill_after_upserts(self):
        conn = FakeConn()
        # Main UPSERT creates one set; backfill SELECT then yields it
        # again as a candidate (era/release_year still blank).
        conn.fetchall_queue = [[("sv2",)]]
        conn.rowcount_queue = [1]   # the backfill UPDATE
        fetch = make_fetch_fn({
            "en": [{"id": "sv2", "name": "Paldea Evolved"}],
        })
        details = make_detail_fn({
            "sv2": {"serie": {"name": "Scarlet & Violet"},
                    "releaseDate": "2023-06-09"},
        })
        w = SetMappingImportWorker(
            conn, fetch_fn=fetch, fetch_detail_fn=details)
        out = w.process({})
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["sets_imported"], 1)
        # Backfill stats merged in.
        self.assertEqual(out["backfill_candidates"], 1)
        self.assertEqual(out["backfill_fetched"], 1)
        self.assertEqual(out["backfill_updated"], 1)
        self.assertEqual(out["backfill_failed"], 0)
        # SQL ordering: UPSERT happens before the backfill SELECT.
        kinds = [
            "UPSERT" if "INSERT INTO ref_set_mapping" in s
            else "SELECT" if "FROM ref_set_mapping" in s
            else "UPDATE" if "UPDATE ref_set_mapping" in s
            else None
            for s, _ in conn.all_sql
        ]
        kinds = [k for k in kinds if k]
        self.assertEqual(kinds, ["UPSERT", "SELECT", "UPDATE"])
        # Still exactly one commit — UPSERT and backfill UPDATE land
        # atomically together.
        self.assertEqual(conn.commits, 1)

    def test_empty_source_skips_backfill_entirely(self):
        # Captive portal — every fetch returns []. Backfill must NOT
        # run because no merged data means no fresh imported_at, and
        # we don't want to spend the detail-GET budget against zero
        # progress.
        conn = FakeConn()
        details = make_detail_fn({})
        w = SetMappingImportWorker(
            conn, fetch_fn=make_fetch_fn({}), fetch_detail_fn=details)
        out = w.process({})
        self.assertEqual(out["status"], "EMPTY_SOURCE")
        # Backfill not invoked → no detail GETs, no SELECT, no
        # backfill_* keys leaked into the response.
        self.assertEqual(details.asked, [])
        self.assertNotIn("backfill_candidates", out)

    def test_max_detail_fetches_per_run_is_overridable(self):
        # Operator may want to drop the cap for a one-shot full
        # backfill on a fast uplink.
        conn = FakeConn()
        conn.fetchall_queue = [[]]
        fetch = make_fetch_fn({
            "en": [{"id": "sv2", "name": "Paldea Evolved"}],
        })
        w = SetMappingImportWorker(
            conn,
            fetch_fn=fetch,
            fetch_detail_fn=make_detail_fn({}),
            max_detail_fetches_per_run=999,
        )
        w.process({})
        select_sql = next(s for s, _ in conn.all_sql
                          if "FROM ref_set_mapping" in s
                          and "LIMIT %s" in s)
        select_params = next(p for s, p in conn.all_sql
                             if "FROM ref_set_mapping" in s
                             and "LIMIT %s" in s)
        self.assertEqual(select_params, (999,))


if __name__ == "__main__":
    unittest.main()
