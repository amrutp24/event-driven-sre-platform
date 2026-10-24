data "aws_region" "current" {}
data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  region     = data.aws_region.current.name
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  # The module's key, or the one passed in with create_kms_key = false
  kms_key_arn = var.create_kms_key ? aws_kms_key.platform[0].arn : var.kms_key_arn

  # Built from the name when the module doesn't create the parameter. Names
  # with a leading slash keep it after "parameter" in the ARN.
  degraded_mode_parameter_name = var.create_degraded_mode_parameter ? aws_ssm_parameter.checkout_degraded_mode[0].name : var.degraded_mode_parameter_name
  degraded_mode_parameter_arn = (var.create_degraded_mode_parameter
    ? aws_ssm_parameter.checkout_degraded_mode[0].arn
    : "arn:${local.partition}:ssm:${local.region}:${local.account_id}:parameter/${trimprefix(var.degraded_mode_parameter_name, "/")}"
  )

  cluster_arn = "arn:${local.partition}:eks:${local.region}:${local.account_id}:cluster/${var.cluster_name}"
}
