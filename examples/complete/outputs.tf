# scripts/ and the docs read these names, so keep them stable

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

output "checkout_role_arn" {
  description = "IRSA role for the checkout service account; reads the degraded-mode parameter"
  value       = module.checkout_irsa.iam_role_arn
}

output "checkout_image_repository" {
  description = "ECR repository for the checkout image; scripts/build-image.sh pushes here"
  value       = aws_ecr_repository.checkout.repository_url
}

output "cloudtrail_bucket" {
  description = "S3 bucket holding the CloudTrail logs; empty when enable_cloudtrail is false"
  value       = try(aws_s3_bucket.cloudtrail[0].bucket, "")
}

output "kms_key_arn" {
  description = "Key for the incidents table, SNS topic, DLQ, log groups and CloudTrail"
  value       = aws_kms_key.platform.arn
}

output "alert_webhook_url" {
  description = "POST alerts here"
  value       = module.sre.alert_webhook_url
}

output "alertmanager_api_key" {
  description = "X-API-Key for Alertmanager; empty when webhook_require_api_key is false"
  # The module outputs null then; deploy.sh reads this with -raw
  value     = module.sre.alertmanager_api_key != null ? module.sre.alertmanager_api_key : ""
  sensitive = true
}

output "datadog_api_key" {
  description = "X-API-Key for the Datadog webhook; empty when webhook_require_api_key is false"
  value       = module.sre.datadog_api_key != null ? module.sre.datadog_api_key : ""
  sensitive   = true
}

output "event_bus_name" {
  value = module.sre.event_bus_name
}

output "step_functions_arn" {
  description = "Runbook state machine, started by the incident.created rule"
  value       = module.sre.runbook_state_machine_arn
}

output "incident_recorder_arn" {
  description = "Express state machine that writes incident events to DynamoDB"
  value       = module.sre.incident_recorder_arn
}

output "incidents_table" {
  value = module.sre.incidents_table_name
}

output "sns_topic_arn" {
  value = module.sre.sns_topic_arn
}

output "events_dlq_url" {
  description = "Events EventBridge couldn't deliver after retries"
  value       = module.sre.events_dlq_url
}

output "events_log_group" {
  description = "Every event on the bus"
  value       = module.sre.events_log_group
}

output "events_archive_arn" {
  description = "Replay source; events are kept 7 days"
  value       = module.sre.events_archive_arn
}

output "runbook_log_group" {
  description = "Runbook execution logs, including state input and output"
  value       = module.sre.runbook_log_group
}

output "incident_recorder_log_group" {
  description = "Incident recorder failures. Express workflows have no execution history, so this is where they show up."
  value       = module.sre.incident_recorder_log_group
}

output "runbook_action_lambda_arn" {
  value = module.sre.runbook_action_function_arn
}

output "rbac_manifest" {
  description = "What runbook_action may do in the cluster; deploy.sh applies it"
  value       = module.sre.rbac_manifest
}
