# Who hears about incidents.
#
# Email subscribes straight to the alerts topic. Slack (or Teams) goes through
# AWS's chat integration, Amazon Q Developer in chat applications, which only
# renders its own custom notification format. So with chat_notifications on,
# the same four rules also feed a chat topic with messages in that format, and
# the channel configuration subscribes to it (see examples/complete/chatbot.tf;
# its API isn't in every region, so it's set up outside the module).

resource "aws_sns_topic_subscription" "email" {
  for_each = toset(var.notification_emails)

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = each.value
}

resource "aws_sns_topic" "chat" {
  count = var.chat_notifications ? 1 : 0

  name              = "${var.name}-chat"
  kms_master_key_id = local.kms_key_arn
  tags              = var.tags
}

resource "aws_sns_topic_policy" "chat" {
  count = var.chat_notifications ? 1 : 0

  arn = aws_sns_topic.chat[0].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EventBridgePublish"
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.chat[0].arn
        Condition = {
          ArnEquals = { "aws:SourceArn" = [for m in local.chat_messages : m.rule_arn] }
        }
      },
      {
        Sid       = "DenyWithoutTLS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.chat[0].arn
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })
}

locals {
  chat_messages = {
    created = {
      rule     = aws_cloudwatch_event_rule.incident_created.name
      rule_arn = aws_cloudwatch_event_rule.incident_created.arn
      title    = "<severity>: <alertname> is firing on <service>"
      text     = "<summary>\n\nIncident <incident>"
      paths    = { severity = "$.detail.severity", summary = "$.detail.annotations.summary" }
    }
    resolved = {
      rule     = aws_cloudwatch_event_rule.incident_resolved.name
      rule_arn = aws_cloudwatch_event_rule.incident_resolved.arn
      title    = "Resolved: <alertname> on <service>"
      text     = "<summary>\n\nIncident <incident>"
      paths    = { summary = "$.detail.annotations.summary" }
    }
    escalated = {
      rule     = aws_cloudwatch_event_rule.incident_escalated.name
      rule_arn = aws_cloudwatch_event_rule.incident_escalated.arn
      title    = "Escalated: <alertname> on <service> needs a person"
      text     = "<reason>\n\nIncident <incident>"
      paths    = { reason = "$.detail.reason" }
    }
    auto_resolved = {
      rule     = aws_cloudwatch_event_rule.incident_auto_resolved.name
      rule_arn = aws_cloudwatch_event_rule.incident_auto_resolved.arn
      title    = "Auto-resolved: <alertname> on <service>"
      text     = "Recovered after <action>, <mttr>s after it was first seen.\n\nIncident <incident>"
      paths    = { action = "$.detail.action", mttr = "$.detail.mttr_seconds" }
    }
  }
}

resource "aws_cloudwatch_event_target" "to_chat" {
  for_each = var.chat_notifications ? local.chat_messages : {}

  rule           = each.value.rule
  event_bus_name = aws_cloudwatch_event_bus.sre.name
  target_id      = "chat"
  arn            = aws_sns_topic.chat[0].arn

  input_transformer {
    input_paths = merge({
      service   = "$.detail.service"
      alertname = "$.detail.alertname"
      incident  = "$.detail.incident_id"
    }, each.value.paths)

    # jsonencode escapes < and >, and EventBridge only sees <placeholders>
    # written plainly, so put them back
    input_template = replace(replace(jsonencode({
      version = "1.0"
      source  = "custom"
      content = {
        textType    = "client-markdown"
        title       = each.value.title
        description = each.value.text
      }
    }), "\\u003c", "<"), "\\u003e", ">")
  }

  retry_policy {
    maximum_event_age_in_seconds = local.target_retry.maximum_event_age_in_seconds
    maximum_retry_attempts       = local.target_retry.maximum_retry_attempts
  }
  dead_letter_config {
    arn = aws_sqs_queue.events_dlq.arn
  }
}
