# Incident messages in a Slack channel, through Amazon Q Developer in chat
# applications (AWS Chatbot). Authorize the Slack workspace in the AWS console
# first (Amazon Q Developer in chat applications > Configure new client >
# Slack), then invite @Amazon Q to the channel.

locals {
  slack_enabled = var.slack_workspace_id != null && var.slack_channel_id != null
}

# The channel only receives notifications, so the role it runs as has no
# permissions of its own
resource "aws_iam_role" "chatbot" {
  count = local.slack_enabled ? 1 : 0

  name = "${var.name}-chatbot"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "chatbot.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_chatbot_slack_channel_configuration" "incidents" {
  count    = local.slack_enabled ? 1 : 0
  provider = aws.chatbot

  configuration_name = "${var.name}-incidents"
  iam_role_arn       = aws_iam_role.chatbot[0].arn
  slack_team_id      = var.slack_workspace_id
  slack_channel_id   = var.slack_channel_id
  sns_topic_arns     = [module.sre.chat_topic_arn]

  # Caps what anyone in the channel could do through the integration
  guardrail_policy_arns = ["arn:aws:iam::aws:policy/ReadOnlyAccess"]
  logging_level         = "ERROR"

  tags = var.tags
}
