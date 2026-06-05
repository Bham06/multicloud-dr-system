# =========================================================================
#  Service Level Objectives — DR Multi-Cloud System
#
#  SLO:  99.9% availability over a 30-day rolling window
#  Metric: fraction of LB requests that return a non-5xx response
#
#  Error budget: 0.1% × 30 days × 24h × 60m = 43.2 minutes / month
#
#  Two burn-rate alerts implement the multi-window / multi-burn-rate pattern:
#    Fast burn  — 1h window  at 14.4×  → ~2% budget consumed → page immediately
#    Slow burn  — 6h window  at  6.0×  → ~5% budget consumed → ticket within hours
# =========================================================================

# Custom service acts as the logical grouping for the SLO
resource "google_monitoring_custom_service" "dr_system" {
  service_id   = "dr-system"
  display_name = "DR Multi-Cloud System"
}

# -------------------------------------------------------------------------
# Availability SLO
# -------------------------------------------------------------------------

resource "google_monitoring_slo" "availability" {
  service      = google_monitoring_custom_service.dr_system.service_id
  slo_id       = "dr-availability-30d"
  display_name = "99.9% Availability (30-day rolling)"

  goal                = 0.999
  rolling_period_days = 30

  # Good = requests that did NOT return 5xx
  # Total = all requests to the load balancer
  request_based_sli {
    good_total_ratio {
      good_service_filter = <<-EOT
        metric.type="loadbalancing.googleapis.com/https/request_count"
        AND resource.type="http_load_balancer"
        AND metric.labels.response_code_class!="5xx"
      EOT

      total_service_filter = <<-EOT
        metric.type="loadbalancing.googleapis.com/https/request_count"
        AND resource.type="http_load_balancer"
      EOT
    }
  }
}

# -------------------------------------------------------------------------
# Fast-burn alert  (1-hour window, 14.4× burn rate)
#
# 14.4× at 1 hour = consuming 14.4 hours of budget per hour.
# Monthly budget = 43.2 min.  At this rate: 43.2 / 14.4 = 3 hours until exhaustion.
# Pages immediately — must resolve within minutes.
# -------------------------------------------------------------------------

resource "google_monitoring_alert_policy" "slo_fast_burn" {
  display_name = "SLO Fast Burn — DR Availability"
  combiner     = "OR"

  conditions {
    display_name = "1-hour burn rate > 14.4×"

    condition_threshold {
      filter = <<-EOT
        select_slo_burn_rate("${google_monitoring_slo.availability.id}", 3600)
      EOT

      comparison      = "COMPARISON_GT"
      threshold_value = 14.4
      duration        = "0s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.slack.id]

  severity = "CRITICAL"

  alert_strategy {
    auto_close = "3600s"

    notification_rate_limit {
      period = "3600s"
    }
  }

  documentation {
    content = <<-EOT
      SLO FAST BURN — DR System Availability

      The DR system is consuming error budget 14.4× faster than sustainable.

      SLO:          99.9% availability over 30 days
      Error budget: ~43 minutes / month
      Time to exhaustion at current rate: ~3 hours

      IMMEDIATE ACTIONS (resolve within 30 minutes):
      1. Check which backend is serving:
         gcloud compute url-maps describe dr-url-map --format="get(defaultService)"
      2. Check LB health in Cloud Monitoring dashboard
      3. Review auto-failover logs:
         gcloud functions logs read auto-failover-function --gen2 --limit=50
      4. Check GCP backend: curl http://${google_compute_global_address.lb.address}/health
      5. Check AWS backend: curl http://${var.aws_eip}/health
      6. If auto-failover hasn't triggered, check Firestore state store
    EOT
  }
}

# -------------------------------------------------------------------------
# Slow-burn alert  (6-hour window, 6× burn rate)
#
# 6× at 6 hours = consuming 36 hours of budget in 6 hours = ~5% of monthly budget.
# At this rate: 43.2 / 6 = 7.2 hours until exhaustion.
# Pages within a few hours — investigate and resolve same day.
# -------------------------------------------------------------------------

resource "google_monitoring_alert_policy" "slo_slow_burn" {
  display_name = "SLO Slow Burn — DR Availability"
  combiner     = "OR"

  conditions {
    display_name = "6-hour burn rate > 6×"

    condition_threshold {
      filter = <<-EOT
        select_slo_burn_rate("${google_monitoring_slo.availability.id}", 21600)
      EOT

      comparison      = "COMPARISON_GT"
      threshold_value = 6.0
      duration        = "0s"

      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.slack.id]

  severity = "WARNING"

  alert_strategy {
    auto_close = "7200s"

    notification_rate_limit {
      period = "7200s"
    }
  }

  documentation {
    content = <<-EOT
      SLO SLOW BURN — DR System Availability

      The DR system is consuming error budget 6× faster than sustainable.

      SLO:          99.9% availability over 30 days
      Error budget: ~43 minutes / month
      Time to exhaustion at current rate: ~7 hours

      ACTIONS (resolve within a few hours):
      1. Check error rate trend in Cloud Monitoring dashboard
      2. Identify whether errors are sporadic or sustained
      3. Review recent config changes or deployments
      4. Check auto-failover execution history:
         gcloud functions logs read auto-failover-function --gen2 --limit=100
      5. Verify database replication lag is within RPO target (< 10 seconds)
      6. Check VPN tunnel health if replication lag is elevated
    EOT
  }
}
