#!/bin/bash
#
# Restore the most recent Cloud SQL dump from S3 into the standby RDS instance.
#
# Runs from cron on the secondary every 5 minutes, and is safe to run by hand
# during a DR event. Configuration comes from the environment; the instance
# writes /opt/db-restore/env at build time and the cron wrapper sources it.
#
# This script is the single source of truth for restore logic. It is embedded
# into the instance by user-data.sh at deploy time, so edits here are what
# actually run in production.

set -euo pipefail

S3_BUCKET_NAME="${S3_BUCKET_NAME:?S3_BUCKET_NAME is required}"
BACKUP_PREFIX="${BACKUP_PREFIX:-backups/}"
RDS_HOST="${RDS_HOST:?RDS_HOST is required}"
RDS_PORT="${RDS_PORT:-5432}"
RDS_DB="${RDS_DB:?RDS_DB is required}"
RDS_USER="${RDS_USER:?RDS_USER is required}"
RDS_PASSWORD="${RDS_PASSWORD:?RDS_PASSWORD is required}"
TEMP_DIR="${TEMP_DIR:-/tmp/db-restore}"
LOG_FILE="${LOG_FILE:-/var/log/db-restore.log}"
LOCK_FILE="${LOCK_FILE:-/var/lock/db-restore.lock}"

mkdir -p "$TEMP_DIR"

# Keep output on stdout for interactive runs while still appending to the log.
exec > >(tee -a "$LOG_FILE") 2>&1

log() {
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*"
}

log "========================================"
log "Starting database restore check"

# Serialise runs. Cron fires every 5 minutes and a large restore can take
# longer than that; two concurrent restores against the same database race.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "Another restore is already in progress; skipping this run"
    exit 0
fi

export PGPASSWORD="$RDS_PASSWORD"

psql_do() {
    psql --host="$RDS_HOST" \
         --port="$RDS_PORT" \
         --username="$RDS_USER" \
         --dbname="$RDS_DB" \
         "$@"
}

# ---------------------------------------------------------------------------
# Find the newest backup
# ---------------------------------------------------------------------------

log "Checking s3://$S3_BUCKET_NAME/$BACKUP_PREFIX for the latest backup"

# Keys are <db>_YYYYmmdd_HHMMSS.sql, so lexical order is chronological order.
LATEST_BACKUP=$(aws s3 ls "s3://$S3_BUCKET_NAME/$BACKUP_PREFIX" --recursive \
    | sort \
    | tail -n 1 \
    | awk '{print $4}')

if [ -z "$LATEST_BACKUP" ]; then
    log "No backups found; nothing to restore"
    exit 0
fi

log "Latest backup: $LATEST_BACKUP"

RESTORE_MARKER="$TEMP_DIR/.last_restored"
if [ -f "$RESTORE_MARKER" ] && [ "$(cat "$RESTORE_MARKER")" = "$LATEST_BACKUP" ]; then
    log "Backup already restored; skipping"
    exit 0
fi

# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

BACKUP_FILE="$TEMP_DIR/$(basename "$LATEST_BACKUP")"
log "Downloading to $BACKUP_FILE"

if ! aws s3 cp "s3://$S3_BUCKET_NAME/$LATEST_BACKUP" "$BACKUP_FILE"; then
    log "ERROR: failed to download backup from S3"
    exit 1
fi

if [ ! -s "$BACKUP_FILE" ]; then
    log "ERROR: downloaded backup is missing or empty"
    exit 1
fi

log "Downloaded $(du -h "$BACKUP_FILE" | cut -f1)"

# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

# Fail fast rather than attempting a restore against a database we cannot
# reach: a half-applied dump is worse than no attempt.
if ! pg_isready --host="$RDS_HOST" --port="$RDS_PORT" --username="$RDS_USER" --dbname="$RDS_DB" --quiet; then
    log "ERROR: database is not accepting connections at $RDS_HOST:$RDS_PORT"
    exit 1
fi

log "Restoring into $RDS_HOST:$RDS_PORT/$RDS_DB"

# ON_ERROR_STOP is essential: without it psql reports success after every
# statement has failed. --single-transaction makes the restore atomic, which
# matters because the dump drops existing objects before recreating them - a
# failure partway through would otherwise leave the standby with a destroyed
# schema and no data.
#
# The exit status is captured directly rather than through a pipe, because a
# pipeline reports the status of its last command and would mask psql entirely.
restore_status=0
psql_do --file="$BACKUP_FILE" --single-transaction --set ON_ERROR_STOP=1 --quiet || restore_status=$?

if [ "$restore_status" -ne 0 ]; then
    log "ERROR: restore failed (psql exit $restore_status)"
    log "Marker left untouched so the next run retries this backup"
    logger -t db-restore -p user.err "Failed to restore backup: $LATEST_BACKUP" || true
    exit 1
fi

# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

# A clean exit only means the statements ran. Confirm the database actually
# holds user tables afterwards, so an empty or truncated dump cannot be
# recorded as a successful recovery.
table_count=$(psql_do --tuples-only --no-align --command \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog', 'information_schema');" \
    | tr -d '[:space:]') || table_count=""

if ! [[ "$table_count" =~ ^[0-9]+$ ]]; then
    log "ERROR: could not verify restore; table count query returned '$table_count'"
    exit 1
fi

if [ "$table_count" -eq 0 ]; then
    log "ERROR: restore reported success but the database contains no user tables"
    exit 1
fi

log "Verified: $table_count user table(s) present"

# ---------------------------------------------------------------------------
# Record success
# ---------------------------------------------------------------------------

echo "$LATEST_BACKUP" > "$RESTORE_MARKER"

BACKUP_TIME=$(basename "$LATEST_BACKUP" | grep -oE '[0-9]{8}_[0-9]{6}' || echo "unknown")
log "Restore complete. Backup timestamp: $BACKUP_TIME"
logger -t db-restore "Successfully restored backup: $LATEST_BACKUP" || true

# Keep the three most recent downloads for troubleshooting. Avoids GNU-only
# find -printf and xargs -r so this behaves the same wherever it is run.
(
    cd "$TEMP_DIR" || exit 0
    ls -t ./*.sql 2>/dev/null | tail -n +4 | while read -r stale; do
        rm -f "$stale"
    done
) || true

log "Restore process completed"
log "========================================"
