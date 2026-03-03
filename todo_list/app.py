from flask import Flask, jsonify 
import os

app = Flask(__name__)

@app.route('/')
def hello():
    return 'Running Smoothly!'

@app.route('/health')
def health():
    force_unhealthy = os.environ.get('FORCE_UNHEALTHY', 'false')
    if force_unhealthy.lower() == 'true':
        return jsonify({'status': 'unhealthy'}), 503
    
    # Check DB connection
    return jsonify({'status': 'healthy', 'provider': 'GCP'}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)