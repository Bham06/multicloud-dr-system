import os
import json
import contextvars
import requests
import time
import uuid
from datetime import datetime
from google.cloud import compute_v1
from google.cloud import logging as cloud_logging
from google.cloud import firestore
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# Environment variables
PROJECT_ID = os.environ.get('PROJECT_ID')
GCP_BACKEND_SERVICE = os.environ.get('GCP_BACKEND_SERVICE')
AWS_BACKEND_SERVICE = os.environ.get('AWS_BACKEND_SERVICE')
URL_MAP_NAME = os.environ.get('URL_MAP_NAME')
GCP_INSTANCE_GROUP = os.environ.get('GCP_INSTANCE_GROUP')
AWS_HEALTH_CHECK_URL = os.environ.get('AWS_HEALTH_CHECK_URL')

# Initialize clients
url_map_client = compute_v1.UrlMapsClient()
backend_service_client = compute_v1.BackendServicesClient()
logging_client = cloud_logging.Client()
logger = logging_client.logger('auto-failover')
db = firestore.Client()

# Firestore collection for state
STATE_COLLECTION = 'failover_state'
STATE_DOC_ID = 'current_state'

# Hysteresis configuration. Read from the environment so Terraform can tune
# these without a code deploy.
#
# Recoveries default higher than failures because the two directions are not
# symmetric: failing over to a healthy secondary is cheap, whereas failing back
# to a primary that has not truly settled costs a second outage. At the
# five-minute check interval, 6 means half an hour of sustained health.
REQUIRED_FAILURES = int(os.environ.get('REQUIRED_FAILURES', '3'))
REQUIRED_RECOVERIES = int(os.environ.get('REQUIRED_RECOVERIES', '6'))

# Returned by get_current_state when Firestore cannot be reached. Acting on a
# fabricated default here would mean deciding failover from invented state.
FIRESTORE_UNAVAILABLE = {'_firestore_unavailable': True}

# Network-level failures worth retrying. An HTTP error response is deliberately
# excluded: a 5xx from the backend is a real health signal, not a blip.
TRANSIENT_ERRORS = (requests.exceptions.Timeout, requests.exceptions.ConnectionError)

# Per-invocation correlation ID, so every line from one scheduler run can be
# grouped when reconstructing what happened during an incident.
_cycle_id: contextvars.ContextVar = contextvars.ContextVar('cycle_id', default='')


def _log(message, severity='INFO'):
    """Emit a log line tagged with the current cycle ID."""
    cycle = _cycle_id.get()
    prefix = f'[{cycle}] ' if cycle else ''
    logger.log_text(f'{prefix}{message}', severity=severity)

def get_current_state():
    """Get current failover state from Firestore"""
    try:
        doc_ref = db.collection(STATE_COLLECTION).document(STATE_DOC_ID)
        doc = doc_ref.get()
        
        if doc.exists:
            state = doc.to_dict()
            _log(
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
            _log("Initialized new state document in Firestore", severity='INFO')
            return default_state
            
    except Exception as e:
        _log(f"Error reading state from Firestore: {str(e)}", severity='ERROR')
        # Previously this returned a default claiming GCP was active and both
        # backends healthy. That is invented state: on a Firestore outage during
        # a real failover it would have reported the wrong active backend and
        # reset the hysteresis counters. The caller aborts on this sentinel.
        return FIRESTORE_UNAVAILABLE

def save_state(state):
    """Save current failover state to Firestore with atomic update"""
    try:
        doc_ref = db.collection(STATE_COLLECTION).document(STATE_DOC_ID)
        
        # Add timestamp
        state['last_updated'] = firestore.SERVER_TIMESTAMP
        state['last_health_check'] = datetime.utcnow().isoformat()
        
        # Atomic update
        doc_ref.set(state, merge=True)
        
        _log(
            f"State saved to Firestore: active={state['active_backend']}, "
            f"failures={state.get('consecutive_failures', 0)}, "
            f"recoveries={state.get('consecutive_recoveries', 0)}",
            severity='DEBUG'
        )
        
    except Exception as e:
        _log(f"Error saving state to Firestore: {str(e)}", severity='ERROR')
        raise

def emit_event(event_type, details):
    """
    Emit structured event for alerting
    These events trigger the alert policies
    """
    logger.log_struct({
        'event_type': event_type,  # failover, failback, or both_unhealthy
        'cycle_id': _cycle_id.get(),
        'details': details,
        'timestamp': datetime.utcnow().isoformat()
    }, severity='WARNING' if event_type in ['failover', 'failback'] else 'ERROR')

def check_gcp_backend_health():
    """
    Check GCP backend health via the load balancer's own health checkers.

    This must not probe the load balancer IP: that address serves whichever
    backend is currently active, so after a failover it would report AWS's
    health as GCP's and immediately fail back to a dead primary. The GCP VM has
    no external IP and this function has no VPC connector, so there is no direct
    path to it either.

    backendServices.getHealth reads the verdict of the health checks already
    running against the instance group, which is authoritative and independent
    of the URL map this function rewrites.

    Returns True/False, or None if health could not be determined - an API
    failure is not evidence that the backend is down.
    """
    try:
        result = backend_service_client.get_health(
            project=PROJECT_ID,
            backend_service=GCP_BACKEND_SERVICE,
            resource_group_reference_resource=compute_v1.ResourceGroupReference(
                group=GCP_INSTANCE_GROUP
            ),
        )

        statuses = list(result.health_status)

        if not statuses:
            # No instance has been probed yet (e.g. just after deploy).
            _log(
                "GCP health check: no instance health reported by the load balancer yet",
                severity='WARNING'
            )
            return False

        healthy = [s for s in statuses if s.health_state == 'HEALTHY']
        is_healthy = len(healthy) > 0

        _log(
            f"GCP health check: {'HEALTHY' if is_healthy else 'UNHEALTHY'} "
            f"({len(healthy)}/{len(statuses)} instances healthy)",
            severity='INFO' if is_healthy else 'WARNING'
        )
        return is_healthy

    except Exception as e:
        _log(
            f"GCP health check error (health undetermined): {str(e)}",
            severity='ERROR'
        )
        return None


@retry(
    retry=retry_if_exception_type(TRANSIENT_ERRORS),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _fetch_health(url):
    """
    One health-check request, retried on transient network errors.

    GCP has no equivalent because its health comes from getHealth, which
    reports "undetermined" and skips the cycle. AWS has no such third state -
    a boolean is required - so a momentary blip must not be allowed to read as
    "AWS is down" and block a failover that is genuinely needed.
    """
    return requests.get(url, timeout=5)


def check_backend_health(url, backend_name):
    """
    Check health of a backend by calling its health endpoint directly.

    Used for AWS, which sits behind an internet NEG that GCP cannot health
    check, so it is probed straight at the Elastic IP.
    """
    try:
        response = _fetch_health(url)
        if response.status_code == 200:
            data = response.json()
            is_healthy = data.get('status') == 'healthy'
            
            _log(
                f"{backend_name} health check: {'HEALTHY' if is_healthy else 'UNHEALTHY'} "
                f"(status={response.status_code})",
                severity='INFO' if is_healthy else 'WARNING'
            )
            return is_healthy
        else:
            _log(
                f"{backend_name} health check failed: HTTP {response.status_code}",
                severity='WARNING'
            )
            return False
    except requests.exceptions.Timeout:
        _log(
            f"{backend_name} health check timeout",
            severity='WARNING'
        )
        return False
    except requests.exceptions.ConnectionError as e:
        _log(
            f"{backend_name} health check connection error: {str(e)}",
            severity='WARNING'
        )
        return False
    except Exception as e:
        _log(
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
            _log(
                f"Unknown backend service in URL map: {default_service}",
                severity='WARNING'
            )
            return 'unknown'
    except Exception as e:
        _log(f"Error getting current backend: {str(e)}", severity='ERROR')
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
        
        _log(
            f"✓ Updated URL map to point to {backend_service_name}",
            severity='INFO'
        )
        return True
        
    except Exception as e:
        _log(
            f"✗ Error updating URL map: {str(e)}",
            severity='ERROR'
        )
        return False

def wait_for_backend_propagation(expected_backend, max_wait_secs=30, poll_interval_secs=5):
    """
    Poll the URL map until it reports expected_backend, or give up.

    update_url_map_backend already waits for the operation to complete, so this
    normally succeeds on the first poll. It replaces a fixed five-second sleep,
    which was simultaneously too long in the common case and too short to be a
    guarantee in the rare one.
    """
    deadline = time.time() + max_wait_secs

    while True:
        current = get_current_backend()
        if current == expected_backend:
            return True

        remaining = deadline - time.time()
        if remaining <= 0:
            break

        _log(
            f"Waiting for URL map to show {expected_backend} (currently {current})",
            severity='DEBUG'
        )
        time.sleep(min(poll_interval_secs, remaining))

    _log(
        f"URL map did not converge to {expected_backend} within {max_wait_secs}s",
        severity='ERROR'
    )
    return False


def rollback_failover(previous_backend):
    """Rollback to previous backend if failover fails"""
    _log(
        f"⚠️  Attempting rollback to {previous_backend.upper()}",
        severity='WARNING'
    )
    
    try:
        backend_service = (
            GCP_BACKEND_SERVICE if previous_backend == 'gcp' 
            else AWS_BACKEND_SERVICE
        )
        
        if update_url_map_backend(backend_service):
            _log(
                f"✓ Rollback to {previous_backend.upper()} successful",
                severity='WARNING'
            )
            return True
        else:
            _log(
                f"✗ Rollback to {previous_backend.upper()} failed",
                severity='ERROR'
            )
            return False
            
    except Exception as e:
        _log(
            f"✗ Rollback exception: {str(e)}",
            severity='ERROR'
        )
        return False

def failover_to_aws():
    """Switch URL map to AWS backend with rollback on failure"""
    _log("=== INITIATING FAILOVER TO AWS ===", severity='WARNING')
    
    start_time = datetime.utcnow()
    previous_backend = 'gcp'
    
    try:
        if update_url_map_backend(AWS_BACKEND_SERVICE):
            # Verify the change actually took effect. RTO is measured to
            # convergence rather than to the API call returning, because
            # traffic has not moved until the URL map reports the switch.
            if wait_for_backend_propagation('aws'):
                rto = (datetime.utcnow() - start_time).total_seconds()
                _log(
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
                _log(
                    "✗ Failover verification failed - rolling back",
                    severity='ERROR'
                )
                rollback_failover(previous_backend)
                return False
        else:
            _log("✗ FAILOVER TO AWS FAILED", severity='ERROR')
            return False
            
    except Exception as e:
        _log(
            f"✗ FAILOVER EXCEPTION: {str(e)} - attempting rollback",
            severity='ERROR'
        )
        rollback_failover(previous_backend)
        return False

def failback_to_gcp():
    """Switch URL map back to GCP backend with rollback on failure"""
    _log("=== INITIATING FAILBACK TO GCP ===", severity='WARNING')
    
    start_time = datetime.utcnow()
    previous_backend = 'aws'
    
    try:
        if update_url_map_backend(GCP_BACKEND_SERVICE):
            # Verify the change actually took effect. RTO is measured to
            # convergence rather than to the API call returning, because
            # traffic has not moved until the URL map reports the switch.
            if wait_for_backend_propagation('gcp'):
                rto = (datetime.utcnow() - start_time).total_seconds()
                _log(
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
                _log(
                    "✗ Failback verification failed - rolling back",
                    severity='ERROR'
                )
                rollback_failover(previous_backend)
                return False
        else:
            _log("✗ FAILBACK TO GCP FAILED", severity='ERROR')
            return False
            
    except Exception as e:
        _log(
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
    
    cycle_id = uuid.uuid4().hex[:8]
    _cycle_id.set(cycle_id)

    _log("=== Auto-Failover Health Check Started ===", severity='INFO')

    # Get current state from Firestore
    state = get_current_state()

    # With no trustworthy state there is nothing safe to decide. Doing nothing
    # for a cycle costs at most five minutes of delay; acting on invented state
    # can move traffic the wrong way during a live incident.
    if state.get('_firestore_unavailable'):
        _log(
            "Aborting cycle: Firestore unavailable, so no failover decision can "
            "be made from trustworthy state",
            severity='ERROR'
        )
        return {
            'status': 'error',
            'action': 'firestore_unavailable',
            'cycle_id': cycle_id,
            'timestamp': datetime.utcnow().isoformat()
        }

    current_active = state.get('active_backend', 'gcp')
    consecutive_failures = state.get('consecutive_failures', 0)
    consecutive_recoveries = state.get('consecutive_recoveries', 0)
    
    # Verify current state matches URL map
    actual_backend = get_current_backend()
    if actual_backend != 'unknown' and actual_backend != current_active:
        _log(
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
    gcp_healthy = check_gcp_backend_health()
    aws_healthy = check_backend_health(AWS_HEALTH_CHECK_URL, 'AWS')

    # An undetermined verdict is not a failure. Counting a transient Compute API
    # error as one would spend a third of the hysteresis budget on an outage
    # that never happened.
    if gcp_healthy is None:
        _log(
            "Skipping failover decision this cycle: GCP health undetermined",
            severity='WARNING'
        )
        return {
            'status': 'success',
            'action': 'health_undetermined',
            'cycle_id': cycle_id,
            'active_backend': current_active,
            'gcp_healthy': None,
            'aws_healthy': aws_healthy,
            'consecutive_failures': consecutive_failures,
            'consecutive_recoveries': consecutive_recoveries,
            'timestamp': datetime.utcnow().isoformat()
        }

    # Update state with current health
    state['gcp_healthy'] = gcp_healthy
    state['aws_healthy'] = aws_healthy
    
    _log(
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
            
            _log(
                f"⚠️  GCP unhealthy check {consecutive_failures}/{REQUIRED_FAILURES}",
                severity='WARNING'
            )
            
            # Only failover after N consecutive failures
            if consecutive_failures >= REQUIRED_FAILURES:
                if aws_healthy:
                    _log(
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
                        _log(
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
                        _log(
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
            
            _log(
                f"✓ GCP healthy check {consecutive_recoveries}/{REQUIRED_RECOVERIES}",
                severity='INFO'
            )
            
            # Only failback after a specific number of consecutive successes
            if consecutive_recoveries >= REQUIRED_RECOVERIES:
                _log(
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
                
                _log(
                    f"⚠️  AWS unhealthy check {consecutive_failures}/{REQUIRED_FAILURES}",
                    severity='WARNING'
                )
                
                if consecutive_failures >= REQUIRED_FAILURES:
                    # Both unhealthy - emit event
                    if state.get('last_both_unhealthy_alert') != 'sent':
                        _log(
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
                        _log(
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
    
    _log("=== Auto-Failover Check Completed ===", severity='INFO')
    
    # Return status
    return {
        'status': 'success',
        'action': action_taken,
        'cycle_id': cycle_id,
        'active_backend': state['active_backend'],
        'gcp_healthy': gcp_healthy,
        'aws_healthy': aws_healthy,
        'consecutive_failures': state.get('consecutive_failures', 0),
        'consecutive_recoveries': state.get('consecutive_recoveries', 0),
        'timestamp': datetime.utcnow().isoformat()
    }

