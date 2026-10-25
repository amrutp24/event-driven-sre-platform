# One customer managed key for the platform's data: the incidents table, the
# SNS topic, the events DLQ and the log groups. With create_kms_key = false
# none of this is created and local.kms_key_arn (main.tf) is var.kms_key_arn;
# that key's policy needs the same statements as this one.
resource "aws_kms_key" "platform" {
  count = var.create_kms_key ? 1 : 0

  description             = "${var.name} data: incidents table, SNS, DLQ, logs"
  enable_key_rotation     = true
  deletion_window_in_days = 30

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AccountAdmin"
        Effect    = "Allow"
        Principal = { AWS = "arn:${local.partition}:iam::${local.account_id}:root" }
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
              "dynamodb.${local.region}.${local.dns_suffix}",
              "sns.${local.region}.${local.dns_suffix}",
              "sqs.${local.region}.${local.dns_suffix}",
              "logs.${local.region}.${local.dns_suffix}",
            ]
          }
        }
      },
      {
        Sid       = "CloudWatchLogs"
        Effect    = "Allow"
        Principal = { Service = "logs.${local.region}.${local.dns_suffix}" }
        Action = [
          "kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*",
          "kms:GenerateDataKey*", "kms:Describe*",
        ]
        Resource = "*"
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:*"
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
    ]
  })

  tags = var.tags
}

resource "aws_kms_alias" "platform" {
  count = var.create_kms_key ? 1 : 0

  name          = "alias/${var.name}"
  target_key_id = aws_kms_key.platform[0].key_id
}
