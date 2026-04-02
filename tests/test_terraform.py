"""
Basic Terraform validation tests
"""
import subprocess
import json

def test_terraform_fmt_gcp():
    """Test that GCP Terraform is formatted correctly"""
    result = subprocess.run(
        ['terraform', 'fmt', '-check', '-recursive'],
        cwd='gcp',
        capture_output=True
    )
    assert result.returncode == 0, "Terraform files not formatted correctly"

def test_terraform_validate_gcp():
    """Test that GCP Terraform is valid"""
    subprocess.run(['terraform', 'init', '-backend=false'], cwd='gcp', check=True)
    result = subprocess.run(['terraform', 'validate'], cwd='gcp', capture_output=True)
    assert result.returncode == 0, "GCP Terraform validation failed"

def test_terraform_validate_aws():
    """Test that AWS Terraform is valid"""
    subprocess.run(['terraform', 'init', '-backend=false'], cwd='aws', check=True)
    result = subprocess.run(['terraform', 'validate'], cwd='aws', capture_output=True)
    assert result.returncode == 0, "AWS Terraform validation failed"