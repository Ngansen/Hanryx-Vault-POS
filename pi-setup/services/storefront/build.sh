#!/usr/bin/env bash
# Pre-build: clone/update storefront source on the host, then Docker COPY uses it
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$SCRIPT_DIR/storefront-src"
REPO_URL="https://github.com/Ngansen/HanRyx-Vault.git"

GITHUB_TOKEN="${GITHUB_TOKEN:-}"
if [ -n "$GITHUB_TOKEN" ]; then
    REPO_URL="https://${GITHUB_TOKEN}@github.com/Ngansen/HanRyx-Vault.git"
fi

export GIT_SSL_NO_VERIFY=1

if [ -d "$SRC_DIR/.git" ]; then
    echo "[storefront] Updating existing source..."
    git -C "$SRC_DIR" pull --ff-only 2>/dev/null || {
        echo "[storefront] Pull failed, re-cloning..."
        rm -rf "$SRC_DIR"
        git clone --depth=1 "$REPO_URL" "$SRC_DIR"
    }
else
    echo "[storefront] Cloning storefront repo..."
    rm -rf "$SRC_DIR"
    git clone --depth=1 "$REPO_URL" "$SRC_DIR"
fi

echo "[storefront] Source ready at $SRC_DIR"
