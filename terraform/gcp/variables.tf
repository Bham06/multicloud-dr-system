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

variable "auth_token" {
  description = "Slack Authentication Token"
  type        = string
  sensitive   = true
}

variable "slack_team" {
  description = "Team for Multi Cloud DR alerts"
  type        = string
  default     = "Multi Cloud DR"
}

variable "aws_vpn_tunnel1_ip" {
  description = "VPN GATEWAY FOR GCP VPN"
  type        = string
  sensitive   = true
}

variable "aws_vpn_tunnel2_ip" {
  description = "VPN GATEWAY FOR GCP VPN"
  type        = string
  sensitive   = true
}

variable "shared_secret" {
  description = "Shared Secret for VPN tunnel"
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.shared_secret) >= 10
    error_message = "VPN shared secret must be at least 10 characters"
  }
}

variable "aws_vpc_cidr" {
  description = "AWS VPC CIDR block for firewall rules"
  type        = string
  default     = "10.1.0.0/26"
}

variable "use_vpn" {
  description = "Enable VPN tunnel monitoring and alerts"
  type        = bool
  default     = false
}
