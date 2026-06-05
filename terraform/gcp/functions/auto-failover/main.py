import os
import uuid
import contextvars
import requests
import time
from datetime import datetime
from google.cloud import compute_v1
from google.cloud import logging as cloud_logging
from google.cloud import firestore
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

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

# Hysteresis configuration — both read from env so Terraform controls the values
# without a code deploy.  REQUIRED_RECOVERIES defaults to 6 (30 minutes of
# sustained health) to prevent premature failback on a flaky GCP recovery.
REQUIRED_FAILURES   = int(os.environ.get('REQUIRED_FAILURES',   '3'))
REQUIRED_RECOVERIES = int(os.environ.get('REQUIRED_RECOVERIES', '6'))

# Sentinel returned when Firestore is unreachable.  The main function treats
# this as a signal to abort the health-check cycle entirely rather than act
# on fabricated state.
_FIRESTORE_UNAVAILABLE = {'_firestore_unavailable': True}

# Transient network errors that are safe to retry on health checks
_TRANSIENT_ERRORS = (requests.exceptions.Timeout, requests.exceptions.ConnectionError)

# Per-invocation correlation ID — set once at the top of auto_failover() so
# every log line emitted during a single scheduler invocation shares an ID.
_cycle_id: contextvars.ContextVar[str] = contextvars.ContextVar('cycle_id', default='')


def _log(message: str, severity: str = 'INFO') -> None:
    """Emit a text log line prefixed with the current cycle ID when set."""
    cid = _cycle_id.get()
    label = f'[{cid}] ' if cid else ''
    logger.log_text(f'{label}{message}', severity=severity)


def get_current_state():
    """Get current failover state from Firestore.

    Returns _FIRESTORE_UNAVAILABLE sentinel if the state store cannot be
    reached.  Callers must check for this sentinel before making any failover
    decision — acting on an unknown state is worse than doing nothing.
    """
    try:
        doc_ref = db.collection(STATE_COLLECTION).document(STATE_DOC_ID)
        doc = doc_ref.get()

        if doc.exists:
            state = doc.to_dict()
            _log(
                f"Retrieved state from Firestore: active_backend={state.get('active_backend')}",
                severity='DEBUG',
            )
            return state
        else:
            default_state = {
                'active_backend': 'gcp',
                'last_change': None,
                'consecutive_failures': 0,
                'consecutive_recoveries': 0,
                'last_health_check': None,
                'gcp_healthy': True,
                'aws_healthy': True,
            }
            doc_ref.set(default_state)
            _log("Initialized new state document in Firestore")
            return default_state

    except Exception as e:
        # Do NOT return a fake-healthy default.  Returning zero failure counts
        # would suppress a real GCP outage if Firestore happens to be down at
        # the same time.  Instead, surface the problem and do nothing.
        _log(
            f"CRITICAL: Cannot read Firestore state: {str(e)}. "
            "Skipping failover decision to avoid acting on unknown state.",
            severity='CRITICAL',
        )
        emit_event('firestore_unavailable', {
            'error': str(e),
            'timestamp': datetime.utcnow().isoformat(),
        })
        return _FIRESTORE_UNAVAILABLE


def save_state(state):
    """Save current failover state to Firestore with atomic update."""
    try:
        doc_ref = db.collection(STATE_COLLECTION).document(STATE_DOC_ID)
        state['last_updated'] = firestore.SERVER_TIMESTAMP
        state['last_health_check'] = datetime.utcnow().isoformat()
        doc_ref.set(state, merge=True)
        _log(
            f"State saved to Firestore: active={state['active_backend']}, "
            f"failures={state.get('consecutive_failures', 0)}, "
            f"recoveries={state.get('consecutive_recoveries', 0)}",
            severity='DEBUG',
        )
    except Exception as e:
        _log(f"Error saving state to Firestore: {str(e)}", severity='ERROR')
        raise


def emit_event(event_type, details):
    """Emit a structured log event consumed by Cloud Monitoring alert policies."""
    cid = _cycle_id.get()
    severity = 'WARNING' if event_type in ('failover', 'failback') else 'ERROR'
    logger.log_struct(
        {
            'event_type': event_type,
            'cycle_id': cid,
            'details': details,
            'timestamp': datetime.utcnow().isoformat(),
        },
        severity=severity,
    )


# ---------------------------------------------------------------------------
# Health checking with retry
# ---------------------------------------------------------------------------

@retry(
    retry=retry_if_exception_type(_TRANSIENT_ERRORS),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _fetch_health(url):
    """Single HTTP health-check attempt.  Retried automatically on transient
    network errors (timeout / connection refused) up to 3 times with
    exponential back-off.  HTTP error responses are NOT retried — a 5xx from
    the backend is a real failure signal, not a transient condition.
    """
    return requests.get(url, timeout=5)


def check_backend_health(url, backend_name):
    """Check health of a backend with retry on transient network errors."""
    try:
        response = _fetch_health(url)
        if response.status_code == 200:
            is_healthy = response.json().get('status') == 'healthy'
            _log(
                f"{backend_name} health check: {'HEALTHY' if is_healthy else 'UNHEALTHY'} "
                f"(status={response.status_code})",
                severity='INFO' if is_healthy else 'WARNING',
            )
            return is_healthy
        else:
            _log(
                f"{backend_name} health check failed: HTTP {response.status_code}",
                severity='WARNING',
            )
            return False

    except _TRANSIENT_ERRORS:
        # All 3 retry attempts exhausted on network-level failures
        _log(
            f"{backend_name} health check failed after 3 attempts (network error)",
            severity='WARNING',
        )
        return False
    except Exception as e:
        _log(
            f"{backend_name} health check unexpected error: {str(e)}",
            severity='ERROR',
        )
        return False


# ---------------------------------------------------------------------------
# URL map helpers
# ---------------------------------------------------------------------------

def get_current_backend():
    """Get current backend from the URL map control plane."""
    try:
        url_map = url_map_client.get(project=PROJECT_ID, url_map=URL_MAP_NAME)
        default_service = url_map.default_service

        if GCP_BACKEND_SERVICE in default_service:
            return 'gcp'
        elif AWS_BACKEND_SERVICE in default_service:
            return 'aws'
        else:
            _log(
                f"Unknown backend service in URL map: {default_service}",
                severity='WARNING',
            )
            return 'unknown'
    except Exception as e:
        _log(f"Error getting current backend: {str(e)}", severity='ERROR')
        return 'unknown'


def update_url_map_backend(backend_service_name):
    """Update URL map to point to specified backend service."""
    try:
        url_map = url_map_client.get(project=PROJECT_ID, url_map=URL_MAP_NAME)
        backend_url = (
            f"https://www.googleapis.com/compute/v1/projects/{PROJECT_ID}"
            f"/global/backendServices/{backend_service_name}"
        )
        url_map.default_service = backend_url
        operation = url_map_client.update(
            project=PROJECT_ID,
            url_map=URL_MAP_NAME,
            url_map_resource=url_map,
        )
        operation.result(timeout=60)
        _log(f"Updated URL map to point to {backend_service_name}")
        return True
    except Exception as e:
        _log(f"Error updating URL map: {str(e)}", severity='ERROR')
        return False


def wait_for_backend_propagation(expected_backend, max_wait_secs=30, poll_interval_secs=5):
    """Poll the URL map control plane until it reports expected_backend or timeout.

    After update_url_map_backend() calls operation.result(), the control-plane
    record is already updated, so this typically succeeds on the first poll.
    A bounded retry guards against rare eventual-consistency edge cases.
    """
    deadline = time.time() + max_wait_secs
    while time.time() < deadline:
        current = get_current_backend()
        if current == expected_backend:
            return True
        remaining = int(deadline - time.time())
        _log(
            f"Waiting for URL map to show {expected_backend} "
            f"(currently {current}, {remaining}s remaining)",
            severity='DEBUG',
        )
        time.sleep(min(poll_interval_secs, max(0, remaining)))

    _log(
        f"URL map did not converge to {expected_backend} within {max_wait_secs}s",
        severity='ERROR',
    )
    return False


def rollback_failover(previous_backend):
    """Rollback to previous backend if failover/failback fails."""
    _log(f"Attempting rollback to {previous_backend.upper()}", severity='WARNING')
    backend_service = GCP_BACKEND_SERVICE if previous_backend == 'gcp' else AWS_BACKEND_SERVICE
    try:
        if update_url_map_backend(backend_service):
            _log(f"Rollback to {previous_backend.upper()} successful", severity='WARNING')
            return True
        else:
            _log(f"Rollback to {previous_backend.upper()} failed", severity='ERROR')
            return False
    except Exception as e:
        _log(f"Rollback exception: {str(e)}", severity='ERROR')
        return False


# ---------------------------------------------------------------------------
# Failover / failback
# ---------------------------------------------------------------------------

def failover_to_aws():
    """Switch URL map to AWS backend with rollback on failure."""
    _log("=== INITIATING FAILOVER TO AWS ===", severity='WARNING')
    start_time = datetime.utcnow()

    try:
        if not update_url_map_backend(AWS_BACKEND_SERVICE):
            _log("FAILOVER TO AWS FAILED (URL map update rejected)", severity='ERROR')
            return False

        if not wait_for_backend_propagation('aws'):
            _log(
                "Failover verification failed (propagation timeout) — rolling back",
                severity='ERROR',
            )
            rollback_failover('gcp')
            return False

        rto = (datetime.utcnow() - start_time).total_seconds()
        _log(f"FAILOVER TO AWS COMPLETED — RTO: {rto:.2f}s", severity='WARNING')
        emit_event('failover', {
            'from': 'gcp',
            'to': 'aws',
            'rto_seconds': rto,
            'reason': 'gcp_unhealthy',
            'timestamp': datetime.utcnow().isoformat(),
        })
        return True

    except Exception as e:
        _log(
            f"FAILOVER EXCEPTION: {str(e)} — attempting rollback",
            severity='ERROR',
        )
        rollback_failover('gcp')
        return False


def failback_to_gcp():
    """Switch URL map back to GCP backend with rollback on failure."""
    _log("=== INITIATING FAILBACK TO GCP ===", severity='WARNING')
    start_time = datetime.utcnow()

    try:
        if not update_url_map_backend(GCP_BACKEND_SERVICE):
            _log("FAILBACK TO GCP FAILED (URL map update rejected)", severity='ERROR')
            return False

        if not wait_for_backend_propagation('gcp'):
            _log(
                "Failback verification failed (propagation timeout) — rolling back",
                severity='ERROR',
            )
            rollback_failover('aws')
            return False

        rto = (datetime.utcnow() - start_time).total_seconds()
        _log(f"FAILBACK TO GCP COMPLETED — RTO: {rto:.2f}s", severity='WARNING')
        emit_event('failback', {
            'from': 'aws',
            'to': 'gcp',
            'rto_seconds': rto,
            'reason': 'gcp_recovered',
            'timestamp': datetime.utcnow().isoformat(),
        })
        return True

    except Exception as e:
        _log(
            f"FAILBACK EXCEPTION: {str(e)} — attempting rollback",
            severity='ERROR',
        )
        rollback_failover('aws')
        return False


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def auto_failover(request):
    """Main auto-failover function — triggered by Cloud Scheduler every 5 minutes."""
    cycle_id = uuid.uuid4().hex[:8]
    _cycle_id.set(cycle_id)
    _log(f'=== Auto-Failover Health Check Started (cycle={cycle_id}) ===')

    # Abort immediately if Firestore is unavailable.  We have no reliable state
    # to act on, so doing nothing is safer than acting on stale or fabricated data.
    state = get_current_state()
    if state.get('_firestore_unavailable'):
        _log(
            "Aborting: Firestore unavailable. No failover decisions will be made "
            "until the state store recovers. An alert has been emitted.",
            severity='ERROR',
        )
        return {
            'status': 'error',
            'reason': 'firestore_unavailable',
            'timestamp': datetime.utcnow().isoformat(),
        }

    current_active = state.get('active_backend', 'gcp')
    consecutive_failures = state.get('consecutive_failures', 0)
    consecutive_recoveries = state.get('consecutive_recoveries', 0)

    # Reconcile Firestore state against the authoritative URL map record
    actual_backend = get_current_backend()
    if actual_backend != 'unknown' and actual_backend != current_active:
        _log(
            f"State mismatch: Firestore says {current_active}, URL map says {actual_backend}. Syncing.",
            severity='WARNING',
        )
        current_active = actual_backend
        state['active_backend'] = actual_backend
        # Reset counters — counters accumulated against the wrong active backend
        # are meaningless and would cause an immediate spurious failover.
        state['consecutive_failures'] = 0
        state['consecutive_recoveries'] = 0
        consecutive_failures = 0
        consecutive_recoveries = 0
        save_state(state)

    # Check health of both backends (each call retries up to 3× on network errors)
    gcp_healthy = check_backend_health(GCP_HEALTH_CHECK_URL, 'GCP')
    aws_healthy = check_backend_health(AWS_HEALTH_CHECK_URL, 'AWS')

    state['gcp_healthy'] = gcp_healthy
    state['aws_healthy'] = aws_healthy

    _log(
        f"State: {current_active.upper()} active | "
        f"GCP: {'HEALTHY' if gcp_healthy else 'UNHEALTHY'} | "
        f"AWS: {'HEALTHY' if aws_healthy else 'UNHEALTHY'} | "
        f"Failures: {consecutive_failures}/{REQUIRED_FAILURES} | "
        f"Recoveries: {consecutive_recoveries}/{REQUIRED_RECOVERIES}",
    )

    action_taken = None

    if current_active == 'gcp':
        if not gcp_healthy:
            consecutive_failures += 1
            consecutive_recoveries = 0

            _log(
                f"GCP unhealthy check {consecutive_failures}/{REQUIRED_FAILURES}",
                severity='WARNING',
            )

            if consecutive_failures >= REQUIRED_FAILURES:
                if aws_healthy:
                    _log(
                        f"GCP UNHEALTHY ({REQUIRED_FAILURES} consecutive) — triggering failover to AWS",
                        severity='WARNING',
                    )
                    if failover_to_aws():
                        state['active_backend'] = 'aws'
                        state['last_change'] = datetime.utcnow().isoformat()
                        state['consecutive_failures'] = 0
                        state['consecutive_recoveries'] = 0
                        save_state(state)
                        action_taken = 'failover_to_aws'
                    else:
                        state['consecutive_failures'] = consecutive_failures
                        save_state(state)
                        action_taken = 'failover_failed'
                else:
                    if state.get('last_both_unhealthy_alert') != 'sent':
                        _log("CRITICAL: Both backends unhealthy!", severity='ERROR')
                        emit_event('both_unhealthy', {
                            'gcp_healthy': False,
                            'aws_healthy': False,
                            'duration_minutes': consecutive_failures * 5,
                        })
                        state['last_both_unhealthy_alert'] = 'sent'
                    state['consecutive_failures'] = consecutive_failures
                    save_state(state)
                    action_taken = 'both_unhealthy'
            else:
                state['consecutive_failures'] = consecutive_failures
                save_state(state)
                action_taken = 'monitoring_gcp_degradation'
        else:
            consecutive_failures = 0
            state['consecutive_failures'] = 0
            state['last_both_unhealthy_alert'] = None
            save_state(state)
            action_taken = 'no_action'

    elif current_active == 'aws':
        if gcp_healthy:
            consecutive_recoveries += 1
            consecutive_failures = 0

            _log(
                f"GCP healthy check {consecutive_recoveries}/{REQUIRED_RECOVERIES}",
            )

            if consecutive_recoveries >= REQUIRED_RECOVERIES:
                _log(
                    f"GCP RECOVERED ({REQUIRED_RECOVERIES} consecutive) — triggering failback to GCP",
                    severity='WARNING',
                )
                if failback_to_gcp():
                    state['active_backend'] = 'gcp'
                    state['last_change'] = datetime.utcnow().isoformat()
                    state['consecutive_failures'] = 0
                    state['consecutive_recoveries'] = 0
                    save_state(state)
                    action_taken = 'failback_to_gcp'
                else:
                    state['consecutive_recoveries'] = consecutive_recoveries
                    save_state(state)
                    action_taken = 'failback_failed'
            else:
                state['consecutive_recoveries'] = consecutive_recoveries
                save_state(state)
                action_taken = 'monitoring_gcp_recovery'
        else:
            consecutive_recoveries = 0
            state['consecutive_recoveries'] = 0

            if not aws_healthy:
                consecutive_failures += 1
                _log(
                    f"AWS unhealthy check {consecutive_failures}/{REQUIRED_FAILURES}",
                    severity='WARNING',
                )

                if consecutive_failures >= REQUIRED_FAILURES:
                    if state.get('last_both_unhealthy_alert') != 'sent':
                        _log("CRITICAL: Both backends unhealthy!", severity='ERROR')
                        emit_event('both_unhealthy', {
                            'gcp_healthy': False,
                            'aws_healthy': False,
                            'duration_minutes': consecutive_failures * 5,
                        })
                        state['last_both_unhealthy_alert'] = 'sent'
                    state['consecutive_failures'] = consecutive_failures
                    save_state(state)
                    action_taken = 'both_unhealthy'
                else:
                    state['consecutive_failures'] = consecutive_failures
                    save_state(state)
                    action_taken = 'monitoring_aws_degradation'
            else:
                consecutive_failures = 0
                state['consecutive_failures'] = 0
                state['last_both_unhealthy_alert'] = None
                save_state(state)
                action_taken = 'no_action'

    _log('=== Auto-Failover Check Completed ===')

    return {
        'status': 'success',
        'action': action_taken,
        'active_backend': state['active_backend'],
        'gcp_healthy': gcp_healthy,
        'aws_healthy': aws_healthy,
        'consecutive_failures': state.get('consecutive_failures', 0),
        'consecutive_recoveries': state.get('consecutive_recoveries', 0),
        'timestamp': datetime.utcnow().isoformat(),
    }
