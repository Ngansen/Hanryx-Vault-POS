# ── Stage 1: build ─────────────────────────────────────────────────────────────
FROM node:20-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY storefront-src/ .

RUN npm ci

RUN npm run build

# ── Stage 2: runtime ───────────────────────────────────────────────────────────
FROM node:20-slim

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
