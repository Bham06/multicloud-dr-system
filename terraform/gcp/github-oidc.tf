# =============================================================================
# GitHub Actions — Workload Identity Federation
#
# Allows GitHub Actions to authenticate to GCP without storing a service
# account JSON key anywhere.  The GitHub OIDC token is exchanged for a
# short-lived GCP access token that expires when the job finishes.
#
# Bootstrap note: this pool/provider must exist before OIDC can be used.
# On first deploy, authenticate with a temporary SA key, apply this file,
# then rotate to OIDC and delete the key.
# =============================================================================

data "google_project" "current" {}

# Dedicated service account for GitHub Actions Terraform runs.
# Kept separate from runtime service accounts so its permissions can be
# audited and revoked independently.
resource "google_service_account" "github_actions" {
  account_id   = "github-actions-terraform"
  display_name = "GitHub Actions Terraform"
}

# roles/editor covers most Terraform-managed resources.
# roles/resourcemanager.projectIamAdmin is needed on top because editor
# explicitly excludes setting project-level IAM bindings.
resource "google_project_iam_member" "github_actions_editor" {
  project = var.project_id
  role    = "roles/editor"
  member  = "serviceAccount:${google_service_account.github_actions.email}"
}

resource "google_project_iam_member" "github_actions_iam_admin" {
  project = var.project_id
  role    = "roles/resourcemanager.projectIamAdmin"
  member  = "serviceAccount:${google_service_account.github_actions.email}"
}

# Workload Identity Pool — logical container for external identity providers
resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github-actions"
  display_name              = "GitHub Actions"
}

# Provider — maps GitHub OIDC token claims to GCP principal attributes.
# attribute.repository lets the SA binding below scope to this repo only.
resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.actor"      = "assertion.actor"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }

  # Hard-scope to this repository so tokens from other repos in the same
  # GitHub org cannot impersonate this SA.
  attribute_condition = "assertion.repository == '${var.github_owner}/${var.github_repo}'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

# Allow any GitHub Actions job in this repo to impersonate the SA.
# Branch restriction (main-only for apply) is enforced by the GitHub
# environment protection rule on the 'production' environment, not here.
resource "google_service_account_iam_member" "github_actions_wif" {
  service_account_id = google_service_account.github_actions.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_owner}/${var.github_repo}"
}

output "workload_identity_provider" {
  description = "WIF provider resource name — set as GCP_WORKLOAD_IDENTITY_PROVIDER in GitHub secrets"
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "github_actions_service_account" {
  description = "GitHub Actions SA email — set as GCP_SERVICE_ACCOUNT in GitHub secrets"
  value       = google_service_account.github_actions.email
}
