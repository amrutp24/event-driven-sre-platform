# API audit trail. Turn it off with enable_cloudtrail = false if the account
# already has an organization trail, since a second copy of management events
# is billed.

locals {
  trail_name = "${var.name}-trail"
  trail_arn  = "arn:aws:cloudtrail:${var.aws_region}:${data.aws_caller_identity.current.account_id}:trail/${local.trail_name}"
}

resource "aws_s3_bucket" "cloudtrail" {
  # Public access block, lifecycle and KMS encryption are the separate
  # resources below; Checkov doesn't link them through count
  #checkov:skip=CKV2_AWS_6:public access block is aws_s3_bucket_public_access_block.cloudtrail
  #checkov:skip=CKV2_AWS_61:lifecycle is aws_s3_bucket_lifecycle_configuration.cloudtrail
  #checkov:skip=CKV_AWS_145:SSE-KMS is aws_s3_bucket_server_side_encryption_configuration.cloudtrail
  count  = var.enable_cloudtrail ? 1 : 0
  bucket = "${var.name}-cloudtrail-${data.aws_caller_identity.current.account_id}"

  # Off by default so the audit logs can't be deleted by accident.
  # scripts/destroy.sh turns it on before tearing down.
  force_destroy = var.cloudtrail_force_destroy

  tags = var.tags
}

resource "aws_s3_bucket_versioning" "cloudtrail" {
  count  = var.enable_cloudtrail ? 1 : 0
  bucket = aws_s3_bucket.cloudtrail[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "cloudtrail" {
  count  = var.enable_cloudtrail ? 1 : 0
  bucket = aws_s3_bucket.cloudtrail[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "cloudtrail" {
  count  = var.enable_cloudtrail ? 1 : 0
  bucket = aws_s3_bucket.cloudtrail[0].id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "cloudtrail" {
  count  = var.enable_cloudtrail ? 1 : 0
  bucket = aws_s3_bucket.cloudtrail[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.platform.arn
    }
    # Fewer KMS requests, and so a smaller KMS bill
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "cloudtrail" {
  count  = var.enable_cloudtrail ? 1 : 0
  bucket = aws_s3_bucket.cloudtrail[0].id

  rule {
    id     = "expire-trail-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = var.cloudtrail_retention_days
    }

    # Versioning keeps the old copy when a log expires; drop those too
    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.cloudtrail]
}

resource "aws_s3_bucket_policy" "cloudtrail" {
  count  = var.enable_cloudtrail ? 1 : 0
  bucket = aws_s3_bucket.cloudtrail[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AWSCloudTrailAclCheck"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:GetBucketAcl"
        Resource  = aws_s3_bucket.cloudtrail[0].arn
        Condition = {
          StringEquals = { "aws:SourceArn" = local.trail_arn }
        }
      },
      {
        Sid       = "AWSCloudTrailWrite"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.cloudtrail[0].arn}/AWSLogs/${data.aws_caller_identity.current.account_id}/*"
        Condition = {
          StringEquals = {
            "aws:SourceArn" = local.trail_arn
            "s3:x-amz-acl"  = "bucket-owner-full-control"
          }
        }
      },
      {
        Sid       = "DenyWithoutTLS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.cloudtrail[0].arn,
          "${aws_s3_bucket.cloudtrail[0].arn}/*",
        ]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      }
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.cloudtrail]
}

resource "aws_cloudtrail" "platform" {
  count = var.enable_cloudtrail ? 1 : 0
  name  = local.trail_name

  s3_bucket_name = aws_s3_bucket.cloudtrail[0].id
  kms_key_id     = aws_kms_key.platform.arn

  is_multi_region_trail         = true
  include_global_service_events = true
  enable_log_file_validation    = true

  # Management events only; data events are billed per event
  event_selector {
    read_write_type           = "All"
    include_management_events = true
  }

  tags = var.tags

  # CloudTrail checks it can write to the bucket when the trail is created
  depends_on = [
    aws_s3_bucket_policy.cloudtrail,
    aws_s3_bucket_server_side_encryption_configuration.cloudtrail,
  ]
}
