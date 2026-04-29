"""
Tests for workers/ocr_indexer.py.

Strategy: inject a fake `paddle_factory(lang) -> ocr` so no real
PaddleOCR is needed. The fake `ocr.ocr(path, cls=False)` returns
canned PaddleOCR-shaped output. DB layer = same FakeConn/FakeCursor
pattern as the other worker tests.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path
from typing import Any

PI_SETUP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PI_SETUP))

from workers.ocr_indexer import (  # noqa: E402
    OcrIndexerWorker,
    PADDLE_LANG_MAP,
    LANG_PRIORITY,
)
from workers.base import WorkerError  # noqa: E402


# ── Fake DB ─────────────────────────────────────────────────────

class FakeCursor:
    def __init__(self, parent: "FakeConn") -> None:
        self.parent = parent
        self.rowcount = 0
        self._fetch_one_q = parent._fetch_one_q

    def execute(self, sql: str, params: Any = None) -> None:
        self.parent.executes.append((sql, params))

    def fetchone(self):
        if not self._fetch_one_q:
            return None
        return self._fetch_one_q.popleft()


class FakeConn:
    def __init__(self) -> None:
        self.executes: list[tuple[str, Any]] = []
        self.commits = 0
        self._fetch_one_q: deque = deque()

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def queue_one(self, row):
        self._fetch_one_q.append(row)


# ── Fake PaddleOCR ──────────────────────────────────────────────

class FakeOcr:
    """Minimal PaddleOCR stand-in. Returns canned `[[ [box, (text, conf)], ...]]`
    structures from a per-instance script."""

    def __init__(self, lang: str, script: list = None,
                 raise_on_call: Exception = None):
        self.lang = lang
        self._script = list(script or [])
        self._raise = raise_on_call
        self.calls: list[tuple[str, dict]] = []

    def ocr(self, image_path: str, cls=False):
        self.calls.append((image_path, {"cls": cls}))
        if self._raise:
            raise self._raise
        if self._script:
            return self._script.pop(0)
        return []


def make_factory(per_lang: dict[str, FakeOcr]):
    def factory(lang: str):
        if lang not in per_lang:
            raise RuntimeError(f"factory called with unknown lang {lang}")
        return per_lang[lang]
    return factory


# ── Tests ───────────────────────────────────────────────────────


class ConstructorTest(unittest.TestCase):
    def test_defaults(self):
        w = OcrIndexerWorker(FakeConn())
        self.assertEqual(w.TASK_TYPE, "ocr_index")
        self.assertEqual(w.model_id, "paddleocr-ppocrv4-1.0")
        self.assertIsNone(w.lang_hint)
        self.assertEqual(w.recheck_after_s, 90 * 86400)

    def test_lang_hint_validation(self):
        with self.assertRaises(ValueError):
            OcrIndexerWorker(FakeConn(), lang_hint="zzz")

    def test_lang_hint_accepts_known(self):
        for lang in PADDLE_LANG_MAP:
            w = OcrIndexerWorker(FakeConn(), lang_hint=lang)
            self.assertEqual(w.lang_hint, lang)

    def test_env_overrides(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"OCR_MODEL_ID": "ppocr-test-9"}):
            w = OcrIndexerWorker(FakeConn())
        self.assertEqual(w.model_id, "ppocr-test-9")


class PaddleLangMapTest(unittest.TestCase):
    def test_all_priorities_have_paddle_codes(self):
        for lang in LANG_PRIORITY:
            self.assertIn(lang, PADDLE_LANG_MAP)

    def test_korean_first_in_priority(self):
        self.assertEqual(LANG_PRIORITY[0], "kr")


class PickPrimaryLangTest(unittest.TestCase):
    def test_kr_only_picks_kr(self):
        self.assertEqual(
            OcrIndexerWorker.pick_primary_lang("피카츄", "", "", ""), "kr")

    def test_jp_only_picks_jp(self):
        self.assertEqual(
            OcrIndexerWorker.pick_primary_lang("", "ピカチュウ", "", ""), "jp")

    def test_chs_only_picks_chs(self):
        self.assertEqual(
            OcrIndexerWorker.pick_primary_lang("", "", "皮卡丘", ""), "chs")

    def test_en_only_picks_en(self):
        self.assertEqual(
            OcrIndexerWorker.pick_primary_lang("", "", "", "Pikachu"), "en")

    def test_kr_beats_jp(self):
        self.assertEqual(
            OcrIndexerWorker.pick_primary_lang("피카츄", "ピカチュウ", "", ""),
            "kr")

    def test_kr_beats_en(self):
        self.assertEqual(
            OcrIndexerWorker.pick_primary_lang("피카츄", "", "", "Pikachu"),
            "kr")

    def test_jp_beats_chs(self):
        self.assertEqual(
            OcrIndexerWorker.pick_primary_lang("", "ピカチュウ", "皮卡丘", ""),
            "jp")

    def test_jp_beats_en(self):
        self.assertEqual(
            OcrIndexerWorker.pick_primary_lang("", "ピカチュウ", "", "Pikachu"),
            "jp")

    def test_chs_beats_en(self):
        self.assertEqual(
            OcrIndexerWorker.pick_primary_lang("", "", "皮卡丘", "Pikachu"),
            "chs")

    def test_all_empty_falls_back_to_en(self):
        self.assertEqual(
            OcrIndexerWorker.pick_primary_lang("", "", "", ""), "en")

    def test_whitespace_only_treated_as_empty(self):
        self.assertEqual(
            OcrIndexerWorker.pick_primary_lang("   ", "", "", "Pikachu"),
            "en")

    def test_none_inputs_safe(self):
        self.assertEqual(
            OcrIndexerWorker.pick_primary_lang(None, None, None, None), "en")


class ParseOcrResultTest(unittest.TestCase):
    def test_empty_returns_empty(self):
        lines, txt, conf = OcrIndexerWorker._parse_ocr_result([])
        self.assertEqual(lines, [])
        self.assertEqual(txt, "")
        self.assertEqual(conf, 0.0)

    def test_none_returns_empty(self):
        lines, txt, conf = OcrIndexerWorker._parse_ocr_result(None)
        self.assertEqual(lines, [])

    def test_paddle_v4_shape_parses(self):
        # Real PaddleOCR shape: [[ [box, (text, conf)], ... ]]
        raw = [[
            [[[10, 20], [80, 20], [80, 40], [10, 40]], ("피카츄", 0.95)],
            [[[10, 50], [120, 50], [120, 70], [10, 70]], ("HP 60", 0.88)],
        ]]
        lines, txt, conf = OcrIndexerWorker._parse_ocr_result(raw)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["text"], "피카츄")
        self.assertAlmostEqual(lines[0]["conf"], 0.95, places=4)
        self.assertEqual(lines[0]["bbox"],
                         [10.0, 20.0, 80.0, 20.0, 80.0, 40.0, 10.0, 40.0])
        self.assertEqual(txt, "피카츄\nHP 60")
        self.assertAlmostEqual(conf, (0.95 + 0.88) / 2, places=4)

    def test_avg_conf_zero_when_no_lines(self):
        raw = [[]]
        lines, _, conf = OcrIndexerWorker._parse_ocr_result(raw)
        self.assertEqual(lines, [])
        self.assertEqual(conf, 0.0)

    def test_malformed_box_does_not_raise(self):
        raw = [[
            [None, ("ok-text", 0.7)],
        ]]
        lines, txt, conf = OcrIndexerWorker._parse_ocr_result(raw)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["text"], "ok-text")
        self.assertEqual(lines[0]["bbox"], [])

    def test_malformed_conf_becomes_zero(self):
        raw = [[
            [[[0, 0], [1, 0], [1, 1], [0, 1]], ("x", "not-a-number")],
        ]]
        lines, _, _ = OcrIndexerWorker._parse_ocr_result(raw)
        self.assertEqual(lines[0]["conf"], 0.0)


class EnsurePaddleTest(unittest.TestCase):
    def test_missing_lib_returns_none_and_records(self):
        from unittest import mock
        w = OcrIndexerWorker(FakeConn())
        with mock.patch.dict(sys.modules, {"paddleocr": None}):
            self.assertIsNone(w._ensure_paddle())
            self.assertEqual(w._load_failure, "NO_LIB")

    def test_injected_factory_returned(self):
        per = {"korean": FakeOcr("korean")}
        w = OcrIndexerWorker(FakeConn(), paddle_factory=make_factory(per))
        f = w._ensure_paddle()
        self.assertIsNotNone(f)
        self.assertIs(f("korean"), per["korean"])

    def test_get_ocr_caches_per_lang(self):
        per = {"korean": FakeOcr("korean"), "japan": FakeOcr("japan")}
        calls = []
        def factory(lang):
            calls.append(lang)
            return per[lang]
        w = OcrIndexerWorker(FakeConn(), paddle_factory=factory)
        a = w._get_ocr("korean")
        b = w._get_ocr("korean")
        self.assertIs(a, b)
        self.assertEqual(calls, ["korean"])  # only one factory call
        c = w._get_ocr("japan")
        self.assertIsNot(a, c)
        self.assertEqual(calls, ["korean", "japan"])

    def test_factory_exception_sets_load_failure(self):
        def boom(_):
            raise RuntimeError("model download failed")
        w = OcrIndexerWorker(FakeConn(), paddle_factory=boom)
        self.assertIsNone(w._get_ocr("korean"))
        self.assertTrue(w._load_failure.startswith("FACTORY_ERROR:"))


class PickImagePathTest(unittest.TestCase):
    def test_returns_first_existing(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"data")
            p = f.name
        try:
            picked = OcrIndexerWorker._pick_image_path([
                {"local": "/nope.png"},
                {"local": p},
                {"local": "/also.png"},
            ])
            self.assertEqual(picked, p)
        finally:
            os.unlink(p)

    def test_skips_zero_byte(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            empty = f.name
        try:
            self.assertEqual(
                OcrIndexerWorker._pick_image_path([{"local": empty}]), "")
        finally:
            os.unlink(empty)


class ProcessTest(unittest.TestCase):
    def _real_image(self):
        f = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        f.write(b"\x89PNG\r\n\x1a\n" + b"x" * 200)
        f.close()
        return f.name

    def test_missing_card_records_failure(self):
        per = {"korean": FakeOcr("korean")}
        w = OcrIndexerWorker(FakeConn(), paddle_factory=make_factory(per))
        w.conn.queue_one(None)
        res = w.process({"task_id": 1,
                         "payload": {"set_id": "S", "card_number": "1"}})
        self.assertEqual(res["status"], "MISSING_CARD")
        params = [p for s, p in w.conn.executes
                  if "INSERT INTO card_ocr" in s][0]
        self.assertIn("MISSING_CARD", params)

    def test_no_image_records_failure(self):
        per = {"korean": FakeOcr("korean")}
        w = OcrIndexerWorker(FakeConn(), paddle_factory=make_factory(per))
        # raw_img is a list with no usable local paths
        w.conn.queue_one(([{"local": "/nope.png"}],
                          "피카츄", "", "", ""))
        res = w.process({"task_id": 1,
                         "payload": {"set_id": "S", "card_number": "1"}})
        self.assertEqual(res["status"], "NO_IMAGE")
        # Lang resolved to 'kr' from name_kr
        params = [p for s, p in w.conn.executes
                  if "INSERT INTO card_ocr" in s][0]
        self.assertIn("kr", params)

    def test_no_lib_records_failure(self):
        path = self._real_image()
        try:
            w = OcrIndexerWorker(FakeConn())  # no factory injected
            w._load_failure = "NO_LIB"
            w._paddle_loaded = False  # ensure _ensure_paddle still tries
            # Patch _ensure_paddle to short-circuit:
            w._ensure_paddle = lambda: None
            w.conn.queue_one(([{"local": path}], "피카츄", "", "", ""))
            res = w.process({"task_id": 1,
                             "payload": {"set_id": "S", "card_number": "1"}})
            self.assertEqual(res["status"], "NO_LIB")
            params = [p for s, p in w.conn.executes
                      if "INSERT INTO card_ocr" in s][0]
            self.assertIn("NO_LIB", params)
        finally:
            os.unlink(path)

    def test_happy_path_inserts_full_text(self):
        path = self._real_image()
        try:
            ocr = FakeOcr("korean", script=[[[
                [[[0, 0], [10, 0], [10, 10], [0, 10]], ("피카츄", 0.91)],
                [[[0, 20], [40, 20], [40, 30], [0, 30]], ("HP 70", 0.83)],
            ]]])
            per = {"korean": ocr}
            w = OcrIndexerWorker(FakeConn(),
                                 paddle_factory=make_factory(per))
            w.conn.queue_one(([{"local": path, "src": "tcgo"}],
                              "피카츄", "", "", ""))
            res = w.process({"task_id": 1,
                             "payload": {"set_id": "SV1",
                                         "card_number": "001"}})
            self.assertEqual(res["status"], "OK")
            self.assertEqual(res["line_count"], 2)
            self.assertGreater(res["chars"], 0)
            inserts = [(s, p) for s, p in w.conn.executes
                       if "INSERT INTO card_ocr" in s
                       and "DO UPDATE SET image_path" in s
                       and "full_text  = EXCLUDED.full_text" in s]
            self.assertEqual(len(inserts), 1)
            _, params = inserts[0]
            # params: sid, num, lang, model, path, full_text,
            # lines_json, line_count, conf, ts
            self.assertEqual(params[0], "SV1")
            self.assertEqual(params[1], "001")
            self.assertEqual(params[2], "kr")
            self.assertEqual(params[4], path)
            self.assertEqual(params[5], "피카츄\nHP 70")
            stored_lines = json.loads(params[6])
            self.assertEqual(len(stored_lines), 2)
            self.assertEqual(stored_lines[0]["text"], "피카츄")
            self.assertEqual(params[7], 2)
            self.assertAlmostEqual(params[8], (0.91 + 0.83) / 2, places=4)
            # Confirm OCR was actually called with cls=False on the image
            self.assertEqual(ocr.calls, [(path, {"cls": False})])
        finally:
            os.unlink(path)

    def test_ocr_exception_records_failure(self):
        path = self._real_image()
        try:
            ocr = FakeOcr("korean", raise_on_call=RuntimeError("boom"))
            per = {"korean": ocr}
            w = OcrIndexerWorker(FakeConn(),
                                 paddle_factory=make_factory(per))
            w.conn.queue_one(([{"local": path}], "피카츄", "", "", ""))
            res = w.process({"task_id": 1,
                             "payload": {"set_id": "S", "card_number": "1"}})
            self.assertEqual(res["status"], "OCR_ERROR")
            params = [p for s, p in w.conn.executes
                      if "INSERT INTO card_ocr" in s][0]
            self.assertTrue(any("OCR_ERROR" in str(x) for x in params))
        finally:
            os.unlink(path)

    def test_instance_lang_hint_overrides_payload_and_card(self):
        path = self._real_image()
        try:
            ocr = FakeOcr("japan", script=[[[
                [[[0, 0], [10, 0], [10, 10], [0, 10]], ("ピカ", 0.9)],
            ]]])
            per = {"japan": ocr}
            # Card has a Korean name BUT we forced lang_hint=jp on the
            # worker instance — should override.
            w = OcrIndexerWorker(FakeConn(),
                                 lang_hint="jp",
                                 paddle_factory=make_factory(per))
            w.conn.queue_one(([{"local": path}], "피카츄", "", "", ""))
            res = w.process({"task_id": 1,
                             "payload": {"set_id": "S",
                                         "card_number": "1",
                                         "lang_hint": "kr"}})
            self.assertEqual(res["status"], "OK")
            params = [p for s, p in w.conn.executes
                      if "INSERT INTO card_ocr" in s
                      and "DO UPDATE" in s][0]
            self.assertEqual(params[2], "jp")  # lang_hint stored

        finally:
            os.unlink(path)

    def test_payload_lang_hint_used_when_no_instance_hint(self):
        path = self._real_image()
        try:
            ocr = FakeOcr("ch", script=[[[
                [[[0, 0], [10, 0], [10, 10], [0, 10]], ("皮卡", 0.85)],
            ]]])
            per = {"ch": ocr}
            w = OcrIndexerWorker(FakeConn(),
                                 paddle_factory=make_factory(per))
            w.conn.queue_one(([{"local": path}], "피카츄", "", "", ""))
            res = w.process({"task_id": 1,
                             "payload": {"set_id": "S",
                                         "card_number": "1",
                                         "lang_hint": "chs"}})
            self.assertEqual(res["status"], "OK")
            params = [p for s, p in w.conn.executes
                      if "INSERT INTO card_ocr" in s
                      and "DO UPDATE" in s][0]
            self.assertEqual(params[2], "chs")
        finally:
            os.unlink(path)

    def test_missing_payload_raises(self):
        w = OcrIndexerWorker(FakeConn())
        with self.assertRaises(WorkerError):
            w.process({"task_id": 1, "payload": {}})


class SeedTest(unittest.TestCase):
    def test_auto_lang_seed_uses_case_expression(self):
        conn = FakeConn()
        w = OcrIndexerWorker(conn)
        n = w.seed()
        self.assertEqual(n, 0)
        sql, params = conn.executes[0]
        self.assertIn("INSERT INTO bg_task_queue", sql)
        self.assertIn("CASE", sql)
        self.assertIn("name_kr", sql)  # KR-first priority
        self.assertIn("name_jp", sql)
        self.assertIn("name_chs", sql)
        self.assertIn("ON CONFLICT (task_type, task_key) DO NOTHING", sql)
        self.assertIn("status IN ('OK','PARTIAL')", sql)

    def test_fixed_lang_seed_uses_param(self):
        conn = FakeConn()
        w = OcrIndexerWorker(conn, lang_hint="jp")
        w.seed()
        sql, params = conn.executes[0]
        # 'jp' should appear in params 3 times (task_key lang, payload
        # lang, NOT EXISTS lang)
        self.assertEqual(params.count("jp"), 3)
        self.assertNotIn("CASE", sql)  # bypassed when lang fixed

    def test_seed_uses_latest_check_only(self):
        conn = FakeConn()
        w = OcrIndexerWorker(conn)
        w.seed()
        sql = conn.executes[0][0]
        self.assertIn("MAX(h2.checked_at)", sql)


# ── models_dir resolution + factory wiring ────────────────────
#
# Verifies that the per-language PaddleOCR model files (det_model_dir
# and rec_model_dir) get pointed at /mnt/cards/models/paddleocr/<lang>/
# instead of PaddleOCR's own ~/.paddleocr cache (which lives on the SD
# card and gets wiped by every `docker compose build`). Uses the same
# sys.modules patch trick as EnsureQuoteFnTest in test_price_refresh.


class ModelsDirTest(unittest.TestCase):
    def test_default_models_dir(self):
        # No kwarg, no env var → the on-drive default. This is what a
        # fresh Pi gets when it's been set up per pi-setup/README.md.
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OCR_MODELS_DIR", None)
            w = OcrIndexerWorker(FakeConn())
        self.assertEqual(w.models_dir, "/mnt/cards/models/paddleocr")

    def test_explicit_kwarg_wins(self):
        w = OcrIndexerWorker(FakeConn(), models_dir="/tmp/custom-paddle")
        self.assertEqual(w.models_dir, "/tmp/custom-paddle")

    def test_env_var_used_when_no_kwarg(self):
        from unittest import mock
        with mock.patch.dict(os.environ,
                             {"OCR_MODELS_DIR": "/srv/paddle"}):
            w = OcrIndexerWorker(FakeConn())
        self.assertEqual(w.models_dir, "/srv/paddle")

    def test_kwarg_beats_env(self):
        from unittest import mock
        with mock.patch.dict(os.environ,
                             {"OCR_MODELS_DIR": "/from/env"}):
            w = OcrIndexerWorker(FakeConn(),
                                 models_dir="/from/kwarg")
        self.assertEqual(w.models_dir, "/from/kwarg")

    def test_empty_string_is_escape_hatch(self):
        # Operator escape hatch: "" means "fall back to PaddleOCR's
        # ~/.paddleocr default". Explicitly NOT replaced with the
        # default — the factory below skips det/rec_model_dir entirely
        # when models_dir is empty, so first-use downloads land in the
        # container default cache instead of the (unmounted) drive.
        w = OcrIndexerWorker(FakeConn(), models_dir="")
        self.assertEqual(w.models_dir, "")

    def test_factory_passes_per_lang_paths_to_paddleocr(self):
        # The whole point of the migration: the (det|rec)_model_dir
        # kwargs PaddleOCR receives must be on /mnt/cards so the
        # 50-100MB-per-lang downloads survive container rebuilds.
        from unittest import mock
        recorded: dict = {}

        class FakePaddleOCR:
            def __init__(self, **kw):
                recorded.clear()
                recorded.update(kw)

        fake_mod = mock.MagicMock()
        fake_mod.PaddleOCR = FakePaddleOCR
        with mock.patch.dict(sys.modules, {"paddleocr": fake_mod}):
            w = OcrIndexerWorker(
                FakeConn(),
                models_dir="/mnt/cards/models/paddleocr",
            )
            factory = w._ensure_paddle()
            self.assertIsNotNone(factory)
            factory("korean")
        self.assertEqual(recorded["lang"], "korean")
        self.assertEqual(recorded["det_model_dir"],
                         "/mnt/cards/models/paddleocr/korean/det")
        self.assertEqual(recorded["rec_model_dir"],
                         "/mnt/cards/models/paddleocr/korean/rec")
        self.assertFalse(recorded["use_angle_cls"])
        self.assertFalse(recorded["show_log"])

    def test_factory_skips_model_dirs_when_empty_string(self):
        # When the drive is unavailable the operator can run with
        # models_dir="" and PaddleOCR falls back to its own cache.
        from unittest import mock
        recorded: dict = {}

        class FakePaddleOCR:
            def __init__(self, **kw):
                recorded.clear()
                recorded.update(kw)

        fake_mod = mock.MagicMock()
        fake_mod.PaddleOCR = FakePaddleOCR
        with mock.patch.dict(sys.modules, {"paddleocr": fake_mod}):
            w = OcrIndexerWorker(FakeConn(), models_dir="")
            factory = w._ensure_paddle()
            factory("japan")
        self.assertEqual(recorded["lang"], "japan")
        self.assertNotIn("det_model_dir", recorded)
        self.assertNotIn("rec_model_dir", recorded)

    def test_factory_uses_paddle_lang_codes_in_path(self):
        # The factory takes the PADDLE-side lang code ('korean',
        # 'japan', 'ch', 'en') — not the worker's internal codes
        # ('kr', 'jp', 'chs', 'en'). This guards against a regression
        # where the worker's internal code accidentally leaks into
        # the on-disk path (which would silently re-download every
        # language because the path wouldn't match what PaddleOCR
        # wrote on a previous run).
        from unittest import mock
        from workers.ocr_indexer import PADDLE_LANG_MAP

        recorded: list[dict] = []

        class FakePaddleOCR:
            def __init__(self, **kw):
                recorded.append(dict(kw))

        fake_mod = mock.MagicMock()
        fake_mod.PaddleOCR = FakePaddleOCR
        with mock.patch.dict(sys.modules, {"paddleocr": fake_mod}):
            w = OcrIndexerWorker(FakeConn(),
                                 models_dir="/mnt/cards/models/paddleocr")
            factory = w._ensure_paddle()
            for paddle_code in PADDLE_LANG_MAP.values():
                factory(paddle_code)

        # Every recorded call's path ends in /<paddle_code>/det or /rec,
        # never /<internal_code>/.
        for kw in recorded:
            paddle_code = kw["lang"]
            self.assertTrue(
                kw["det_model_dir"].endswith(f"/{paddle_code}/det"),
                f"det path {kw['det_model_dir']!r} doesn't end in "
                f"/{paddle_code}/det — internal code may have leaked")
            self.assertTrue(
                kw["rec_model_dir"].endswith(f"/{paddle_code}/rec"),
                f"rec path {kw['rec_model_dir']!r} doesn't end in "
                f"/{paddle_code}/rec — internal code may have leaked")

    def test_factory_caches_per_lang_so_paddleocr_constructed_once(self):
        # _get_ocr / the per-lang cache should only construct PaddleOCR
        # once per language even if called many times. A regression
        # here would mean every card OCR re-downloads the model.
        from unittest import mock
        construction_count = {"n": 0}

        class FakePaddleOCR:
            def __init__(self, **kw):
                construction_count["n"] += 1

            def ocr(self, *a, **kw):
                return []

        fake_mod = mock.MagicMock()
        fake_mod.PaddleOCR = FakePaddleOCR
        with mock.patch.dict(sys.modules, {"paddleocr": fake_mod}):
            w = OcrIndexerWorker(FakeConn(),
                                 models_dir="/mnt/cards/models/paddleocr")
            for _ in range(5):
                w._get_ocr("korean")
        self.assertEqual(construction_count["n"], 1)


if __name__ == "__main__":
    unittest.main()
