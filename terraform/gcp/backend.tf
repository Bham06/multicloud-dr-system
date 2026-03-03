terraform {
  backend "gcs" {
    bucket = "final-year-project1-484523-terraform-state"
    prefix = "multicloud-dr/gcp"
  }

  required_version = ">= 1.0"

}
