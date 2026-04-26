#!/usr/bin/env bash
# setup-ollama.sh — One-time Ollama model pull for the AI cashier assistant.
#
# Run AFTER `docker compose up -d assistant` has started the assistant
# container. This is a separate manual step (not an entrypoint hook)
# because:
#
#   1. Pulling the model is a 2 GB download — wrapping it in the container
#      entrypoint would silently turn `docker compose up` into a 20-minute
#      operation on a slow link, with no progress feedback to the operator.
#   2. Once pulled, the model is cached in the `ollama-data` Docker volume
#      and persists across container rebuilds — re-running this script is
#      a no-op (Ollama checks the local model cache before pulling).
#
# Usage:
#   ./pi-setup/scripts/setup-ollama.sh           # pulls the default qwen2.5:3b
#   OLLAMA_MODEL=qwen2.5:1.5b ./setup-ollama.sh  # smaller, faster on Pi 5 8GB
#
# Why Qwen 2.5 (default):
#   - Apache 2.0 licence (commercial-use safe in a card shop)
#   - Strong Korean / Japanese / Chinese performance (Alibaba is a Chinese
#     lab and Qwen's Asian-language tokeniser + training data is best-in-class
#     for our four-language requirement)
#   - 3B variant runs at 5-15 tok/s on Pi 5 CPU (workable for 1-2 sentence
#     replies). 1.5B is 2x faster but noticeably dumber.

set -euo pipefail

OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:3b}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"

# Quick liveness check — refuse to proceed if the assistant container isn't up.
if ! curl -sf --max-time 5 "$OLLAMA_URL/api/tags" >/dev/null; then
    echo "[setup-ollama] ERROR: $OLLAMA_URL is not responding."
    echo "               Start the container first: docker compose up -d assistant"
    exit 1
fi

echo "[setup-ollama] Pulling model: $OLLAMA_MODEL"
echo "[setup-ollama] First run downloads ~2 GB. Subsequent runs are cached."
echo

# Pull via the assistant container so the model lands in the ollama-data
# Docker volume (not on the host), keeping the host clean.
docker exec -it pi-setup-assistant-1 ollama pull "$OLLAMA_MODEL"

# Quick warm-up so the first `/ai/chat` call doesn't take 30 seconds.
echo
echo "[setup-ollama] Warming up the model with a one-token query…"
curl -sf "$OLLAMA_URL/api/chat" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$OLLAMA_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"ok\"}],\"stream\":false,\"options\":{\"num_predict\":1}}" \
    | head -c 200
echo
echo
echo "[setup-ollama] Done. Verify the assistant blueprint can reach Ollama:"
echo "               curl http://localhost:8080/ai/health"
