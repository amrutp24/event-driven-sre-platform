output "aws_region" {
  value = var.aws_region
}

output "eks_cluster_name" {
  value = module.eks.cluster_name
}

output "eks_cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

output "eks_oidc_provider_arn" {
  value = module.eks.oidc_provider_arn
}

output "vpc_id" {
  value = module.vpc.vpc_id
}

output "private_subnets" {
  value = module.vpc.private_subnets
}

output "public_subnets" {
  value = module.vpc.public_subnets
}

output "alb_controller_role_arn" {
  value = module.alb_controller_irsa.iam_role_arn
}

output "external_dns_role_arn" {
  value = module.external_dns_irsa.iam_role_arn
}

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

output "runbook_action_lambda_arn" {
  value = aws_lambda_function.runbook_action.arn
}

output "event_bus_name" {
  value = aws_cloudwatch_event_bus.sre.name
}

output "step_functions_arn" {
  description = "Runbook state machine, started by the incident.created rule"
  value       = aws_sfn_state_machine.runbook.arn
}

output "incident_recorder_arn" {
  description = "Express state machine that writes incident events to DynamoDB"
  value       = aws_sfn_state_machine.incident_recorder.arn
}

output "incidents_table" {
  value = aws_dynamodb_table.incidents.name
}

output "sns_topic_arn" {
  value = aws_sns_topic.alerts.arn
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
