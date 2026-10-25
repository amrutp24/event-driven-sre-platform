data "aws_region" "current" {}
data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  region     = data.aws_region.current.name
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  # The module's key, or the one passed in with create_kms_key = false
  kms_key_arn = var.create_kms_key ? aws_kms_key.platform[0].arn : var.kms_key_arn

  # Degraded mode only exists with a deployment to read it. The ARN is built
  # from the name when the module doesn't create the parameter; names with a
  # leading slash keep it after "parameter" in the ARN.
  degraded_mode_enabled          = var.degraded_mode_deployment != null
  create_degraded_mode_parameter = local.degraded_mode_enabled && var.create_degraded_mode_parameter
  degraded_mode_parameter_name = (local.degraded_mode_enabled
    ? coalesce(var.degraded_mode_parameter_name, "/${var.name}/degraded-mode")
    : null
  )
  degraded_mode_parameter_arn = (!local.degraded_mode_enabled ? null
    : local.create_degraded_mode_parameter ? aws_ssm_parameter.degraded_mode[0].arn
    : "arn:${local.partition}:ssm:${local.region}:${local.account_id}:parameter/${trimprefix(local.degraded_mode_parameter_name, "/")}"
  )

  cluster_arn = "arn:${local.partition}:eks:${local.region}:${local.account_id}:cluster/${var.cluster_name}"
}
