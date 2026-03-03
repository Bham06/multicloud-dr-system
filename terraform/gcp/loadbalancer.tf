# Global Stativ IP
resource "google_compute_global_address" "lb" {
  name = "dr-lb-ip"
}

# Domain for test using nip.io 
locals {
  lb_ip_dashes = replace(google_compute_global_address.lb.address, ".", "-")
  domain_name  = "${local.lb_ip_dashes}.nip.io"
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

# AWS Internet NEG
resource "google_compute_global_network_endpoint_group" "aws_secondary" {
  name                  = "dr-neg-aws-secondary"
  network_endpoint_type = "INTERNET_FQDN_PORT"
  default_port          = 80
}

resource "google_compute_global_network_endpoint" "aws" {
  global_network_endpoint_group = google_compute_global_network_endpoint_group.aws_secondary.id
  fqdn                          = "${replace(var.aws_eip, ".", "-")}.nip.io"
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

resource "google_project_iam_member" "auto_failover_logging" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.auto_failover.email}"
}

# Monitoring Alert Channel
resource "google_monitoring_notification_channel" "email" {
  display_name = "DR Failover Email Alerts"
  type         = "email"

  labels = {
    email_address = var.alert_email
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
      PROJECT_ID           = var.project_id
      GCP_BACKEND_SERVICE  = google_compute_backend_service.gcp_primary.name
      AWS_BACKEND_SERVICE  = google_compute_backend_service.aws_secondary.name
      URL_MAP_NAME         = google_compute_url_map.lb.name
      GCP_HEALTH_CHECK_URL = "http://${google_compute_instance.primary.network_interface[0].network_ip}/health"
      AWS_HEALTH_CHECK_URL = "http://${var.aws_eip}/health"
    }

    service_account_email = google_service_account.auto_failover.email
  }
}

# Cloud Scheduler for Auto-Failover
resource "google_cloud_scheduler_job" "auto_failover" {
  name             = "auto-failover-scheduler"
  description      = "Monitor health and trigger automated failover"
  schedule         = "*/1 * * * *"
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

# Alert Policy - GCP unhealthy
resource "google_monitoring_alert_policy" "gcp_backend_unhealthy" {
  display_name = "GCP Bakend Unhealthy - Auto Failover"
  combiner     = "OR"

  conditions {
    display_name = "GCP Instance down"

    condition_threshold {
      filter          = "resource.type=\"gce_instance\" AND metric.type=\"compute.googleapis.com/instance/uptime\""
      duration        = "60s"
      comparison      = "COMPARISON_LT"
      threshold_value = 1

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_RATE"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.id]

  alert_strategy {
    auto_close = "1800s"
  }
}

# Alert Policy - Auto Failover Function Errors
resource "google_monitoring_alert_policy" "auto_failover_errors" {
  display_name = "Auto-Failover Function Errors"
  combiner     = "OR"

  conditions {
    display_name = "Function execution errors"

    condition_threshold {
      filter          = "resource.type=\"cloud_function\" AND resource.labels.function_name=\"auto-failover-function\" AND metric.type=\"cloudfunctions.googleapis.com/function/execution_count\" AND metric.labels.status!=\"ok\""
      duration        = "60s"
      comparison      = "COMPARISON_GT"
      threshold_value = 0

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_RATE"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.id]
}
