# EventBridge as the central bus. alert_ingest only puts events on the bus;
# these rules decide where each one goes.
#
#   incident.created        -> incident recorder (DynamoDB), SNS, runbook
#   incident.resolved       -> incident recorder (DynamoDB), SNS
#   action.completed        -> incident recorder (DynamoDB)
#   incident.escalated      -> SNS
#   incident.auto_resolved  -> SNS
#   every sre.* event       -> CloudWatch Logs
#
# DynamoDB isn't an EventBridge target, so an Express state machine (the
# incident recorder) does the writes.

locals {
  alert_sources = ["sre.alertmanager", "sre.datadog"]

  # Every target retries, then dead-letters
  target_retry = {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 20
  }
}

# ------------------------------------------------------------------ rules

resource "aws_cloudwatch_event_rule" "incident_created" {
  name           = "${var.name}-incident-created"
  description    = "A firing alert, new or re-sent"
  event_bus_name = aws_cloudwatch_event_bus.sre.name
  event_pattern = jsonencode({
    source        = local.alert_sources
    "detail-type" = ["incident.created"]
  })
  tags = var.tags
}

resource "aws_cloudwatch_event_rule" "incident_resolved" {
  name           = "${var.name}-incident-resolved"
  description    = "An alert that has resolved"
  event_bus_name = aws_cloudwatch_event_bus.sre.name
  event_pattern = jsonencode({
    source        = local.alert_sources
    "detail-type" = ["incident.resolved"]
  })
  tags = var.tags
}

resource "aws_cloudwatch_event_rule" "action_completed" {
  name           = "${var.name}-action-completed"
  description    = "The runbook finished, successfully or not"
  event_bus_name = aws_cloudwatch_event_bus.sre.name
  event_pattern = jsonencode({
    source        = ["sre.runbook"]
    "detail-type" = ["action.completed"]
  })
  tags = var.tags
}

resource "aws_cloudwatch_event_rule" "incident_escalated" {
  name           = "${var.name}-incident-escalated"
  description    = "The runbook handed the incident to a person"
  event_bus_name = aws_cloudwatch_event_bus.sre.name
  event_pattern = jsonencode({
    source        = ["sre.runbook"]
    "detail-type" = ["incident.escalated"]
  })
  tags = var.tags
}

resource "aws_cloudwatch_event_rule" "incident_auto_resolved" {
  name           = "${var.name}-incident-auto-resolved"
  description    = "The alert resolved after the runbook's action"
  event_bus_name = aws_cloudwatch_event_bus.sre.name
  event_pattern = jsonencode({
    source        = ["sre.runbook"]
    "detail-type" = ["incident.auto_resolved"]
  })
  tags = var.tags
}

resource "aws_cloudwatch_event_rule" "all_events" {
  name           = "${var.name}-all-events"
  description    = "Everything on the bus, for the audit log"
  event_bus_name = aws_cloudwatch_event_bus.sre.name
  event_pattern = jsonencode({
    source = [{ prefix = "sre." }]
  })
  tags = var.tags
}

# ------------------------------------------------------------------ targets

resource "aws_cloudwatch_event_target" "created_to_recorder" {
  rule           = aws_cloudwatch_event_rule.incident_created.name
  event_bus_name = aws_cloudwatch_event_bus.sre.name
  target_id      = "incident-recorder"
  arn            = aws_sfn_state_machine.incident_recorder.arn
  role_arn       = aws_iam_role.events_invoke.arn

  retry_policy {
    maximum_event_age_in_seconds = local.target_retry.maximum_event_age_in_seconds
    maximum_retry_attempts       = local.target_retry.maximum_retry_attempts
  }
  dead_letter_config {
    arn = aws_sqs_queue.events_dlq.arn
  }
}

resource "aws_cloudwatch_event_target" "created_to_runbook" {
  rule           = aws_cloudwatch_event_rule.incident_created.name
  event_bus_name = aws_cloudwatch_event_bus.sre.name
  target_id      = "runbook"
  arn            = aws_sfn_state_machine.runbook.arn
  role_arn       = aws_iam_role.events_invoke.arn
  input_path     = "$.detail"

  retry_policy {
    maximum_event_age_in_seconds = local.target_retry.maximum_event_age_in_seconds
    maximum_retry_attempts       = local.target_retry.maximum_retry_attempts
  }
  dead_letter_config {
    arn = aws_sqs_queue.events_dlq.arn
  }
}

resource "aws_cloudwatch_event_target" "to_sns" {
  for_each = {
    created  = aws_cloudwatch_event_rule.incident_created.name
    resolved = aws_cloudwatch_event_rule.incident_resolved.name
  }

  rule           = each.value
  event_bus_name = aws_cloudwatch_event_bus.sre.name
  target_id      = "sns"
  arn            = aws_sns_topic.alerts.arn

  input_transformer {
    input_paths = {
      severity  = "$.detail.severity"
      service   = "$.detail.service"
      alertname = "$.detail.alertname"
      status    = "$.detail.status"
      incident  = "$.detail.incident_id"
      summary   = "$.detail.annotations.summary"
    }
    input_template = "\"[<severity>] <service> - <alertname> is <status>. <summary> (incident <incident>)\""
  }

  retry_policy {
    maximum_event_age_in_seconds = local.target_retry.maximum_event_age_in_seconds
    maximum_retry_attempts       = local.target_retry.maximum_retry_attempts
  }
  dead_letter_config {
    arn = aws_sqs_queue.events_dlq.arn
  }
}

resource "aws_cloudwatch_event_target" "runbook_outcome_to_sns" {
  for_each = {
    escalated = {
      rule     = aws_cloudwatch_event_rule.incident_escalated.name
      template = "\"[ESCALATED] <service> - <alertname> needs a person: <reason> (incident <incident>)\""
      paths    = { reason = "$.detail.reason" }
    }
    auto_resolved = {
      rule     = aws_cloudwatch_event_rule.incident_auto_resolved.name
      template = "\"[AUTO-RESOLVED] <service> - <alertname> recovered after <action>, <mttr>s after it was first seen (incident <incident>)\""
      paths    = { action = "$.detail.action", mttr = "$.detail.mttr_seconds" }
    }
  }

  rule           = each.value.rule
  event_bus_name = aws_cloudwatch_event_bus.sre.name
  target_id      = "sns"
  arn            = aws_sns_topic.alerts.arn

  input_transformer {
    input_paths = merge({
      service   = "$.detail.service"
      alertname = "$.detail.alertname"
      incident  = "$.detail.incident_id"
    }, each.value.paths)
    input_template = each.value.template
  }

  retry_policy {
    maximum_event_age_in_seconds = local.target_retry.maximum_event_age_in_seconds
    maximum_retry_attempts       = local.target_retry.maximum_retry_attempts
  }
  dead_letter_config {
    arn = aws_sqs_queue.events_dlq.arn
  }
}

resource "aws_cloudwatch_event_target" "resolved_to_recorder" {
  rule           = aws_cloudwatch_event_rule.incident_resolved.name
  event_bus_name = aws_cloudwatch_event_bus.sre.name
  target_id      = "incident-recorder"
  arn            = aws_sfn_state_machine.incident_recorder.arn
  role_arn       = aws_iam_role.events_invoke.arn

  retry_policy {
    maximum_event_age_in_seconds = local.target_retry.maximum_event_age_in_seconds
    maximum_retry_attempts       = local.target_retry.maximum_retry_attempts
  }
  dead_letter_config {
    arn = aws_sqs_queue.events_dlq.arn
  }
}

resource "aws_cloudwatch_event_target" "action_to_recorder" {
  rule           = aws_cloudwatch_event_rule.action_completed.name
  event_bus_name = aws_cloudwatch_event_bus.sre.name
  target_id      = "incident-recorder"
  arn            = aws_sfn_state_machine.incident_recorder.arn
  role_arn       = aws_iam_role.events_invoke.arn

  retry_policy {
    maximum_event_age_in_seconds = local.target_retry.maximum_event_age_in_seconds
    maximum_retry_attempts       = local.target_retry.maximum_retry_attempts
  }
  dead_letter_config {
    arn = aws_sqs_queue.events_dlq.arn
  }
}

resource "aws_cloudwatch_event_target" "all_to_logs" {
  rule           = aws_cloudwatch_event_rule.all_events.name
  event_bus_name = aws_cloudwatch_event_bus.sre.name
  target_id      = "audit-log"
  arn            = aws_cloudwatch_log_group.events.arn

  retry_policy {
    maximum_event_age_in_seconds = local.target_retry.maximum_event_age_in_seconds
    maximum_retry_attempts       = local.target_retry.maximum_retry_attempts
  }
  dead_letter_config {
    arn = aws_sqs_queue.events_dlq.arn
  }
}

# ------------------------------------------------------------------ incident recorder

resource "aws_iam_role" "incident_recorder" {
  name = "${var.name}-incident-recorder"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "incident_recorder" {
  role = aws_iam_role.incident_recorder.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.incidents.arn
      },
      {
        # Log delivery setup; these actions don't take resource-level permissions
        Effect = "Allow"
        Action = ["logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery",
          "logs:DeleteLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy",
        "logs:DescribeResourcePolicies", "logs:DescribeLogGroups"]
        Resource = "*"
      }
    ]
  })
}

# Express workflows keep no execution history, so failed writes only show up here
resource "aws_cloudwatch_log_group" "incident_recorder" {
  name              = "/aws/vendedlogs/states/${var.name}-incident-recorder"
  retention_in_days = 30
  kms_key_id        = aws_kms_key.platform.arn
  tags              = var.tags
}

resource "aws_sfn_state_machine" "incident_recorder" {
  name     = "${var.name}-incident-recorder"
  type     = "EXPRESS"
  role_arn = aws_iam_role.incident_recorder.arn
  definition = templatefile("${path.module}/stepfunctions/incident_recorder.asl.json", {
    table_name  = aws_dynamodb_table.incidents.name
    ttl_seconds = var.incident_retention_days * 86400
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.incident_recorder.arn}:*"
    include_execution_data = true
    level                  = "ERROR"
  }

  depends_on = [aws_iam_role_policy.incident_recorder]

  tags = var.tags
}

# ------------------------------------------------------------------ EventBridge -> Step Functions

resource "aws_iam_role" "events_invoke" {
  name = "${var.name}-eventbridge-targets"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "events_invoke" {
  role = aws_iam_role.events_invoke.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["states:StartExecution"]
      Resource = [
        aws_sfn_state_machine.runbook.arn,
        aws_sfn_state_machine.incident_recorder.arn,
      ]
    }]
  })
}

# ------------------------------------------------------------------ SNS, logs, archive, DLQ

resource "aws_sns_topic_policy" "alerts" {
  arn = aws_sns_topic.alerts.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EventBridgePublish"
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.alerts.arn
        Condition = {
          ArnEquals = {
            "aws:SourceArn" = [
              aws_cloudwatch_event_rule.incident_created.arn,
              aws_cloudwatch_event_rule.incident_resolved.arn,
              aws_cloudwatch_event_rule.incident_escalated.arn,
              aws_cloudwatch_event_rule.incident_auto_resolved.arn,
            ]
          }
        }
      },
      {
        Sid       = "DenyWithoutTLS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.alerts.arn
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })
}

# EventBridge requires log groups it writes to be under /aws/events/
resource "aws_cloudwatch_log_group" "events" {
  name              = "/aws/events/${var.name}"
  retention_in_days = 30
  kms_key_id        = aws_kms_key.platform.arn
  tags              = var.tags
}

resource "aws_cloudwatch_log_resource_policy" "events" {
  policy_name = "${var.name}-eventbridge-to-logs"
  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = ["events.amazonaws.com", "delivery.logs.amazonaws.com"] }
      Action    = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource  = "${aws_cloudwatch_log_group.events.arn}:*"
    }]
  })
}

resource "aws_cloudwatch_event_archive" "sre" {
  name             = "${var.name}-archive"
  description      = "Every event on the bus, kept 7 days for replay"
  event_source_arn = aws_cloudwatch_event_bus.sre.arn
  retention_days   = 7
}

resource "aws_sqs_queue" "events_dlq" {
  name                      = "${var.name}-events-dlq"
  message_retention_seconds = 1209600 # 14 days, the maximum
  kms_master_key_id         = aws_kms_key.platform.arn
  tags                      = var.tags
}

resource "aws_sqs_queue_policy" "events_dlq" {
  queue_url = aws_sqs_queue.events_dlq.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EventBridgeDeadLetters"
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.events_dlq.arn
        Condition = {
          ArnEquals = {
            "aws:SourceArn" = [
              aws_cloudwatch_event_rule.incident_created.arn,
              aws_cloudwatch_event_rule.incident_resolved.arn,
              aws_cloudwatch_event_rule.action_completed.arn,
              aws_cloudwatch_event_rule.incident_escalated.arn,
              aws_cloudwatch_event_rule.incident_auto_resolved.arn,
              aws_cloudwatch_event_rule.all_events.arn,
            ]
          }
        }
      },
      {
        Sid       = "DenyWithoutTLS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "sqs:*"
        Resource  = aws_sqs_queue.events_dlq.arn
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })
}
