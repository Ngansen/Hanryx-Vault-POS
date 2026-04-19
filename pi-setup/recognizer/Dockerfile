# ──────────────────────────────────────────────────────────────────────────────
# pokemon-card-recognizer service
#
# Wraps prateekt/pokemon-card-recognizer (PyPI: pokemon-card-recognizer) in a
# small Flask service.  Exposes:
#   POST /recognize/image   — multipart `image` field   → list of card guesses
#   POST /recognize/video   — multipart `video` field   → list of frame guesses
#   GET  /healthz           — liveness probe
#
# This image is intentionally a separate service from the POS so that the
# recognizer's heavy dependencies (torch, easyocr, opencv, ffmpeg) don't
# bloat the POS container.  On a Pi 5 (CPU) recognition is slow but works
# for one-off binder scans / booster opening videos.
# ──────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
        tesseract-ocr \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt

# Install CPU-only torch first (much smaller than the default CUDA build),
# then the recogniser package itself.
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu \
        torch==2.2.2 torchvision==0.17.2 \
    && pip install -r /app/requirements.txt

COPY server.py /app/server.py

EXPOSE 8081
CMD ["python", "-u", "server.py"]
