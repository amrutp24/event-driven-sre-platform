# One customer managed key for the platform's data: the incidents table, the
# SNS topic, the events DLQ, the log groups and the CloudTrail bucket.
#
# EKS secrets use the key the EKS module creates for the cluster. A cluster
# can't be moved to a different key in place.

# local.trail_arn is in cloudtrail.tf. The key goes to the sre module as
# kms_key_arn, so the module doesn't make its own.
locals {
  account_id = data.aws_caller_identity.current.account_id
}

resource "aws_kms_key" "platform" {
  description             = "${var.name} data: incidents table, SNS, DLQ, logs, CloudTrail"
  enable_key_rotation     = true
  deletion_window_in_days = 30

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AccountAdmin"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${local.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        # Roles in this account reading and writing through these services:
        # the Step Functions roles on the table, Lambda roles writing logs.
        # Calls made directly to KMS still need IAM permissions.
        Sid       = "ThisAccountThroughServices"
        Effect    = "Allow"
        Principal = { AWS = "*" }
        Action = [
          "kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*",
          "kms:GenerateDataKey*", "kms:DescribeKey", "kms:CreateGrant",
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:CallerAccount" = local.account_id
            "kms:ViaService" = [
              "dynamodb.${var.aws_region}.amazonaws.com",
              "sns.${var.aws_region}.amazonaws.com",
              "sqs.${var.aws_region}.amazonaws.com",
              "logs.${var.aws_region}.amazonaws.com",
            ]
          }
        }
      },
      {
        Sid       = "CloudWatchLogs"
        Effect    = "Allow"
        Principal = { Service = "logs.${var.aws_region}.amazonaws.com" }
        Action = [
          "kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*",
          "kms:GenerateDataKey*", "kms:Describe*",
        ]
        Resource = "*"
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:*"
          }
        }
      },
      {
        # Publishing to the encrypted SNS topic and dead-lettering to the
        # encrypted queue, for this account's rules only
        Sid       = "EventBridge"
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = ["kms:Decrypt", "kms:GenerateDataKey*"]
        Resource  = "*"
        Condition = {
          StringEquals = { "aws:SourceAccount" = local.account_id }
        }
      },
      {
        Sid       = "CloudTrailEncrypt"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "kms:GenerateDataKey*"
        Resource  = "*"
        Condition = {
          StringEquals = { "aws:SourceArn" = local.trail_arn }
          StringLike = {
            "kms:EncryptionContext:aws:cloudtrail:arn" = "arn:aws:cloudtrail:*:${local.account_id}:trail/*"
          }
        }
      },
      {
        Sid       = "CloudTrailDescribe"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "kms:DescribeKey"
        Resource  = "*"
        Condition = {
          StringEquals = { "aws:SourceArn" = local.trail_arn }
        }
      },
    ]
  })

  tags = var.tags
}

resource "aws_kms_alias" "platform" {
  name          = "alias/${var.name}"
  target_key_id = aws_kms_key.platform.key_id
}
