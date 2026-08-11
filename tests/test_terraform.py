"""
Static checks on the Terraform configuration and its Cloud Function sources.

These need no cloud credentials. They exist because `terraform validate` does
not evaluate data sources: a config can validate cleanly and still fail at plan
time, which is exactly how a missing function directory survived in the repo.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TERRAFORM_DIR = REPO_ROOT / "terraform"
GCP_DIR = TERRAFORM_DIR / "gcp"
FUNCTIONS_DIR = GCP_DIR / "functions"

STACKS = ["gcp", "aws"]

# Import name -> distribution name, for the third-party imports we can map
# unambiguously. Anything not listed here is skipped rather than guessed at.
IMPORT_TO_PACKAGE = {
    "boto3": "boto3",
    "requests": "requests",
    "functions_framework": "functions-framework",
    "googleapiclient": "google-api-python-client",
}

terraform_cli = pytest.mark.skipif(
    shutil.which("terraform") is None, reason="terraform CLI not installed"
)


def tf_text(directory):
    return "\n".join(p.read_text() for p in sorted(directory.rglob("*.tf")))


def function_dirs():
    if not FUNCTIONS_DIR.is_dir():
        return []
    return sorted(
        d for d in FUNCTIONS_DIR.iterdir() if d.is_dir() and (d / "main.py").exists()
    )


def by_name(path):
    return path.name


# --------------------------------------------------------------------------
# Terraform references resolve to things that exist
# --------------------------------------------------------------------------


@pytest.mark.parametrize("stack", STACKS)
def test_archive_file_source_dirs_exist(stack):
    """
    Regression test. data.tf archived functions/db-backup, which .gitignore had
    silently excluded from the repo. validate passed; plan failed.
    """
    stack_dir = TERRAFORM_DIR / stack
    referenced = re.findall(
        r'source_dir\s*=\s*"\$\{path\.module\}/([^"]+)"', tf_text(stack_dir)
    )
    missing = sorted(r for r in referenced if not (stack_dir / r).is_dir())
    assert not missing, f"archive_file source_dir does not exist in {stack}: {missing}"


@pytest.mark.parametrize("stack", STACKS)
def test_file_sources_exist(stack):
    """
    file() reads are resolved at plan time, same as archive_file. The restore
    script is injected into the instance this way, so a rename here breaks the
    deploy rather than the tests.
    """
    stack_dir = TERRAFORM_DIR / stack
    referenced = re.findall(
        r'\bfile\(\s*"\$\{path\.module\}/([^"]+)"', tf_text(stack_dir)
    )
    missing = sorted(r for r in referenced if not (stack_dir / r).is_file())
    assert not missing, f"file() source does not exist in {stack}: {missing}"


@pytest.mark.parametrize("stack", STACKS)
def test_templatefile_sources_exist(stack):
    stack_dir = TERRAFORM_DIR / stack
    referenced = re.findall(
        r'templatefile\(\s*"\$\{path\.module\}/([^"]+)"', tf_text(stack_dir)
    )
    missing = sorted(r for r in referenced if not (stack_dir / r).is_file())
    assert not missing, f"templatefile source does not exist in {stack}: {missing}"


def test_entry_points_resolve_to_a_function():
    entry_points = re.findall(r'entry_point\s*=\s*"([^"]+)"', tf_text(TERRAFORM_DIR))
    assert entry_points, "expected at least one Cloud Function entry_point"

    sources = "\n".join(p.read_text() for p in FUNCTIONS_DIR.rglob("main.py"))
    for name in entry_points:
        assert re.search(rf"^def {re.escape(name)}\(", sources, re.M), (
            f"entry_point '{name}' has no matching function definition"
        )


# --------------------------------------------------------------------------
# Function code and its deployment agree
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fn_dir", function_dirs(), ids=by_name)
def test_env_vars_read_by_function_are_set_in_terraform(fn_dir):
    """A variable the code reads but nothing sets is a None at runtime."""
    source = (fn_dir / "main.py").read_text()
    read = set(re.findall(r"os\.environ\.get\(\s*['\"]([A-Z0-9_]+)['\"]", source))
    declared = set(re.findall(r"^\s*([A-Z0-9_]+)\s*=", tf_text(GCP_DIR), re.M))

    missing = sorted(read - declared)
    assert not missing, f"{fn_dir.name} reads env vars never set in Terraform: {missing}"


@pytest.mark.parametrize("fn_dir", function_dirs(), ids=by_name)
def test_function_declares_its_third_party_imports(fn_dir):
    """
    Regression test. gcs-s3-sync needed functions-framework for its 2nd-gen
    CloudEvent signature and did not declare it.
    """
    requirements = fn_dir / "requirements.txt"
    assert requirements.exists(), f"{fn_dir.name} has no requirements.txt"

    declared = requirements.read_text().lower()
    source = (fn_dir / "main.py").read_text()
    imported = set(re.findall(r"^\s*(?:import|from)\s+([a-zA-Z0-9_]+)", source, re.M))

    missing = sorted(
        package
        for name, package in IMPORT_TO_PACKAGE.items()
        if name in imported and package.lower() not in declared
    )
    assert not missing, f"{fn_dir.name} imports undeclared packages: {missing}"


@pytest.mark.parametrize("fn_dir", function_dirs(), ids=by_name)
def test_requirements_are_pinned(fn_dir):
    """Unpinned deps make a redeploy a different build than the one tested."""
    unpinned = []
    for line in (fn_dir / "requirements.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not re.search(r"[=><~]=|[><]", line):
            unpinned.append(line)
    assert not unpinned, f"{fn_dir.name} has unpinned requirements: {unpinned}"


# --------------------------------------------------------------------------
# Repository hygiene
# --------------------------------------------------------------------------


def test_no_source_files_are_git_ignored():
    """
    Regression test for the root cause of the missing function: .gitignore
    carried `*-backup`, which matched the functions/db-backup directory name and
    kept it out of every commit.
    """
    candidates = [
        p
        for p in TERRAFORM_DIR.rglob("*")
        if p.is_file()
        and p.suffix in {".tf", ".py", ".txt", ".sh"}
        and ".terraform" not in p.parts
    ]
    assert candidates, "no source files found to check"

    # --no-index is essential: without it git suppresses already-tracked files,
    # so the check would pass even while the ignore rules were actively hiding
    # newly added ones. That is precisely the failure this test exists to catch.
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        cwd=REPO_ROOT,
        input="\n".join(str(p.relative_to(REPO_ROOT)) for p in candidates),
        capture_output=True,
        text=True,
    )
    ignored = sorted(line for line in result.stdout.splitlines() if line.strip())
    assert not ignored, f"source files excluded by .gitignore: {ignored}"


def test_sensitive_variables_are_marked_sensitive():
    """Anything credential-shaped must not surface in plan output or logs."""
    pattern = re.compile(
        r'variable\s+"([^"]+)"\s*\{(.*?)\n\}', re.S
    )
    offenders = []
    for stack in STACKS:
        variables_file = TERRAFORM_DIR / stack / "variables.tf"
        if not variables_file.exists():
            continue
        for name, body in pattern.findall(variables_file.read_text()):
            looks_sensitive = re.search(
                r"password|secret|token|access_key|private_key", name, re.I
            )
            if looks_sensitive and "sensitive" not in body:
                offenders.append(f"{stack}/{name}")

    assert not offenders, f"sensitive variables not marked sensitive: {offenders}"


def test_no_literal_credentials_committed():
    patterns = {
        "AWS access key id": r"AKIA[0-9A-Z]{16}",
        "private key block": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    }
    scanned = [
        p
        for p in REPO_ROOT.rglob("*")
        if p.is_file()
        and p.suffix in {".tf", ".py", ".sh", ".yml", ".yaml"}
        and ".git" not in p.parts
        and ".terraform" not in p.parts
    ]

    findings = []
    for path in scanned:
        text = path.read_text(errors="ignore")
        for label, pattern in patterns.items():
            if re.search(pattern, text):
                findings.append(f"{path.relative_to(REPO_ROOT)}: {label}")

    assert not findings, f"literal credentials committed: {findings}"


# --------------------------------------------------------------------------
# Terraform CLI
# --------------------------------------------------------------------------


@terraform_cli
def test_terraform_is_formatted():
    result = subprocess.run(
        ["terraform", "fmt", "-check", "-recursive", "terraform"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"needs `terraform fmt`:\n{result.stdout}"


@terraform_cli
@pytest.mark.parametrize("stack", STACKS)
def test_terraform_validates(stack, tmp_path):
    stack_dir = TERRAFORM_DIR / stack

    init = subprocess.run(
        ["terraform", "init", "-backend=false", "-input=false"],
        cwd=stack_dir,
        capture_output=True,
        text=True,
    )
    assert init.returncode == 0, f"{stack} init failed:\n{init.stderr}"

    validate = subprocess.run(
        ["terraform", "validate"],
        cwd=stack_dir,
        capture_output=True,
        text=True,
    )
    assert validate.returncode == 0, f"{stack} validate failed:\n{validate.stdout}"
