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

  webhook_private         = var.webhook_private
  webhook_require_api_key = var.webhook_require_api_key

  auto_remediate_severities         = var.auto_remediate_severities
  verify_timeout_seconds            = var.verify_timeout_seconds
  incidents_deletion_protection     = var.incidents_deletion_protection
  alert_ingest_reserved_concurrency = var.alert_ingest_reserved_concurrency
}
