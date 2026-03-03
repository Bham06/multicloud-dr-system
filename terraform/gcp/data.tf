# ======================================
#           AWS CREDENTIALS
# ======================================

resource "google_secret_manager_secret" "aws_credentials" {
  secret_id = "aws-credentials"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "aws_credentials" {
  secret = google_secret_manager_secret.aws_credentials.id

  secret_data = jsonencode({
    access_key_id     = var.aws_access_key_id
    secret_access_key = var.aws_secret_access_key
    region            = var.aws_region
  })
}

resource "google_secret_manager_secret" "db_password" {
  secret_id = "db-password"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "db_password" {
  secret      = google_secret_manager_secret.db_password.id
  secret_data = var.db_password
}

# ============================================
# GCS BUCKET - Cloud Function Code Storage
# ============================================

resource "google_storage_bucket" "function_source" {
  name     = "${var.project_id}-function-source"
  location = var.region

  uniform_bucket_level_access = true
  force_destroy               = true
}

# ==========================================
#        Cloud Function GCS -> S3
# ==========================================

# Service account for cloud function
resource "google_service_account" "sync_function" {
  account_id   = "gcs-s3-sync-function"
  display_name = "GCS to S3 Synce Function"
}

# Grant access to Secret Manager
resource "google_secret_manager_secret_iam_member" "aws_creds_access" {
  secret_id = google_secret_manager_secret.aws_credentials.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.sync_function.email}"
}

# Grant access to GCS
resource "google_storage_bucket_iam_member" "sync_function_reader" {
  bucket = google_storage_bucket.primary.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.sync_function.email}"
}

# Archive source code
data "archive_file" "sync_function_source" {
  type        = "zip"
  source_dir  = "${path.module}/functions/gcs-s3-sync"
  output_path = "${path.module}/.terraform/functions/gcs-s3-sync.zip"
}

# Upload function code to GCS
resource "google_storage_bucket_object" "sync_function_code" {
  name   = "functions/gcs-to-s3-${data.archive_file.sync_function_source.output_md5}.zip"
  bucket = google_storage_bucket.function_source.name
  source = data.archive_file.sync_function_source.output_path
}

# Deploy Cloud Function
resource "google_cloudfunctions2_function" "gcs_to_s3_sync" {
  name     = "gcs-to-s3-sync"
  location = var.region

  build_config {
    runtime     = "python311"
    entry_point = "sync_file"

    source {
      storage_source {
        bucket = google_storage_bucket.function_source.name
        object = google_storage_bucket_object.sync_function_code.name
      }
    }
  }

  service_config {
    max_instance_count = 10
    available_memory   = "256M"
    timeout_seconds    = 300

    environment_variables = {
      GCP_PROJECT    = var.project_id
      S3_BUCKET_NAME = var.s3_bucket_name
    }

    service_account_email = google_service_account.sync_function.email
  }

  event_trigger {
    trigger_region = var.region
    event_type     = "google.cloud.storage.object.v1.finalized"

    event_filters {
      attribute = "bucket"
      value     = google_storage_bucket.primary.name
    }
  }
}

# ================================
# Cloud Function - Database Backup
# ================================

resource "google_service_account" "db_baackup_function" {
  account_id   = "db-backup-function"
  display_name = "Database Backup Function"
}

# Grant access to secrets
resource "google_secret_manager_secret_iam_member" "db_baackup_aws_creds" {
  secret_id = google_secret_manager_secret.aws_credentials.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.db_baackup_function.email}"
}

resource "google_secret_manager_secret_iam_member" "db_backup_db_password" {
  secret_id = google_secret_manager_secret.db_password.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.db_baackup_function.email}"
}

# Grant Cloud SQL Client Role
resource "google_project_iam_member" "db_backup_sql_client" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.db_baackup_function.email}"
}

# Grant GCS access
resource "google_storage_bucket_iam_member" "db_backup_writer" {
  bucket = google_storage_bucket.primary.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.db_baackup_function.email}"
}

# Archive DB backup function source
data "archive_file" "db_backup_function_source" {
  type        = "zip"
  source_dir  = "${path.module}/functions/db-backup"
  output_path = "${path.module}/.terraform/functions/db-backup.zip"
}

# Upload DB backup function code
resource "google_storage_bucket_object" "db_backup_function_code" {
  name   = "functions/db-backup-${data.archive_file.db_backup_function_source.output_md5}.zip"
  bucket = google_storage_bucket.function_source.name
  source = data.archive_file.db_backup_function_source.output_path
}

# Deploy DB Backup Function
resource "google_cloudfunctions2_function" "db_backup" {
  name     = "db-backup-function"
  location = var.region

  build_config {
    runtime     = "python311"
    entry_point = "backup_database"

    source {
      storage_source {
        bucket = google_storage_bucket.function_source.name
        object = google_storage_bucket_object.db_backup_function_code.name
      }
    }
  }

  service_config {
    max_instance_count = 1
    available_memory   = "512M"
    timeout_seconds    = 540

    environment_variables = {
      GCP_PROJECT        = var.project_id
      DB_CONNECTION_NAME = google_sql_database_instance.primary.connection_name
      DB_USER            = "appuser"
      DB_NAME            = "application"
      GCS_BACKUP_BUCKET  = google_storage_bucket.primary.name
      S3_BUCKET_NAME     = var.s3_bucket_name
    }

    service_account_email = google_service_account.db_baackup_function.email
  }
}

# ==================================================
#  Cloud Scheduler - Trigger Backup every 5 minutes  
# ==================================================

resource "google_cloud_scheduler_job" "db_backup" {
  name             = "db-backup-scheduler"
  description      = "Triggers database backup every 5 minutes"
  schedule         = "*/5 * * * *"
  time_zone        = "UTC"
  attempt_deadline = "600s"
  region           = var.region

  http_target {
    http_method = "POST"
    uri         = google_cloudfunctions2_function.db_backup.service_config[0].uri

    oidc_token {
      service_account_email = google_service_account.db_baackup_function.email
    }
  }

  retry_config {
    retry_count = 3
  }
}

# IAM for Cloud Scheduler to invoke function
resource "google_cloud_run_service_iam_member" "db_backup_invoker" {
  location = google_cloudfunctions2_function.db_backup.location
  service  = google_cloudfunctions2_function.db_backup.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.db_baackup_function.email}"
}
