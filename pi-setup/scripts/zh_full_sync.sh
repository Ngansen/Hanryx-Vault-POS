#!/usr/bin/env bash
# zh_full_sync.sh — One-shot full Chinese (TC + SC) card database sync.
#
# Drives the entire ZH pipeline end-to-end, mirroring kr_full_sync.sh:
#   setup-ocr-models.sh zh-cht zh-sim       (preflight: model files)
#   Phase A → Phase D            (sync_card_mirror; A re-runs idempotently
#                                 to refresh PTCG-CHS-Datasets clone, then
#                                 D walks zh_sources for TC scrape +
#                                 SC local-mirror copy)
#   zh_set_audit                 (TC+SC completeness + auto-refresh
#                                 zh_sc.json from ptcg_chs_infos.json)
#   cross_region_alias           (link new ZH rows to JP canonical via
#                                 set-abbrev → CLIP fallback chain)
#   image_health → image_mirror  (rot detection + recovery, same as KR)
#   clip_embed                   (CLIP visual fingerprints for ZH cards)
#   ocr_index --ocr-lang-hint zh-cht   (Traditional pack: PP-OCRv4
#                                       chinese_cht model)
#   ocr_index --ocr-lang-hint zh-sim   (Simplified pack: PP-OCRv4 ch
#                                       model — same model as the
#                                       legacy `chs` pass but indexed
#                                       under a separate lang_hint so
#                                       the SC mirror gets its own
#                                       searchable rows in card_ocr)
#   image_thumbnail              (200/800 WEBP tiers, lang-agnostic)
#   price_refresh                (bulk catalogue prefill — Cardmarket
#                                 sometimes lists the JP equivalent's
#                                 price for ZH cards via the alias)
#
# Why TWO ocr_index passes?
#   card_ocr's PRIMARY KEY is (set_id, card_number, lang_hint, model_id),
#   so the same physical card can have one row per language pass without
#   collision. The recognizer query at lookup time can then prefer
#   lang_hint='zh-cht' for a TC scan and fall back to 'zh-sim' / 'jp' /
#   'kr' / 'en' if no Traditional row exists. Running both passes during
#   an overnight sync means the lookup is always offline and instant
#   instead of paying a Paddle model-load cost mid-trade.
#
# Strict mode: -u (undefined vars are errors) + -o pipefail
#              (catch failures inside pipelines), but NOT -e.
# We deliberately do NOT abort on a single step's non-zero exit:
# if clip_embed fails on a corrupt PNG, we still want ocr_index and
# image_thumbnail to run — the failed work is queued via the bg
# task system and a re-run picks up where this one left off. Per-step
# failures are visible in the log via the LOG_TAG banners.
#
# Usage:
#   tmux new-session -d -s zhsync \
#     'bash ~/Hanryx-Vault-POS/pi-setup/scripts/zh_full_sync.sh \
#      2>&1 | tee -a /mnt/cards/zhsync.log'
#
#   tmux attach -t zhsync           # watch live; ctrl-B then D detaches
#   tail -f /mnt/cards/zhsync.log   # alternative live tail
#
# To run only TC or only SC, pass through to sync_card_mirror's flags:
#   ZH_PHASE_D_FLAGS="--exclude-zh-sc"  bash zh_full_sync.sh   # TC only
#   ZH_PHASE_D_FLAGS="--exclude-zh-tc"  bash zh_full_sync.sh   # SC only
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1   # → pi-setup/

LOG_TAG() {
    echo
    echo "============================================================"
    echo "=== $(date -u +%H:%M:%S) UTC — $1"
    echo "============================================================"
}

DC="docker compose exec -T --workdir /app sync"
ZH_PHASE_D_FLAGS="${ZH_PHASE_D_FLAGS:-}"

# ── Preflight ────────────────────────────────────────────────────────────────
# Same shape as kr_full_sync.sh: bail early if the sync container is missing
# or the package layout inside it doesn't match what the run expects.
LOG_TAG "Preflight: verify sync container + python package layout"
if ! docker compose ps --status running --services 2>/dev/null | grep -qx sync; then
    echo "FATAL: sync container is not running."
    echo "Fix: cd $(pwd) && docker compose up -d   # then re-run this script"
    exit 2
fi
if ! $DC python -c "import scripts.sync_card_mirror, workers.run, scripts.zh_sources" 2>&1; then
    echo "FATAL: scripts/workers/zh_sources packages not importable inside sync container."
    echo "Diagnostic: $DC pwd && $DC ls /app"
    exit 3
fi
echo "Preflight OK — scripts, workers, and zh_sources import cleanly."

# OCR model preflight runs from the HOST side, not inside the container —
# it writes to /mnt/cards/models/paddleocr/ which is the bind-mount source.
# Inside-container the models dir is read-only-ish from the worker's POV.
# Idempotent, so re-runs are cheap. Failure here is fatal because the two
# ocr_index steps below will silently produce NO_LIB rows for every card.
LOG_TAG "Preflight: PP-OCRv4 model packs (zh-cht + zh-sim)"
if ! bash "$(dirname "$0")/setup-ocr-models.sh" zh-cht zh-sim; then
    echo "FATAL: setup-ocr-models.sh failed — OCR steps would produce only NO_LIB rows." >&2
    exit 4
fi

LOG_TAG "Phase A: refresh source repos (PTCG-CHS-Datasets etc., idempotent)"
$DC python -m scripts.sync_card_mirror --phase A

LOG_TAG "Phase D: walk zh_sources (TC scrape + SC local-mirror copy)"
# shellcheck disable=SC2086  # intentional word-splitting for $ZH_PHASE_D_FLAGS
$DC python -m scripts.sync_card_mirror --phase D $ZH_PHASE_D_FLAGS

LOG_TAG "zh_set_audit: TC+SC completeness + auto-refresh zh_sc.json"
$DC python -m workers.run zh_set_audit --seed --once

LOG_TAG "cross_region_alias: link ZH cards to JP canonical (set-abbrev → CLIP)"
$DC python -m workers.run cross_region_alias --seed --once

LOG_TAG "image_health: decode/missing/empty audit (auto-enqueues image_mirror)"
$DC python -m workers.run image_health --seed --once

LOG_TAG "image_mirror: drain rot-recovery queue"
$DC python -m workers.run image_mirror --once

LOG_TAG "clip_embed: CLIP visual fingerprints for ZH cards"
$DC python -m workers.run clip_embed --seed --once

LOG_TAG "ocr_index (zh-cht): Traditional Chinese text indexing"
$DC python -m workers.run ocr_index --seed --once --ocr-lang-hint zh-cht

LOG_TAG "ocr_index (zh-sim): Simplified Chinese text indexing"
$DC python -m workers.run ocr_index --seed --once --ocr-lang-hint zh-sim

LOG_TAG "image_thumbnail: 200/800 WEBP tiers"
$DC python -m workers.run image_thumbnail --seed --once

LOG_TAG "price_refresh: bulk prefill catalogue tier (uses card_alias for ZH→JP price fallback)"
$DC python -m workers.run price_refresh --seed --once

LOG_TAG "ALL DONE — disk usage:"
df -h /mnt/cards
echo
echo "ZH mirror tree sizes:"
du -sh /mnt/cards/zh/{zh-tc,zh-sc} 2>/dev/null || true
du -sh /mnt/cards/models/paddleocr/{zh-cht,zh-sim} 2>/dev/null || true
