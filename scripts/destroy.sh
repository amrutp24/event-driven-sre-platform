#!/usr/bin/env bash
set -euo pipefail

# The incidents table has deletion protection on, so turn it off first
terraform -chdir=terraform apply -auto-approve \
  -target=aws_dynamodb_table.incidents \
  -var incidents_deletion_protection=false

terraform -chdir=terraform destroy -auto-approve -var incidents_deletion_protection=false
