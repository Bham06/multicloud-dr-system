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

# Restore script. The canonical source lives at
# terraform/aws/scripts/restore-db.sh and is injected here at deploy time, so
# there is exactly one copy of the restore logic to keep correct.
cat > /opt/db-restore/restore_db.sh << 'RESTORE_SCRIPT'
${restore_script}
RESTORE_SCRIPT

# Configuration lives in an env file rather than being baked into the script,
# so the database password is not left in a world-readable file.
cat > /opt/db-restore/env << 'ENVFILE'
export S3_BUCKET_NAME="${s3_bucket_name}"
export RDS_HOST="${db_host}"
export RDS_PORT="${db_port}"
export RDS_DB="${db_name}"
export RDS_USER="${db_user}"
export RDS_PASSWORD="${db_password}"
export AWS_DEFAULT_REGION="${aws_region}"
ENVFILE

chown root:root /opt/db-restore/env
chmod 600 /opt/db-restore/env

# Make restore script executable
chmod +x /opt/db-restore/restore_db.sh

# Create wrapper for cron (handles environment)
cat > /opt/db-restore/run_restore.sh << 'WRAPPER'
#!/bin/bash
# Cron wrapper - ensures proper environment
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
set -a
# shellcheck disable=SC1091
. /opt/db-restore/env
set +a
exec /opt/db-restore/restore_db.sh
WRAPPER

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
# Via the wrapper, so it picks up the environment the same way cron does.
/opt/db-restore/run_restore.sh || echo "[$(date)] Initial restore failed or no backups available yet"


# Start services
systemctl daemon-reload
systemctl enable dr-app
systemctl start dr-app
systemctl restart nginx

echo "Deployment completed"