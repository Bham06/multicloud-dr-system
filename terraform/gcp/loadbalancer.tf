# Global Static IP
resource "google_compute_global_address" "lb" {
  name = "dr-lb-ip"
}

# A Google-managed certificate cannot be issued for a bare IP, so unlike the
# NEG this genuinely needs a domain. Set var.domain_name to one you control;
# the nip.io fallback exists so dev and test environments still get valid TLS
# without owning a domain.
locals {
  lb_ip_dashes = replace(google_compute_global_address.lb.address, ".", "-")
  domain_name  = var.domain_name != "" ? var.domain_name : "${local.lb_ip_dashes}.nip.io"
}

# Health Check
resource "google_compute_health_check" "app" {
  name                = "dr-app-health-check"
  check_interval_sec  = 5
  timeout_sec         = 3
  healthy_threshold   = 2
  unhealthy_threshold = 2

  http_health_check {
    port         = 80
    request_path = "/health"
  }

  log_config {
    enable = true
  }
}

# GCP Primary Backend Service
resource "google_compute_backend_service" "gcp_primary" {
  name                  = "dr-backend-gcp-primary"
  protocol              = "HTTP"
  port_name             = "http"
  timeout_sec           = 30
  health_checks         = [google_compute_health_check.app.id]
  load_balancing_scheme = "EXTERNAL_MANAGED"

  backend {
    group           = google_compute_instance_group.gcp.id
    balancing_mode  = "UTILIZATION"
    capacity_scaler = 1.0
    max_utilization = 0.8
  }

  log_config {
    enable      = true
    sample_rate = 1.0
  }

  connection_draining_timeout_sec = 60
}

# AWS Internet NEG.
# INTERNET_IP_PORT points at the Elastic IP directly, so the failover data path
# resolves no DNS at all. It previously used INTERNET_FQDN_PORT against a nip.io
# name, which made reaching the secondary depend on a third-party DNS service
# being up at exactly the moment the primary was not.
resource "google_compute_global_network_endpoint_group" "aws_secondary" {
  name                  = "dr-neg-aws-secondary"
  network_endpoint_type = "INTERNET_IP_PORT"
  default_port          = 80
}

resource "google_compute_global_network_endpoint" "aws" {
  global_network_endpoint_group = google_compute_global_network_endpoint_group.aws_secondary.id
  ip_address                    = var.aws_eip
  port                          = 80
}

# AWS Secondary Backend Service
resource "google_compute_backend_service" "aws_secondary" {
  name                  = "dr-backend-aws-secondary"
  protocol              = "HTTP"
  timeout_sec           = 30
  load_balancing_scheme = "EXTERNAL_MANAGED"

  backend {
    group           = google_compute_global_network_endpoint_group.aws_secondary.id
    capacity_scaler = 1.0
  }

  log_config {
    enable      = true
    sample_rate = 1.0
  }

  connection_draining_timeout_sec = 60

  # Outlier detection to compensate for lack of health checks
  outlier_detection {
    consecutive_errors                    = 3
    consecutive_gateway_failure           = 3
    enforcing_consecutive_errors          = 100
    enforcing_consecutive_gateway_failure = 100
    max_ejection_percent                  = 100

    # Success rate detection 
    success_rate_minimum_hosts  = 5
    success_rate_request_volume = 100
    success_rate_stdev_factor   = 1900
  }
}

# Managed SSL Certificates
resource "google_compute_managed_ssl_certificate" "lb" {
  name = "dr-lb-cert"

  managed {
    domains = [local.domain_name]
  }

  lifecycle {
    create_before_destroy = true
  }
}

# URL Map
resource "google_compute_url_map" "lb" {
  name            = "dr-url-map"
  default_service = google_compute_backend_service.gcp_primary.id
}

# URL Map for HTTPS Redirect
resource "google_compute_url_map" "https_redirect" {
  name = "dr-https-redirect"

  default_url_redirect {
    https_redirect         = true
    redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
    strip_query            = false
  }
}

# HTTP Target Proxy
resource "google_compute_target_http_proxy" "lb" {
  name    = "dr-http-proxy"
  url_map = google_compute_url_map.https_redirect.id
}

# HTTPS Target Proxy
resource "google_compute_target_https_proxy" "lb" {
  name             = "dr-https-proxy"
  url_map          = google_compute_url_map.lb.id
  ssl_certificates = [google_compute_managed_ssl_certificate.lb.id]
}

# HTTP Forwarding Rule
resource "google_compute_global_forwarding_rule" "http" {
  name                  = "dr-forwarding-rule-http"
  ip_protocol           = "TCP"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  port_range            = 80
  target                = google_compute_target_http_proxy.lb.id
  ip_address            = google_compute_global_address.lb.id
}

# HTTPS Forwarding Rule
resource "google_compute_global_forwarding_rule" "https" {
  name                  = "dr-forwarding-rule-https"
  ip_protocol           = "TCP"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  port_range            = "443"
  target                = google_compute_target_https_proxy.lb.id
  ip_address            = google_compute_global_address.lb.id
}

# ================================
# Auto-Failover Automation
# ================================

# Service Account for Auto-Failover
resource "google_service_account" "auto_failover" {
  account_id   = "auto-failover-function"
  display_name = "Automated Failover Function"
}

# IAM Permissions for Auto-Failover
resource "google_project_iam_member" "auto_failover_compute_admin" {
  project = var.project_id
  role    = "roles/compute.loadBalancerAdmin"
  member  = "serviceAccount:${google_service_account.auto_failover.email}"
}

# Read-only access so the function can call backendServices.getHealth
resource "google_project_iam_member" "auto_failover_compute_viewer" {
  project = var.project_id
  role    = "roles/compute.viewer"
  member  = "serviceAccount:${google_service_account.auto_failover.email}"
}

resource "google_project_iam_member" "auto_failover_logging" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.auto_failover.email}"
}

# Grant firestore access to failover function
resource "google_project_iam_member" "auto_failover_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.auto_failover.email}"
}

# Monitoring Alert Channel
resource "google_monitoring_notification_channel" "slack" {
  display_name = "Slack Alerts"
  type         = "slack"

  labels = {
    auth_token   = var.auth_token
    channel_name = "#fyp-monitor"
    team         = var.slack_team
  }
}

# Archive Auto-Failover Source 
data "archive_file" "auto_failover_source" {
  type        = "zip"
  source_dir  = "${path.module}/functions/auto-failover"
  output_path = "${path.module}/.terraform/functions/auto-failover.zip"
}

# Upload Auto-Failover Code
resource "google_storage_bucket_object" "auto_failover_code" {
  name   = "functions/auto-failover-${data.archive_file.auto_failover_source.output_md5}.zip"
  bucket = google_storage_bucket.primary.name
  source = data.archive_file.auto_failover_source.output_path
}

# Auto-Failover Cloud Function
resource "google_cloudfunctions2_function" "auto_failover" {
  name     = "auto-failover-function"
  location = var.region

  build_config {
    runtime     = "python311"
    entry_point = "auto_failover"

    source {
      storage_source {
        bucket = google_storage_bucket.primary.name
        object = google_storage_bucket_object.auto_failover_code.name
      }
    }
  }

  service_config {
    max_instance_count = 1
    available_memory   = "256M"
    timeout_seconds    = 60

    environment_variables = {
      PROJECT_ID          = var.project_id
      GCP_BACKEND_SERVICE = google_compute_backend_service.gcp_primary.name
      AWS_BACKEND_SERVICE = google_compute_backend_service.aws_secondary.name
      URL_MAP_NAME        = google_compute_url_map.lb.name

      # GCP health is read from the load balancer's health checkers via
      # backendServices.getHealth, not probed over HTTP. Probing the LB address
      # would measure whichever backend is currently active rather than GCP.
      GCP_INSTANCE_GROUP = google_compute_instance_group.gcp.self_link

      # AWS sits behind an internet NEG that GCP cannot health check, so it is
      # probed directly at the Elastic IP.
      AWS_HEALTH_CHECK_URL = "http://${var.aws_eip}/health"

      # Hysteresis, tunable without a code deploy. Recoveries are deliberately
      # higher than failures: at a 5 minute interval these mean 15 minutes of
      # sustained failure before leaving GCP, and 30 minutes of sustained health
      # before returning to it.
      REQUIRED_FAILURES   = tostring(var.required_failures)
      REQUIRED_RECOVERIES = tostring(var.required_recoveries)
    }

    service_account_email = google_service_account.auto_failover.email
  }
}

# Cloud Scheduler for Auto-Failover
resource "google_cloud_scheduler_job" "auto_failover" {
  name             = "auto-failover-scheduler"
  description      = "Monitor health and trigger automated failover"
  schedule         = "*/5 * * * *"
  time_zone        = "UTC"
  attempt_deadline = "60s"
  region           = var.region

  http_target {
    http_method = "POST"
    uri         = google_cloudfunctions2_function.auto_failover.service_config[0].uri

    oidc_token {
      service_account_email = google_service_account.auto_failover.email
    }
  }

  retry_config {
    retry_count = 1
  }
}

# IAM for Cloud Scheduler to invoke function
resource "google_cloud_run_service_iam_member" "auto_failover_invoker" {
  location = google_cloudfunctions2_function.auto_failover.location
  service  = google_cloudfunctions2_function.auto_failover.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.auto_failover.email}"
}

# Alert Policy - GCP Unhealthy
resource "google_monitoring_alert_policy" "failover_event" {
  display_name = "DR Failover to AWS Occurred"
  combiner     = "OR"

  conditions {
    display_name = "Failover event detected"

    condition_matched_log {
      filter = <<-EOT
        resource.type="cloud_run_revision"
        resource.labels.service_name="auto-failover-function"
        jsonPayload.event_type="failover"
      EOT

      label_extractors = {
        "rto"  = "EXTRACT(jsonPayload.details.rto_seconds)"
        "from" = "EXTRACT(jsonPayload.details.from)"
        "to"   = "EXTRACT(jsonPayload.details.to)"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.slack.id]

  alert_strategy {
    auto_close = "3600s" # Auto-close after 1 hour

    # Max 1 alert per hour
    notification_rate_limit {
      period = "3600s"
    }
  }

  documentation {
    content = <<-EOT
      DR FAILOVER EVENT
      
      The system has automatically failed over from GCP to AWS.
      
      NEXT STEPS:
      1. Verify AWS backend: curl http://${var.aws_eip}/health
      2. Check GCP issues: gcloud compute instances describe dr-app-primary
      3. Review logs: gcloud functions logs read auto-failover-function --gen2
      
      System will automatically failback when GCP recovers.
      
      RTO: {{rto}} seconds
      Direction: {{from}} → {{to}}
    EOT
  }
}

# Alert Policy - Failback Event 
resource "google_monitoring_alert_policy" "failback_event" {
  display_name = "DR Failback to GCP Occurred"
  combiner     = "OR"

  conditions {
    display_name = "Failback event detected"

    condition_matched_log {
      filter = <<-EOT
        resource.type="cloud_run_revision"
        resource.labels.service_name="auto-failover-function"
        jsonPayload.event_type="failback"
      EOT

      label_extractors = {
        "rto"  = "EXTRACT(jsonPayload.details.rto_seconds)"
        "from" = "EXTRACT(jsonPayload.details.from)"
        "to"   = "EXTRACT(jsonPayload.details.to)"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.slack.id]

  alert_strategy {
    auto_close = "3600s"

    notification_rate_limit {
      period = "3600s"
    }
  }

  documentation {
    content = <<-EOT
      DR FAILBACK EVENT
      
      The system has automatically failed back from AWS to GCP.
      GCP backend has recovered and is now serving traffic.
      
      RTO: {{rto}} seconds
      Direction: {{from}} → {{to}}
    EOT
  }
}

# Alert Policy : Both Backends Unhealthy (Critical)
resource "google_monitoring_alert_policy" "both_backends_unhealthy" {
  display_name = "CRITICAL: Both DR Backends Unhealthy"
  combiner     = "OR"

  conditions {
    display_name = "Both backends failed"

    condition_matched_log {
      filter = <<-EOT
        resource.type="cloud_run_revision"
        resource.labels.service_name="auto-failover-function"
        jsonPayload.event_type="both_unhealthy"
        severity="ERROR"
      EOT
    }
  }

  notification_channels = [google_monitoring_notification_channel.slack.id]

  alert_strategy {
    auto_close = "1800s" # Auto-close after 30 minutes to accomodate for restrictions

    notification_rate_limit {
      period = "1800s" # Max 1 alert per 30 minutes
    }
  }

  severity = "CRITICAL"

  documentation {
    content = <<-EOT
      CRITICAL: BOTH DR BACKENDS UNHEALTHY
      
      This indicates a complete service outage.
      
      IMMEDIATE ACTIONS:
      1. Check GCP: gcloud compute instances describe dr-app-primary --zone=${var.zone}
      2. Check AWS: aws ec2 describe-instances --instance-ids i-XXXXX
      3. Test LB: curl http://${google_compute_global_address.lb.address}/health
      4. Review function logs
      5. Consider manual intervention
    EOT
  }
}

# Alert Policy - Function Execution Failures
resource "google_monitoring_alert_policy" "auto_failover_execution_failure" {
  display_name = "Auto-Failover Function Errors"
  combiner     = "OR"

  conditions {
    display_name = "Function execution failures"

    condition_threshold {
      filter = <<-EOT
        resource.type="cloud_run_revision"
        resource.labels.service_name="auto-failover-function"
        metric.type="run.googleapis.com/request_count"
        metric.labels.response_code_class="5xx"
      EOT

      # Must fail for 5 minutes (not just 1 error)
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 3

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_RATE"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.slack.id]

  alert_strategy {
    auto_close = "1800s"

    # notification_rate_limit {
    #   period = "3600s" # Max 1 alert per hour
    # }
  }

  documentation {
    content = <<-EOT
      Auto-Failover Function Experiencing Errors
      
      The failover automation may not be working correctly.
      
      ACTIONS:
      1. Check logs: gcloud functions logs read auto-failover-function --gen2 --limit=50
      2. Verify IAM permissions
      3. Check URL map accessibility
      4. Verify Firestore state
      
      Manual failover may be required.
    EOT
  }
}
