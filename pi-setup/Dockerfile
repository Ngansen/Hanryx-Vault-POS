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

# Create hanryx user, AND ensure the lp group (gid 7, owner of /dev/usb/lp0
# on Raspberry Pi OS) exists inside the container with hanryx as a member.
# Baking this at build time means gunicorn (which runs as hanryx via
# entrypoint.sh `su`) can open the USB receipt printer with no runtime
# user-management commands required, regardless of whether the host's lp gid
# happens to be 7. The runtime safety net in entrypoint.sh covers exotic
# hosts where gid != 7.
RUN groupadd -r hanryx \
    && (getent group lp >/dev/null 2>&1 || groupadd -g 7 lp) \
    && useradd -r -g hanryx -G lp -d /app -s /sbin/nologin hanryx

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
