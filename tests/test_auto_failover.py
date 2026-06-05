"""
Unit tests for the auto-failover Cloud Function (terraform/gcp/functions/auto-failover/main.py).

conftest.py installs GCP library mocks before this file is collected, so `import main`
works without real credentials.  tenacity and requests are imported for real so the
retry decorator exercises actual retry logic with time.sleep patched to zero.
"""
import pytest
import requests as req
from unittest.mock import MagicMock, patch, call

import main

# ---------------------------------------------------------------------------
# Module-level constants wired in for every test
# ---------------------------------------------------------------------------
main.PROJECT_ID           = 'test-project'
main.GCP_BACKEND_SERVICE  = 'dr-backend-gcp-primary'
main.AWS_BACKEND_SERVICE  = 'dr-backend-aws-secondary'
main.URL_MAP_NAME         = 'dr-url-map'
main.GCP_HEALTH_CHECK_URL = 'http://1.2.3.4/health'
main.AWS_HEALTH_CHECK_URL = 'http://5.6.7.8/health'
main.REQUIRED_FAILURES    = 3
main.REQUIRED_RECOVERIES  = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _state(**kwargs):
    """Return a Firestore state dict with sensible defaults."""
    base = {
        'active_backend':       'gcp',
        'consecutive_failures':  0,
        'consecutive_recoveries': 0,
        'last_change':           None,
        'last_health_check':     None,
        'gcp_healthy':           True,
        'aws_healthy':           True,
        'last_both_unhealthy_alert': None,
    }
    base.update(kwargs)
    return base


def _firestore_doc(state):
    """Return a mock Firestore document snapshot for the given state dict."""
    doc = MagicMock()
    doc.exists = True
    doc.to_dict.return_value = state
    return doc


def _url_map_for(backend):
    """Return a mock URL map whose default_service matches backend ('gcp'|'aws')."""
    m = MagicMock()
    svc = main.GCP_BACKEND_SERVICE if backend == 'gcp' else main.AWS_BACKEND_SERVICE
    m.default_service = f'https://www.googleapis.com/compute/v1/projects/test/global/backendServices/{svc}'
    return m


@pytest.fixture(autouse=True)
def reset_mocks():
    """Reinitialise the module-level GCP client mocks before each test.

    reset_mock() does not clear side_effect on deeply nested child mocks, so
    tests that set side_effect (e.g. the Firestore error test) would pollute
    later tests.  Replacing the objects entirely is the safest solution.
    """
    main.db             = MagicMock()
    main.url_map_client = MagicMock()
    main.logger         = MagicMock()
    yield


# ===========================================================================
# get_current_state
# ===========================================================================

class TestGetCurrentState:

    def test_returns_existing_document(self):
        main.db.collection.return_value.document.return_value.get.return_value = (
            _firestore_doc(_state(active_backend='aws'))
        )
        state = main.get_current_state()
        assert state['active_backend'] == 'aws'

    def test_creates_default_when_document_missing(self):
        missing = MagicMock()
        missing.exists = False
        main.db.collection.return_value.document.return_value.get.return_value = missing

        state = main.get_current_state()

        assert state['active_backend'] == 'gcp'
        assert state['consecutive_failures'] == 0
        main.db.collection.return_value.document.return_value.set.assert_called_once()

    def test_returns_sentinel_on_firestore_error(self):
        main.db.collection.return_value.document.return_value.get.side_effect = (
            Exception('Firestore connection refused')
        )
        state = main.get_current_state()
        assert state.get('_firestore_unavailable') is True


# ===========================================================================
# check_backend_health
# ===========================================================================

class TestCheckBackendHealth:

    def test_healthy_200_response(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {'status': 'healthy'}
        with patch('main.requests.get', return_value=resp):
            assert main.check_backend_health('http://x/health', 'GCP') is True

    def test_unhealthy_json_field(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {'status': 'unhealthy'}
        with patch('main.requests.get', return_value=resp):
            assert main.check_backend_health('http://x/health', 'GCP') is False

    def test_http_5xx_returns_false_without_retry(self):
        resp = MagicMock(status_code=503)
        with patch('main.requests.get', return_value=resp) as mock_get:
            result = main.check_backend_health('http://x/health', 'GCP')
        assert result is False
        # HTTP errors are NOT retried — backend is genuinely unhealthy
        assert mock_get.call_count == 1

    def test_connection_error_retries_three_times_then_returns_false(self):
        with patch('main.requests.get', side_effect=req.exceptions.ConnectionError('refused')) as mock_get:
            with patch('time.sleep'):
                result = main.check_backend_health('http://x/health', 'GCP')
        assert result is False
        assert mock_get.call_count == 3

    def test_timeout_retries_three_times_then_returns_false(self):
        with patch('main.requests.get', side_effect=req.exceptions.Timeout('timed out')) as mock_get:
            with patch('time.sleep'):
                result = main.check_backend_health('http://x/health', 'GCP')
        assert result is False
        assert mock_get.call_count == 3

    def test_succeeds_on_third_attempt_after_transient_errors(self):
        good = MagicMock(status_code=200)
        good.json.return_value = {'status': 'healthy'}
        with patch('main.requests.get', side_effect=[
            req.exceptions.ConnectionError('refused'),
            req.exceptions.ConnectionError('refused'),
            good,
        ]) as mock_get:
            with patch('time.sleep'):
                result = main.check_backend_health('http://x/health', 'GCP')
        assert result is True
        assert mock_get.call_count == 3


# ===========================================================================
# wait_for_backend_propagation
# ===========================================================================

class TestWaitForBackendPropagation:

    def test_succeeds_immediately(self):
        with patch('main.get_current_backend', return_value='aws'):
            with patch('main.time.sleep') as mock_sleep:
                assert main.wait_for_backend_propagation('aws') is True
        mock_sleep.assert_not_called()

    def test_succeeds_on_second_poll(self):
        with patch('main.get_current_backend', side_effect=['gcp', 'aws']):
            with patch('main.time.sleep'):
                assert main.wait_for_backend_propagation('aws', max_wait_secs=30) is True

    def test_returns_false_when_never_converges(self):
        # First call sets the deadline (returns 0 → deadline = 30).
        # Every subsequent call returns 100, which is already past the deadline,
        # so the while loop exits immediately after the first failed poll.
        call_n = [0]
        def advancing_time():
            call_n[0] += 1
            return 0 if call_n[0] == 1 else 100

        with patch('main.get_current_backend', return_value='gcp'):
            with patch('main.time.time', side_effect=advancing_time):
                with patch('main.time.sleep'):
                    assert main.wait_for_backend_propagation('aws', max_wait_secs=30) is False


# ===========================================================================
# auto_failover — full state-machine tests
# ===========================================================================

class TestAutoFailover:

    def _setup_firestore(self, state):
        main.db.collection.return_value.document.return_value.get.return_value = (
            _firestore_doc(state)
        )

    def _setup_url_map(self, backend):
        main.url_map_client.get.return_value = _url_map_for(backend)

    # -----------------------------------------------------------------------

    def test_aborts_when_firestore_unavailable(self):
        main.db.collection.return_value.document.return_value.get.side_effect = (
            Exception('unavailable')
        )
        result = main.auto_failover(None)

        assert result['status'] == 'error'
        assert result['reason'] == 'firestore_unavailable'
        main.url_map_client.update.assert_not_called()

    def test_no_action_when_gcp_active_and_healthy(self):
        self._setup_firestore(_state())
        self._setup_url_map('gcp')

        with patch('main.check_backend_health', side_effect=[True, True]):
            result = main.auto_failover(None)

        assert result['action'] == 'no_action'
        main.url_map_client.update.assert_not_called()

    def test_first_failure_increments_counter_no_failover(self):
        self._setup_firestore(_state(consecutive_failures=0))
        self._setup_url_map('gcp')

        with patch('main.check_backend_health', side_effect=[False, True]):
            result = main.auto_failover(None)

        assert result['action'] == 'monitoring_gcp_degradation'
        assert result['consecutive_failures'] == 1
        main.url_map_client.update.assert_not_called()

    def test_third_failure_triggers_failover_to_aws(self):
        # consecutive_failures already at 2; this run makes it 3 → failover
        self._setup_firestore(_state(consecutive_failures=2))
        self._setup_url_map('gcp')
        main.url_map_client.update.return_value = MagicMock()

        with patch('main.check_backend_health', side_effect=[False, True]):
            with patch('main.wait_for_backend_propagation', return_value=True):
                with patch('main.emit_event') as mock_emit:
                    result = main.auto_failover(None)

        assert result['action'] == 'failover_to_aws'
        assert result['active_backend'] == 'aws'
        assert result['consecutive_failures'] == 0
        mock_emit.assert_called_once()
        assert mock_emit.call_args[0][0] == 'failover'

    def test_third_failure_both_unhealthy_emits_event_no_failover(self):
        self._setup_firestore(_state(consecutive_failures=2))
        self._setup_url_map('gcp')

        with patch('main.check_backend_health', side_effect=[False, False]):
            with patch('main.emit_event') as mock_emit:
                result = main.auto_failover(None)

        assert result['action'] == 'both_unhealthy'
        main.url_map_client.update.assert_not_called()
        mock_emit.assert_called_once()
        event_type, details = mock_emit.call_args[0]
        assert event_type == 'both_unhealthy'
        assert details['gcp_healthy'] is False
        assert details['aws_healthy'] is False

    def test_propagation_timeout_triggers_rollback(self):
        self._setup_firestore(_state(consecutive_failures=2))
        self._setup_url_map('gcp')
        main.url_map_client.update.return_value = MagicMock()

        with patch('main.check_backend_health', side_effect=[False, True]):
            with patch('main.wait_for_backend_propagation', return_value=False):
                with patch('main.rollback_failover') as mock_rollback:
                    result = main.auto_failover(None)

        assert result['action'] == 'failover_failed'
        mock_rollback.assert_called_once_with('gcp')

    def test_recovery_increments_counter_no_immediate_failback(self):
        # AWS is active; GCP has been healthy for 1 check — needs 3 to failback
        self._setup_firestore(_state(active_backend='aws', consecutive_recoveries=1))
        self._setup_url_map('aws')

        with patch('main.check_backend_health', side_effect=[True, True]):
            result = main.auto_failover(None)

        assert result['action'] == 'monitoring_gcp_recovery'
        assert result['consecutive_recoveries'] == 2
        main.url_map_client.update.assert_not_called()

    def test_enough_recoveries_triggers_failback_to_gcp(self):
        self._setup_firestore(_state(active_backend='aws', consecutive_recoveries=2))
        self._setup_url_map('aws')
        main.url_map_client.update.return_value = MagicMock()

        with patch('main.check_backend_health', side_effect=[True, True]):
            with patch('main.wait_for_backend_propagation', return_value=True):
                with patch('main.emit_event') as mock_emit:
                    result = main.auto_failover(None)

        assert result['action'] == 'failback_to_gcp'
        assert result['active_backend'] == 'gcp'
        assert result['consecutive_recoveries'] == 0
        mock_emit.assert_called_once()
        assert mock_emit.call_args[0][0] == 'failback'

    def test_state_mismatch_is_reconciled_and_counters_reset(self):
        # Firestore thinks GCP active with 2 failures accumulated, but URL map shows AWS
        self._setup_firestore(_state(active_backend='gcp', consecutive_failures=2))
        # URL map disagrees → state mismatch
        main.url_map_client.get.return_value = _url_map_for('aws')

        with patch('main.check_backend_health', side_effect=[True, True]):
            result = main.auto_failover(None)

        # Mismatch detected → counters reset, AWS treated as active → monitoring_gcp_recovery
        assert result['consecutive_failures'] == 0

    def test_both_unhealthy_alert_sent_only_once(self):
        # Alert already sent → should not emit again
        self._setup_firestore(_state(
            active_backend='gcp',
            consecutive_failures=2,
            last_both_unhealthy_alert='sent',
        ))
        self._setup_url_map('gcp')

        with patch('main.check_backend_health', side_effect=[False, False]):
            with patch('main.emit_event') as mock_emit:
                main.auto_failover(None)

        mock_emit.assert_not_called()
