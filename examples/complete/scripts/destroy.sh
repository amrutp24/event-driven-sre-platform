#!/usr/bin/env bash
set -euo pipefail

# Run from examples/complete, wherever the script is called from
cd "$(dirname "$0")/.."

# The incidents table has deletion protection on and the CloudTrail bucket
# refuses to be deleted with logs in it, so turn both off first
TEARDOWN_VARS=(-var incidents_deletion_protection=false -var cloudtrail_force_destroy=true)

terraform apply -auto-approve \
  -target=module.sre.aws_dynamodb_table.incidents \
  -target=aws_s3_bucket.cloudtrail \
  "${TEARDOWN_VARS[@]}"

terraform destroy -auto-approve "${TEARDOWN_VARS[@]}"
