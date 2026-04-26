# Pinned point release so `docker compose build` on the Pi is reproducible.
# Bump deliberately — an Alpine roll can drop busybox applets the entrypoint
# scripts rely on.
FROM nginx:1.27.2-alpine

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
# nginx:1.27.2-alpine image).
ARG ALPINE_GIT_VERSION=2.45.4-r0
ARG ALPINE_BASH_VERSION=5.2.37-r0

RUN apk add --no-cache \
        git=${ALPINE_GIT_VERSION} \
        bash=${ALPINE_BASH_VERSION}

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY init.sh /docker-entrypoint.d/40-clone-pokeapi.sh
RUN chmod +x /docker-entrypoint.d/40-clone-pokeapi.sh

EXPOSE 80
