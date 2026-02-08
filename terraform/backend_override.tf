# CI Local Backend Configuration
# This overrides the S3 backend for CI/CD testing
terraform {
  backend "local" {
    path = "./terraform.tfstate"
  }
}