FROM python:3.11-slim-bookworm AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    git \
    libpq-dev \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-kor \
    tesseract-ocr-jpn \
    tesseract-ocr-chi-sim \
    libtesseract-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir -r requirements.txt

FROM python:3.11-slim-bookworm

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    postgresql-client \
    git \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-kor \
    tesseract-ocr-jpn \
    tesseract-ocr-chi-sim \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -r hanryx && useradd -r -g hanryx -d /app -s /sbin/nologin hanryx

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY . .

RUN mkdir -p /app/card-images /app/data /data && \
    chown -R hanryx:hanryx /app /data

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
