#!/bin/bash

# EC2 restore script
# cron job that runs every 5 minutes

set -e

# Configuration
S3_BUCKET="${S3_BUCKET_NAME}"
BACKUP_PREFIX="backups/"
RDS_HOST="${RDS_ENDPOINT}"
RDS_PORT="${RDS_PORT:-5432}"
RDS_DB="${RDS_DATABASE}"
RDS_USER="${RDS_USER}"
RDS_PASSWORD="${RDS_PASSWORD}"
TEMP_DIR="/tmp/db_restore"

# Logging
LOG_FILE="/var/log/db-restore.log"
exec 1>> "$LOG_FILE"
exec 2>&1

echo "[$(date)] Starting database restore check"

# Create temp directory
mkdir -p "$TEMP_DIR"

# Get latest backup from S3
LATEST_BACKUP=$(aws s3 ls "s3://${S3_BUCKET}/${BACKUP_PREFIX}" \
    --recursive \
    | sort \
    | tail -n 1 \
    | awk '{print $4}')

if [ -z "$LATEST_BACKUP" ]; then
    echo "[$(date)] No backups found in S3"
    exit 0
fi

echo "[$(date)] Latest backup: $LATEST_BACKUP"

# Check if this backup has already been restored
RESTORE_MARKER="${TEMP_DIR}/.last_restored"
if [ -f "$RESTORE_MARKER" ]; then
    LAST_RESTORED=$(cat "$RESTORE_MARKER")
    if [ "$LAST_RESTORED" = "$LATEST_BACKUP" ]; then
        echo "[$(date)] Backup already restored, skipping"
        exit 0
    fi
fi

# Download backup from S3
BACKUP_FILE="${TEMP_DIR}/$(basename $LATEST_BACKUP)"
echo "[$(date)] Downloading backup: $LATEST_BACKUP"

aws s3 cp "s3://${S3_BUCKET}/${LATEST_BACKUP}" "$BACKUP_FILE"

if [ ! -f "$BACKUP_FILE" ]; then
    echo "[$(date)] ERROR: Failed to download backup"
    exit 1
fi

echo "[$(date)] Backup downloaded: $(du -h $BACKUP_FILE | cut -f1)"

# Restore to RDS
echo "[$(date)] Restoring to RDS: $RDS_HOST"

export PGPASSWORD="$RDS_PASSWORD"

# Check RDS connectivity
if ! pg_isready -h "$RDS_HOST" -p "$RDS_PORT" -U "$RDS_USER" -d "$RDS_DB" -q; then
    echo "[$(date)] ERROR: Cannot connect to RDS"
    exit 1
fi

# Restore database
psql -h "$RDS_HOST" \
     -p "$RDS_PORT" \
     -U "$RDS_USER" \
     -d "$RDS_DB" \
     -f "$BACKUP_FILE"

if [ $? -eq 0 ]; then
    echo "[$(date)] Database restore completed successfully"
    echo "$LATEST_BACKUP" > "$RESTORE_MARKER"
    
    # Cleanup old backups (keep last 3)
    ls -t "${TEMP_DIR}"/*.sql 2>/dev/null | tail -n +4 | xargs -r rm
    
    # Report restore time
    BACKUP_TIME=$(basename "$LATEST_BACKUP" | grep -oP '\d{8}_\d{6}')
    CURRENT_TIME=$(date -u +%Y%m%d_%H%M%S)
    echo "[$(date)] Backup time: $BACKUP_TIME"
    echo "[$(date)] Restore time: $CURRENT_TIME"
else
    echo "[$(date)] ERROR: Database restore failed"
    exit 1
fi

unset PGPASSWORD

echo "[$(date)] Restore process completed"