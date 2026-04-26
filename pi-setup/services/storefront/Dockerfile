# ── Stage 1: build ─────────────────────────────────────────────────────────────
# Pinned point release so `docker compose build` on the Pi is reproducible.
# Keep in lock-step with the runtime stage's FROM below so `npm ci --omit=dev`
# runs against the same Node ABI the build used.
FROM node:20.18.0-bookworm-slim AS builder

# ── apt snapshot pin ─────────────────────────────────────────────────────────
# Lock apt to a specific point in time on snapshot.debian.org so two
# `docker compose build` runs install byte-identical .deb files. Without this,
# Debian security updates between rebuilds silently change the bits in the
# image. Bump APT_SNAPSHOT_DATE deliberately — see
# `../../docs/REPRODUCIBILITY.md` for the full bump procedure.
ARG APT_SNAPSHOT_DATE=20260415T000000Z

RUN set -eux; \
    rm -f /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; \
    { \
        echo "Types: deb"; \
        echo "URIs: https://snapshot.debian.org/archive/debian/${APT_SNAPSHOT_DATE}/"; \
        echo "Suites: bookworm bookworm-updates"; \
        echo "Components: main"; \
        echo "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg"; \
        echo "Check-Valid-Until: no"; \
        echo ""; \
        echo "Types: deb"; \
        echo "URIs: https://snapshot.debian.org/archive/debian-security/${APT_SNAPSHOT_DATE}/"; \
        echo "Suites: bookworm-security"; \
        echo "Components: main"; \
        echo "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg"; \
        echo "Check-Valid-Until: no"; \
    } > /etc/apt/sources.list.d/snapshot.sources

RUN apt-get -o Acquire::Check-Valid-Until=false update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY storefront-src/ .

# `npm ci` requires a committed `package-lock.json` — without one it falls
# back to a non-reproducible `npm install`-style resolve, which silently
# drifts between rebuilds. Fail loudly here so the storefront repo never
# silently regresses on this guarantee. The lockfile lives in the upstream
# `Ngansen/HanRyx-Vault` repo (cloned by `build.sh`); if it's missing,
# someone deleted it there and needs to put it back before we can build.
RUN test -f /app/package-lock.json \
    || (echo "[storefront] FATAL: storefront-src/package-lock.json is missing." \
            "  npm ci needs a committed lockfile to install reproducibly." \
            "  Restore it in the upstream HanRyx-Vault repo and re-run build.sh." >&2; \
        exit 1)

RUN npm ci

RUN npm run build

# ── Stage 2: runtime ───────────────────────────────────────────────────────────
# Runtime stage — MUST stay pinned to the same Node + bookworm-slim point
# release as the builder above. Native addons in node_modules link against
# that exact Node ABI; a mismatch would silently break modules at boot.
FROM node:20.18.0-bookworm-slim

WORKDIR /app

COPY --from=builder /app/dist ./dist
COPY --from=builder /app/package*.json ./

# Same lockfile guarantee for the runtime install — `npm ci --omit=dev`
# also needs `package-lock.json`.
RUN test -f /app/package-lock.json \
    || (echo "[storefront] FATAL: package-lock.json missing in runtime stage." >&2; exit 1)

RUN npm ci --omit=dev --ignore-scripts

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV NODE_ENV=production
ENV PORT=3000

EXPOSE 3000

ENTRYPOINT ["docker-entrypoint.sh"]
