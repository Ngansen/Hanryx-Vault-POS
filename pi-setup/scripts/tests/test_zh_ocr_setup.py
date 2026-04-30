"""
Tests for ZH-5: PaddleOCR zh-cht / zh-sim wiring + setup-ocr-models.sh
+ zh_full_sync.sh.

Hermetic: no real PaddleOCR is loaded (the worker is constructed with
its `paddle_factory` injection point) and no real network calls are
made (setup-ocr-models.sh is exercised in --dry-run mode plus a
filesystem-only idempotency check).

Coverage:
  PADDLE_LANG_MAP    — zh-cht, zh-sim, chs entries match the OCR model
                       wiring expected by setup-ocr-models.sh
  Worker construction — accepts zh-cht, accepts zh-sim, rejects unknown
  card_ocr DDL       — composite PK includes lang_hint so two ZH
                       variants of the same card coexist as distinct rows
  setup-ocr-models.sh — --help, --dry-run, unknown-lang reject, REC_URLS
                       parity with PADDLE_LANG_MAP keys, idempotent skip
                       when inference.pdmodel already exists, lang dir
                       layout matches what the worker expects
  zh_full_sync.sh    — bash syntax OK, expected stages present in order,
                       OCR preflight runs setup-ocr-models.sh for both
                       zh langs, both ocr_index passes are present
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workers.ocr_indexer import OcrIndexerWorker, PADDLE_LANG_MAP
from unified.schema import DDL_CARD_OCR

SETUP_SCRIPT = ROOT / "scripts" / "setup-ocr-models.sh"
ZH_SYNC_SCRIPT = ROOT / "scripts" / "zh_full_sync.sh"


# ── Minimal FakeConn so the worker constructor doesn't choke ───────────


class _NoopCursor:
    def execute(self, *a, **kw): pass
    def fetchone(self): return None
    def fetchall(self): return []


class _NoopConn:
    def cursor(self): return _NoopCursor()
    def commit(self): pass


# ── PADDLE_LANG_MAP ────────────────────────────────────────────────────


class PaddleLangMapTests(unittest.TestCase):
    def test_zh_cht_maps_to_chinese_cht(self):
        # The Traditional pack is a SEPARATE PaddleOCR model
        # (chinese_cht_PP-OCRv4_rec). Mis-mapping this to "ch" would
        # silently OCR Traditional text with the Simplified-glyph
        # model, producing low-confidence garbage on every TC card.
        self.assertEqual(PADDLE_LANG_MAP["zh-cht"], "chinese_cht")

    def test_zh_sim_aliases_chs(self):
        # zh-sim is deliberately the same Paddle model as the legacy
        # chs key (PaddleOCR `lang="ch"` IS the simplified pack).
        # Keeping them as separate lang_hints in card_ocr lets the
        # operator wipe one set of rows without touching the other.
        self.assertEqual(PADDLE_LANG_MAP["zh-sim"], PADDLE_LANG_MAP["chs"])
        self.assertEqual(PADDLE_LANG_MAP["zh-sim"], "ch")

    def test_legacy_chs_still_present(self):
        # name_chs auto-pick path in pick_primary_lang depends on
        # this; removing chs would break OCR for every card seeded
        # before ZH-5.
        self.assertIn("chs", PADDLE_LANG_MAP)

    def test_no_unexpected_keys(self):
        # Lock the set so a casual edit doesn't add (e.g.) "zh"
        # without thinking — every key here MUST also have a row in
        # setup-ocr-models.sh::REC_URLS.
        self.assertEqual(
            set(PADDLE_LANG_MAP),
            {"kr", "jp", "chs", "zh-sim", "zh-cht", "en"},
        )


# ── Worker construction ────────────────────────────────────────────────


class WorkerLangHintTests(unittest.TestCase):
    def test_accepts_zh_cht(self):
        # No exception = pass. We don't load Paddle yet (lazy).
        w = OcrIndexerWorker(_NoopConn(), lang_hint="zh-cht")
        self.assertEqual(w.lang_hint, "zh-cht")

    def test_accepts_zh_sim(self):
        w = OcrIndexerWorker(_NoopConn(), lang_hint="zh-sim")
        self.assertEqual(w.lang_hint, "zh-sim")

    def test_rejects_unknown(self):
        # Old-style typos like "zh-tw" (we use zh-cht) or "zhs" must
        # fail at construction, NOT silently process every card with
        # the default English model.
        with self.assertRaises(ValueError) as ctx:
            OcrIndexerWorker(_NoopConn(), lang_hint="zh-tw")
        self.assertIn("zh-tw", str(ctx.exception))
        self.assertIn("zh-cht", str(ctx.exception))     # error lists known

    def test_strips_whitespace_from_lang_hint(self):
        # Operators copy-paste the flag value from the wiki table;
        # trailing whitespace must not turn a valid lang into a
        # spurious ValueError.
        w = OcrIndexerWorker(_NoopConn(), lang_hint="  zh-cht  ")
        self.assertEqual(w.lang_hint, "zh-cht")


# ── card_ocr DDL — coexistence proof ───────────────────────────────────


class CardOcrCoexistenceTests(unittest.TestCase):
    def test_lang_hint_in_primary_key(self):
        # PRIMARY KEY must include lang_hint, otherwise running both
        # zh_full_sync ocr_index passes against the same physical
        # card would produce a UPSERT collision and we'd lose one of
        # the two languages every night.
        self.assertIn(
            "PRIMARY KEY (set_id, card_number, lang_hint, model_id)",
            DDL_CARD_OCR,
        )

    def test_card_ocr_indexes_lang_hint(self):
        # The recognizer's "prefer Traditional, fall back to anything"
        # query path depends on a lang-hinted index for fast lookup —
        # without it, scanning a Traditional card would seq-scan
        # card_ocr at every snapshot.
        self.assertIn("idx_card_ocr_lang", DDL_CARD_OCR)


# ── setup-ocr-models.sh ────────────────────────────────────────────────


class SetupOcrModelsScriptTests(unittest.TestCase):
    def test_script_exists_and_executable(self):
        self.assertTrue(SETUP_SCRIPT.exists(), SETUP_SCRIPT)
        self.assertTrue(os.access(SETUP_SCRIPT, os.X_OK), "must be +x")

    def test_bash_syntax(self):
        # `bash -n` parses without executing — catches stray $-quote
        # bugs that would only surface mid-trade-show.
        r = subprocess.run(
            ["bash", "-n", str(SETUP_SCRIPT)],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help_flag_prints_usage(self):
        r = subprocess.run(
            ["bash", str(SETUP_SCRIPT), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("setup-ocr-models.sh", r.stdout)

    def test_unknown_lang_rejected(self):
        # Operator typo must exit non-zero with a clear message
        # naming the bad lang AND listing what's known.
        env = os.environ.copy()
        env["OCR_MODELS_DIR"] = "/tmp/zh-test-bogus"
        r = subprocess.run(
            ["bash", str(SETUP_SCRIPT), "zh-tw"],   # typo for zh-cht
            capture_output=True, text=True, timeout=10, env=env,
        )
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("zh-tw", r.stderr)

    def test_dry_run_makes_no_network_calls(self):
        # We can't easily assert "no network", but we CAN assert that
        # --dry-run does not create model files and prints a plan.
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["OCR_MODELS_DIR"] = tmp
            r = subprocess.run(
                ["bash", str(SETUP_SCRIPT), "--dry-run", "zh-cht"],
                capture_output=True, text=True, timeout=10, env=env,
            )
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("DRY RUN", r.stdout)
            self.assertIn("would fetch", r.stdout)
            # No actual model dir should have been populated.
            zh_cht = Path(tmp) / "zh-cht" / "rec" / "inference.pdmodel"
            self.assertFalse(zh_cht.exists())

    def test_idempotent_skip_when_already_extracted(self):
        # Pre-stage a fake inference.pdmodel for zh-cht. The script
        # must NOT touch the network for it. We also pre-stage the
        # det/ subdir so the script doesn't try to fetch det either.
        # Then we run for ONLY zh-cht (so no other lang triggers a
        # download attempt). The run should succeed, both required
        # files left untouched.
        with tempfile.TemporaryDirectory() as tmp:
            for sub in ("det", "rec"):
                d = Path(tmp) / "zh-cht" / sub
                d.mkdir(parents=True)
                (d / "inference.pdmodel").write_text("FAKE")
                (d / "inference.pdiparams").write_text("FAKE")
            env = os.environ.copy()
            env["OCR_MODELS_DIR"] = tmp
            r = subprocess.run(
                ["bash", str(SETUP_SCRIPT), "zh-cht"],
                capture_output=True, text=True, timeout=10, env=env,
            )
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("[skip]", r.stdout)
            # Files unchanged (would have been replaced by a download).
            self.assertEqual(
                (Path(tmp) / "zh-cht" / "rec" / "inference.pdmodel").read_text(),
                "FAKE",
            )

    def test_rec_urls_parity_with_paddle_lang_map(self):
        # The script's REC_URLS associative array MUST cover every
        # key in workers/ocr_indexer.py::PADDLE_LANG_MAP. If this
        # ever drifts, --ocr-lang-hint <new> will fail at OCR time
        # with FACTORY_ERROR because the rec dir is empty.
        text = SETUP_SCRIPT.read_text()
        # Extract the bash array keys: ["foo"]="...".
        keys = set(re.findall(r'\["([^"]+)"\]=', text))
        self.assertEqual(
            keys, set(PADDLE_LANG_MAP),
            f"setup-ocr-models.sh REC_URLS keys ({sorted(keys)}) "
            f"diverged from PADDLE_LANG_MAP ({sorted(PADDLE_LANG_MAP)})",
        )

    def test_lang_dir_layout_matches_worker_expectation(self):
        # The worker does:
        #   lang_dir = os.path.join(models_dir, lang)
        #   det_model_dir = os.path.join(lang_dir, "det")
        #   rec_model_dir = os.path.join(lang_dir, "rec")
        # so the script must write into exactly that layout. We
        # assert by literal-string check on the script — cheap but
        # catches a refactor that changes one side without the other.
        text = SETUP_SCRIPT.read_text()
        self.assertIn('"$lang_dir/det"', text)
        self.assertIn('"$lang_dir/rec"', text)


# ── zh_full_sync.sh ────────────────────────────────────────────────────


class ZhFullSyncScriptTests(unittest.TestCase):
    def test_script_exists_and_executable(self):
        self.assertTrue(ZH_SYNC_SCRIPT.exists(), ZH_SYNC_SCRIPT)
        self.assertTrue(os.access(ZH_SYNC_SCRIPT, os.X_OK), "must be +x")

    def test_bash_syntax(self):
        r = subprocess.run(
            ["bash", "-n", str(ZH_SYNC_SCRIPT)],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_runs_setup_ocr_models_for_both_zh_langs(self):
        # The OCR preflight must request BOTH zh-cht and zh-sim;
        # otherwise one of the two ocr_index passes below would
        # silently produce only NO_LIB rows.
        text = ZH_SYNC_SCRIPT.read_text()
        self.assertIn("setup-ocr-models.sh zh-cht zh-sim", text)

    def test_both_ocr_index_passes_present(self):
        text = ZH_SYNC_SCRIPT.read_text()
        self.assertIn("--ocr-lang-hint zh-cht", text)
        self.assertIn("--ocr-lang-hint zh-sim", text)

    def test_runs_zh_specific_workers_in_expected_order(self):
        # We don't assert the ENTIRE order (kr_full_sync.sh's prose
        # does that for KR), only the ZH-specific dependency chain:
        # phase D must come before zh_set_audit, which must come
        # before cross_region_alias, which must come before the OCR
        # passes (you can't OCR a card whose row doesn't exist yet).
        #
        # Slice the script BODY (after `set -uo pipefail`) so the
        # prose header — which mentions stage names in a documenting
        # order, not the executing order — doesn't fool text.find().
        text = ZH_SYNC_SCRIPT.read_text()
        body_start = text.find("set -uo pipefail")
        self.assertGreater(body_start, 0, "script lost its strict-mode line")
        body = text[body_start:]
        order_markers = [
            "--phase D",
            "zh_set_audit",
            "cross_region_alias",
            "--ocr-lang-hint zh-cht",
        ]
        positions = [body.find(m) for m in order_markers]
        self.assertTrue(all(p > 0 for p in positions),
                        f"missing marker(s) in sync script body: {dict(zip(order_markers, positions))}")
        self.assertEqual(positions, sorted(positions),
                         f"stages out of order in body: {dict(zip(order_markers, positions))}")

    def test_strict_mode_no_e_flag(self):
        # Script must use `set -uo pipefail` (NOT `-e`) so a single
        # worker's transient failure doesn't abort the rest of the
        # overnight sync. This is a deliberate safety property called
        # out in the script header — guard it with a test.
        text = ZH_SYNC_SCRIPT.read_text()
        self.assertIn("set -uo pipefail", text)
        self.assertNotIn("set -euo pipefail", text)
        self.assertNotIn("set -eo pipefail", text)


if __name__ == "__main__":
    unittest.main()
