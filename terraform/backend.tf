# Configure remote state for production (S3 + DynamoDB lock).
# Fill these in and uncomment to use.
# terraform {
#   backend "s3" {
#     bucket         = "<STATE_BUCKET>"
#     key            = "event-driven-sre-platform/terraform.tfstate"
#     region         = "us-east-1"
#     dynamodb_table = "<LOCK_TABLE>"
#     encrypt        = true
#   }
# }
