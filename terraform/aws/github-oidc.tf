# =============================================================================
# GitHub Actions — OIDC Identity Provider + IAM Role
#
# Allows GitHub Actions to call AWS APIs without storing access keys anywhere.
# The GitHub OIDC token is exchanged for short-lived STS credentials via
# AssumeRoleWithWebIdentity.
#
# Bootstrap note: this role must exist before OIDC can be used.
# On first deploy, authenticate with temporary access keys, apply this file,
# set AWS_GITHUB_ACTIONS_ROLE_ARN in GitHub secrets, then remove the keys.
# =============================================================================

resource "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"

  # AWS validates GitHub's OIDC endpoint against its root CA store rather than
  # this thumbprint for github.com, but the field is required by the API.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]

  client_id_list = ["sts.amazonaws.com"]
}

# IAM role assumed by GitHub Actions during Terraform runs
resource "aws_iam_role" "github_actions" {
  name        = "github-actions-terraform"
  description = "Assumed by GitHub Actions OIDC for Terraform operations"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
        Action    = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          }
          StringLike = {
            # Scoped to this repo; allows any branch so PR plans work.
            # Apply-only restriction (main branch) is enforced via the GitHub
            # 'production' environment protection rule, not here.
            "token.actions.githubusercontent.com:sub" = "repo:${var.github_owner}/${var.github_repo}:*"
          }
        }
      }
    ]
  })
}

# AdministratorAccess — Terraform needs to manage VPC, EC2, RDS, Secrets
# Manager, IAM, VPN, and more.  Tighten once the resource inventory is stable.
resource "aws_iam_role_policy_attachment" "github_actions_admin" {
  role       = aws_iam_role.github_actions.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

output "github_actions_role_arn" {
  description = "IAM role ARN for GitHub Actions — set as AWS_GITHUB_ACTIONS_ROLE_ARN in GitHub secrets"
  value       = aws_iam_role.github_actions.arn
}
