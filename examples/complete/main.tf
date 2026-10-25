# A full environment: VPC, EKS cluster, IRSA roles, ECR, CloudTrail and the
# alert-to-remediation pipeline from the root module.

data "aws_caller_identity" "current" {}

module "sre" {
  source = "../.."

  name = var.name
  tags = var.tags

  cluster_name              = module.eks.cluster_name
  vpc_id                    = module.vpc.vpc_id
  subnet_ids                = module.vpc.private_subnets
  cluster_security_group_id = module.eks.cluster_security_group_id

  # One key for the module's data and the CloudTrail bucket, in kms.tf
  create_kms_key = false
  kms_key_arn    = aws_kms_key.platform.arn

  # The checkout app's alerts (helm/observability) and what the runbook does
  # for each. drain_node only touches the apps node group.
  runbook_routes = {
    CheckoutHighErrorRate  = { action = "degrade_or_scale" }
    CheckoutHighLatencyP95 = { action = "degrade_or_scale" }
    CheckoutSLOBurnFast    = { action = "degrade_or_scale" }
    CheckoutDown           = { action = "restart" }
    NodeUnderPressure      = { action = "drain_node" }
  }
  remediation_namespaces       = ["apps"]
  default_deployment           = "checkout"
  degraded_mode_deployment     = "checkout"
  degraded_mode_parameter_name = "/checkout/degraded_mode"
  drainable_node_label         = "nodegroup=apps"

  webhook_private         = var.webhook_private
  webhook_require_api_key = var.webhook_require_api_key

  auto_remediate_severities           = var.auto_remediate_severities
  verify_timeout_seconds              = var.verify_timeout_seconds
  incidents_deletion_protection       = var.incidents_deletion_protection
  alert_ingest_reserved_concurrency   = var.alert_ingest_reserved_concurrency
  runbook_action_reserved_concurrency = var.runbook_action_reserved_concurrency

  # Email straight from SNS; Slack through chatbot.tf
  notification_emails = var.notification_emails
  chat_notifications  = var.slack_workspace_id != null && var.slack_channel_id != null
}
