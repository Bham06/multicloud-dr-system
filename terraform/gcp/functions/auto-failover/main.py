import os
import json
import requests
import time
from datetime import datetime
from google.cloud import compute_v1
from google.cloud import logging as cloud_logging
from google.cloud import firestore

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
db = firestore.Client()

# Firestore collection for state
STATE_COLLECTION = 'failover_state'
STATE_DOC_ID = 'current_state'

# Hysteresis configuration
REQUIRED_FAILURES = 3  # Require 3 consecutive failures before failover
REQUIRED_RECOVERIES = 3  # Require 3 consecutive successes before failback

def get_current_state():
    """Get current failover state from Firestore"""
    try:
        doc_ref = db.collection(STATE_COLLECTION).document(STATE_DOC_ID)
        doc = doc_ref.get()
        
        if doc.exists:
            state = doc.to_dict()
            logger.log_text(
                f"Retrieved state from Firestore: active_backend={state.get('active_backend')}",
                severity='DEBUG'
            )
            return state
        else:
            # Initialize default state
            default_state = {
                'active_backend': 'gcp',
                'last_change': None,
                'consecutive_failures': 0,
                'consecutive_recoveries': 0,
                'last_health_check': None,
                'gcp_healthy': True,
                'aws_healthy': True
            }
            doc_ref.set(default_state)
            logger.log_text("Initialized new state document in Firestore", severity='INFO')
            return default_state
            
    except Exception as e:
        logger.log_text(f"Error reading state from Firestore: {str(e)}", severity='ERROR')
        # Return safe default
        return {
            'active_backend': 'gcp',
            'last_change': None,
            'consecutive_failures': 0,
            'consecutive_recoveries': 0,
            'last_health_check': None,
            'gcp_healthy': True,
            'aws_healthy': True
        }

def save_state(state):
    """Save current failover state to Firestore with atomic update"""
    try:
        doc_ref = db.collection(STATE_COLLECTION).document(STATE_DOC_ID)
        
        # Add timestamp
        state['last_updated'] = firestore.SERVER_TIMESTAMP
        state['last_health_check'] = datetime.utcnow().isoformat()
        
        # Atomic update
        doc_ref.set(state, merge=True)
        
        logger.log_text(
            f"State saved to Firestore: active={state['active_backend']}, "
            f"failures={state.get('consecutive_failures', 0)}, "
            f"recoveries={state.get('consecutive_recoveries', 0)}",
            severity='DEBUG'
        )
        
    except Exception as e:
        logger.log_text(f"Error saving state to Firestore: {str(e)}", severity='ERROR')
        raise

def emit_event(event_type, details):
    """
    Emit structured event for alerting
    These events trigger the alert policies
    """
    logger.log_struct({
        'event_type': event_type,  # failover, failback, or both_unhealthy
        'details': details,
        'timestamp': datetime.utcnow().isoformat()
    }, severity='WARNING' if event_type in ['failover', 'failback'] else 'ERROR')

def check_backend_health(url, backend_name):
    """Check health of a backend by calling its health endpoint"""
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            is_healthy = data.get('status') == 'healthy'
            
            logger.log_text(
                f"{backend_name} health check: {'HEALTHY' if is_healthy else 'UNHEALTHY'} "
                f"(status={response.status_code})",
                severity='INFO' if is_healthy else 'WARNING'
            )
            return is_healthy
        else:
            logger.log_text(
                f"{backend_name} health check failed: HTTP {response.status_code}",
                severity='WARNING'
            )
            return False
    except requests.exceptions.Timeout:
        logger.log_text(
            f"{backend_name} health check timeout",
            severity='WARNING'
        )
        return False
    except requests.exceptions.ConnectionError as e:
        logger.log_text(
            f"{backend_name} health check connection error: {str(e)}",
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
            f"✓ Updated URL map to point to {backend_service_name}",
            severity='INFO'
        )
        return True
        
    except Exception as e:
        logger.log_text(
            f"✗ Error updating URL map: {str(e)}",
            severity='ERROR'
        )
        return False

def rollback_failover(previous_backend):
    """Rollback to previous backend if failover fails"""
    logger.log_text(
        f"⚠️  Attempting rollback to {previous_backend.upper()}",
        severity='WARNING'
    )
    
    try:
        backend_service = (
            GCP_BACKEND_SERVICE if previous_backend == 'gcp' 
            else AWS_BACKEND_SERVICE
        )
        
        if update_url_map_backend(backend_service):
            logger.log_text(
                f"✓ Rollback to {previous_backend.upper()} successful",
                severity='WARNING'
            )
            return True
        else:
            logger.log_text(
                f"✗ Rollback to {previous_backend.upper()} failed",
                severity='ERROR'
            )
            return False
            
    except Exception as e:
        logger.log_text(
            f"✗ Rollback exception: {str(e)}",
            severity='ERROR'
        )
        return False

def failover_to_aws():
    """Switch URL map to AWS backend with rollback on failure"""
    logger.log_text("=== INITIATING FAILOVER TO AWS ===", severity='WARNING')
    
    start_time = datetime.utcnow()
    previous_backend = 'gcp'
    
    try:
        if update_url_map_backend(AWS_BACKEND_SERVICE):
            end_time = datetime.utcnow()
            rto = (end_time - start_time).total_seconds()
            
            # Verify failover succeeded
            time.sleep(5)  # Wait for propagation
            current_backend = get_current_backend()
            
            if current_backend == 'aws':
                logger.log_text(
                    f"✓ FAILOVER TO AWS COMPLETED - RTO: {rto:.2f} seconds",
                    severity='WARNING'
                )
                
                # Emit failover event for alerting
                emit_event('failover', {
                    'from': 'gcp',
                    'to': 'aws',
                    'rto_seconds': rto,
                    'reason': 'gcp_unhealthy',
                    'timestamp': datetime.utcnow().isoformat()
                })
                
                return True
            else:
                # Failover didn't take effect - rollback
                logger.log_text(
                    "✗ Failover verification failed - rolling back",
                    severity='ERROR'
                )
                rollback_failover(previous_backend)
                return False
        else:
            logger.log_text("✗ FAILOVER TO AWS FAILED", severity='ERROR')
            return False
            
    except Exception as e:
        logger.log_text(
            f"✗ FAILOVER EXCEPTION: {str(e)} - attempting rollback",
            severity='ERROR'
        )
        rollback_failover(previous_backend)
        return False

def failback_to_gcp():
    """Switch URL map back to GCP backend with rollback on failure"""
    logger.log_text("=== INITIATING FAILBACK TO GCP ===", severity='WARNING')
    
    start_time = datetime.utcnow()
    previous_backend = 'aws'
    
    try:
        if update_url_map_backend(GCP_BACKEND_SERVICE):
            end_time = datetime.utcnow()
            rto = (end_time - start_time).total_seconds()
            
            # Verify failback succeeded
            time.sleep(5)
            current_backend = get_current_backend()
            
            if current_backend == 'gcp':
                logger.log_text(
                    f"✓ FAILBACK TO GCP COMPLETED - RTO: {rto:.2f} seconds",
                    severity='WARNING'
                )
                
                # Failback event for alerting
                emit_event('failback', {
                    'from': 'aws',
                    'to': 'gcp',
                    'rto_seconds': rto,
                    'reason': 'gcp_recovered',
                    'timestamp': datetime.utcnow().isoformat()
                })
                
                return True
            else:
                logger.log_text(
                    "✗ Failback verification failed - rolling back",
                    severity='ERROR'
                )
                rollback_failover(previous_backend)
                return False
        else:
            logger.log_text("✗ FAILBACK TO GCP FAILED", severity='ERROR')
            return False
            
    except Exception as e:
        logger.log_text(
            f"✗ FAILBACK EXCEPTION: {str(e)} - attempting rollback",
            severity='ERROR'
        )
        rollback_failover(previous_backend)
        return False

def auto_failover(request):
    """
    Main auto-failover function with hysteresis and state-change detection
    
    Triggered by Cloud Scheduler every 2 minutes
    """
    
    logger.log_text("=== Auto-Failover Health Check Started ===", severity='INFO')
    
    # Get current state from Firestore
    state = get_current_state()
    current_active = state.get('active_backend', 'gcp')
    consecutive_failures = state.get('consecutive_failures', 0)
    consecutive_recoveries = state.get('consecutive_recoveries', 0)
    
    # Verify current state matches URL map
    actual_backend = get_current_backend()
    if actual_backend != 'unknown' and actual_backend != current_active:
        logger.log_text(
            f"⚠️  State mismatch detected. Firestore: {current_active}, URL map: {actual_backend}. Syncing...",
            severity='WARNING'
        )
        current_active = actual_backend
        state['active_backend'] = actual_backend
        # Reset counters on mismatch
        state['consecutive_failures'] = 0
        state['consecutive_recoveries'] = 0
        consecutive_failures = 0
        consecutive_recoveries = 0
        save_state(state)
    
    # Check health of both backends
    gcp_healthy = check_backend_health(GCP_HEALTH_CHECK_URL, 'GCP')
    aws_healthy = check_backend_health(AWS_HEALTH_CHECK_URL, 'AWS')
    
    # Update state with current health
    state['gcp_healthy'] = gcp_healthy
    state['aws_healthy'] = aws_healthy
    
    logger.log_text(
        f"Current state: {current_active.upper()} active | "
        f"GCP: {'HEALTHY' if gcp_healthy else 'UNHEALTHY'} | "
        f"AWS: {'HEALTHY' if aws_healthy else 'UNHEALTHY'} | "
        f"Consecutive failures: {consecutive_failures}/{REQUIRED_FAILURES} | "
        f"Consecutive recoveries: {consecutive_recoveries}/{REQUIRED_RECOVERIES}",
        severity='INFO'
    )
    
    # Decision logic with hysteresis
    action_taken = None
    
    if current_active == 'gcp':
        if not gcp_healthy:
            # GCP is active but unhealthy
            consecutive_failures += 1
            consecutive_recoveries = 0  # Reset recovery counter
            
            logger.log_text(
                f"⚠️  GCP unhealthy check {consecutive_failures}/{REQUIRED_FAILURES}",
                severity='WARNING'
            )
            
            # Only failover after N consecutive failures
            if consecutive_failures >= REQUIRED_FAILURES:
                if aws_healthy:
                    logger.log_text(
                        f"🚨 GCP UNHEALTHY ({REQUIRED_FAILURES} consecutive failures) - Triggering failover to AWS",
                        severity='WARNING'
                    )
                    if failover_to_aws():
                        state['active_backend'] = 'aws'
                        state['last_change'] = datetime.utcnow().isoformat()
                        state['consecutive_failures'] = 0  # Reset
                        state['consecutive_recoveries'] = 0
                        save_state(state)
                        action_taken = 'failover_to_aws'
                    else:
                        # Failover failed, keep state
                        state['consecutive_failures'] = consecutive_failures
                        save_state(state)
                        action_taken = 'failover_failed'
                else:
                    if state.get('last_both_unhealthy_alert') != 'sent':
                        logger.log_text(
                            "⚠️  CRITICAL: Both backends unhealthy!",
                            severity='ERROR'
                        )
                        emit_event('both_unhealthy', {
                            'gcp_healthy': False,
                            'aws_healthy': False,
                            'duration_minutes': consecutive_failures * 2
                        })
                        state['last_both_unhealthy_alert'] = 'sent'
                    else:
                        logger.log_text(
                            "Both backends still unhealthy (no state change)",
                            severity='INFO'
                        )
                    
                    state['consecutive_failures'] = consecutive_failures
                    save_state(state)
                    action_taken = 'both_unhealthy'
            else:
                # Not enough consecutive failures yet
                state['consecutive_failures'] = consecutive_failures
                save_state(state)
                action_taken = 'monitoring_gcp_degradation'
        else:
            # GCP is healthy - reset failure counter
            consecutive_failures = 0
            state['consecutive_failures'] = 0
            state['last_both_unhealthy_alert'] = None  # Reset alert flag
            save_state(state)
            action_taken = 'no_action'
    
    elif current_active == 'aws':
        if gcp_healthy:
            # GCP recovered - increments recovery counter
            consecutive_recoveries += 1
            consecutive_failures = 0  # Reset failure counter
            
            logger.log_text(
                f"✓ GCP healthy check {consecutive_recoveries}/{REQUIRED_RECOVERIES}",
                severity='INFO'
            )
            
            # Only failback after a specific number of consecutive successes
            if consecutive_recoveries >= REQUIRED_RECOVERIES:
                logger.log_text(
                    f"✓ GCP RECOVERED ({REQUIRED_RECOVERIES} consecutive successes) - Triggering failback to GCP",
                    severity='WARNING'
                )
                if failback_to_gcp():
                    state['active_backend'] = 'gcp'
                    state['last_change'] = datetime.utcnow().isoformat()
                    state['consecutive_failures'] = 0
                    state['consecutive_recoveries'] = 0  # Reset
                    save_state(state)
                    action_taken = 'failback_to_gcp'
                else:
                    # Failback failed
                    state['consecutive_recoveries'] = consecutive_recoveries
                    save_state(state)
                    action_taken = 'failback_failed'
            else:
                # Not enough consecutive recoveries yet
                state['consecutive_recoveries'] = consecutive_recoveries
                save_state(state)
                action_taken = 'monitoring_gcp_recovery'
        else:
            # GCP still unhealthy - reset recovery counter
            consecutive_recoveries = 0
            state['consecutive_recoveries'] = 0
            
            # Check if AWS is also unhealthy
            if not aws_healthy:
                consecutive_failures += 1
                
                logger.log_text(
                    f"⚠️  AWS unhealthy check {consecutive_failures}/{REQUIRED_FAILURES}",
                    severity='WARNING'
                )
                
                if consecutive_failures >= REQUIRED_FAILURES:
                    # Both unhealthy - emit event
                    if state.get('last_both_unhealthy_alert') != 'sent':
                        logger.log_text(
                            "⚠️  CRITICAL: Both backends unhealthy!",
                            severity='ERROR'
                        )
                        emit_event('both_unhealthy', {
                            'gcp_healthy': False,
                            'aws_healthy': False,
                            'duration_minutes': consecutive_failures * 2
                        })
                        state['last_both_unhealthy_alert'] = 'sent'
                    else:
                        logger.log_text(
                            "Both backends still unhealthy (no state change)",
                            severity='INFO'
                        )
                    
                    state['consecutive_failures'] = consecutive_failures
                    save_state(state)
                    action_taken = 'both_unhealthy'
                else:
                    state['consecutive_failures'] = consecutive_failures
                    save_state(state)
                    action_taken = 'monitoring_aws_degradation'
            else:
                # AWS healthy, keep monitoring
                consecutive_failures = 0
                state['consecutive_failures'] = 0
                state['last_both_unhealthy_alert'] = None
                save_state(state)
                action_taken = 'no_action'
    
    logger.log_text("=== Auto-Failover Check Completed ===", severity='INFO')
    
    # Return status
    return {
        'status': 'success',
        'action': action_taken,
        'active_backend': state['active_backend'],
        'gcp_healthy': gcp_healthy,
        'aws_healthy': aws_healthy,
        'consecutive_failures': state.get('consecutive_failures', 0),
        'consecutive_recoveries': state.get('consecutive_recoveries', 0),
        'timestamp': datetime.utcnow().isoformat()
    }

