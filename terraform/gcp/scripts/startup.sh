#!/bin/bash
set -e

exec > >(tee -a /var/log/startup-script.log)
exec 2>&1

echo "Starting DR app deployment!"

# Update system
sudo apt-get update
sudo apt-get install -y python3-pip nginx postgresql-client

# Create app directory
mkdir -p /opt/dr-app
cd /opt/dr-app

# =================================
#   FETCH DB PASSWORD AT RUNTIME
# =================================

# Create protected directory — only root can read the env file
mkdir -p /etc/dr-app
chmod 700 /etc/dr-app

echo "Fetching DB credentials from Secret Manager..."
set +x
DB_PASSWORD=$(gcloud secrets versions access latest \
  --secret=db-password \
  --project="${project_id}" 2>/dev/null)

if [ -z "$DB_PASSWORD" ]; then
  echo "ERROR: Failed to fetch DB password from Secret Manager" >&2
  exit 1
fi

printf 'DB_PASSWORD=%s\n' "$DB_PASSWORD" > /etc/dr-app/env
set -x

chmod 600 /etc/dr-app/env
chown root:root /etc/dr-app/env
echo "DB credentials written to protected env file"

# Create Flask application — reads DB password from environment only
cat > app.py << 'EOF'
from flask import Flask, jsonify
import psycopg2
from psycopg2 import pool
import socket
import os
from datetime import datetime

app = Flask(__name__)

db_pool = None

def init_db_pool():
    global db_pool
    try:
        db_pool = psycopg2.pool.SimpleConnectionPool(
            1, 10,
            host="/cloudsql/${db_connection_name}",
            database="application",
            user="appuser",
            password=os.environ.get('DB_PASSWORD', '')
        )
        print("Database pool created successfully")
    except Exception as e:
        print(f"Failed to create database pool: {e}")

init_db_pool()

@app.route('/')
def home():
    return jsonify({
        'status': 'success',
        'message': 'DR System is running',
        'provider': '${provider_name}',
        'hostname': socket.gethostname(),
        'timestamp': datetime.utcnow().isoformat()
    })

@app.route('/health')
def health():
    force_unhealthy = os.environ.get('FORCE_UNHEALTHY', 'false')
    if force_unhealthy.lower() == 'true':
        return jsonify({
            'status': 'unhealthy',
            'reason': 'manual_test',
            'provider': '${provider_name}'
        }), 503

    try:
        if db_pool:
            conn = db_pool.getconn()
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
            db_pool.putconn(conn)

        return jsonify({
            'status': 'healthy',
            'provider': '${provider_name}',
            'hostname': socket.gethostname(),
            'timestamp': datetime.utcnow().isoformat()
        }), 200
    except Exception as e:
        return jsonify({
            'status': 'unhealthy',
            'error': str(e),
            'provider': '${provider_name}'
        }), 503

@app.route('/api/data')
def get_data():
    try:
        conn = db_pool.getconn()
        cursor = conn.cursor()
        cursor.execute("SELECT NOW(), version()")
        result = cursor.fetchone()
        cursor.close()
        db_pool.putconn(conn)

        return jsonify({
            'timestamp': str(result[0]),
            'database_version': result[1],
            'provider': '${provider_name}',
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
EOF

# Install dependencies
pip3 install flask psycopg2-binary gunicorn

# Create systemd service — loads DB_PASSWORD from the protected env file
cat > /etc/systemd/system/dr-app.service << 'EOF'
[Unit]
Description=DR Application
After=network.target

[Service]
Type=simple
User=humble
WorkingDirectory=/opt/dr-app
EnvironmentFile=/etc/dr-app/env
ExecStart=/usr/local/bin/gunicorn --bind 0.0.0.0:5000 --workers 2 app:app
Restart=always

[Install]
WantedBy=multi-user.target
EOF

# Configure Nginx
cat > /etc/nginx/conf.d/app.conf << 'EOF'
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /health {
        proxy_pass http://127.0.0.1:5000/health;
        proxy_connect_timeout 2s;
        proxy_send_timeout 3s;
        proxy_read_timeout 3s;
        access_log off;
    }
}
EOF

# Start services
systemctl daemon-reload
systemctl enable dr-app
systemctl start dr-app
systemctl restart nginx

echo "Deployment complete!"
