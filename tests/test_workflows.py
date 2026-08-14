"""
Static checks on the GitHub Actions workflows.

These exist because the plan and deploy jobs are gated behind ENABLE_TF_PLAN /
ENABLE_TF_DEPLOY and have never run. Nothing else in the suite would notice a
workflow that cannot get past `terraform plan`, so the failure would surface on
the first real apply rather than here.
"""
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
TERRAFORM_DIR = REPO_ROOT / "terraform"

STACKS = ["gcp", "aws"]

VARIABLE_BLOCK = re.compile(r'variable\s+"([^"]+)"\s*\{(.*?)\n\}', re.S)


def load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def required_variables(stack):
    """Variables with no default. Terraform refuses to plan without these."""
    text = (TERRAFORM_DIR / stack / "variables.tf").read_text()
    return {
        name
        for name, body in VARIABLE_BLOCK.findall(text)
        if not re.search(r"^\s*default\s*=", body, re.M)
    }


def tf_vars_in_scope(job, working_directory):
    """
    TF_VAR_* names visible to the terraform steps for one stack.

    Env is collected from every step whose working-directory matches, plus the
    job-level env, since a plan reads whichever is in scope.
    """
    supplied = set(job.get("env", {}))
    for step in job.get("steps", []):
        if step.get("working-directory") in working_directory:
            supplied |= set(step.get("env", {}))
    return {name[len("TF_VAR_") :] for name in supplied if name.startswith("TF_VAR_")}


# --------------------------------------------------------------------------
# Every required variable is actually passed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("stack", STACKS)
def test_deploy_workflow_supplies_every_required_variable(stack):
    """
    Regression test. PR #8 added github_owner and github_repo as required
    variables in both stacks and wired them into neither workflow. The gap was
    invisible because the deploy job is opt-in: the first apply after enabling
    it would have failed on "No value for required variable".
    """
    workflow = load("terraform-deploy.yml")
    job = workflow["jobs"][stack]

    missing = required_variables(stack) - tf_vars_in_scope(job, {f"terraform/{stack}"})
    assert not missing, (
        f"terraform-deploy.yml job '{stack}' never sets: "
        f"{sorted('TF_VAR_' + name for name in missing)}"
    )


def test_plan_workflow_supplies_every_required_variable():
    """
    The plan job is a matrix over both stacks sharing one env block, so it needs
    the union of both stacks' required variables.
    """
    workflow = load("terraform-pr.yml")
    job = workflow["jobs"]["plan"]

    # working-directory is templated on the matrix, so match the literal.
    supplied = tf_vars_in_scope(job, {"terraform/${{ matrix.stack }}"})
    required = required_variables("gcp") | required_variables("aws")

    missing = required - supplied
    assert not missing, (
        f"terraform-pr.yml plan job never sets: "
        f"{sorted('TF_VAR_' + name for name in missing)}"
    )


# --------------------------------------------------------------------------
# The IaC scan reports where GitHub can find it
# --------------------------------------------------------------------------


def test_trivy_scans_from_the_repo_root():
    """
    Regression test. Trivy writes SARIF locations relative to its scan root.
    With scan-ref: terraform the results pointed at `aws/database.tf`, which
    does not exist in this repo, and code scanning discarded every alert — the
    job passed green having reported nothing while Trivy was finding 30+
    misconfigurations. Only a scan rooted at the repo emits paths that resolve.
    """
    workflow = load("security-scan.yml")
    steps = workflow["jobs"]["iac"]["steps"]

    trivy = [s for s in steps if "trivy-action" in str(s.get("uses", ""))]
    assert trivy, "no Trivy step in the iac job"

    for step in trivy:
        assert step["with"]["scan-ref"] == ".", (
            "Trivy must scan from the repo root or its SARIF paths will not "
            "resolve and code scanning will drop the findings"
        )


def test_trivy_is_given_variable_values():
    """
    Without values Trivy cannot evaluate the root modules, and any check reading
    a variable-derived attribute is evaluated against an unknown.
    """
    workflow = load("security-scan.yml")
    steps = workflow["jobs"]["iac"]["steps"]

    for step in [s for s in steps if "trivy-action" in str(s.get("uses", ""))]:
        tf_vars = step["with"].get("tf-vars")
        assert tf_vars, "Trivy step passes no tf-vars file"
        assert (REPO_ROOT / tf_vars).is_file(), f"tf-vars file missing: {tf_vars}"


def test_scan_tfvars_covers_every_required_variable():
    """
    A required variable absent from the scan file puts the warning back and
    reopens the blind spot, so this tracks the stacks as they change.
    """
    text = (TERRAFORM_DIR / "scan.tfvars").read_text()
    defined = set(re.findall(r"^\s*([a-z0-9_]+)\s*=", text, re.M))

    missing = (required_variables("gcp") | required_variables("aws")) - defined
    assert not missing, f"terraform/scan.tfvars is missing values for: {sorted(missing)}"


def test_scan_tfvars_is_committed():
    """
    Regression test. .gitignore carries `*.tfvars`, which matched this file and
    kept it out of the commit — the same shape of bug as the `*-backup` rule
    that hid functions/db-backup. CI would then hand Trivy a path that does not
    exist in the checkout. It is exempted by an explicit negation.
    """
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "terraform/scan.tfvars"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        "terraform/scan.tfvars is excluded by .gitignore; CI will not see it"
    )


def test_scan_tfvars_is_not_auto_loaded_by_terraform():
    """
    Terraform auto-loads terraform.tfvars and *.auto.tfvars. If the scan file
    ever takes one of those names, its placeholders would silently become the
    values a real plan uses.
    """
    for path in TERRAFORM_DIR.rglob("*.tfvars"):
        assert path.name != "terraform.tfvars", f"auto-loaded by terraform: {path}"
        assert not path.name.endswith(".auto.tfvars"), (
            f"auto-loaded by terraform: {path}"
        )
