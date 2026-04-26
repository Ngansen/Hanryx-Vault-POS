#!/usr/bin/env bash
# Pre-build: clone/update storefront source on the host, then Docker COPY uses it
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$SCRIPT_DIR/storefront-src"
REPO_URL="https://github.com/Ngansen/HanRyx-Vault.git"

# ── Storefront source pin ────────────────────────────────────────────────────
# Pin the clone to a specific upstream commit so two `docker compose build`
# runs from the same git SHA produce byte-identical storefront bits.
# A commit SHA IS a content hash (same rationale as the git+ pin in
# `requirements-vcs.txt`), so this gives us the same content-addressed
# guarantee that `@sha256:…` gives the base images.
#
# Bump deliberately — see `pi-setup/docs/REPRODUCIBILITY.md` §4 for the
# procedure. Override at build time with STOREFRONT_GIT_REF=<sha> ./build.sh.
STOREFRONT_GIT_REF="${STOREFRONT_GIT_REF:-7b88ca2150350fba72661064bf9a2d32ac8611d7}"

# Reproducibility guard — the pin MUST be a full 40-char lowercase hex SHA.
# A branch name (`main`), a tag (`v1.0`), or a short SHA all silently drift
# when upstream moves and break the byte-for-byte rebuild guarantee. Same
# invariant the CI script `check-vcs-pins-are-full-shas.py` enforces for
# the Python git+ pins in `requirements-vcs.txt`.
if ! [[ "$STOREFRONT_GIT_REF" =~ ^[0-9a-f]{40}$ ]]; then
    echo "[storefront] FATAL: STOREFRONT_GIT_REF='$STOREFRONT_GIT_REF' is not a full 40-char lowercase hex commit SHA." >&2
    echo "[storefront] A branch name, tag, or short SHA cannot give byte-for-byte reproducibility on the Pi." >&2
    exit 1
fi

GITHUB_TOKEN="${GITHUB_TOKEN:-}"
if [ -n "$GITHUB_TOKEN" ]; then
    REPO_URL="https://${GITHUB_TOKEN}@github.com/Ngansen/HanRyx-Vault.git"
fi

if [ "${HANRYX_DEBUG_INSECURE_GIT:-0}" = "1" ]; then
    echo "[storefront] WARNING: HANRYX_DEBUG_INSECURE_GIT=1 set, disabling SSL verification for git. Do NOT use in production."
    # hanryx-allow-insecure: gated by HANRYX_DEBUG_INSECURE_GIT, echoes a warning. See replit.md "Security Policy — TLS verification".
    export GIT_SSL_NO_VERIFY=1
fi

# We use --filter=blob:none (instead of --depth=1) because --depth=1 only
# fetches HEAD of the default branch, so `git checkout <historical-sha>`
# would fail. Blob-filter gives the same bandwidth savings while keeping
# every commit reachable.
need_clone=1
if [ -d "$SRC_DIR/.git" ]; then
    current_sha="$(git -C "$SRC_DIR" rev-parse HEAD 2>/dev/null || echo "")"
    if [ "$current_sha" = "$STOREFRONT_GIT_REF" ]; then
        echo "[storefront] Source already at pinned commit $STOREFRONT_GIT_REF"
        need_clone=0
    else
        echo "[storefront] Source at ${current_sha:-<unknown>}, want $STOREFRONT_GIT_REF — fetching pinned commit..."
        if git -C "$SRC_DIR" fetch --filter=blob:none origin "$STOREFRONT_GIT_REF" \
            && git -C "$SRC_DIR" checkout --quiet --detach "$STOREFRONT_GIT_REF"; then
            need_clone=0
        else
            echo "[storefront] Fetch/checkout failed, re-cloning..."
            rm -rf "$SRC_DIR"
        fi
    fi
fi

if [ "$need_clone" = "1" ]; then
    echo "[storefront] Cloning storefront repo @ $STOREFRONT_GIT_REF..."
    rm -rf "$SRC_DIR"
    git clone --filter=blob:none "$REPO_URL" "$SRC_DIR"
    git -C "$SRC_DIR" checkout --quiet --detach "$STOREFRONT_GIT_REF"
fi

# Belt-and-braces — confirm we actually landed on the pinned commit before
# the much-slower docker build kicks off. If git silently checked out
# something else (shouldn't happen, but a corrupted local repo could do
# anything), surface it here, not after `npm ci` has run for ten minutes.
checked_out_sha="$(git -C "$SRC_DIR" rev-parse HEAD)"
if [ "$checked_out_sha" != "$STOREFRONT_GIT_REF" ]; then
    echo "[storefront] FATAL: HEAD is $checked_out_sha but STOREFRONT_GIT_REF=$STOREFRONT_GIT_REF" >&2
    exit 1
fi

# Reproducibility guard — npm ci needs a committed lockfile, otherwise it
# falls back to a non-deterministic `npm install`-style resolve and the
# build silently drifts between rebuilds. Fail HERE (host side) so the
# error surfaces before the much-slower docker build kicks off.
# See `pi-setup/docs/REPRODUCIBILITY.md` for context.
if [ ! -f "$SRC_DIR/package-lock.json" ]; then
    echo "[storefront] FATAL: $SRC_DIR/package-lock.json is missing." >&2
    echo "[storefront] npm ci needs a committed lockfile to install the same bits twice." >&2
    echo "[storefront] Restore it in the upstream HanRyx-Vault repo and re-run build.sh." >&2
    exit 1
fi

echo "[storefront] Source ready at $SRC_DIR @ $STOREFRONT_GIT_REF (lockfile present)"
