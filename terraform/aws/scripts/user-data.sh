#!/bin/bash

set -e

exec > >(tee -a /var/log/user-data.log)
exec 2>&1

echo "Starting DR app deployment"

# Update system
sudo apt-get update 
sudo apt-get install -y python3-pip nginx postgresql-client

# Create app directory
mkdir -p /opt/dr-app
cd /opt/dr-app

# Creating test app
cat > app.py << 'EOF'
from flask import Flask, jsonify
import psycopg2
from psycopg2 import pool
import socket
import os
from datetime import datetime

app = Flask(__name__)

db_pool = psycopg2.pool.SimpleConnectionPool(
    1, 10,
    host="${db_host}",
    port="${db_port}",
    database="${db_name}",
    user="${db_user}",
    password="${db_password}"
)

@app.route('/')
def home():
    return jsonify({
        'status': 'success',
        'message': 'DR System is running',
        'provider': 'AWS',
        'hostname': socket.gethostname(),
        'timestamp': datetime.utcnow().isoformat()
    })

@app.route('/health')
def health():
    force_unhealthy = os.environ.get('FORCE_UNHEALTHY', 'false')
    if force_unhealthy.lower() == 'true':
        return jsonify({'status': 'unhealthy', 'provider': 'AWS'}), 503
    
    try:
        conn = db_pool.getconn()
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.close()
        db_pool.putconn(conn)
        
        return jsonify({
            'status': 'healthy',
            'provider': 'AWS',
            'hostname': socket.gethostname(),
            'timestamp': datetime.utcnow().isoformat()
        }), 200
    except Exception as e:
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 503

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
EOF

# Install dependencies
pip3 install flask psycopg2-binary gunicorn

# Create systemd service
cat > /etc/systemd/system/dr-app.service << 'EOF'
[Unit]
Description=DR Application
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/dr-app
ExecStart=/usr/local/bin/gunicorn --bind 0.0.0.0:5000 --workers 2 app:app
Restart=always

[Install]
WantedBy=multi-user.target
EOF

# Configure Nginx
cat > /etc/nginx/conf.d/app.conf << 'EOF'
server {
    listen 80;
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
    }
    location /health {
        proxy_pass http://127.0.0.1:5000/health;
        access_log off;
    }
}
EOF

# =================================
#        DATABASE RESTORE
# =================================

echo "Setting up database restore functionality"

# Restore directory
mkdir -p /opt/db-restore
cd /opt/db-restore

# Restore script
cat > /opt/db-restore/restore_db.sh << 'RESTORE_SCRIPT'
#!/bin/bash

set -e

# Configuration
S3_BUCKET_NAME="${s3_bucket_name}"
BACKUP_PREFIX="backups/"
RDS_HOST="${db_host}"
RDS_PORT="${db_port}"
RDS_DB="${db_name}"
RDS_USER="${db_user}"
RDS_PASSWORD="${db_password}"
TEMP_DIR="/tmp/db-restore"

# Logging
LOG_FILE="/var/log/db-restore.log"
exec 1>> "$LOG_FILE"
exec 2>&1

echo "[$(date)] ========================================"
echo "[$(date)] Starting database restore check"

# Create temp directory
mkdir -p "$TEMP_DIR"

# Get latest backup from S3
echo "[$(date)] Checking S3 for latest backup..."
LATEST_BACKUP=$(aws s3 ls "s3://$${S3_BUCKET_NAME}/$${BACKUP_PREFIX}" \
    --recursive \
    2>/dev/null \
    | sort \
    | tail -n 1 \
    | awk '{print $4}')

if [ -z "$LATEST_BACKUP" ]; then
    echo "[$(date)] No backups found in S3 bucket: s3://$${S3_BUCKET_NAME}/$${BACKUP_PREFIX}"
    exit 0
fi

echo "[$(date)] Latest backup found: $LATEST_BACKUP"

# Check if this backup has already been restored
RESTORE_MARKER="$${TEMP_DIR}/.last_restored"
if [ -f "$RESTORE_MARKER" ]; then
    LAST_RESTORED=$(cat "$RESTORE_MARKER")
    if [ "$LAST_RESTORED" = "$LATEST_BACKUP" ]; then
        echo "[$(date)] Backup already restored, skipping"
        exit 0
    fi
    echo "[$(date)] New backup detected. Previous: $LAST_RESTORED"
fi

# Download backup from S3
BACKUP_FILE="$${TEMP_DIR}/$(basename $LATEST_BACKUP)"
echo "[$(date)] Downloading backup to: $BACKUP_FILE"

if aws s3 cp "s3://$${S3_BUCKET_NAME}/$${LATEST_BACKUP}" "$BACKUP_FILE" 2>&1; then
    echo "[$(date)] Download successful"
else
    echo "[$(date)] ERROR: Failed to download backup from S3"
    exit 1
fi

if [ ! -f "$BACKUP_FILE" ]; then
    echo "[$(date)] ERROR: Backup file does not exist after download"
    exit 1
fi

BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "[$(date)] Backup downloaded: $BACKUP_SIZE"

# Restore to RDS
echo "[$(date)] Restoring to RDS: $RDS_HOST:$RDS_PORT/$RDS_DB"

export PGPASSWORD="$RDS_PASSWORD"

# Check RDS connectivity
echo "[$(date)] Testing database connection..."
if ! pg_isready -h "$RDS_HOST" -p "$RDS_PORT" -U "$RDS_USER" -d "$RDS_DB" -q 2>/dev/null; then
    echo "[$(date)] WARNING: Database not ready, but attempting restore anyway..."
fi

# Restore database
echo "[$(date)] Executing SQL restore..."
if psql -h "$RDS_HOST" \
     -p "$RDS_PORT" \
     -U "$RDS_USER" \
     -d "$RDS_DB" \
     -f "$BACKUP_FILE" 2>&1 | tee -a "$LOG_FILE"; then
    
    echo "[$(date)] ✓ Database restore completed successfully"
    echo "$LATEST_BACKUP" > "$RESTORE_MARKER"
    
    # Cleanup old backup files (keep last 3)
    echo "[$(date)] Cleaning up old backups..."
    ls -t "$${TEMP_DIR}"/*.sql 2>/dev/null | tail -n +4 | xargs -r rm
    
    # Calculate and report timing
    BACKUP_TIME=$(basename "$LATEST_BACKUP" | grep -oP '\d{8}_\d{6}' || echo "unknown")
    CURRENT_TIME=$(date -u +%Y%m%d_%H%M%S)
    echo "[$(date)] Backup timestamp: $BACKUP_TIME"
    echo "[$(date)] Restore timestamp: $CURRENT_TIME"
    
    # Report to application log
    logger -t db-restore "Successfully restored backup: $LATEST_BACKUP"
    
else
    echo "[$(date)] ✗ ERROR: Database restore failed"
    logger -t db-restore -p user.err "Failed to restore backup: $LATEST_BACKUP"
    exit 1
fi

unset PGPASSWORD

echo "[$(date)] Restore process completed"
echo "[$(date)] ========================================"
RESTORE_SCRIPT

# Make restore script executable
chmod +x /opt/db-restore/restore_db.sh

# Create wrapper for cron (handles environment)
cat > /opt/db-restore/run_restore.sh << 'WRAPPER'
#!/bin/bash
# Cron wrapper - ensures proper environment
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export AWS_DEFAULT_REGION=${aws_region}
/opt/db-restore/restore_db.sh
WRAPPER

chmod +x /opt/db-restore/run_restore.sh

# Add to root's crontab (every 5 minutes)
echo "[$(date)] Setting up cron job for database restore"
(sudo crontab -l 2>/dev/null | grep -v restore_db.sh; echo "*/5 * * * * /opt/db-restore/run_restore.sh") | sudo crontab -

# Verify cron job was added
echo "[$(date)] Cron jobs configured:"
sudo crontab -l | grep restore

# Create log rotation for restore logs
cat > /etc/logrotate.d/db-restore << 'LOGROTATE'
/var/log/db-restore.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    create 0644 root root
}
LOGROTATE

# Run initial restore (if backups exist)
echo "[$(date)] Running initial database restore attempt..."
/opt/db-restore/restore_db.sh || echo "[$(date)] Initial restore failed or no backups available yet"


# Start services
systemctl daemon-reload
systemctl enable dr-app
systemctl start dr-app
systemctl restart nginx

echo "Deployment completed"