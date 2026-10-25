output "alert_webhook_url" {
  description = "POST alerts here"
  value       = "${aws_api_gateway_stage.prod.invoke_url}/webhook"
}

output "alertmanager_api_key" {
  description = "X-API-Key for Alertmanager; empty when webhook_require_api_key is false"
  value       = try(aws_api_gateway_api_key.webhook["alertmanager"].value, "")
  sensitive   = true
}

output "datadog_api_key" {
  description = "X-API-Key for the Datadog webhook; empty when webhook_require_api_key is false"
  value       = try(aws_api_gateway_api_key.webhook["datadog"].value, "")
  sensitive   = true
}

output "event_bus_name" {
  description = "Custom EventBridge bus every sre.* event goes through"
  value       = aws_cloudwatch_event_bus.sre.name
}

output "event_bus_arn" {
  description = "ARN of the event bus, for rules or targets of your own"
  value       = aws_cloudwatch_event_bus.sre.arn
}

output "runbook_state_machine_arn" {
  description = "Runbook state machine, started by the incident.created rule"
  value       = aws_sfn_state_machine.runbook.arn
}

output "incident_recorder_arn" {
  description = "Express state machine that writes incident events to DynamoDB"
  value       = aws_sfn_state_machine.incident_recorder.arn
}

output "incidents_table_name" {
  description = "DynamoDB table with one item per incident; query the by-status index for open or escalated ones"
  value       = aws_dynamodb_table.incidents.name
}

output "incidents_table_arn" {
  description = "ARN of the incidents table"
  value       = aws_dynamodb_table.incidents.arn
}

output "sns_topic_arn" {
  description = "Incident notifications; subscribe on-call here"
  value       = aws_sns_topic.alerts.arn
}

output "events_dlq_url" {
  description = "Events EventBridge couldn't deliver after retries"
  value       = aws_sqs_queue.events_dlq.url
}

output "events_log_group" {
  description = "Every event on the bus"
  value       = aws_cloudwatch_log_group.events.name
}

output "events_archive_arn" {
  description = "Replay source; events are kept 7 days"
  value       = aws_cloudwatch_event_archive.sre.arn
}

output "runbook_log_group" {
  description = "Runbook execution logs, including state input and output"
  value       = aws_cloudwatch_log_group.runbook.name
}

output "incident_recorder_log_group" {
  description = "Incident recorder failures. Express workflows have no execution history, so this is where they show up."
  value       = aws_cloudwatch_log_group.incident_recorder.name
}

output "kms_key_arn" {
  description = "Key for the incidents table, SNS topic, DLQ and log groups: the module's own, or kms_key_arn"
  value       = local.kms_key_arn
}

output "runbook_action_function_arn" {
  description = "Lambda that carries out remediation actions against the cluster"
  value       = aws_lambda_function.runbook_action.arn
}

output "runbook_action_role_arn" {
  description = "IAM role runbook_action calls the Kubernetes API as"
  value       = aws_iam_role.runbook_lambda_role.arn
}

output "runbook_action_security_group_id" {
  description = "Security group of runbook_action; the module already allows it to the cluster API on 443"
  value       = aws_security_group.runbook_lambda.id
}

output "kubernetes_group" {
  description = "Group runbook_action's role is in; rbac_manifest binds it"
  value       = var.kubernetes_group
}

output "rbac_manifest" {
  description = "Kubernetes RBAC for kubernetes_group: a Role and RoleBinding in each of remediation_namespaces, and a ClusterRole for nodes and evictions when drainable_node_label is set. Apply it with terraform output -raw rbac_manifest | kubectl apply -f -"
  value = templatefile("${path.module}/templates/rbac.yaml.tftpl", {
    group      = var.kubernetes_group
    namespaces = var.remediation_namespaces
    drain_node = var.drainable_node_label != null
  })
}

output "degraded_mode_parameter_name" {
  description = "SSM parameter the degrade action sets to true; the app reads it"
  value       = local.degraded_mode_parameter_name
}

output "degraded_mode_parameter_arn" {
  description = "Grant the degraded-mode deployment ssm:GetParameter on this"
  value       = local.degraded_mode_parameter_arn
}

output "chat_topic_arn" {
  description = "Topic for a Slack or Teams channel configuration to subscribe to; empty when chat_notifications is false"
  value       = try(aws_sns_topic.chat[0].arn, "")
}
