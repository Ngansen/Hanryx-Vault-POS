# ── Stage 1: build ─────────────────────────────────────────────────────────────
# Pinned point release so `docker compose build` on the Pi is reproducible.
# Keep in lock-step with the runtime stage's FROM below so `npm ci --omit=dev`
# runs against the same Node ABI the build used.
FROM node:20.18.0-bookworm-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY storefront-src/ .

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

RUN npm ci --omit=dev --ignore-scripts

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV NODE_ENV=production
ENV PORT=3000

EXPOSE 3000

ENTRYPOINT ["docker-entrypoint.sh"]
