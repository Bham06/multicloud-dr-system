# Multi-Cloud Disaster Recovery (GCP primary + AWS secondary)

![Badge](https://img.shields.io/badge/status-draft-yellow)
![Badge](https://img.shields.io/badge/terraform-%3E=1.0-blue)
![Badge](https://img.shields.io/badge/python-3.11-brightgreen)

Table of Contents
- [Overview](#overview)
- [Architecture Overview](#architecture-overview)
- [Components](#components)
- [Detailed API Reference](#detailed-api-reference)
  - [auto_failover Cloud Function](#auto_failover-cloud-function)
  - [Health Check Contract](#health-check-contract)
- [Usage Examples](#usage-examples)
  - [Prerequisites](#prerequisites)
  - [Terraform deploy (quickstart)](#terraform-deploy-quickstart)
  - [Deploy/Update Cloud Functions (source archive)](#deployupdate-cloud-functions-source-archive)
  - [Invoke the auto_failover function manually](#invoke-the-auto_failover-function-manually)
  - [CLI script usage (GCP / AWS)](#cli-script-usage-gcp--aws)
  - [Database restore script (EC2 / RDS)](#database-restore-script-ec2--rds)
- [Configuration Options](#configuration-options)
  - [Terraform variables (summary)](#terraform-variables-summary)
  - [Cloud Function environment variables](#cloud-function-environment-variables)
  - [Key outputs](#key-outputs)
- [Permissions & IAM Roles](#permissions--iam-roles)
- [Operational Notes & Limitations](#operational-notes--limitations)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License](#license)

Overview
--------
This repository implements a multi-cloud disaster recovery pattern where Google Cloud Platform (GCP) hosts the primary application and an AWS instance (exposed via an Internet NEG) serves as a warm/cold secondary. An automated failover Cloud Function monitors both backends and updates the GCP URL map to route traffic between GCP and AWS backends.

Architecture Overview
---------------------
Textual diagram:

GCP Load Balancer (Global IP) --> URL Map (default -> GCP backend) --> GCP Instance Group (primary)
                              \
                               --> Internet NEG (FQDN pointing to AWS Elastic IP) --> AWS EC2 (secondary)

Auxiliary components:
- Cloud Functions:
  - auto-failover: health monitoring & URL Map updates (failover/failback)
  - gcs-to-s3 sync: replicates GCS objects to S3
  - db-backup: creates DB backups to GCS (and optionally replicates to S3)
- Cloud Scheduler: triggers auto-failover and db-backup functions periodically.
- Secret Manager: stores AWS credentials & DB password.
- Cloud Monitoring & Alerting: notifies via email on failover events or function errors.

Components
----------
- Terraform modules / resources:
  - network.tf: VPC, subnets, and firewalls
  - compute.tf: VM, instance group, NAT, router
  - loadbalancer.tf: health checks, backend services (GCP & AWS Internet NEG), URL map, forwarding rules, managed SSL
  - database.tf: Cloud SQL (Postgres), private IP setup
  - storage.tf: GCS bucket for backups and function source
  - data.tf: Secret Manager secrets, Cloud Functions packaging & deployments
  - outputs.tf, providers.tf, variables.tf
- Python code:
  - functions/auto-failover/main.py — auto_failover(request) function (health check and URL map update logic)
- CLI scripts (examples):
  - deployment-gcp.sh, deployment-aws.sh — helper scripts to start/stop instances (scripts contain issues; see Usage section)
  - restore-db.sh — EC2 script to fetch backups from S3 and restore to RDS

Detailed API Reference
----------------------

auto_failover Cloud Function
- Entry point: auto_failover(request)
- Trigger: HTTP (invoked by Cloud Scheduler via OIDC token); Cloud Scheduler sets http_target -> URI
- Location: deployed via Terraform (see loadbalancer.tf)
- Runtime: Python 3.11 (as packaged in Terraform)

Environment variables (used by the function)
- PROJECT_ID: GCP project id
- GCP_BACKEND_SERVICE: name of the GCP backend service (e.g., dr-backend-gcp-primary)
- AWS_BACKEND_SERVICE: name of the AWS backend service (e.g., dr-backend-aws-secondary)
- URL_MAP_NAME: name of the URL map to update (e.g., dr-url-map)
- GCP_HEALTH_CHECK_URL: e.g., http://<gcp-internal-ip>/health (expects JSON {"status":"healthy"})
- AWS_HEALTH_CHECK_URL: e.g., http://<aws-elastic-ip>/health (expects JSON {"status":"healthy"})

Behavior / Decision logic
- The function maintains ephemeral state in /tmp/failover_state.json with keys:
  - active_backend: "gcp" or "aws"
  - last_change: ISO timestamp of last change
- On each invocation:
  - It reads the local state file (if present) and verifies the URL map's actual backend to keep state in sync.
  - Calls both health endpoints (GCP_HEALTH_CHECK_URL and AWS_HEALTH_CHECK_URL).
  - If active backend is unhealthy and the other is healthy, it updates the URL map to point to the healthy backend.
  - Logs events (start/completion/errors) to Cloud Logging and emits structured failover/failback events.
  - Returns JSON with status, action taken, active_backend, health booleans, and timestamp.

Example response (successful run)
```json
{
  "status": "success",
  "action": "failover_to_aws",
  "active_backend": "aws",
  "gcp_healthy": false,
  "aws_healthy": true,
  "timestamp": "2026-01-28T12:34:56.789012"
}
```

Return value notes:
- The function returns a Python dict which is serialized as JSON by the Cloud Functions runtime.
- The function writes detailed operational logs to Cloud Logging; use `gcloud functions logs read auto-failover-function` or the Cloud Console Logs Viewer.

Health Check Contract
- The function expects each backend health endpoint to return:
  - HTTP 200 with JSON payload: {"status": "healthy"} when healthy.
- Any non-200 code or JSON with different value => considered unhealthy.
- Timeout: requests.get timeout in code is 5 seconds per backend check.

Usage Examples
--------------

Prerequisites
- Terraform >= 1.0
- gcloud CLI (authenticated)
- gcloud project with billing enabled and required APIs enabled:
  - compute.googleapis.com, cloudfunctions.googleapis.com, cloudscheduler.googleapis.com, secretmanager.googleapis.com, sqladmin.googleapis.com, monitoring.googleapis.com, storage.googleapis.com
- Service account / user with privileges to manage IAM, compute, Cloud Functions, and Secret Manager.
- AWS account credentials for the Internet NEG target (Elastic IP) and S3 bucket if using cross-cloud sync/restore.

Terraform deploy (quickstart)
1. Initialize Terraform
```bash
terraform init
```

2. Review and set variables. You can provide them via:
- terraform.tfvars
- CLI flags: -var 'project_id=...'
- Environment variables (TF_VAR_project_id)

Required variables (minimum):
- project_id
- region
- zone
- db_password
- aws_access_key_id
- aws_secret_access_key
- aws_eip
- alert_email

Example apply:
```bash
terraform plan -out=plan.tfplan \
  -var="project_id=your-project" \
  -var="region=us-central1" \
  -var="zone=us-central1-a" \
  -var="db_password=YOUR_DB_PASS" \
  -var="aws_access_key_id=AKIA..." \
  -var="aws_secret_access_key=..." \
  -var="aws_eip=203.0.113.12" \
  -var="alert_email=ops@example.com"

terraform apply plan.tfplan
```

Note: The apply will:
- Create networking, compute instance(s), Cloud SQL, storage buckets, backend services, URL map, managed certificate, and Cloud Functions.
- Upload function source archives (Terraform archives directories under /functions/*).

Deploy/Update Cloud Functions (source archive)
- Terraform already packages function source via data.archive_file and uploads to GCS. If you modify any function code under `functions/`, re-run:
```bash
terraform apply -target=google_storage_bucket_object.sync_function_code
# or simply
terraform apply
```
- For local testing (invoking function code), you can use the `functions` directory and run the Python code locally (requires mocking of GCP clients) or use `gcloud` to deploy a one-off function for testing.

Invoke the auto_failover function manually
- The Cloud Scheduler invokes the function automatically every minute. To run a manual test:
1. Acquire an identity token (for authenticated Cloud Functions):
```bash
FUNCTION_URL=$(terraform output -raw auto_failover_function_url)
TOKEN=$(gcloud auth print-identity-token)
curl -X POST -H "Authorization: Bearer $TOKEN" "$FUNCTION_URL"
```
2. You should receive JSON like the example in the API reference. Check Cloud Logs for details:
```bash
gcloud functions logs read auto-failover-function --limit=50 --project=your-project
```

CLI script usage (GCP / AWS)
- Note: The repository contains example scripts that require fixes/adjustments prior to use. Below are corrected usage patterns.

GCP helper (corrected example)
- The repository's deployment-gcp.sh has placeholders. Example corrected script to start/stop a VM:
```bash
#!/bin/bash
VM_NAME="$1"
ZONE="$2"
ACTION="$3"

if [[ -z "$VM_NAME" || -z "$ZONE" || ( "$ACTION" != "start" && "$ACTION" != "stop" ) ]]; then
  echo "Usage: $0 <vm-name> <zone> <start|stop>"
  exit 1
fi

if [ "$ACTION" = "start" ]; then
  gcloud compute instances start "$VM_NAME" --zone="$ZONE"
else
  gcloud compute instances stop "$VM_NAME" --zone="$ZONE"
fi
```

AWS helper (corrected example)
- The repository's deployment-aws.sh has a conditional syntax error. Example corrected script:
```bash
#!/bin/bash
INSTANCE_ID="$1"
ACTION="$2"

if [[ -z "$INSTANCE_ID" || ( "$ACTION" != "start" && "$ACTION" != "stop" ) ]]; then
  echo "Usage: $0 <instance-id> <start|stop>"
  exit 1
fi

if [ "$ACTION" = "start" ]; then
  aws ec2 start-instances --instance-ids "$INSTANCE_ID"
else
  aws ec2 stop-instances --instance-ids "$INSTANCE_ID"
fi
```

Cloud function local deploy script notes (cloud-function-deploy.sh)
- The file in the repo contains syntactic placeholders. Use the `gcloud` or Terraform packaging shown above; for GCF gen1, use `gcloud functions deploy` with proper parameters. For this repo, Terraform manages GCF deployment and packaging.

Database restore script (EC2 / RDS)
- The provided `restore-db.sh` is intended to run on an EC2 instance that periodically checks S3 for the latest backup, downloads it, and restores it to RDS. It expects environment variables such as:
  - S3_BUCKET_NAME, RDS_ENDPOINT, RDS_PORT, RDS_DATABASE, RDS_USER, RDS_PASSWORD
- Ensure `pg_isready`, `psql` are installed and AWS CLI is configured.

Configuration Options
---------------------

Terraform variables (summary)
- project_id (string) — GCP Project ID (required)
- region (string) — GCP region (required)
- zone (string) — GCP zone (required)
- db_password (string, sensitive) — CloudSQL DB user password (required)
- aws_access_key_id (string, sensitive) — AWS access key id (required)
- aws_secret_access_key (string, sensitive) — AWS secret access key (required)
- aws_region (string) — AWS region (default: us-east-1)
- s3_bucket_name (string) — S3 bucket name for replication (default: dr-storage-secondary-6u1fs0vc)
- aws_eip (string) — AWS Elastic IP to map to Internet NEG (required)
- domain_name (string) — Domain name for SSL certificate (default: "")
- alert_email (string) — Email for monitoring alerts (required)

Cloud Function environment variables (auto_failover)
- PROJECT_ID
- GCP_BACKEND_SERVICE
- AWS_BACKEND_SERVICE
- URL_MAP_NAME
- GCP_HEALTH_CHECK_URL
- AWS_HEALTH_CHECK_URL

Other function env vars (db_backup, gcs_to_s3)
- DB_CONNECTION_NAME, DB_USER, DB_NAME, GCS_BACKUP_BUCKET, S3_BUCKET_NAME, etc. (see data.tf and loadbalancer.tf for full set)

Key outputs (select from outputs.tf)
- load_balancer_ip — external IP of load balancer
- access_url_https — HTTPS URL (based on nip.io domain if domain_name empty)
- gcs_primary_bucket — primary GCS bucket for backups and function code
- s3_secondary_bucket — S3 bucket name for replicated data
- auto_failover_function_url — HTTP trigger URL for auto-failover function

Permissions & IAM Roles
-----------------------
The Terraform configuration requests and assigns several IAM roles. Confirm the following roles are granted to service accounts used by Cloud Functions and Terraform:
- roles/compute.loadBalancerAdmin (auto_failover function service account)
- roles/logging.logWriter (auto_failover)
- roles/secretmanager.secretAccessor (functions that read secrets)
- roles/cloudsql.client (VM & backup functions)
- roles/storage.objectViewer / objectAdmin (functions needing bucket access)
- roles/run.invoker (to allow Cloud Scheduler to invoke functions)
- Additionally, ensure the user running Terraform has Owner or a combination of necessary roles to create the resources.

Operational Notes & Limitations
------------------------------
- State persistence: The auto_failover function stores state at /tmp/failover_state.json inside the function's runtime environment. This is ephemeral — when the function instance restarts or scales, the state can be lost. Recommended fix: use a persistent store (Cloud Storage, Firestore, or Memorystore) for durable state.
- Health-check contract: Ensure backends expose /health returning JSON {"status": "healthy"} on HTTP 200. Health check path and behavior must match terraform health check configuration and the function's expectations.
- URL map update latency: Updating a URL map and waiting for the Compute operation to finish can take several seconds. The function waits up to 60 seconds for the update operation; if your environment requires more time, adjust timeout handling.
- Certificate provisioning: Managed SSL certs (nip.io domain or your domain) may take time to provision — verify DNS resolution and domain ownership if you set domain_name.

Troubleshooting
---------------
Common issues and resolutions:

- Missing environment variables (PROJECT_ID, GCP/AWS backend names, health check URLs)
  - Symptom: function logs show errors "Error getting current backend" or health check URL missing/None.
  - Fix: Confirm environment variables are set in the Cloud Function service_config (Terraform sets these by default from variables in loadbalancer.tf). Use `terraform output` to inspect values.

- Permission errors when updating URL map
  - Symptom: logs show 403 or "permission denied" when function tries to call Compute API.
  - Fix: Ensure the auto-failover service account has roles/compute.loadBalancerAdmin and roles/run.invoker (for scheduler invocation), and that the Cloud Function uses that service account.

- Both backends unhealthy
  - Symptom: action both_unhealthy, logs show "CRITICAL: Both backends unhealthy!"
  - Fix: Verify actual backend services (instance health, EC2 / web server), check health endpoint implementation, and confirm firewall rules allow health check traffic.

- State mismatch between URL map and function state
  - Symptom: Function logs: "State mismatch detected. State file: X, URL map: Y"
  - Cause: Function instance restart or manual URL map change outside the function.
  - Fix: Ensure only the auto-failover function or authorized operators modify the URL map. Consider persisting state in a centralized store.

- Function timeouts or operation.result timeout
  - Symptom: Terraform or function logs indicate operation timed out waiting for Compute update.
  - Fix: Increase operation wait timeout in code (main.py, update_url_map_backend) or adjust function timeout and retry logic.

- Secrets handling
  - Symptom: Secrets not accessible by functions
  - Fix: Ensure service accounts are granted secretmanager.secretAccessor for the specific secrets. Use Secret Manager, not plaintext env vars.

- Terraform plan/apply errors (quota, API not enabled)
  - Fix: Enable required APIs and verify quotas; run `gcloud services enable <API>` for each required service.

Contributing
------------
Contributions welcome. Please:
- Open issues for bugs or documentation gaps.
- Create PRs with clear description and tests where applicable.
- If modifying function behavior, ensure unit tests and integration testing where possible.

Recommended improvements
- Persist the failover state to Cloud Storage or Firestore instead of /tmp.
- Harden health checks (include latency, error rates) and add backoff/retry for transient errors.
- Add automated tests for failover logic (unit tests that mock compute and requests).

License
-------
This repository does not include a license file. Add a LICENSE if you want to make the code open source. Suggested: Apache-2.0 or MIT.

Appendix — Example health endpoint
----------------------------------
A minimal HTTP health endpoint that satisfies the health-check contract:
```python
from flask import Flask, jsonify
app = Flask(__name__)

@app.route("/health")
def health():
    return jsonify({"status": "healthy"}), 200
```

Appendix — Useful commands
--------------------------
- View function logs:
```bash
gcloud functions logs read auto-failover-function --limit=100 --project=your-project
```
- Check backend services:
```bash
gcloud compute backend-services describe dr-multi-backend-service --global --format="table(backends[].group.basename(),backends[].capacityScaler)"
```
- Inspect function URL:
```bash
terraform output auto_failover_function_url
```
