#!/usr/bin/env bash
# setup-ocr-models.sh — Pre-stage PP-OCRv4 model files for offline OCR.
#
# WHAT THIS DOES
# --------------
# Downloads + extracts the per-language detection and recognition
# models PaddleOCR needs into the layout the `ocr_indexer` worker
# expects (one subdir per language with `det/` and `rec/` children).
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
# WHAT GETS DOWNLOADED
# --------------------
# For each language:
#   <models_dir>/<lang>/det/     ← detection model (text boxes)
#   <models_dir>/<lang>/rec/     ← recognition model (chars in box)
#
# Detection: PP-OCRv4 multilingual `ch_PP-OCRv4_det_infer` works for
# all CJK + Latin scripts (the same det model PaddleOCR ships by
# default for any non-English language). We copy it into every
# lang/det/ rather than symlinking, so the drive stays safe to
# replug into another Pi without worrying about symlink targets.
#
# Recognition: per-language. Mappings here MUST stay in sync with
# `workers/ocr_indexer.py::PADDLE_LANG_MAP` — if you add a new
# lang_hint there you must add a row to `MODEL_URLS` here, otherwise
# `--ocr-lang-hint <new>` will fail with FACTORY_ERROR on first run.
#
# IDEMPOTENCY
# -----------
# Each lang/{det,rec}/ is checked for `inference.pdmodel` before
# touching the network. Re-running the script after a successful
# pass is a fast no-op — safe to leave in a cron or to call from
# zh_full_sync.sh as a preflight.
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

# lang_hint → recognition tarball URL. Keep in lockstep with
# PADDLE_LANG_MAP in workers/ocr_indexer.py.
declare -A REC_URLS=(
    ["kr"]="${PADDLE_BASE}/multilingual/korean_PP-OCRv4_rec_infer.tar"
    ["jp"]="${PADDLE_BASE}/multilingual/japan_PP-OCRv4_rec_infer.tar"
    ["chs"]="${PADDLE_BASE}/chinese/ch_PP-OCRv4_rec_infer.tar"
    # zh-sim deliberately points at the same chinese-simplified
    # tarball as `chs`. The worker maps both lang_hints to PaddleOCR
    # `lang="ch"`; staging into a separate dir lets the operator wipe
    # one without affecting the other (e.g. retire the `chs` legacy
    # entries without losing the explicit zh-sim cache).
    ["zh-sim"]="${PADDLE_BASE}/chinese/ch_PP-OCRv4_rec_infer.tar"
    ["zh-cht"]="${PADDLE_BASE}/multilingual/chinese_cht_PP-OCRv4_rec_infer.tar"
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
            sed -n '2,50p' "$0"
            exit 0
            ;;
        --*)
            echo "FATAL: unknown flag: $arg" >&2
            exit 1
            ;;
        *)
            if [[ -z "${REC_URLS[$arg]:-}" ]]; then
                echo "FATAL: unknown lang '$arg'." >&2
                echo "Known langs: ${!REC_URLS[*]}" >&2
                exit 1
            fi
            REQUESTED+=("$arg")
            ;;
    esac
done
if [[ ${#REQUESTED[@]} -eq 0 ]]; then
    REQUESTED=("${ALL_LANGS[@]}")
fi

MODELS_DIR="${OCR_MODELS_DIR:-/mnt/cards/models/paddleocr}"
echo "[ocr-models] target dir: $MODELS_DIR"
echo "[ocr-models] languages:  ${REQUESTED[*]}"
[[ $DRY_RUN -eq 1 ]] && echo "[ocr-models] DRY RUN — nothing will be downloaded"

# ── Helpers ────────────────────────────────────────────────────────────
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
    local tmp; tmp="$(mktemp -d)"
    local tarball="$tmp/$(basename "$url")"
    echo "    [get]  $url"
    if ! wget -c -q --show-progress -O "$tarball" "$url"; then
        echo "    FATAL: download failed: $url" >&2
        rm -rf "$tmp"
        return 2
    fi
    if ! tar -xf "$tarball" -C "$tmp"; then
        echo "    FATAL: extraction failed: $tarball" >&2
        rm -rf "$tmp"
        return 3
    fi
    # Tarballs unpack to a single dir like `ch_PP-OCRv4_rec_infer/`.
    # Move its contents into dest, atomically-ish. Stage to a sibling
    # dir first so a power loss mid-mv leaves either the old (empty)
    # dest or the new files — never a half-populated dest that
    # PaddleOCR would interpret as a corrupt model.
    local extracted; extracted="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -n1)"
    if [[ -z "$extracted" ]]; then
        echo "    FATAL: tarball had no top-level dir: $tarball" >&2
        rm -rf "$tmp"
        return 3
    fi
    mkdir -p "$(dirname "$dest")"
    rm -rf "$dest"
    mv "$extracted" "$dest"
    rm -rf "$tmp"
    echo "    [ok]   $dest"
    return 0
}

# ── Main loop ──────────────────────────────────────────────────────────
mkdir -p "$MODELS_DIR"
errors=0
for lang in "${REQUESTED[@]}"; do
    echo
    echo "[ocr-models] === $lang ==="
    lang_dir="$MODELS_DIR/$lang"
    fetch_and_extract "$DET_URL" "$lang_dir/det" || errors=$?
    fetch_and_extract "${REC_URLS[$lang]}" "$lang_dir/rec" || errors=$?
done

echo
if [[ $errors -ne 0 ]]; then
    echo "[ocr-models] FAIL — last error code: $errors" >&2
    exit "$errors"
fi
echo "[ocr-models] ALL DONE — ${#REQUESTED[@]} language(s) ready."
