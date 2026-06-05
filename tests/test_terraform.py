"""
Structural sanity checks for the Terraform configuration.
These run quickly without any cloud connectivity — they just verify that
required files and variables are present and well-formed.
"""
import os
import glob
import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), '..')
GCP_DIR   = os.path.join(REPO_ROOT, 'terraform', 'gcp')
AWS_DIR   = os.path.join(REPO_ROOT, 'terraform', 'aws')


def terraform_files(directory):
    return glob.glob(os.path.join(directory, '*.tf'))


# ---------------------------------------------------------------------------
# Required file presence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('path', [
    'terraform/gcp/providers.tf',
    'terraform/gcp/variables.tf',
    'terraform/gcp/network.tf',
    'terraform/gcp/compute.tf',
    'terraform/gcp/database.tf',
    'terraform/gcp/loadbalancer.tf',
    'terraform/gcp/monitoring.tf',
    'terraform/gcp/slo.tf',
    'terraform/aws/providers.tf',
    'terraform/aws/variables.tf',
    'terraform/aws/network.tf',
    'terraform/aws/compute.tf',
    'terraform/aws/database.tf',
    'terraform/aws/secrets.tf',
    'terraform/gcp/functions/auto-failover/main.py',
    'terraform/gcp/functions/auto-failover/requirements.txt',
    'terraform/gcp/functions/gcs-s3-sync/main.py',
])
def test_required_file_exists(path):
    full = os.path.join(REPO_ROOT, path)
    assert os.path.isfile(full), f'Required file missing: {path}'


# ---------------------------------------------------------------------------
# Terraform files must not be empty
# ---------------------------------------------------------------------------

def test_gcp_tf_files_non_empty():
    for tf in terraform_files(GCP_DIR):
        size = os.path.getsize(tf)
        assert size > 0, f'Empty Terraform file: {tf}'


def test_aws_tf_files_non_empty():
    for tf in terraform_files(AWS_DIR):
        size = os.path.getsize(tf)
        assert size > 0, f'Empty Terraform file: {tf}'


# ---------------------------------------------------------------------------
# Security: db_password must not appear as plaintext in startup scripts
# ---------------------------------------------------------------------------

def test_db_password_not_in_gcp_startup_script():
    path = os.path.join(GCP_DIR, 'scripts', 'startup.sh')
    with open(path) as f:
        content = f.read()
    # The template variable must NOT be in the script — it should be fetched at runtime
    assert '${db_password}' not in content, (
        'GCP startup.sh still interpolates db_password — '
        'credentials must be fetched from Secret Manager at runtime'
    )


def test_db_password_not_in_aws_user_data():
    path = os.path.join(AWS_DIR, 'scripts', 'user-data.sh')
    with open(path) as f:
        content = f.read()
    assert '${db_password}' not in content, (
        'AWS user-data.sh still interpolates db_password — '
        'credentials must be fetched from Secrets Manager at runtime'
    )


# ---------------------------------------------------------------------------
# Security: RDS must not be publicly accessible
# ---------------------------------------------------------------------------

def test_rds_not_publicly_accessible():
    path = os.path.join(AWS_DIR, 'database.tf')
    with open(path) as f:
        content = f.read()
    assert 'publicly_accessible    = false' in content, (
        'RDS instance must have publicly_accessible = false'
    )
    assert 'publicly_accessible    = true' not in content


# ---------------------------------------------------------------------------
# Security: SSH must not allow 0.0.0.0/0
# ---------------------------------------------------------------------------

def test_no_ssh_open_to_world():
    path = os.path.join(AWS_DIR, 'network.tf')
    with open(path) as f:
        lines = f.readlines()
    # Find any line with 0.0.0.0/0 and verify it's not adjacent to port 22
    open_cidrs = [i for i, l in enumerate(lines) if '0.0.0.0/0' in l]
    for idx in open_cidrs:
        # Check a small window around the CIDR for port 22 references
        window = ''.join(lines[max(0, idx - 5): idx + 5])
        assert 'from_port   = 22' not in window, (
            f'SSH (port 22) appears to allow 0.0.0.0/0 near line {idx + 1} in network.tf'
        )


# ---------------------------------------------------------------------------
# Auto-failover function requirements include tenacity
# ---------------------------------------------------------------------------

def test_auto_failover_requirements_include_tenacity():
    path = os.path.join(GCP_DIR, 'functions', 'auto-failover', 'requirements.txt')
    with open(path) as f:
        content = f.read()
    assert 'tenacity' in content, 'tenacity must be in auto-failover requirements.txt'


# ---------------------------------------------------------------------------
# SLO file must be uncommented (not all comments)
# ---------------------------------------------------------------------------

def test_slo_tf_has_active_resources():
    path = os.path.join(GCP_DIR, 'slo.tf')
    with open(path) as f:
        lines = f.readlines()
    active = [l for l in lines if l.strip() and not l.strip().startswith('#')]
    assert len(active) > 5, 'slo.tf appears to be entirely commented out'
