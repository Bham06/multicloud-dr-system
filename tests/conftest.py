"""
Shared test fixtures.

The Cloud Function modules construct API clients at import time, so the
google-cloud and requests packages are stubbed here before any import happens.
That keeps the whole suite runnable in CI with no cloud credentials and no
network access.
"""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FUNCTIONS_DIR = REPO_ROOT / "terraform" / "gcp" / "functions"

# archive_file packages a function directory straight off the filesystem, so a
# __pycache__ left behind by the tests would be uploaded as part of the
# deployment. Stale bytecode also masks edits when a mutation happens to leave
# the file the same size within the same second.
sys.dont_write_bytecode = True


def _install_stub(name, module):
    """Register a stub module, remembering nothing about any real install."""
    sys.modules[name] = module
    return module


def _install_google_cloud_stubs():
    google = _install_stub("google", types.ModuleType("google"))
    google.__path__ = []

    cloud = _install_stub("google.cloud", types.ModuleType("google.cloud"))
    cloud.__path__ = []
    google.cloud = cloud

    compute_v1 = types.ModuleType("google.cloud.compute_v1")
    compute_v1.UrlMapsClient = MagicMock(name="UrlMapsClient")
    compute_v1.BackendServicesClient = MagicMock(name="BackendServicesClient")
    # Real ResourceGroupReference takes a keyword-only group argument; the stub
    # keeps that shape so a call-site typo still fails the test.
    compute_v1.ResourceGroupReference = lambda group: types.SimpleNamespace(group=group)
    _install_stub("google.cloud.compute_v1", compute_v1)
    cloud.compute_v1 = compute_v1

    cloud_logging = types.ModuleType("google.cloud.logging")
    cloud_logging.Client = MagicMock(name="LoggingClient")
    _install_stub("google.cloud.logging", cloud_logging)
    cloud.logging = cloud_logging

    firestore = types.ModuleType("google.cloud.firestore")
    firestore.Client = MagicMock(name="FirestoreClient")
    firestore.SERVER_TIMESTAMP = "SERVER_TIMESTAMP"
    _install_stub("google.cloud.firestore", firestore)
    cloud.firestore = firestore


def _install_requests_stub():
    requests = types.ModuleType("requests")

    class RequestException(Exception):
        pass

    class Timeout(RequestException):
        pass

    class ConnectionError(RequestException):  # noqa: A001 - mirrors requests' name
        pass

    exceptions = types.ModuleType("requests.exceptions")
    exceptions.RequestException = RequestException
    exceptions.Timeout = Timeout
    exceptions.ConnectionError = ConnectionError

    requests.exceptions = exceptions
    requests.get = MagicMock(name="requests.get")

    _install_stub("requests", requests)
    _install_stub("requests.exceptions", exceptions)


_install_google_cloud_stubs()
_install_requests_stub()


def load_function_module(function_name, module_alias):
    """Import a Cloud Function's main.py by path, outside any package."""
    path = FUNCTIONS_DIR / function_name / "main.py"
    spec = importlib.util.spec_from_file_location(module_alias, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeDocument:
    def __init__(self, store):
        self._store = store

    @property
    def exists(self):
        return self._store.data is not None

    def to_dict(self):
        return dict(self._store.data or {})


class FakeDocumentRef:
    def __init__(self, store):
        self._store = store

    def get(self):
        return FakeDocument(self._store)

    def set(self, value, merge=False):
        if merge and self._store.data is not None:
            self._store.data.update(value)
        else:
            self._store.data = dict(value)
        self._store.writes.append(dict(self._store.data))


class FakeCollection:
    def __init__(self, store):
        self._store = store

    def document(self, _doc_id):
        return FakeDocumentRef(self._store)


class FakeFirestore:
    """Minimal stand-in for the single-document state store main.py uses."""

    def __init__(self, initial=None):
        self.data = dict(initial) if initial is not None else None
        self.writes = []

    def collection(self, _name):
        return FakeCollection(self)


class FakeUrlMapClient:
    """URL map that actually remembers what it was pointed at."""

    BASE = "https://www.googleapis.com/compute/v1/projects/test-project/global/backendServices"

    def __init__(self, backend_service_name):
        self.default_service = f"{self.BASE}/{backend_service_name}"
        self.updates = []

    def get(self, project, url_map):
        return types.SimpleNamespace(default_service=self.default_service)

    def update(self, project, url_map, url_map_resource):
        self.default_service = url_map_resource.default_service
        self.updates.append(url_map_resource.default_service)
        return MagicMock()


def make_health_response(states):
    """Build a getHealth response with one entry per supplied health state."""
    return types.SimpleNamespace(
        health_status=[types.SimpleNamespace(health_state=s) for s in states]
    )


@pytest.fixture
def failover(monkeypatch):
    """
    Load the auto-failover function with a known environment.

    Environment variables are read at module scope, so they must be set before
    the module is executed rather than patched afterwards.
    """
    monkeypatch.setenv("PROJECT_ID", "test-project")
    monkeypatch.setenv("GCP_BACKEND_SERVICE", "dr-backend-gcp-primary")
    monkeypatch.setenv("AWS_BACKEND_SERVICE", "dr-backend-aws-secondary")
    monkeypatch.setenv("URL_MAP_NAME", "dr-url-map")
    monkeypatch.setenv("GCP_INSTANCE_GROUP", "https://example/instanceGroups/dr-ig")
    monkeypatch.setenv("AWS_HEALTH_CHECK_URL", "http://198.51.100.10/health")

    module = load_function_module("auto-failover", "auto_failover_main")

    # Failover verification sleeps to let the change propagate; nothing to wait
    # for against fakes.
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    return module
