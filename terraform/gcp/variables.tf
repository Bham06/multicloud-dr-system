variable "project_id" {
  description = "The GCP Project ID"
  type        = string
}

variable "region" {
  description = "The GCP region for resources"
  type        = string
}

variable "zone" {
  description = "The GCP zone for the VM"
  type        = string
}

variable "db_password" {
  description = "CloudSQL database user password"
  type        = string
  sensitive   = true
}

variable "aws_access_key_id" {
  description = "ACCESS KEY ID for iam user"
  type        = string
  sensitive   = true
}

variable "aws_secret_access_key" {
  description = "AWS SECRET ACCES KEY"
  type        = string
  sensitive   = true
}

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "s3_bucket_name" {
  description = "Bucket name"
  type        = string
  default     = "dr-storage-secondary-6u1fs0vc"
}

variable "aws_eip" {
  description = "AWS Elastic IP"
  type        = string
}
variable "domain_name" {
  description = "Domain name for SSL certificate"
  type        = string
  default     = ""
}

variable "alert_email" {
  description = "Email address for failover alerts"
  type        = string
}

# variable "slack_webhook_url" {
#   description = "Slack webhook URL"
#   type        = string
#   sensitive   = true
# }
