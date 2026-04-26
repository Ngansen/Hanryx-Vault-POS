#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# resolve-alpine-apk-versions.sh — print pinned apk versions for the pokeapi
# image, resolved against the actual base image's apk index.
#
# Why this exists
# ───────────────
# `pi-setup/pokeapi/Dockerfile` pins its two extra apk packages with build
# args:
#
#     ARG ALPINE_GIT_VERSION=2.45.4-r0
#     ARG ALPINE_BASH_VERSION=5.2.37-r0
#
# Those versions must exist in the apk index of the pinned base image
# (`nginx:1.27.2-alpine@sha256:…`) — otherwise `apk add git=<ver>` fails the
# build with `unable to select packages`. When you bump either the base
# image digest or the apk pins for security updates, you have to look the
# right versions up by hand. This helper does it for you: it runs the
# pinned base image, asks its apk index for the currently-available
# versions of `git` and `bash`, and prints a build-arg snippet ready to
# paste into the Dockerfile.
#
# This is the Alpine analogue of `lock-python-deps.sh` — same idea (resolve
# the pin from the upstream index instead of guessing), same usage pattern
# (run it, commit the diff, rebuild).
#
# Usage
# ─────
#     # Default: read the FROM line from pi-setup/pokeapi/Dockerfile.
#     pi-setup/scripts/resolve-alpine-apk-versions.sh
#
#     # Or override with an explicit image reference:
#     pi-setup/scripts/resolve-alpine-apk-versions.sh nginx:1.27.2-alpine@sha256:74175cf3...
#
# Prerequisites
# ─────────────
# `docker` must be installed and able to pull the base image (the same
# requirement as `docker compose build`). This script is read-only — it
# does not modify the Dockerfile, it just prints the snippet to stdout so
# you can review the diff before committing.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PI_SETUP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DOCKERFILE="$PI_SETUP_DIR/pokeapi/Dockerfile"

usage() {
    echo "Usage: $0 [<image-ref>]" >&2
    echo "" >&2
    echo "  <image-ref>  Optional. An image ref like" >&2
    echo "               'nginx:1.27.2-alpine@sha256:<digest>'. Defaults to" >&2
    echo "               the FROM line in $DOCKERFILE." >&2
    exit 2
}

case "${1:-}" in
    -h|--help) usage ;;
esac

if ! command -v docker >/dev/null 2>&1; then
    echo "[resolve] FATAL: 'docker' not found in PATH. Install docker, then re-run." >&2
    exit 1
fi

IMAGE="${1:-}"
if [ -z "$IMAGE" ]; then
    if [ ! -f "$DOCKERFILE" ]; then
        echo "[resolve] FATAL: $DOCKERFILE not found, and no <image-ref> given." >&2
        exit 1
    fi
    # Match the first uncommented `FROM <image>` line. The Dockerfile pins
    # the base image as `name:tag@sha256:…` — keep the whole reference so
    # we resolve apk versions against the exact digest, not a floating tag.
    IMAGE="$(awk '
        /^[[:space:]]*#/ { next }
        /^[[:space:]]*FROM[[:space:]]+/ { print $2; exit }
    ' "$DOCKERFILE")"
    if [ -z "$IMAGE" ]; then
        echo "[resolve] FATAL: could not find a FROM line in $DOCKERFILE" >&2
        exit 1
    fi
    echo "[resolve] Using base image from $DOCKERFILE:" >&2
    echo "[resolve]   $IMAGE" >&2
else
    echo "[resolve] Using base image from argv:" >&2
    echo "[resolve]   $IMAGE" >&2
fi

# Run `apk policy` once for both packages — one container start, one apk
# index refresh. `apk policy` prints, per package:
#
#     git policy:
#       2.45.4-r0:
#         lib/apk/db/installed
#         https://dl-cdn.alpinelinux.org/alpine/v3.20/main
#     bash policy:
#       5.2.37-r0:
#         https://dl-cdn.alpinelinux.org/alpine/v3.20/main
#
# Versions are listed highest-first, so the first indented version line
# under each `<pkg> policy:` header is the one apk would install for an
# unpinned `apk add <pkg>` against this base image. That's exactly the
# version we want to pin.
echo "[resolve] Querying apk index inside the image (this pulls if needed)..." >&2
POLICY_OUT="$(docker run --rm --entrypoint sh "$IMAGE" -c \
    'apk update -q >/dev/null && apk policy git bash')" || {
    echo "[resolve] FATAL: 'docker run ... apk policy' failed for $IMAGE" >&2
    exit 1
}

extract_version() {
    # $1: package name (must match the `<pkg> policy:` header)
    # Reads $POLICY_OUT and prints the first version line under that header.
    local pkg="$1"
    local ver
    ver="$(printf '%s\n' "$POLICY_OUT" | awk -v pkg="$pkg" '
        $0 == pkg " policy:" { in_block = 1; next }
        in_block && /^[a-zA-Z]/ { in_block = 0 }
        in_block && /^[[:space:]]+[0-9]/ {
            # Strip leading whitespace and trailing colon, e.g.
            # "  2.45.4-r0:" → "2.45.4-r0".
            sub(/^[[:space:]]+/, "", $0)
            sub(/:.*$/, "", $0)
            print
            exit
        }
    ')"
    if [ -z "$ver" ]; then
        echo "[resolve] FATAL: could not parse a version for '$pkg' from apk policy output:" >&2
        echo "----" >&2
        printf '%s\n' "$POLICY_OUT" >&2
        echo "----" >&2
        exit 1
    fi
    printf '%s' "$ver"
}

GIT_VER="$(extract_version git)"
BASH_VER="$(extract_version bash)"

echo "[resolve] git  → $GIT_VER" >&2
echo "[resolve] bash → $BASH_VER" >&2
echo "" >&2
echo "[resolve] Paste the snippet below into $DOCKERFILE" >&2
echo "[resolve] (replace the existing ARG ALPINE_*_VERSION lines):" >&2
echo "" >&2

# stdout-only — easy to redirect into a file or diff-check from CI.
cat <<EOF
ARG ALPINE_GIT_VERSION=${GIT_VER}
ARG ALPINE_BASH_VERSION=${BASH_VER}
EOF
