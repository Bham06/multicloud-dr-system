# ========================================
# External Monitoring - GCP Uptime Checks
# ========================================

# Uptime Check Load Balancer
resource "google_monitoring_uptime_check_config" "load_balancer" {
  display_name = "DR Load Balancer Health"
  timeout      = "10s"
  period       = "60s"

  http_check {
    path           = "/health"
    port           = "80"
    request_method = "GET"

    accepted_response_status_codes {
      status_class = "STATUS_CLASS_2XX"
    }
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = google_compute_global_address.lb.address
    }
  }

  # Check from multiple locations globally
  checker_type = "STATIC_IP_CHECKERS"
}

# Uptime Checks: AWS Backend Direct
resource "google_monitoring_uptime_check_config" "aws_backend" {
  display_name = "DR AWS Backend Direct Health"
  timeout      = "10s"
  period       = "60s"

  http_check {
    path = "/health"
    port = "80"

    accepted_response_status_codes {
      status_class = "STATUS_CLASS_2XX"
    }
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = var.aws_eip
    }
  }

  checker_type = "STATIC_IP_CHECKERS"
}

# Alert Policy: Load Balancer Down
resource "google_monitoring_alert_policy" "load_balancer_down" {
  display_name = "CRITICAL: DR Load Balancer Unreachable"
  combiner     = "OR"

  conditions {
    display_name = "Load balancer failing uptime checks"

    condition_threshold {
      filter = <<-EOT
        resource.type = "uptime_url"
        metric.type = "monitoring.googleapis.com/uptime_check/check_passed"
        metadata.user_labels.host = "${google_compute_global_address.lb.address}"
      EOT

      duration        = "1800s"
      comparison      = "COMPARISON_LT"
      threshold_value = 0.5 # Less than 50% checks passing

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_FRACTION_TRUE"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.slack.id]

  alert_strategy {
    auto_close = "1800s"
  }

  severity = "WARNING"

  documentation {
    content = <<-EOT
      AWS secondary backend is unreachable from external monitoring.

      This is expected if:
      - GCP is primary and healthy (normal operation)
      - Scheduled maintenance on AWS

      This requires attention if:
      - System has failed over to AWS
      - You're testing AWS functionality

      Next steps:
      1. Check if failover is active: gcloud url-maps describe dr-url-map --format="get(defaultService)"
      2. If using AWS, this is CRITICAL
      3. If using GCP, this is informational
    EOT 
  }
}

# =======================================
# Database Replication Monitoring System
# =======================================

# CloudSQL export success (consider doing metric lag with custom metric form RDS)
resource "google_monitoring_alert_policy" "db_backup_failed" {
  display_name = "Database Backup Export Failed"
  combiner     = "OR"

  conditions {
    display_name = "Cloud SQL export operations failing"

    condition_threshold {
      filter = <<-EOT
        resource.type = "cloudsql_database"
        resource.labels.database_id = "${var.project_id}:${google_sql_database_instance.primary.name}"
        metric.type = "cloudsql.googleapis.com/database/up"
      EOT

      duration        = "300s"
      comparison      = "COMPARISON_LT"
      threshold_value = 1

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.slack.id]

  alert_strategy {
    auto_close = "3600s"
  }

  documentation {
    content = <<-EOT
      Cloud SQL database is down or unreachable.

      Impact: Database replication to AWS will fail.

      Actions:
      1. Check Cloud SQL status: gcloud sql instance dr-primary-db
      2. Check recent operations: gcloud sql operations list --instance=dr-primary-db
      3. Check database logs: gcloud logging read "resource.type=cloudsql_database"
    EOT
  }
}

# Monitor VPN Tunnel Health
resource "google_monitoring_alert_policy" "vpn_tunnel_down" {
  count = var.use_vpn ? 1 : 0

  display_name = "VPN Tunnel to AWS Down"
  combiner     = "OR"

  conditions {
    display_name = "VPN Tunnel status not established"

    condition_threshold {
      filter = <<-EOT
        resource.type = "vpn_gateway"
        metric.type = "vpn.googleapis.com/tunnel_established"
      EOT

      duration        = "300s"
      comparison      = "COMPARISON_LT"
      threshold_value = 1

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.slack.id]

  alert_strategy {
    auto_close = "1800s"

    notification_rate_limit {
      period = "1800s"
    }
  }

  severity = "CRITICAL"

  documentation {
    content = <<-EOT
      VPN Tunnel to AWS is down - database replication will fail!

      Impact: Cloud SQL cannot reach RDS for logical replication.

      Immediate actions:
      1. Check tunnel status: gcloud compute vpn-tunnels describe tunnel-to-aws --region=us-east1
      2. Check BGP: gcloud compute routers get-status gcp-vpn-router --region=us-east1
      3. Check AWS side: aws ec2 describe-vpn-connections --vpn-connection-ids <ID>
      4. Verify pre-shared key matches on both sides
    EOT
  }
}

# ================================================
# Cloud Monitoring Dashboard
# ================================================

resource "google_monitoring_dashboard" "dr_dashboard" {
  dashboard_json = jsonencode({
    displayName = "DR System Health"

    mosaicLayout = {
      columns = 12

      tiles = [
        # Tile 1: Backend Health
        {
          width  = 6
          height = 4
          widget = {
            title = "Backend Health Status"
            xyChart = {
              dataSets = [{
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "resource.type=\"gce_backend_service\" AND metric.type=\"loadbalancing.googleapis.com/https/backend_request_count\""

                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_RATE"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = ["resource.backend_service_name"]
                    }
                  }
                }
                plotType = "LINE"
              }]

              yAxis = {
                label = "Requests/sec"
                scale = "LINEAR"
              }
            }
          }
        },

        # Tile 2: Response Codes
        {
          xPos   = 6
          width  = 6
          height = 4
          widget = {
            title = "HTTP Response Codes"
            xyChart = {
              dataSets = [{
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "resource.type=\"gce_backend_service\" AND metric.type=\"loadbalancing.googleapis.com/https/backend_request_count\""

                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_RATE"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = ["metric.response_code_class"]
                    }
                  }
                }
                plotType = "STACKED_AREA"
              }]
            }
          }
        },

        # Tile 3: Backend Latency
        {
          yPos   = 4
          width  = 6
          height = 4
          widget = {
            title = "Backend Latency (p50, p95, p99)"
            xyChart = {
              dataSets = [
                # p50
                {
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "resource.type=\"gce_backend_service\" AND metric.type=\"loadbalancing.googleapis.com/https/backend_latencies\""

                      aggregation = {
                        alignmentPeriod    = "60s"
                        perSeriesAligner   = "ALIGN_DELTA"
                        crossSeriesReducer = "REDUCE_PERCENTILE_50"
                        groupByFields      = ["resource.backend_service_name"]
                      }
                    }
                  }
                  plotType       = "LINE"
                  legendTemplate = "p50 - $${resource.backend_service_name}"
                },
                # p95
                {
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "resource.type=\"gce_backend_service\" AND metric.type=\"loadbalancing.googleapis.com/https/backend_latencies\""

                      aggregation = {
                        alignmentPeriod    = "60s"
                        perSeriesAligner   = "ALIGN_DELTA"
                        crossSeriesReducer = "REDUCE_PERCENTILE_95"
                        groupByFields      = ["resource.backend_service_name"]
                      }
                    }
                  }
                  plotType       = "LINE"
                  legendTemplate = "p95 - $${resource.backend_service_name}"
                },
                # p99
                {
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "resource.type=\"gce_backend_service\" AND metric.type=\"loadbalancing.googleapis.com/https/backend_latencies\""

                      aggregation = {
                        alignmentPeriod    = "60s"
                        perSeriesAligner   = "ALIGN_DELTA"
                        crossSeriesReducer = "REDUCE_PERCENTILE_99"
                        groupByFields      = ["resource.backend_service_name"]
                      }
                    }
                  }
                  plotType       = "LINE"
                  legendTemplate = "p99 - $${resource.backend_service_name}"
                }
              ]

              yAxis = {
                label = "Latency (ms)"
                scale = "LINEAR"
              }
            }
          }
        },

        # Tile 4: Cloud Function Executions
        {
          xPos   = 6
          yPos   = 4
          width  = 6
          height = 4
          widget = {
            title = "Auto-Failover Function Executions"
            xyChart = {
              dataSets = [{
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    # ✅ Cloud Run (Gen2 functions) metric
                    filter = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"auto-failover-function\" AND metric.type=\"run.googleapis.com/request_count\""

                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_RATE"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = ["metric.response_code_class"]
                    }
                  }
                }
                plotType = "STACKED_BAR"
              }]
            }
          }
        },

        # Tile 5: VPN Tunnel Status
        {
          yPos   = 8
          width  = 6
          height = 4
          widget = {
            title = "VPN Tunnel Status"
            xyChart = {
              dataSets = [{
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "resource.type=\"vpn_gateway\" AND metric.type=\"compute.googleapis.com/vpn/tunnel_established\""

                    aggregation = {
                      alignmentPeriod  = "60s"
                      perSeriesAligner = "ALIGN_MEAN"
                    }
                  }
                }
                plotType = "LINE"
              }]

              yAxis = {
                label = "Status (1=up, 0=down)"
                scale = "LINEAR"
              }
            }
          }
        },

        # Tile 6: Database Replication Lag
        {
          xPos   = 6
          yPos   = 8
          width  = 6
          height = 4
          widget = {
            title = "Database Replication Lag"
            xyChart = {
              dataSets = [{
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    # Cloud SQL replication lag
                    filter = "resource.type=\"cloudsql_database\" AND metric.type=\"cloudsql.googleapis.com/database/replication/replica_lag\""

                    aggregation = {
                      alignmentPeriod  = "60s"
                      perSeriesAligner = "ALIGN_MEAN"
                    }
                  }
                }
                plotType = "LINE"
              }]

              yAxis = {
                label = "Lag (seconds)"
                scale = "LINEAR"
              }

              # Add threshold line at 10 seconds (RPO target)
              thresholds = [{
                value = 10
              }]
            }
          }
        }
      ]
    }
  })
}
