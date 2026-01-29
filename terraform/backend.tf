# Configure remote state for production (S3 + DynamoDB lock).
# Fill these in and uncomment to use.
 terraform {
   backend "s3" {
     bucket         = "amrutp24-event-driven-sre-tfstate-prod"
     key            = "event-driven-sre-platform/terraform.tfstate"
     region         = "us-east-1"
     dynamodb_table = "amrutp24-event-driven-sre-tflock-prod"
     encrypt        = true
   }
 }
