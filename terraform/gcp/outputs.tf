output "db_connection_name_primary" {
  value = google_sql_database_instance.primary.connection_name
}

output "db_private_ip_primary" {
  value = google_sql_database_instance.primary.private_ip_address
}

output "gcp_vm_internal_ip" {
  value = google_compute_instance.primary.network_interface[0].network_ip
}

# output "gcp_instance_group" {
#   value = google_compute_instance_group.gcp.self_link
# }

output "db_connection_name" {
  value = google_sql_database_instance.primary.connection_name
}

# output "bucket_name" {
#   value = google_storage_bucket.primary.name
# }

output "load_balancer_ip" {
  value       = google_compute_global_address.lb.address
  description = "Load balancer IP address"
}

output "access_url_http" {
  value       = "http://${local.domain_name}"
  description = "HTTP access URL (will redirect to HTTPS)"
}

output "access_url_https" {
  value       = "https://${local.domain_name}"
  description = "HTTPS access URL"
}

# output "ssl_certificate_status" {
#   value       = google_compute_managed_ssl_certificate.lb.managed[0].status
#   description = "SSL certificate provisioning status"
# }

output "nip_io_domain" {
  value       = local.domain_name
  description = "Auto-generated nip.io domain"
}

output "gcs_primary_bucket" {
  value       = google_storage_bucket.primary.name
  description = "GCS bucket for data sync and backups"
}

output "s3_secondary_bucket" {
  value       = var.s3_bucket_name
  description = "S3 bucket for replicated data"
}

output "sync_function_url" {
  value       = google_cloudfunctions2_function.gcs_to_s3_sync.service_config[0].uri
  description = "GCS to S3 sync function URL"
}

output "db_backup_function_url" {
  value       = google_cloudfunctions2_function.db_backup.service_config[0].uri
  description = "Database backup function URL"
}

output "aws_elastic_ip" {
  value       = var.aws_eip
  description = "AWS Elastic IP for Internet NEG"
}

output "aws_fqdn" {
  value       = "${replace(var.aws_eip, ".", "-")}.nip.io"
  description = "AWS FQDN used in Internet NEG"
}

output "auto_failover_function_url" {
  value       = google_cloudfunctions2_function.auto_failover.service_config[0].uri
  description = "Auto-failover function URL"
}

output "view_failover_logs" {
  value       = "gcloud functions logs read auto-failover-function --limit=50"
  description = "Command to view auto-failover logs"
}

output "backend_status_command" {
  value       = "gcloud compute backend-services describe dr-multi-backend-service --global --format='table(backends[].group.basename(),backends[].capacityScaler)'"
  description = "Command to check current backend capacities"
}
