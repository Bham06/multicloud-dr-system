# Service Account
resource "google_service_account" "app" {
  account_id   = "dr-app-vm"
  display_name = "DR Application VM"
}

# IAM - Cloud SQL Client
resource "google_project_iam_member" "app_sql_primary" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.app.email}"
}

# IAM - Storage Object Viewer
resource "google_project_iam_member" "app_storage_viewer" {
  project = var.project_id
  role    = "roles/storage.objectViewer"
  member  = "serviceAccount:${google_service_account.app.email}"
}

# IAM - Cloud Logging Writer (required for structured application logs)
resource "google_project_iam_member" "app_logging" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.app.email}"
}

# VM Instance
resource "google_compute_instance" "primary" {
  name         = "dr-app-primary"
  machine_type = "f1-micro"
  zone         = var.zone

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-11"
      size  = 20
      type  = "pd-standard"
    }
  }

  network_interface {
    network    = google_compute_network.vpc.id
    subnetwork = google_compute_subnetwork.subnet.id
    # No external IP
  }

  # db_password is NOT passed here — the instance fetches it at runtime
  # from Secret Manager using its service account identity.
  metadata = {
    startup-script = templatefile("${path.module}/scripts/startup.sh", {
      db_connection_name = google_sql_database_instance.primary.connection_name
      project_id         = var.project_id
      provider_name      = "GCP"
    })
  }

  service_account {
    email = google_service_account.app.email
    # cloud-platform scope is intentional and GCP-recommended for VMs.
    # Secret Manager's client library has no narrower OAuth scope, so this
    # scope cannot be reduced without breaking the runtime secret fetch.
    # Actual access is restricted to exactly what the IAM bindings above allow:
    # cloudsql.client, storage.objectViewer, logging.logWriter,
    # and secretmanager.secretAccessor on the db-password secret only.
    scopes = ["cloud-platform"]
  }

  tags = ["http-server", "allow-health-check"]

  allow_stopping_for_update = true
}

# Cloud Router
resource "google_compute_router" "router" {
  name    = "dr-router"
  region  = var.region
  network = google_compute_network.vpc.id
}

# Cloud NAT
resource "google_compute_router_nat" "nat" {
  name                               = "dr-nat"
  router                             = google_compute_router.router.name
  region                             = google_compute_router.router.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# Unmanaged Instance Group
resource "google_compute_instance_group" "gcp" {
  name = "dr-instance-group-gcp"
  zone = var.zone

  instances = [
    google_compute_instance.primary.id
  ]

  named_port {
    name = "http"
    port = 80
  }
}
