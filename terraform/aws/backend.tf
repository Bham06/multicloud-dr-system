terraform {
  backend "s3" {
    bucket         = "956574163309-terraform-state"
    key            = "multicloud-dr/aws/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    dynamodb_table = "terraform-state-lock"
  }

  required_version = ">= 1.0"
}
