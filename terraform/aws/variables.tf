variable "region" {
  description = "AWS Region"
  type        = string
  default     = "us-east-1"
}

variable "db_user" {
  description = "Database username"
  type        = string
  default     = "appuser"
}

variable "db_password" {
  description = "Database password"
  type        = string
  sensitive   = true
}

variable "gcp_vpn_gateway_interface0_ip" {
  description = "GCP VPN Gateway Public IP"
  type        = string
  sensitive   = true
}

variable "gcp_vpn_gateway_interface1_ip" {
  description = "GCP VPN Gateway 2 Public IP"
  type        = string
  sensitive   = true
}

variable "vpn_shared_secret" {
  description = "Pre-shared key for VPN Tunnel"
  type        = string
  sensitive   = true
}

variable "gcp_vpc_cidr" {
  description = "GCP VPC CIDR block for security group rules"
  type        = string
  default     = "10.0.1.0/24"
}

variable "admin_cidr" {
  description = "CIDR block allowed to SSH to the EC2 instance. Leave empty to disable SSH entirely."
  type        = string
  default     = ""

  validation {
    condition     = var.admin_cidr == "" || can(cidrhost(var.admin_cidr, 0))
    error_message = "admin_cidr must be a valid CIDR block or empty string."
  }
}

variable "github_owner" {
  description = "GitHub organization or user name that owns this repository"
  type        = string
}

variable "github_repo" {
  description = "GitHub repository name (without owner prefix)"
  type        = string
}
