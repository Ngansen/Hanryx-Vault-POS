#!/usr/bin/env bash
# HanryxVault POS — Automated Database Backup
# Runs inside the Docker container or directly on the Pi.
# Uploads to S3 if AWS_* vars set, or saves locally otherwise.
#
# Usage:   bash backup.sh
# Cron:    0 3 * * * /home/pi/hanryx-vault-pos/pi-setup/scripts/backup.sh >> /var/log/hanryx-backup.log 2>&1

set -euo pipefail

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="${BACKUP_DIR:-/backups}"
DB_URL="${DATABASE_URL:-postgresql://vaultpos:vaultpos@postgres:5432/vaultpos}"
FILENAME="hanryxvault_${TIMESTAMP}.sql.gz"
FILEPATH="${BACKUP_DIR}/${FILENAME}"

# --------------------------------------------------------------------------
# 1. Dump and compress
# --------------------------------------------------------------------------
echo "[backup] $(date -Iseconds) Starting backup → ${FILENAME}"
mkdir -p "${BACKUP_DIR}"
pg_dump "${DB_URL}" | gzip > "${FILEPATH}"
SIZE=$(du -sh "${FILEPATH}" | cut -f1)
echo "[backup] Dump complete — ${SIZE}"

# --------------------------------------------------------------------------
# 2. Upload to S3 (optional)
# --------------------------------------------------------------------------
if [ -n "${AWS_BUCKET:-}" ] && [ -n "${AWS_ACCESS_KEY_ID:-}" ]; then
    echo "[backup] Uploading to s3://${AWS_BUCKET}/backups/${FILENAME}"
    aws s3 cp "${FILEPATH}" "s3://${AWS_BUCKET}/backups/${FILENAME}" \
        --storage-class STANDARD_IA \
        --region "${AWS_REGION:-us-east-1}"
    echo "[backup] S3 upload complete"
fi

# --------------------------------------------------------------------------
# 3. Upload via SFTP (optional — set SFTP_HOST / SFTP_USER / SFTP_PATH)
# --------------------------------------------------------------------------
if [ -n "${SFTP_HOST:-}" ] && [ -n "${SFTP_USER:-}" ]; then
    echo "[backup] Uploading via SFTP to ${SFTP_USER}@${SFTP_HOST}"
    sftp -o StrictHostKeyChecking=no -b - "${SFTP_USER}@${SFTP_HOST}" << SFTP
mkdir -p ${SFTP_PATH:-/backups}
put ${FILEPATH} ${SFTP_PATH:-/backups}/${FILENAME}
quit
SFTP
    echo "[backup] SFTP upload complete"
fi

# --------------------------------------------------------------------------
# 4. Prune old local backups (keep last 14)
# --------------------------------------------------------------------------
cd "${BACKUP_DIR}"
ls -t hanryxvault_*.sql.gz 2>/dev/null | tail -n +15 | xargs -r rm --
KEPT=$(ls hanryxvault_*.sql.gz 2>/dev/null | wc -l)
echo "[backup] Local retention: ${KEPT} backups kept"
echo "[backup] $(date -Iseconds) Done"
