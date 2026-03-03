import os
import json
import requests
from datetime import datetime
from google.cloud import compute_v1
from google.cloud import logging as cloud_logging

# Environment variables
PROJECT_ID = os.environ.get('PROJECT_ID')
GCP_BACKEND_SERVICE = os.environ.get('GCP_BACKEND_SERVICE')
AWS_BACKEND_SERVICE = os.environ.get('AWS_BACKEND_SERVICE')
URL_MAP_NAME = os.environ.get('URL_MAP_NAME')
GCP_HEALTH_CHECK_URL = os.environ.get('GCP_HEALTH_CHECK_URL')
AWS_HEALTH_CHECK_URL = os.environ.get('AWS_HEALTH_CHECK_URL')

# Initialize clients
url_map_client = compute_v1.UrlMapsClient()
logging_client = cloud_logging.Client()
logger = logging_client.logger('auto-failover')

# State file
STATE_FILE = '/tmp/failover_state.json'

def get_current_state():
    """Get current failover state from memory"""
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        return {'active_backend': 'gcp', 'last_change': None}
    except Exception as e:
        logger.log_text(f"Error reading state: {str(e)}", severity='ERROR')
        return {'active_backend': 'gcp', 'last_change': None}

def save_state(state):
    """Save current failover state to memory"""
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        logger.log_text(f"Error saving state: {str(e)}", severity='ERROR')

def check_backend_health(url, backend_name):
    """Check health of a backend by calling its health endpoint"""
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            is_healthy = data.get('status') == 'healthy'
            logger.log_text(
                f"{backend_name} health check: {'HEALTHY' if is_healthy else 'UNHEALTHY'}",
                severity='INFO'
            )
            return is_healthy
        else:
            logger.log_text(
                f"{backend_name} health check failed: HTTP {response.status_code}",
                severity='WARNING'
            )
            return False
    except Exception as e:
        logger.log_text(
            f"{backend_name} health check error: {str(e)}",
            severity='ERROR'
        )
        return False

def get_current_backend():
    """Get current backend from URL map"""
    try:
        url_map = url_map_client.get(
            project=PROJECT_ID,
            url_map=URL_MAP_NAME
        )
        
        default_service = url_map.default_service
        
        if GCP_BACKEND_SERVICE in default_service:
            return 'gcp'
        elif AWS_BACKEND_SERVICE in default_service:
            return 'aws'
        else:
            logger.log_text(
                f"Unknown backend service in URL map: {default_service}",
                severity='WARNING'
            )
            return 'unknown'
    except Exception as e:
        logger.log_text(f"Error getting current backend: {str(e)}", severity='ERROR')
        return 'unknown'

def update_url_map_backend(backend_service_name):
    """Update URL map to point to specified backend service"""
    try:
        # Get current URL map
        url_map = url_map_client.get(
            project=PROJECT_ID,
            url_map=URL_MAP_NAME
        )
        
        # Update default service
        backend_url = f"https://www.googleapis.com/compute/v1/projects/{PROJECT_ID}/global/backendServices/{backend_service_name}"
        url_map.default_service = backend_url
        
        # Apply update
        operation = url_map_client.update(
            project=PROJECT_ID,
            url_map=URL_MAP_NAME,
            url_map_resource=url_map
        )
        
        # Wait for operation to complete
        operation.result(timeout=60)
        
        logger.log_text(
            f"Updated URL map to point to {backend_service_name}",
            severity='INFO'
        )
        return True
        
    except Exception as e:
        logger.log_text(
            f"Error updating URL map: {str(e)}",
            severity='ERROR'
        )
        return False

def failover_to_aws():
    """Switch URL map to AWS backend"""
    logger.log_text("=== INITIATING FAILOVER TO AWS ===", severity='WARNING')
    
    start_time = datetime.utcnow()
    
    if update_url_map_backend(AWS_BACKEND_SERVICE):
        end_time = datetime.utcnow()
        rto = (end_time - start_time).total_seconds()
        
        logger.log_text(
            f"✓ FAILOVER TO AWS COMPLETED - RTO: {rto:.2f} seconds",
            severity='WARNING'
        )
        
        logger.log_struct({
            'event': 'failover',
            'target': 'aws',
            'rto_seconds': rto,
            'timestamp': datetime.utcnow().isoformat()
        }, severity='WARNING')
        
        return True
    else:
        logger.log_text("✗ FAILOVER TO AWS FAILED", severity='ERROR')
        return False

def failback_to_gcp():
    """Switch URL map back to GCP backend"""
    logger.log_text("=== INITIATING FAILBACK TO GCP ===", severity='WARNING')
    
    start_time = datetime.utcnow()
    
    if update_url_map_backend(GCP_BACKEND_SERVICE):
        end_time = datetime.utcnow()
        rto = (end_time - start_time).total_seconds()
        
        logger.log_text(
            f"✓ FAILBACK TO GCP COMPLETED - RTO: {rto:.2f} seconds",
            severity='WARNING'
        )
        
        logger.log_struct({
            'event': 'failback',
            'target': 'gcp',
            'rto_seconds': rto,
            'timestamp': datetime.utcnow().isoformat()
        }, severity='WARNING')
        
        return True
    else:
        logger.log_text("✗ FAILBACK TO GCP FAILED", severity='ERROR')
        return False

def auto_failover(request):
    """Main auto-failover function triggered by Cloud Scheduler"""
    
    logger.log_text("=== Auto-Failover Check Started ===", severity='INFO')
    
    # Get current state
    state = get_current_state()
    current_active = state.get('active_backend', 'gcp')
    
    # Verify current state matches URL map
    actual_backend = get_current_backend()
    if actual_backend != 'unknown' and actual_backend != current_active:
        logger.log_text(
            f"State mismatch detected. State file: {current_active}, URL map: {actual_backend}. Syncing...",
            severity='WARNING'
        )
        current_active = actual_backend
        state['active_backend'] = actual_backend
        save_state(state)
    
    # Check health of both backends
    gcp_healthy = check_backend_health(GCP_HEALTH_CHECK_URL, 'GCP')
    aws_healthy = check_backend_health(AWS_HEALTH_CHECK_URL, 'AWS')
    
    logger.log_text(
        f"Current state: {current_active.upper()} active | "
        f"GCP: {'HEALTHY' if gcp_healthy else 'UNHEALTHY'} | "
        f"AWS: {'HEALTHY' if aws_healthy else 'UNHEALTHY'}",
        severity='INFO'
    )
    
    # Decision logic
    action_taken = None
    
    if current_active == 'gcp' and not gcp_healthy:
        # GCP is active but unhealthy - failover to AWS
        if aws_healthy:
            logger.log_text(
                "🚨 GCP UNHEALTHY - Triggering failover to AWS",
                severity='WARNING'
            )
            if failover_to_aws():
                state['active_backend'] = 'aws'
                state['last_change'] = datetime.utcnow().isoformat()
                save_state(state)
                action_taken = 'failover_to_aws'
        else:
            logger.log_text(
                "⚠️  CRITICAL: Both backends unhealthy!",
                severity='ERROR'
            )
            action_taken = 'both_unhealthy'
    
    elif current_active == 'aws' and gcp_healthy:
        # AWS is active but GCP has recovered - failback to GCP
        logger.log_text(
            "✓ GCP RECOVERED - Triggering failback to GCP",
            severity='WARNING'
        )
        if failback_to_gcp():
            state['active_backend'] = 'gcp'
            state['last_change'] = datetime.utcnow().isoformat()
            save_state(state)
            action_taken = 'failback_to_gcp'
    
    elif current_active == 'aws' and not aws_healthy:
        # AWS is active but unhealthy
        if gcp_healthy:
            logger.log_text(
                "🚨 AWS UNHEALTHY - Triggering emergency failback to GCP",
                severity='WARNING'
            )
            if failback_to_gcp():
                state['active_backend'] = 'gcp'
                state['last_change'] = datetime.utcnow().isoformat()
                save_state(state)
                action_taken = 'emergency_failback'
        else:
            logger.log_text(
                "⚠️  CRITICAL: Both backends unhealthy!",
                severity='ERROR'
            )
            action_taken = 'both_unhealthy'
    
    else:
        # No action needed - current backend is healthy
        logger.log_text(
            f"✓ No action needed - {current_active.upper()} is healthy",
            severity='INFO'
        )
        action_taken = 'no_action'
    
    logger.log_text("=== Auto-Failover Check Completed ===", severity='INFO')
    
    # Return status
    return {
        'status': 'success',
        'action': action_taken,
        'active_backend': state['active_backend'],
        'gcp_healthy': gcp_healthy,
        'aws_healthy': aws_healthy,
        'timestamp': datetime.utcnow().isoformat()
    }