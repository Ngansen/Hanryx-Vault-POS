#!/usr/bin/env bash
# kr_full_sync.sh — One-shot full Korean card database sync.
#
# Drives the entire pipeline end-to-end:
#   Phase A → Phase B → Phase C  (sync_card_mirror)
#   kr_set_audit                  (completeness check)
#   image_health → image_mirror   (rot detection + recovery)
#   clip_embed                    (CLIP visual fingerprints)
#   ocr_index --ocr-lang-hint kr  (Hangul text indexing)
#   image_thumbnail               (200 / 800 px WEBP tiers)
#   price_refresh                 (bulk catalogue prefill)
#
# Designed for overnight runs in tmux. Each step is idempotent and
# uses --seed --once semantics, so re-running after a failure picks
# up where the previous run left off — no cleanup required.
#
# Strict mode: -u (undefined vars are errors) + -o pipefail
#              (catch failures inside pipelines), but NOT -e.
# We deliberately do NOT abort on a single step's non-zero exit:
# if clip_embed fails, we still want image_thumbnail to run, etc.
# Per-step failures are visible in the log via the LOG_TAG banners.
#
# Usage:
#   tmux new-session -d -s krsync \
#     'bash ~/Hanryx-Vault-POS/pi-setup/scripts/kr_full_sync.sh \
#      2>&1 | tee -a /mnt/cards/krsync.log'
#
#   tmux attach -t krsync           # watch live; ctrl-B then D detaches
#   tail -f /mnt/cards/krsync.log   # alternative live tail
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1   # → pi-setup/

LOG_TAG() {
    echo
    echo "============================================================"
    echo "=== $(date -u +%H:%M:%S) UTC — $1"
    echo "============================================================"
}

DC="docker compose exec -T --workdir /app sync"

# ── Preflight ────────────────────────────────────────────────────────────────
# Bail early if the sync container is missing or the package layout inside it
# doesn't match what the run expects. Without this, a misconfigured container
# silently fails every step and you wake up to an empty mirror.
LOG_TAG "Preflight: verify sync container + python package layout"
if ! docker compose ps --status running --services 2>/dev/null | grep -qx sync; then
    echo "FATAL: sync container is not running."
    echo "Fix: cd $(pwd) && docker compose up -d   # then re-run this script"
    exit 2
fi
if ! $DC python -c "import scripts.sync_card_mirror, workers.run" 2>&1; then
    echo "FATAL: scripts/workers packages not importable inside sync container."
    echo "Diagnostic: $DC pwd && $DC ls /app"
    exit 3
fi
echo "Preflight OK — both 'scripts' and 'workers' packages import cleanly."

LOG_TAG "Phase A: clone source repos (~4h on USB2, ~25min on USB3)"
$DC python -m scripts.sync_card_mirror --phase A

LOG_TAG "Phase B: download KR card images (~15min)"
$DC python -m scripts.sync_card_mirror --phase B

LOG_TAG "Phase C: CDN walk for image_url_alt fallbacks (~10min)"
$DC python -m scripts.sync_card_mirror --phase C

LOG_TAG "kr_set_audit: completeness vs canonical KR set list"
$DC python -m workers.run kr_set_audit --seed --once

LOG_TAG "image_health: decode/missing/empty audit (auto-enqueues image_mirror)"
$DC python -m workers.run image_health --seed --once

LOG_TAG "image_mirror: drain rot-recovery queue"
$DC python -m workers.run image_mirror --once

LOG_TAG "clip_embed: CLIP visual fingerprints (~14min)"
$DC python -m workers.run clip_embed --seed --once

LOG_TAG "ocr_index: Hangul text indexing (~37min)"
$DC python -m workers.run ocr_index --seed --once --ocr-lang-hint kr

LOG_TAG "image_thumbnail: 200/800 WEBP tiers (~36min)"
$DC python -m workers.run image_thumbnail --seed --once

LOG_TAG "price_refresh: bulk prefill catalogue tier"
$DC python -m workers.run price_refresh --seed --once

LOG_TAG "ALL DONE — disk usage:"
df -h /mnt/cards
echo
echo "Mirror tree sizes:"
du -sh /mnt/cards/{git,cdn,thumbs,jp_pokell,jp_pcc,faiss} 2>/dev/null || true
