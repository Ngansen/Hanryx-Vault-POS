# ── Stage 1: build ─────────────────────────────────────────────────────────────
FROM node:20-slim AS builder

ARG GITHUB_TOKEN

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN GIT_SSL_NO_VERIFY=1 git clone --depth=1 https://${GITHUB_TOKEN}@github.com/Ngansen/HanRyx-Vault.git .

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
