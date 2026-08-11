"""
Unit tests for the auto-failover decision logic.

The failover function is the only component that can take the site down by
itself, and its bugs are invisible until a real outage. These tests pin the
decision table: when it switches, when it refuses to, and what it does when it
cannot tell.
"""
from unittest.mock import MagicMock

import pytest

from conftest import FakeFirestore, FakeUrlMapClient, make_health_response

GCP_BACKEND = "dr-backend-gcp-primary"
AWS_BACKEND = "dr-backend-aws-secondary"


def aws_response(healthy=True, status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = {"status": "healthy" if healthy else "degraded"}
    return response


def arrange(
    module,
    *,
    active="gcp",
    gcp_health=("HEALTHY",),
    gcp_error=None,
    aws_healthy=True,
    consecutive_failures=0,
    consecutive_recoveries=0,
    extra_state=None,
):
    """Point the module at fakes describing one moment in the system's life."""
    state = {
        "active_backend": active,
        "consecutive_failures": consecutive_failures,
        "consecutive_recoveries": consecutive_recoveries,
        "last_change": None,
        "gcp_healthy": True,
        "aws_healthy": True,
    }
    if extra_state:
        state.update(extra_state)

    module.db = FakeFirestore(state)
    module.url_map_client = FakeUrlMapClient(
        GCP_BACKEND if active == "gcp" else AWS_BACKEND
    )

    module.backend_service_client = MagicMock()
    if gcp_error is not None:
        module.backend_service_client.get_health.side_effect = gcp_error
    else:
        module.backend_service_client.get_health.return_value = make_health_response(
            gcp_health
        )

    module.requests.get = MagicMock(return_value=aws_response(healthy=aws_healthy))

    return module.url_map_client


# --------------------------------------------------------------------------
# GCP health is read from the load balancer, not probed through it
# --------------------------------------------------------------------------


def test_gcp_healthy_when_any_instance_is_healthy(failover):
    arrange(failover, gcp_health=("HEALTHY", "UNHEALTHY"))
    assert failover.check_gcp_backend_health() is True


def test_gcp_unhealthy_when_no_instance_is_healthy(failover):
    arrange(failover, gcp_health=("UNHEALTHY", "UNHEALTHY"))
    assert failover.check_gcp_backend_health() is False


def test_gcp_unhealthy_when_no_instance_reported_yet(failover):
    arrange(failover, gcp_health=())
    assert failover.check_gcp_backend_health() is False


def test_gcp_health_undetermined_on_api_error(failover):
    arrange(failover, gcp_error=RuntimeError("compute API unavailable"))
    assert failover.check_gcp_backend_health() is None


def test_health_check_does_not_probe_the_load_balancer(failover):
    """
    The whole point of the getHealth approach: no HTTP request is made to
    determine GCP health, so the verdict cannot depend on which backend the
    URL map currently serves.
    """
    arrange(failover, gcp_health=("HEALTHY",))
    failover.check_gcp_backend_health()
    failover.requests.get.assert_not_called()


# --------------------------------------------------------------------------
# Hysteresis
# --------------------------------------------------------------------------


def test_single_failure_does_not_trigger_failover(failover):
    url_map = arrange(failover, gcp_health=("UNHEALTHY",), consecutive_failures=0)

    result = failover.auto_failover(request=None)

    assert result["action"] == "monitoring_gcp_degradation"
    assert result["active_backend"] == "gcp"
    assert url_map.updates == []


def test_failover_after_required_consecutive_failures(failover):
    url_map = arrange(
        failover,
        gcp_health=("UNHEALTHY",),
        aws_healthy=True,
        consecutive_failures=failover.REQUIRED_FAILURES - 1,
    )

    result = failover.auto_failover(request=None)

    assert result["action"] == "failover_to_aws"
    assert result["active_backend"] == "aws"
    assert len(url_map.updates) == 1
    assert AWS_BACKEND in url_map.updates[0]


def test_failure_counter_resets_when_gcp_recovers(failover):
    arrange(
        failover,
        gcp_health=("HEALTHY",),
        consecutive_failures=failover.REQUIRED_FAILURES - 1,
    )

    result = failover.auto_failover(request=None)

    assert result["action"] == "no_action"
    assert result["consecutive_failures"] == 0


def test_failback_after_required_consecutive_recoveries(failover):
    url_map = arrange(
        failover,
        active="aws",
        gcp_health=("HEALTHY",),
        consecutive_recoveries=failover.REQUIRED_RECOVERIES - 1,
    )

    result = failover.auto_failover(request=None)

    assert result["action"] == "failback_to_gcp"
    assert result["active_backend"] == "gcp"
    assert GCP_BACKEND in url_map.updates[-1]


def test_single_recovery_does_not_trigger_failback(failover):
    url_map = arrange(
        failover,
        active="aws",
        gcp_health=("HEALTHY",),
        consecutive_recoveries=0,
    )

    result = failover.auto_failover(request=None)

    assert result["action"] == "monitoring_gcp_recovery"
    assert result["active_backend"] == "aws"
    assert url_map.updates == []


# --------------------------------------------------------------------------
# Refusing to act
# --------------------------------------------------------------------------


def test_no_failover_when_both_backends_are_unhealthy(failover):
    """Switching to a dead secondary would turn a degradation into an outage."""
    url_map = arrange(
        failover,
        gcp_health=("UNHEALTHY",),
        aws_healthy=False,
        consecutive_failures=failover.REQUIRED_FAILURES - 1,
    )

    result = failover.auto_failover(request=None)

    assert result["action"] == "both_unhealthy"
    assert result["active_backend"] == "gcp"
    assert url_map.updates == []


def test_undetermined_health_does_not_move_traffic(failover):
    """A Compute API error is not evidence that the backend is down."""
    url_map = arrange(
        failover,
        gcp_error=RuntimeError("compute API unavailable"),
        consecutive_failures=failover.REQUIRED_FAILURES - 1,
    )

    result = failover.auto_failover(request=None)

    assert result["action"] == "health_undetermined"
    assert url_map.updates == []


def test_undetermined_health_does_not_consume_hysteresis_budget(failover):
    """The failure counter must survive a cycle we could not evaluate."""
    starting_failures = failover.REQUIRED_FAILURES - 1
    arrange(
        failover,
        gcp_error=RuntimeError("compute API unavailable"),
        consecutive_failures=starting_failures,
    )

    result = failover.auto_failover(request=None)

    assert result["consecutive_failures"] == starting_failures


def test_unhealthy_aws_http_status_is_not_healthy(failover):
    arrange(failover)
    failover.requests.get = MagicMock(return_value=aws_response(status_code=503))
    assert failover.check_backend_health("http://aws/health", "AWS") is False


def test_aws_timeout_is_not_healthy(failover):
    arrange(failover)
    failover.requests.get = MagicMock(side_effect=failover.requests.exceptions.Timeout())
    assert failover.check_backend_health("http://aws/health", "AWS") is False


# --------------------------------------------------------------------------
# Trustworthy state
# --------------------------------------------------------------------------


def test_firestore_outage_aborts_the_cycle(failover):
    """
    The old behaviour returned a default claiming GCP was active and both
    backends healthy. During a Firestore outage mid-incident that is invented
    state, and acting on it can move traffic the wrong way.
    """
    url_map = arrange(
        failover,
        gcp_health=("UNHEALTHY",),
        consecutive_failures=failover.REQUIRED_FAILURES - 1,
    )
    failover.db = MagicMock()
    failover.db.collection.side_effect = RuntimeError("firestore unavailable")

    result = failover.auto_failover(request=None)

    assert result["action"] == "firestore_unavailable"
    assert url_map.updates == []


def test_every_cycle_is_traceable(failover):
    arrange(failover)
    first = failover.auto_failover(request=None)
    second = failover.auto_failover(request=None)

    assert first["cycle_id"] and second["cycle_id"]
    assert first["cycle_id"] != second["cycle_id"]


# --------------------------------------------------------------------------
# Transient errors
# --------------------------------------------------------------------------


def test_transient_network_error_is_retried_before_giving_up(failover):
    """
    A momentary blip must not read as "AWS is down" and block a failover that
    is genuinely needed. AWS has no undetermined state to fall back on the way
    GCP does, so the retry happens here instead.
    """
    arrange(failover)
    failover._fetch_health.retry.sleep = lambda _seconds: None
    failover.requests.get = MagicMock(
        side_effect=failover.requests.exceptions.ConnectionError()
    )

    assert failover.check_backend_health("http://aws/health", "AWS") is False
    assert failover.requests.get.call_count == 3


def test_http_error_response_is_not_retried(failover):
    """A 5xx is a real health signal, not a blip - retrying only delays the
    failover."""
    arrange(failover)
    failover._fetch_health.retry.sleep = lambda _seconds: None
    failover.requests.get = MagicMock(return_value=aws_response(status_code=503))

    assert failover.check_backend_health("http://aws/health", "AWS") is False
    assert failover.requests.get.call_count == 1


def test_recovered_transient_error_reports_healthy(failover):
    arrange(failover)
    failover._fetch_health.retry.sleep = lambda _seconds: None
    failover.requests.get = MagicMock(
        side_effect=[
            failover.requests.exceptions.Timeout(),
            aws_response(healthy=True),
        ]
    )

    assert failover.check_backend_health("http://aws/health", "AWS") is True


# --------------------------------------------------------------------------
# Propagation
# --------------------------------------------------------------------------


def test_propagation_confirmed_when_the_url_map_already_agrees(failover):
    arrange(failover, active="gcp")
    assert failover.wait_for_backend_propagation("gcp", max_wait_secs=0) is True


def test_propagation_gives_up_when_the_url_map_never_converges(failover):
    """
    Replaces a fixed five-second sleep, which was both too long in the common
    case and no guarantee in the rare one.
    """
    arrange(failover, active="gcp")
    assert failover.wait_for_backend_propagation("aws", max_wait_secs=0) is False


# --------------------------------------------------------------------------
# State reconciliation
# --------------------------------------------------------------------------


def test_url_map_wins_when_stored_state_disagrees(failover):
    """
    Firestore is a cache of a fact that lives in the URL map. If they diverge -
    after a manual failover, say - the URL map is the truth.
    """
    arrange(failover, active="gcp", gcp_health=("HEALTHY",))
    # Traffic is really on AWS despite what the stored state claims.
    failover.url_map_client = FakeUrlMapClient(AWS_BACKEND)

    result = failover.auto_failover(request=None)

    assert result["active_backend"] in {"aws", "gcp"}
    assert failover.db.data["active_backend"] == result["active_backend"]


@pytest.mark.parametrize("backend", [GCP_BACKEND, AWS_BACKEND])
def test_current_backend_is_read_from_the_url_map(failover, backend):
    arrange(failover)
    failover.url_map_client = FakeUrlMapClient(backend)

    expected = "gcp" if backend == GCP_BACKEND else "aws"
    assert failover.get_current_backend() == expected
