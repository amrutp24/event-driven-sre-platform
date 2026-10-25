# One item per incident. incident_id is deterministic (see alert_ingest), so it
# already identifies a single occurrence and needs no sort key.
resource "aws_dynamodb_table" "incidents" {
  name         = "${var.name}-incidents"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "incident_id"

  # Set incidents_deletion_protection = false and apply before destroying
  # (examples/complete/scripts/destroy.sh does this)
  deletion_protection_enabled = var.incidents_deletion_protection

  attribute {
    name = "incident_id"
    type = "S"
  }
  attribute {
    name = "status"
    type = "S"
  }
  attribute {
    name = "first_seen_at"
    type = "S"
  }

  # Open or escalated incidents, newest first
  global_secondary_index {
    name            = "by-status"
    hash_key        = "status"
    range_key       = "first_seen_at"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = local.kms_key_arn
  }

  # Set by the incident recorder: last activity + incident_retention_days
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  tags = var.tags

  # Here because a variable validation can't refer to another variable
  # before Terraform 1.9
  lifecycle {
    precondition {
      condition     = var.create_kms_key || var.kms_key_arn != null
      error_message = "Set kms_key_arn when create_kms_key is false."
    }
  }
}

resource "aws_sns_topic" "alerts" {
  name              = "${var.name}-alerts"
  kms_master_key_id = local.kms_key_arn
  tags              = var.tags
}

resource "aws_cloudwatch_event_bus" "sre" {
  name = "${var.name}-bus"
  tags = var.tags
}

resource "aws_iam_role" "lambda_role" {
  name = "${var.name}-alert-ingest-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect    = "Allow",
      Principal = { Service = "lambda.amazonaws.com" },
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "lambda_policy" {
  role = aws_iam_role.lambda_role.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect = "Allow",
        Action = [
          "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"
        ],
        Resource = "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.name}-alert-ingest:*"
      },
      {
        Effect   = "Allow",
        Action   = ["events:PutEvents"],
        Resource = aws_cloudwatch_event_bus.sre.arn
      }
    ]
  })
}

resource "aws_iam_role" "sfn_role" {
  name = "${var.name}-sfn-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect    = "Allow",
      Principal = { Service = "states.amazonaws.com" },
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "sfn_policy" {
  role = aws_iam_role.sfn_role.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect = "Allow",
        # Step Functions log delivery requires broad permissions to manage CloudWatch Logs
        # These actions don't support resource-level permissions per AWS documentation
        Action = ["logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery",
          "logs:DeleteLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy",
        "logs:DescribeResourcePolicies", "logs:DescribeLogGroups"],
        Resource = "*"
      },
      {
        # LogIncident, MarkResolved and Escalate write; VerifyResolution reads
        Effect   = "Allow",
        Action   = ["dynamodb:UpdateItem", "dynamodb:GetItem"],
        Resource = aws_dynamodb_table.incidents.arn
      },
      {
        # action.completed, incident.auto_resolved, incident.escalated
        Effect   = "Allow",
        Action   = ["events:PutEvents"],
        Resource = aws_cloudwatch_event_bus.sre.arn
      }
    ]
  })
}

locals {
  # Checks after the stabilization wait, one every verify_poll_seconds, until
  # verify_timeout_seconds after the action
  verify_attempts = floor(max(var.verify_timeout_seconds - var.stabilization_seconds, 0) / var.verify_poll_seconds) + 1
}

resource "aws_cloudwatch_log_group" "runbook" {
  name              = "/aws/vendedlogs/states/${var.name}-runbook"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.kms_key_arn
  tags              = var.tags
}

resource "aws_sfn_state_machine" "runbook" {
  name     = "${var.name}-runbook"
  role_arn = aws_iam_role.sfn_role.arn
  definition = templatefile("${path.module}/stepfunctions/runbook.asl.json", {
    partition                 = local.partition
    table_name                = aws_dynamodb_table.incidents.name
    runbook_action_arn        = aws_lambda_function.runbook_action.arn
    event_bus_name            = aws_cloudwatch_event_bus.sre.name
    auto_remediate_severities = join(", ", formatlist("'%s'", var.auto_remediate_severities))
    routes                    = jsonencode(var.runbook_routes)
    # What the runbook targets when the alert doesn't name a namespace or
    # deployment. An empty deployment is rejected by runbook_action, so the
    # incident escalates.
    default_namespace     = var.remediation_namespaces[0]
    default_deployment    = var.default_deployment != null ? var.default_deployment : ""
    default_replicas      = var.default_scale_replicas
    stabilization_seconds = var.stabilization_seconds
    verify_poll_seconds   = var.verify_poll_seconds
    verify_attempts       = local.verify_attempts
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.runbook.arn}:*"
    include_execution_data = true
    level                  = "ALL"
  }

  # The role needs its log delivery permissions before logging can be set up
  depends_on = [aws_iam_role_policy.sfn_policy]

  tags = var.tags

  # Here because a variable validation can't refer to another variable
  # before Terraform 1.9
  lifecycle {
    precondition {
      condition     = var.default_scale_replicas <= var.remediation_max_replicas
      error_message = "default_scale_replicas can't be more than remediation_max_replicas."
    }
  }
}

# Zipped at plan time from the source in lambda/. Neither function has
# dependencies outside the runtime, so there's no pip step.
locals {
  lambdas = toset(["alert_ingest", "runbook_action"])
}

data "archive_file" "lambda" {
  for_each = local.lambdas

  type        = "zip"
  source_dir  = "${path.module}/lambda/${each.key}"
  output_path = "${path.module}/build/${each.key}.zip"
  excludes = [
    "tests", "tests/**",
    "__pycache__", "**/__pycache__", "**/__pycache__/**",
    "requirements*.txt",
    ".pytest_cache", ".pytest_cache/**",
    ".coverage", "coverage.xml",
  ]
}

# Lambda would create these on first run, unencrypted and kept forever.
# Existing stacks: import them first (see the README).
resource "aws_cloudwatch_log_group" "alert_ingest" {
  name              = "/aws/lambda/${var.name}-alert-ingest"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.kms_key_arn
  tags              = var.tags
}

resource "aws_cloudwatch_log_group" "runbook_action" {
  name              = "/aws/lambda/${var.name}-runbook-action"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.kms_key_arn
  tags              = var.tags
}

resource "aws_lambda_function" "alert_ingest" {
  function_name = "${var.name}-alert-ingest"
  role          = aws_iam_role.lambda_role.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.13"
  memory_size   = 256

  filename         = data.archive_file.lambda["alert_ingest"].output_path
  source_code_hash = data.archive_file.lambda["alert_ingest"].output_base64sha256
  timeout          = 30

  # Caps concurrent alert processing; -1 leaves it unreserved
  reserved_concurrent_executions = var.alert_ingest_reserved_concurrency

  environment {
    variables = {
      EVENT_BUS_NAME = aws_cloudwatch_event_bus.sre.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.alert_ingest]

  tags = var.tags
}

# degrade sets this to "true" and degraded_mode_deployment reads it. The value
# is left alone after creation so an apply doesn't undo a remediation.
resource "aws_ssm_parameter" "degraded_mode" {
  count = local.create_degraded_mode_parameter ? 1 : 0

  name  = local.degraded_mode_parameter_name
  type  = "String"
  value = "false"
  tags  = var.tags

  lifecycle {
    ignore_changes = [value]
  }
}

moved {
  from = aws_ssm_parameter.checkout_degraded_mode
  to   = aws_ssm_parameter.degraded_mode
}

# Runbook action Lambda: performs remediation against EKS and/or SSM
resource "aws_iam_role" "runbook_lambda_role" {
  name = "${var.name}-runbook-action-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect    = "Allow",
      Principal = { Service = "lambda.amazonaws.com" },
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "runbook_lambda_policy" {
  role = aws_iam_role.runbook_lambda_role.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = concat(
      [
        {
          Effect   = "Allow",
          Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
          Resource = "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.name}-runbook-action:*"
        },
        {
          Effect   = "Allow",
          Action   = ["eks:DescribeCluster"],
          Resource = local.cluster_arn
        },
        {
          Effect = "Allow",
          # sts:GetCallerIdentity doesn't support resource-level permissions
          # This is required for the Lambda to authenticate with EKS
          Action   = ["sts:GetCallerIdentity"],
          Resource = "*"
        },
      ],
      # Only with degraded mode on
      local.degraded_mode_enabled ? [
        {
          Effect   = "Allow",
          Action   = ["ssm:PutParameter", "ssm:GetParameter"],
          Resource = local.degraded_mode_parameter_arn
        },
      ] : [],
    )
  })
}

# ENI management for running in the VPC. AWS-managed; uses Resource "*"
# because ENIs don't exist until Lambda creates them.
resource "aws_iam_role_policy_attachment" "runbook_vpc_access" {
  role       = aws_iam_role.runbook_lambda_role.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_security_group" "runbook_lambda" {
  name        = "${var.name}-runbook-action"
  description = "runbook-action Lambda"
  vpc_id      = var.vpc_id

  # The Kubernetes API, and AWS APIs (EKS, SSM, STS) through the NAT gateways
  egress {
    description = "HTTPS out"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = var.tags
}

resource "aws_lambda_function" "runbook_action" {
  function_name = "${var.name}-runbook-action"
  role          = aws_iam_role.runbook_lambda_role.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.13"
  memory_size   = 512

  filename         = data.archive_file.lambda["runbook_action"].output_path
  source_code_hash = data.archive_file.lambda["runbook_action"].output_base64sha256
  timeout          = 120

  # At most this many remediations run against the cluster at once
  reserved_concurrent_executions = var.runbook_action_reserved_concurrency

  # Private subnets, so it reaches the cluster through the private API endpoint
  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = [aws_security_group.runbook_lambda.id]
  }

  depends_on = [
    aws_iam_role_policy_attachment.runbook_vpc_access,
    aws_cloudwatch_log_group.runbook_action,
  ]

  # degrade and drain_node are rejected when their variables are left out
  environment {
    variables = { for k, v in {
      REGION                = local.region
      CLUSTER_NAME          = var.cluster_name
      ALLOWED_NAMESPACES    = join(",", var.remediation_namespaces)
      MAX_REPLICAS          = var.remediation_max_replicas
      DEGRADED_DEPLOYMENT   = var.degraded_mode_deployment
      DEGRADED_PARAM        = local.degraded_mode_parameter_name
      DRAINABLE_NODE_LABEL  = var.drainable_node_label
      MIN_SCHEDULABLE_NODES = var.drain_min_schedulable_nodes
    } : k => tostring(v) if v != null }
  }

  tags = var.tags
}

# Allow Step Functions to invoke the runbook action lambda
resource "aws_iam_role_policy" "sfn_invoke_lambda" {
  role = aws_iam_role.sfn_role.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect   = "Allow",
        Action   = ["lambda:InvokeFunction"],
        Resource = [aws_lambda_function.runbook_action.arn]
      }
    ]
  })
}

# ------------------------------------------------------------------ cluster access

# runbook_action calls the Kubernetes API as its IAM role. IRSA only applies to
# pods, so the role gets an access entry instead, in a group that the
# rbac_manifest output binds. No access policy is associated, so those
# bindings are all it's allowed.
resource "aws_eks_access_entry" "runbook_action" {
  count = var.create_access_entry ? 1 : 0

  cluster_name      = var.cluster_name
  principal_arn     = aws_iam_role.runbook_lambda_role.arn
  kubernetes_groups = [var.kubernetes_group]
  type              = "STANDARD"
  tags              = var.tags
}

# The Lambda runs in the private subnets and reaches the private API endpoint
resource "aws_vpc_security_group_ingress_rule" "cluster_from_runbook_action" {
  security_group_id            = var.cluster_security_group_id
  description                  = "runbook-action Lambda to the Kubernetes API"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.runbook_lambda.id
  tags                         = var.tags
}
