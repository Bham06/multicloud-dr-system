# Creation of the bucket
resource "google_storage_bucket" "primary" {
  name          = "${var.project_id}-dr-primary"
  location      = var.region
  force_destroy = true

  uniform_bucket_level_access = true

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }
}

