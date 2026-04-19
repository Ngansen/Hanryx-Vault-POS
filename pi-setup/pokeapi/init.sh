#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# Bootstrap script for the offline PokeAPI nginx service.
#
# Runs once on container start *before* nginx itself starts (it lives in the
# /docker-entrypoint.d/ directory recognised by the official nginx image).
#
# Behaviour:
#   * If /usr/share/nginx/html/data/api/v2 already exists, do nothing.
#   * Otherwise: shallow-clone PokeAPI/api-data into a temp dir and move the
#     `data/` subtree into the nginx web root.  ~240 MB transfer; takes a
#     couple of minutes on a Pi 5 + decent home connection.
# ──────────────────────────────────────────────────────────────────────────────
set -e

HTML_DIR=/usr/share/nginx/html
TARGET="$HTML_DIR/data/api/v2"
REPO_URL="${POKEAPI_DATA_REPO:-https://github.com/PokeAPI/api-data.git}"
TMP_DIR=/tmp/pokeapi-clone

if [ -d "$TARGET" ]; then
    n_endpoints=$(find "$TARGET" -mindepth 2 -maxdepth 2 -name 'index.json' 2>/dev/null | wc -l)
    echo "[pokeapi-init] Data already present ($n_endpoints endpoints) — skipping clone"
    exit 0
fi

echo "[pokeapi-init] Cloning $REPO_URL (~240 MB, this takes a few minutes)…"
rm -rf "$TMP_DIR"
git clone --depth 1 --filter=blob:none --no-checkout "$REPO_URL" "$TMP_DIR"
git -C "$TMP_DIR" sparse-checkout set data
git -C "$TMP_DIR" checkout

if [ ! -d "$TMP_DIR/data" ]; then
    echo "[pokeapi-init] ERROR: /data directory missing after sparse checkout"
    exit 1
fi

mkdir -p "$HTML_DIR"
mv "$TMP_DIR/data" "$HTML_DIR/data"
rm -rf "$TMP_DIR"

n_endpoints=$(find "$HTML_DIR/data/api/v2" -mindepth 2 -maxdepth 2 -name 'index.json' 2>/dev/null | wc -l)
echo "[pokeapi-init] Done — $n_endpoints endpoints loaded into $HTML_DIR/data/api/v2"
