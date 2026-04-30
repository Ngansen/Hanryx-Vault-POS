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


class WorkersRunCliTests(unittest.TestCase):
    """Architect FIX 1: workers/run.py used to hard-code argparse
    choices=['kr','jp','chs','en'], which made --ocr-lang-hint
    zh-cht / zh-sim silently unreachable from CLI even after
    PADDLE_LANG_MAP grew them — argparse rejected the value before
    the worker ever validated. These tests pin the new behavior."""

    @staticmethod
    def _extract_ocr_lang_hint_block(run_py: str) -> str:
        # The help text contains parentheses (e.g. "(KR > JP > CHS >
        # EN)"), so a regex like add_argument\(...[^)]*?\) collapses
        # at the first inner `)`. Slice by sentinel boundaries
        # instead: from `--ocr-lang-hint"` up to the next
        # `--ocr-models-dir"` add_argument call.
        start = run_py.index('"--ocr-lang-hint"')
        end = run_py.index('"--ocr-models-dir"', start)
        return run_py[start:end]

    def test_argparse_does_not_restrict_ocr_lang_hint(self):
        # Source-level guard: the choices= keyword argument must not
        # appear on the --ocr-lang-hint flag definition. Validation
        # is delegated to OcrIndexerWorker against PADDLE_LANG_MAP.
        run_py = (ROOT / "workers" / "run.py").read_text()
        block = self._extract_ocr_lang_hint_block(run_py)
        self.assertNotIn("choices=", block,
                         "FIX 1 regressed: argparse re-restricted the "
                         "lang hint with choices=, will reject zh-cht "
                         "before the worker validates")

    def test_help_text_mentions_new_zh_lang_hints(self):
        # Operator-discoverability: `workers.run --help` should list
        # the ZH-5 hints so an operator finds them without grepping.
        run_py = (ROOT / "workers" / "run.py").read_text()
        block = self._extract_ocr_lang_hint_block(run_py)
        self.assertIn("zh-cht", block)
        self.assertIn("zh-sim", block)


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
        # Note the dir we check is the *paddle_lang* dir (chinese_cht),
        # not the lang_hint key (zh-cht), because the worker reads
        # from the paddle_lang dir at runtime — FIX 2 in the architect
        # review.
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
            # No actual model dir under paddle_lang should be populated.
            self.assertFalse(
                (Path(tmp) / "chinese_cht" / "rec" / "inference.pdmodel").exists())
            # And explicitly NOT under the lang_hint key — if this
            # ever fires, the script regressed back to the old
            # mis-layout and the worker would never find the models.
            self.assertFalse(
                (Path(tmp) / "zh-cht" / "rec" / "inference.pdmodel").exists())

    def test_idempotent_skip_when_already_extracted(self):
        # Pre-stage a fake inference.pdmodel under the *paddle_lang*
        # dir (chinese_cht — what the worker actually reads), not
        # the lang_hint key. The script must NOT touch the network.
        with tempfile.TemporaryDirectory() as tmp:
            for sub in ("det", "rec"):
                d = Path(tmp) / "chinese_cht" / sub
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
                (Path(tmp) / "chinese_cht" / "rec" / "inference.pdmodel").read_text(),
                "FAKE",
            )

    def test_chs_and_zh_sim_share_paddle_dir(self):
        # chs and zh-sim both map to PaddleOCR `lang="ch"`, so they
        # MUST share the same on-disk dir (otherwise we'd download
        # the same ~100MB tarball twice). Pre-stage `ch/` and ask
        # for both hints; the script should skip both as already
        # present and log only ONE paddle dir in its summary.
        with tempfile.TemporaryDirectory() as tmp:
            for sub in ("det", "rec"):
                d = Path(tmp) / "ch" / sub
                d.mkdir(parents=True)
                (d / "inference.pdmodel").write_text("FAKE")
            env = os.environ.copy()
            env["OCR_MODELS_DIR"] = tmp
            r = subprocess.run(
                ["bash", str(SETUP_SCRIPT), "chs", "zh-sim"],
                capture_output=True, text=True, timeout=10, env=env,
            )
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            # Should mention only one paddle dir even though we
            # asked for two CLI hints.
            self.assertIn("paddle dirs:    ch", r.stdout)
            self.assertIn("1 paddle dir(s) ready", r.stdout)

    @staticmethod
    def _extract_bash_assoc_block(text: str, name: str) -> str:
        # bash associative-array bodies can contain inline `# … (…)`
        # comments whose `)` would terminate a non-greedy `.*?\)`
        # match prematurely. Use the closing-paren-on-its-own-line
        # convention as the boundary instead.
        pattern = (
            r'declare -A ' + re.escape(name) + r'=\((.*?)^\)\s*$'
        )
        m = re.search(pattern, text, flags=re.DOTALL | re.MULTILINE)
        if m is None:
            raise AssertionError(
                f"declare -A {name} block not found (or closing `)` "
                "is not on its own line — required by this parser)"
            )
        return m.group(1)

    def test_rec_urls_parity_with_paddle_lang_map(self):
        # Two parity invariants:
        #   (a) every key in PADDLE_LANG_MAP must appear in the
        #       script's LANG_HINT_TO_PADDLE map (otherwise the
        #       lang_hint is unreachable from this script);
        #   (b) every VALUE in PADDLE_LANG_MAP must appear as a key
        #       in REC_URLS (otherwise the script doesn't know
        #       where to download the rec model from);
        #   (c) the mapping in the script must agree with PADDLE_LANG_MAP
        #       exactly — if PADDLE_LANG_MAP says `zh-cht→chinese_cht`,
        #       the script must too.
        text = SETUP_SCRIPT.read_text()
        body = self._extract_bash_assoc_block(text, "LANG_HINT_TO_PADDLE")
        script_map = dict(re.findall(r'\["([^"]+)"\]="([^"]+)"', body))
        # (a) + (c) — full agreement on keys and values.
        self.assertEqual(
            script_map, dict(PADDLE_LANG_MAP),
            f"setup-ocr-models.sh LANG_HINT_TO_PADDLE diverged from "
            f"PADDLE_LANG_MAP. Script: {script_map}; Python: "
            f"{dict(PADDLE_LANG_MAP)}",
        )
        # (b) REC_URLS keys cover all unique paddle_lang values.
        rec_body = self._extract_bash_assoc_block(text, "REC_URLS")
        rec_keys = set(re.findall(r'\["([^"]+)"\]=', rec_body))
        self.assertEqual(
            rec_keys, set(PADDLE_LANG_MAP.values()),
            f"REC_URLS keys ({sorted(rec_keys)}) don't cover unique "
            f"PADDLE_LANG_MAP values ({sorted(set(PADDLE_LANG_MAP.values()))})",
        )

    def test_stages_in_paddle_lang_dirs_not_lang_hint_dirs(self):
        # Architect FIX 2: the worker's _factory builds model paths
        # from the PaddleOCR `lang=` value (the VALUE of
        # PADDLE_LANG_MAP), not from the CLI lang_hint key.
        # Pre-architect-review the script staged by lang_hint, which
        # silently mis-aligned with runtime. Lock the new behavior
        # by checking the script uses the resolved $paddle_lang
        # variable for the dir path.
        text = SETUP_SCRIPT.read_text()
        # The main loop must iterate over paddle_lang and use it
        # (not $lang or $hint) to build lang_dir.
        self.assertIn('lang_dir="$MODELS_DIR/$paddle_lang"', text)
        self.assertIn('"$lang_dir/det"', text)
        self.assertIn('"$lang_dir/rec"', text)

    def test_atomicity_uses_same_fs_staging(self):
        # Architect FIX 3: mktemp -d defaults to /tmp on a Pi
        # (tmpfs in RAM), and `mv /tmp/... /mnt/cards/...` is
        # cross-filesystem — degrades to cp+rm, NOT atomic. The
        # script must stage under $MODELS_DIR so rename(2) is one
        # syscall regardless of where the operator mounted the
        # drive. Also verify there's no `rm -rf "$dest"` BEFORE the
        # rename, which would open a window of missing-dir on crash.
        text = SETUP_SCRIPT.read_text()
        self.assertIn('mktemp -d -p "$MODELS_DIR"', text)
        # mv must use -T for atomic dir-rename semantics.
        self.assertIn('mv -T --', text)
        # No pre-rename rm of the destination dir.
        self.assertNotIn('rm -rf "$dest"', text)
        self.assertNotIn('rm -rf -- "$dest"', text)


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
