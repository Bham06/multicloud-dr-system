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
