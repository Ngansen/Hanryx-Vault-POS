#!/usr/bin/env bash
# setup-ocr-models.sh — Pre-stage PP-OCRv4 model files for offline OCR.
#
# WHAT THIS DOES
# --------------
# Downloads + extracts the per-language detection and recognition
# models PaddleOCR needs into the layout the `ocr_indexer` worker
# actually queries at runtime: one subdir per *PaddleOCR language*
# (NOT per CLI lang_hint), with `det/` and `rec/` children.
#
# Without this, the worker still works — PaddleOCR will lazily
# download into `~/.paddleocr` on first OCR call. BUT that cache
# lives inside the container and gets wiped every `docker compose
# build`, so the operator pays a network tax (~50-100MB per language)
# every rebuild. At a trade show with no LAN this can mean OCR is
# silently broken for the whole day. Pre-staging onto the USB drive
# (which is the same drive cards_master + faiss live on, see
# docker-compose.yml `OCR_MODELS_DIR`) makes the entire OCR stack
# survive container rebuilds and SD card reflashes.
#
# WHAT GETS DOWNLOADED + WHERE
# ----------------------------
# For each requested CLI lang_hint we resolve the corresponding
# PaddleOCR `lang=` value via LANG_HINT_TO_PADDLE (which mirrors
# workers/ocr_indexer.py::PADDLE_LANG_MAP exactly), and lay out:
#
#   <models_dir>/<paddle_lang>/det/   ← detection model (text boxes)
#   <models_dir>/<paddle_lang>/rec/   ← recognition model (chars in box)
#
# The *paddle_lang* dir name matters because that's what the worker's
# `_factory(paddle_lang)` builds when telling PaddleOCR where to
# read/cache its weights. Staging by lang_hint key (kr, zh-cht, …)
# would be wrong — PaddleOCR would never look there, and the lazy
# fallback would re-download into the container on every cold start.
#
# Two CLI hints can share the same paddle dir:
#   chs → ch
#   zh-sim → ch    (deliberate alias; same Simplified pack)
# The script de-dupes paddle dirs internally so we don't re-fetch
# the same tarball for both.
#
# Detection: PP-OCRv4 ships ONE multilingual det model under
# chinese/, which is what `lang=korean`, `lang=japan`, etc. all use
# under PaddleOCR's defaults. We copy it into every <paddle_lang>/det/
# rather than symlinking, so the drive stays safe to replug into
# another Pi without worrying about symlink targets.
#
# Recognition: per-language. Mappings here MUST stay in sync with
# `workers/ocr_indexer.py::PADDLE_LANG_MAP` — if you add a new
# lang_hint there you must add a row to LANG_HINT_TO_PADDLE *and*
# REC_URLS here, otherwise `--ocr-lang-hint <new>` will fail with
# FACTORY_ERROR on first run. There's a Python parity test
# (test_zh_ocr_setup::test_rec_urls_parity_with_paddle_lang_map)
# that fails CI/local-suite if these drift.
#
# IDEMPOTENCY
# -----------
# Each <paddle_lang>/{det,rec}/ is checked for `inference.pdmodel`
# before touching the network. Re-running the script after a
# successful pass is a fast no-op — safe to leave in cron or to
# call from zh_full_sync.sh as a preflight.
#
# ATOMICITY
# ---------
# All staging happens inside `<models_dir>/.staging-XXXX/` (same
# physical filesystem as the destination), and the final move is
# `mv -T -- staged_dir final_dir` so it's a single rename(2)
# syscall. Power loss mid-extract leaves the .staging-XXXX dir
# orphaned (cleaned by the next run via the trap below) and the
# real <paddle_lang>/{det,rec}/ either fully present from a
# previous run or fully absent — never half-populated. We
# specifically do NOT erase the destination dir ahead of the
# rename: a crash there would leave a window where the dir is
# missing while PaddleOCR thinks it's just been erased.
#
# USAGE
# -----
#   bash setup-ocr-models.sh              # all languages
#   bash setup-ocr-models.sh kr jp        # only kr + jp
#   bash setup-ocr-models.sh --dry-run    # print plan, fetch nothing
#   OCR_MODELS_DIR=/tmp/p bash …          # override target dir
#
# Exit codes:
#   0 — all requested langs are present (or were just installed)
#   1 — bad CLI argument
#   2 — download failed (network, mirror down, bad URL)
#   3 — extraction failed (corrupt tarball, disk full)
set -uo pipefail

# Stable PP-OCRv4 download base. Bumped only when the upstream
# repo bumps a major version; see PaddleOCR release notes before
# touching. The model paths under this base have been stable since
# PP-OCRv4 GA (Aug 2023) — `multilingual/`, `chinese/`, `english/`
# subdirs.
PADDLE_BASE="https://paddleocr.bj.bcebos.com/PP-OCRv4"

# Shared detection model. PP-OCRv4 ships ONE multilingual det model
# under the chinese/ subdir; it's what `lang=korean`, `lang=japan`,
# etc. all use under PaddleOCR's own defaults.
DET_URL="${PADDLE_BASE}/chinese/ch_PP-OCRv4_det_infer.tar"

# CLI lang_hint → PaddleOCR `lang=` value.
# **MUST** match workers/ocr_indexer.py::PADDLE_LANG_MAP exactly.
declare -A LANG_HINT_TO_PADDLE=(
    ["kr"]="korean"
    ["jp"]="japan"
    ["chs"]="ch"
    ["zh-sim"]="ch"             # alias of chs (same model)
    ["zh-cht"]="chinese_cht"
    ["en"]="en"
)

# PaddleOCR `lang=` value → recognition tarball URL.
# Keyed by paddle_lang (NOT lang_hint) since chs and zh-sim collapse.
declare -A REC_URLS=(
    ["korean"]="${PADDLE_BASE}/multilingual/korean_PP-OCRv4_rec_infer.tar"
    ["japan"]="${PADDLE_BASE}/multilingual/japan_PP-OCRv4_rec_infer.tar"
    ["ch"]="${PADDLE_BASE}/chinese/ch_PP-OCRv4_rec_infer.tar"
    # Traditional-Chinese rec is pinned to PP-OCRv3 because Paddle
    # never published a v4 chinese_cht rec model — the v4 URL under
    # ${PADDLE_BASE}/multilingual/ returns hard 404, confirmed by
    # HEAD probe 2026-05. The v3 model is the latest available
    # upstream artefact for Traditional Chinese, lives at the
    # PP-OCRv3 base path, and is fully compatible with PaddleOCR's
    # current runtime (the worker reads rec_model_dir from the
    # filesystem; the inference protocol is stable across v3↔v4).
    # If/when Paddle ships v4 chinese_cht, swap this back to
    # ${PADDLE_BASE}/multilingual/chinese_cht_PP-OCRv4_rec_infer.tar
    # and bump the existing dir aside so it gets re-extracted.
    ["chinese_cht"]="https://paddleocr.bj.bcebos.com/PP-OCRv3/multilingual/chinese_cht_PP-OCRv3_rec_infer.tar"
    ["en"]="${PADDLE_BASE}/english/en_PP-OCRv4_rec_infer.tar"
)

ALL_LANGS=("kr" "jp" "chs" "zh-sim" "zh-cht" "en")

# ── CLI parsing ────────────────────────────────────────────────────────
DRY_RUN=0
REQUESTED=()
for arg in "$@"; do
    case "$arg" in
        --dry-run|-n)  DRY_RUN=1 ;;
        --help|-h)
            sed -n '2,80p' "$0"
            exit 0
            ;;
        --*)
            echo "FATAL: unknown flag: $arg" >&2
            exit 1
            ;;
        *)
            if [[ -z "${LANG_HINT_TO_PADDLE[$arg]:-}" ]]; then
                echo "FATAL: unknown lang '$arg'." >&2
                echo "Known langs: ${!LANG_HINT_TO_PADDLE[*]}" >&2
                exit 1
            fi
            REQUESTED+=("$arg")
            ;;
    esac
done
if [[ ${#REQUESTED[@]} -eq 0 ]]; then
    REQUESTED=("${ALL_LANGS[@]}")
fi

# Resolve to UNIQUE paddle_lang values (chs and zh-sim collapse to ch).
declare -A SEEN_PADDLE=()
PADDLE_LANGS=()
for hint in "${REQUESTED[@]}"; do
    pl="${LANG_HINT_TO_PADDLE[$hint]}"
    if [[ -z "${SEEN_PADDLE[$pl]:-}" ]]; then
        SEEN_PADDLE[$pl]=1
        PADDLE_LANGS+=("$pl")
    fi
done

MODELS_DIR="${OCR_MODELS_DIR:-/mnt/cards/models/paddleocr}"
echo "[ocr-models] target dir:     $MODELS_DIR"
echo "[ocr-models] CLI lang_hints: ${REQUESTED[*]}"
echo "[ocr-models] paddle dirs:    ${PADDLE_LANGS[*]}"
[[ $DRY_RUN -eq 1 ]] && echo "[ocr-models] DRY RUN — nothing will be downloaded"

# ── Helpers ────────────────────────────────────────────────────────────
mkdir -p "$MODELS_DIR"

# Same-FS staging. mktemp -p must succeed before we can clean orphans.
if [[ $DRY_RUN -eq 0 ]]; then
    STAGING_ROOT="$(mktemp -d -p "$MODELS_DIR" .staging-XXXXXX)" || {
        echo "FATAL: could not create staging dir under $MODELS_DIR" >&2
        exit 3
    }
    # Always clean up our staging on exit (success OR failure).
    # shellcheck disable=SC2064  # expand STAGING_ROOT now, not at trap time
    trap "rm -rf -- '$STAGING_ROOT'" EXIT
else
    STAGING_ROOT="/tmp/ocr-models-dry-run-noop"
fi

fetch_and_extract() {
    # $1 = URL, $2 = destination dir (will hold inference.pdmodel etc.)
    local url="$1" dest="$2"
    if [[ -f "$dest/inference.pdmodel" ]]; then
        echo "    [skip] $dest already has inference.pdmodel"
        return 0
    fi
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "    [plan] would fetch  $url"
        echo "    [plan] would extract → $dest"
        return 0
    fi
    # Per-fetch staging subdir, sibling-of-dest (same FS guaranteed).
    local stage_sub; stage_sub="$(mktemp -d -p "$STAGING_ROOT" fetch-XXXXXX)"
    local tarball="$stage_sub/$(basename "$url")"
    echo "    [get]  $url"
    if ! wget -c -q --show-progress -O "$tarball" "$url"; then
        echo "    FATAL: download failed: $url" >&2
        rm -rf -- "$stage_sub"
        return 2
    fi
    if ! tar -xf "$tarball" -C "$stage_sub"; then
        echo "    FATAL: extraction failed: $tarball" >&2
        rm -rf -- "$stage_sub"
        return 3
    fi
    # Tarballs unpack to a single dir like `ch_PP-OCRv4_rec_infer/`.
    local extracted
    extracted="$(find "$stage_sub" -mindepth 1 -maxdepth 1 -type d | head -n1)"
    if [[ -z "$extracted" ]]; then
        echo "    FATAL: tarball had no top-level dir: $tarball" >&2
        rm -rf -- "$stage_sub"
        return 3
    fi
    if [[ ! -f "$extracted/inference.pdmodel" ]]; then
        echo "    FATAL: extracted dir missing inference.pdmodel: $extracted" >&2
        rm -rf -- "$stage_sub"
        return 3
    fi
    mkdir -p "$(dirname "$dest")"
    # Atomic-ish swap: move staged into final position. mv -T treats
    # the destination as a regular target rather than "move-into-dir";
    # combined with same-FS staging that's a single rename(2) syscall.
    # If $dest already exists (e.g. concurrent install or partial
    # crash leftover that lacked inference.pdmodel — we wouldn't be
    # here otherwise), `mv -T` would fail; pre-move the existing dir
    # aside so we can clean it after the swap is durable.
    local backup=""
    if [[ -e "$dest" ]]; then
        backup="$dest.old.$$"
        mv -T -- "$dest" "$backup" || {
            echo "    FATAL: could not move existing $dest aside" >&2
            rm -rf -- "$stage_sub"
            return 3
        }
    fi
    if ! mv -T -- "$extracted" "$dest"; then
        echo "    FATAL: atomic rename failed: $extracted → $dest" >&2
        # Best-effort restore.
        [[ -n "$backup" && -e "$backup" ]] && mv -T -- "$backup" "$dest"
        rm -rf -- "$stage_sub"
        return 3
    fi
    [[ -n "$backup" ]] && rm -rf -- "$backup"
    rm -rf -- "$stage_sub"
    echo "    [ok]   $dest"
    return 0
}

# Clean any orphaned .staging-XXXXXX dirs from prior crashed runs.
# Safe because we're about to create our own and nothing else writes
# .staging-* names under this dir.
if [[ $DRY_RUN -eq 0 ]]; then
    find "$MODELS_DIR" -mindepth 1 -maxdepth 1 -type d -name '.staging-*' \
        ! -path "$STAGING_ROOT" -exec rm -rf -- {} + 2>/dev/null || true
fi

# ── Main loop ──────────────────────────────────────────────────────────
errors=0
for paddle_lang in "${PADDLE_LANGS[@]}"; do
    echo
    echo "[ocr-models] === $paddle_lang ==="
    lang_dir="$MODELS_DIR/$paddle_lang"
    fetch_and_extract "$DET_URL"               "$lang_dir/det" || errors=$?
    fetch_and_extract "${REC_URLS[$paddle_lang]}" "$lang_dir/rec" || errors=$?
done

echo
if [[ $errors -ne 0 ]]; then
    echo "[ocr-models] FAIL — last error code: $errors" >&2
    exit "$errors"
fi
echo "[ocr-models] ALL DONE — ${#PADDLE_LANGS[@]} paddle dir(s) ready."
