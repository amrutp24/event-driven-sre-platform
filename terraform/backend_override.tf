# CI Local Backend Configuration
# This overrides S3 backend for CI/CD testing
terraform {
  backend "local" {
    path = "./terraform.tfstate"
  }
}
}

# Also disable production backend configuration
terraform {
  backend "local" {
    path = "/dev/null"
  }
}