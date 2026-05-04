# Pinned point release so `docker compose build` on the Pi is reproducible.
# Bump deliberately — an Alpine roll can drop busybox applets the entrypoint
# scripts rely on.
#
# The trailing @sha256:… is the content-addressable digest of the multi-arch
# manifest list. Tags on Docker Hub are mutable in principle (a compromised
# upstream could re-push the same tag with different bits); pinning by digest
# makes the pull tamper-evident — substituted bits change the digest and
# `docker pull` refuses them. The tag is kept for human readability only.
# See `../docs/REPRODUCIBILITY.md` for the bump procedure.
FROM nginx:1.27.2-alpine@sha256:74175cf34632e88c6cfe206897cbfe2d2fecf9bf033c40e7f9775a3689e8adc7

# ── apk version pin ──────────────────────────────────────────────────────────
# Alpine doesn't have an official time-based snapshot service like Debian, so
# we lock the two extra packages we install (git + bash) to specific versions.
# This makes two `docker compose build` runs install byte-identical .apk files.
# Without this, Alpine 3.20 maintenance updates silently change the bits in
# the image.
#
# Bump deliberately when picking up security updates — see
# `../docs/REPRODUCIBILITY.md` for the bump procedure (it includes the
# `apk policy <pkg>` command for finding the current version inside the
# nginx:1.27.2-alpine@sha256:… base image).
ARG ALPINE_GIT_VERSION=2.45.4-r0
ARG ALPINE_BASH_VERSION=5.2.26-r0

RUN apk add --no-cache \
        git=${ALPINE_GIT_VERSION} \
        bash=${ALPINE_BASH_VERSION}

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY init.sh /docker-entrypoint.d/40-clone-pokeapi.sh
RUN chmod +x /docker-entrypoint.d/40-clone-pokeapi.sh

EXPOSE 80
