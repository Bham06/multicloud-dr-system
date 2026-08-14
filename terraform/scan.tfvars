# Placeholder values for static analysis only — never used by a real plan or
# apply. Trivy cannot evaluate a root module whose variables have no values, so
# without this it emits "Variable values were not found" and any check that
# depends on a variable-derived attribute is evaluated against an unknown.
#
# Deliberately not named terraform.tfvars or *.auto.tfvars so that Terraform
# will not auto-load it. Only the Trivy job passes it, via --tf-vars.
#
# Every value here is fake. Nothing in this file is or should ever be a secret;
# if a real value is ever needed to make a check fire, the check belongs
# somewhere other than this scan.

# --- both stacks ---
db_password  = "placeholder-not-a-real-password"
github_owner = "example-org"
github_repo  = "example-repo"

# --- gcp ---
project_id            = "example-project"
region                = "us-east1"
zone                  = "us-east1-b"
aws_access_key_id     = "placeholder-access-key-id"
aws_secret_access_key = "placeholder-secret-access-key"
aws_eip               = "203.0.113.10"
auth_token            = "placeholder-slack-token"
aws_vpn_tunnel1_ip    = "203.0.113.11"
aws_vpn_tunnel2_ip    = "203.0.113.12"
shared_secret         = "placeholder-shared-secret"

# --- aws ---
ssh_public_key                = "ssh-rsa PLACEHOLDER scan@example"
gcp_vpn_gateway_interface0_ip = "198.51.100.10"
gcp_vpn_gateway_interface1_ip = "198.51.100.11"
vpn_shared_secret             = "placeholder-shared-secret"
