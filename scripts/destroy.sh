#!/usr/bin/env bash
set -euo pipefail

# The incidents table has deletion protection on and the CloudTrail bucket
# refuses to be deleted with logs in it, so turn both off first
TEARDOWN_VARS=(-var incidents_deletion_protection=false -var cloudtrail_force_destroy=true)

terraform -chdir=examples/complete apply -auto-approve \
  -target=module.sre.aws_dynamodb_table.incidents \
  -target=aws_s3_bucket.cloudtrail \
  "${TEARDOWN_VARS[@]}"

terraform -chdir=examples/complete destroy -auto-approve "${TEARDOWN_VARS[@]}"
