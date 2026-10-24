# One item per incident. incident_id is deterministic (see alert_ingest), so it
# already identifies a single occurrence and needs no sort key.
resource "aws_dynamodb_table" "incidents" {
  name         = "${var.name}-incidents"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "incident_id"

  # scripts/destroy.sh turns this off before tearing down
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
    kms_key_arn = aws_kms_key.platform.arn
  }

  # Set by the incident recorder: last activity + incident_retention_days
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  tags = var.tags
}

resource "aws_sns_topic" "alerts" {
  name              = "${var.name}-alerts"
  kms_master_key_id = aws_kms_key.platform.arn
  tags              = var.tags
}

resource "aws_cloudwatch_event_bus" "sre" {
  name = "${var.name}-bus"
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
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.name}-alert-ingest:*"
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
  # What the runbook targets when the alert doesn't name a namespace or deployment
  remediation_namespace  = "apps"
  remediation_deployment = "checkout"

  # Checks after the stabilization wait, one every verify_poll_seconds, until
  # verify_timeout_seconds after the action
  verify_attempts = floor(max(var.verify_timeout_seconds - var.stabilization_seconds, 0) / var.verify_poll_seconds) + 1
}

resource "aws_cloudwatch_log_group" "runbook" {
  name              = "/aws/vendedlogs/states/${var.name}-runbook"
  retention_in_days = 30
  kms_key_id        = aws_kms_key.platform.arn
  tags              = var.tags
}

resource "aws_sfn_state_machine" "runbook" {
  name     = "${var.name}-runbook"
  role_arn = aws_iam_role.sfn_role.arn
  definition = templatefile("${path.module}/stepfunctions/runbook.asl.json", {
    table_name                = aws_dynamodb_table.incidents.name
    runbook_action_arn        = aws_lambda_function.runbook_action.arn
    event_bus_name            = aws_cloudwatch_event_bus.sre.name
    auto_remediate_severities = join(", ", formatlist("'%s'", var.auto_remediate_severities))
    default_namespace         = local.remediation_namespace
    default_deployment        = local.remediation_deployment
    default_replicas          = 4
    stabilization_seconds     = var.stabilization_seconds
    verify_poll_seconds       = var.verify_poll_seconds
    verify_attempts           = local.verify_attempts
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.runbook.arn}:*"
    include_execution_data = true
    level                  = "ALL"
  }

  # The role needs its log delivery permissions before logging can be set up
  depends_on = [aws_iam_role_policy.sfn_policy]

  tags = var.tags
}

# Lambda would create these on first run, unencrypted and kept forever.
# Existing stacks: import them first (see the README).
resource "aws_cloudwatch_log_group" "alert_ingest" {
  name              = "/aws/lambda/${var.name}-alert-ingest"
  retention_in_days = 30
  kms_key_id        = aws_kms_key.platform.arn
  tags              = var.tags
}

resource "aws_cloudwatch_log_group" "runbook_action" {
  name              = "/aws/lambda/${var.name}-runbook-action"
  retention_in_days = 30
  kms_key_id        = aws_kms_key.platform.arn
  tags              = var.tags
}

resource "aws_lambda_function" "alert_ingest" {
  function_name = "${var.name}-alert-ingest"
  role          = aws_iam_role.lambda_role.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.13"
  filename      = "${path.module}/lambda/alert_ingest.zip"
  memory_size   = 256
  timeout       = 30

  # Caps concurrent alert processing; -1 leaves it unreserved
  reserved_concurrent_executions = var.alert_ingest_reserved_concurrency

  environment {
    variables = {
      EVENT_BUS_NAME = aws_cloudwatch_event_bus.sre.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.alert_ingest]
}

resource "aws_ssm_parameter" "checkout_degraded_mode" {
  name  = "/checkout/degraded_mode"
  type  = "String"
  value = "false"
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
}

resource "aws_iam_role_policy" "runbook_lambda_policy" {
  role = aws_iam_role.runbook_lambda_role.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect   = "Allow",
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.name}-runbook-action:*"
      },
      {
        Effect   = "Allow",
        Action   = ["eks:DescribeCluster"],
        Resource = "arn:aws:eks:${var.aws_region}:${data.aws_caller_identity.current.account_id}:cluster/${var.eks_cluster_name}"
      },
      {
        Effect   = "Allow",
        Action   = ["ssm:PutParameter", "ssm:GetParameter"],
        Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/checkout/*"
      },
      {
        Effect = "Allow",
        # sts:GetCallerIdentity doesn't support resource-level permissions
        # This is required for the Lambda to authenticate with EKS
        Action   = ["sts:GetCallerIdentity"],
        Resource = "*"
      }
    ]
  })
}

# ENI management for running in the VPC. AWS-managed; uses Resource "*"
# because ENIs don't exist until Lambda creates them.
resource "aws_iam_role_policy_attachment" "runbook_vpc_access" {
  role       = aws_iam_role.runbook_lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_security_group" "runbook_lambda" {
  name        = "${var.name}-runbook-action"
  description = "runbook-action Lambda"
  vpc_id      = module.vpc.vpc_id

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
  filename      = "${path.module}/lambda/runbook_action.zip"
  memory_size   = 512
  timeout       = 120

  # Private subnets, so it reaches the cluster through the private API endpoint
  vpc_config {
    subnet_ids         = module.vpc.private_subnets
    security_group_ids = [aws_security_group.runbook_lambda.id]
  }

  depends_on = [
    aws_iam_role_policy_attachment.runbook_vpc_access,
    aws_cloudwatch_log_group.runbook_action,
  ]

  environment {
    variables = {
      REGION                = var.aws_region
      CLUSTER_NAME          = var.eks_cluster_name
      ALLOWED_NAMESPACES    = local.remediation_namespace
      MAX_REPLICAS          = var.remediation_max_replicas
      DEGRADED_DEPLOYMENT   = local.remediation_deployment
      DEGRADED_PARAM        = aws_ssm_parameter.checkout_degraded_mode.name
      DRAINABLE_NODE_LABEL  = "nodegroup=apps"
      MIN_SCHEDULABLE_NODES = var.drain_min_schedulable_nodes
    }
  }
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
