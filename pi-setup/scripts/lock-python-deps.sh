#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# lock-python-deps.sh — regenerate hash-pinned requirements.txt files.
#
# Why this exists
# ───────────────
# `pi-setup/Dockerfile` and `pi-setup/recognizer/Dockerfile` install Python
# deps with `pip install --require-hashes -r requirements.txt`. The
# `requirements.txt` files in those directories are NOT hand-edited — they
# are generated from the matching `requirements.in` by `uv pip compile`.
# This script wraps that regeneration.
#
# CRITICAL: the lockfile is resolved against the `aarch64-unknown-linux-gnu`
# platform (the Pi 5 target), not against whatever machine you happen to be
# running this on. We use `uv pip compile --python-platform=...` which reads
# wheel metadata from PyPI and resolves transitive deps as if it were running
# on the target — so an x86 maintainer can regenerate without spinning up a
# Pi or qemu.
#
# Usage
# ─────
#     pi-setup/scripts/lock-python-deps.sh pi-setup    # locks pi-setup/requirements.{in,txt}
#     pi-setup/scripts/lock-python-deps.sh recognizer  # locks pi-setup/recognizer/requirements.{in,txt}
#     pi-setup/scripts/lock-python-deps.sh all         # both
#
# After running, commit the resulting `requirements.txt` change. The next
# `docker compose build` will then install the newly pinned versions.
#
# Bumping the OpenAI CLIP git pin (`pi-setup/requirements-vcs.txt`) is a
# separate, manual step — see `pi-setup/docs/REPRODUCIBILITY.md`.
#
# Prerequisites
# ─────────────
# `uv` must be installed (https://github.com/astral-sh/uv). On the Pi or any
# Linux dev box:
#     curl -LsSf https://astral.sh/uv/install.sh | sh
# Or with pip:
#     python3 -m pip install --user uv
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# Match the FROM line in pi-setup/Dockerfile and pi-setup/recognizer/Dockerfile.
# Bump in lock-step with the Dockerfiles' Python pin.
PYTHON_VERSION="${PYTHON_VERSION:-3.11}"

# The Pi 5 target. Bump only if the Pi target arch changes.
PYTHON_PLATFORM="${PYTHON_PLATFORM:-aarch64-unknown-linux-gnu}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PI_SETUP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
    echo "Usage: $0 {pi-setup|recognizer|all}" >&2
    exit 2
}

[ $# -eq 1 ] || usage

require_uv() {
    if ! command -v uv >/dev/null 2>&1 && ! python3 -m uv --version >/dev/null 2>&1; then
        echo "[lock] FATAL: 'uv' not found. Install it with:" >&2
        echo "    python3 -m pip install --user uv" >&2
        echo "or" >&2
        echo "    curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
        exit 1
    fi
}

uv_cmd() {
    if command -v uv >/dev/null 2>&1; then
        uv "$@"
    else
        python3 -m uv "$@"
    fi
}

run_lock() {
    # $1: human-readable name (for log lines)
    # $2: directory inside pi-setup/ that holds requirements.in
    local name="$1"
    local rel_dir="$2"
    local in_path="$PI_SETUP_DIR/$rel_dir/requirements.in"
    local out_path="$PI_SETUP_DIR/$rel_dir/requirements.txt"

    if [ ! -f "$in_path" ]; then
        echo "[lock] FATAL: $in_path not found" >&2
        exit 1
    fi

    echo "[lock] Regenerating $out_path"
    echo "[lock]   from $in_path"
    echo "[lock]   for python ${PYTHON_VERSION} on ${PYTHON_PLATFORM}"

    ( cd "$PI_SETUP_DIR/$rel_dir" && uv_cmd pip compile \
        --generate-hashes \
        --python-version="$PYTHON_VERSION" \
        --python-platform="$PYTHON_PLATFORM" \
        --output-file=requirements.txt \
        requirements.in )

    echo "[lock] $name lockfile regenerated. Diff with 'git diff $out_path'."
}

require_uv

case "$1" in
    pi-setup)
        run_lock "pi-setup (POS)" "."
        ;;
    recognizer)
        run_lock "recognizer" "recognizer"
        ;;
    all)
        run_lock "pi-setup (POS)" "."
        run_lock "recognizer" "recognizer"
        ;;
    *)
        usage
        ;;
esac
