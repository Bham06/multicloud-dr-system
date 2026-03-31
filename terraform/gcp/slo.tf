# # =============================
# #  Service Level Objectives
# # =============================

# # Custom Service Definition
# resource "google_monitoring_custom_service" "dr_system" {
#   service_id   = "dr-system"
#   display_name = "DR Multi-Cloud System"
# }

# #SLO: 99.9% Availabilty
# resource "google_monitoring_slo" "availability" {
#   service = google_monitoring_custom_service.dr_system.service_id

#   display_name = "99.9% Availabilty SLO"

#   goal                = 0.999
#   rolling_period_days = 30

#   windows_based_sli {
#     window_period = "3600s"

#     good_bad_metric_filter = <<-EOT
#       metric.type="loadbalancing.googleapis.com/https/request_count"
#       AND resource.type="http_load_balancer"
#       AND metric.labels.response_code_class="200"
#     EOT
#   }
# }

# # Alert SLO Burn Rate
# resource "google_monitoring_alert_policy" "slo_burn_rate" {
#   display_name = "SLO Burn Rate Alert - Availability Degraded"
#   combiner     = "OR"

#   conditions {
#     display_name = "Burning through error budget too fast"

#     condition_threshold {
#       filter = <<-EOT
#         select_slo_burn_rate("${google_monitoring_slo.availability.id}", 3600)
#       EOT

#       comparison      = "COMPARISON_GT"
#       threshold_value = 10
#       duration        = "300s"
#     }
#   }

#   notification_channels = [google_monitoring_notification_channel.slack.id]

#   alert_strategy {
#     notification_channel_strategy {} # period = "3600s"
#   }

#   severity = "WARNING"

#   documentation {
#     content = <<-EOT
#       The DR system is consuming is error budget fastest than sustainable.

#       Current SLO: 99.9% availabilty over 30 days

#       This means you're experiencing elevated error rates that, if continued,
#       will violate the SLO.

#       Actions: 
#       1. Check current error rate in dashboard
#       2. Investigate recent failures
#       3. Determine if manual intervention needed
#       4. Check if failover is working correctly
#     EOT
#   }
# }
