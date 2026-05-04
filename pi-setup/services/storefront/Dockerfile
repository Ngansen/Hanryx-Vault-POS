# ── Stage 1: build ─────────────────────────────────────────────────────────────
# Pinned point release so `docker compose build` on the Pi is reproducible.
# Keep in lock-step with the runtime stage's FROM below so `npm ci --omit=dev`
# runs against the same Node ABI the build used.
#
# The trailing @sha256:… is the content-addressable digest of the multi-arch
# manifest list. Tags on Docker Hub are mutable in principle (a compromised
# upstream could re-push the same tag with different bits); pinning by digest
# makes the pull tamper-evident — substituted bits change the digest and
# `docker pull` refuses them. The tag is kept for human readability only.
# See `../../docs/REPRODUCIBILITY.md` for the bump procedure.
FROM node:20.18.0-bookworm-slim@sha256:28fbbb764069c698ead61d6a739a7615e8f0e07a4b8fe1473ceca70c1c3d6aaa AS builder

# ── apt snapshot pin (DISABLED) ──────────────────────────────────────────────
# snapshot.debian.org is unreachable from Docker BuildKit on the Pi after the
# data-root migration. Using default Debian repos from the base image instead.
# Base image is pinned by @sha256 digest so the Debian release is still locked.
# Re-enable when Docker networking to snapshot.debian.org is fixed — see
# `../../docs/REPRODUCIBILITY.md` for the procedure.
# ARG APT_SNAPSHOT_DATE=20260415T022652Z

RUN apt-get update && apt-get install -y --no-install-recommends \
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
# release AND the same @sha256 digest as the builder above. Native addons in
# node_modules link against that exact Node ABI; a mismatch would silently
# break modules at boot. Bumping one without the other is a bug.
FROM node:20.18.0-bookworm-slim@sha256:28fbbb764069c698ead61d6a739a7615e8f0e07a4b8fe1473ceca70c1c3d6aaa

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
