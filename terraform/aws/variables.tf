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

variable "ssh_public_key" {
  description = "SSH public key authorized on the secondary EC2 instance"
  type        = string

  validation {
    condition     = can(regex("^(ssh-rsa|ssh-ed25519|ecdsa-sha2-) ", var.ssh_public_key))
    error_message = "ssh_public_key must be an OpenSSH public key, e.g. the contents of ~/.ssh/id_ed25519.pub"
  }
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
