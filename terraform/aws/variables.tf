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
