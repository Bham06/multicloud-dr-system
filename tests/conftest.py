"""
pytest configuration: installs GCP library mocks into sys.modules before any
test file imports `main`.  The auto-failover module runs client-construction
code at import time, so these mocks must exist first.

tenacity and requests are kept real so retry logic is tested as-is.
"""
import sys
import os
from unittest.mock import MagicMock

_GCP_STUBS = {
    'google.cloud.compute_v1': MagicMock(),
    'google.cloud.logging':    MagicMock(),
    'google.cloud.firestore':  MagicMock(),
}

for _mod, _mock in _GCP_STUBS.items():
    sys.modules.setdefault(_mod, _mock)

# Ensure parent namespace packages exist
sys.modules.setdefault('google',       MagicMock())
sys.modules.setdefault('google.cloud', MagicMock())

# Make the Cloud Function importable without installing it as a package
_FUNC_DIR = os.path.join(
    os.path.dirname(__file__), '..', 'terraform', 'gcp', 'functions', 'auto-failover'
)
if _FUNC_DIR not in sys.path:
    sys.path.insert(0, _FUNC_DIR)
