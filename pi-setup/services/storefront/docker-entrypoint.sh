#!/bin/sh
set -e

echo "[storefront] Running database migrations..."
# Drizzle push — idempotent; safe to run on every start
npx drizzle-kit push 2>&1 || echo "[storefront] Migration warning (non-fatal, tables may already exist)"

echo "[storefront] Starting server on port ${PORT:-3000}..."
exec node dist/index.cjs
