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
  value = try("${aws_apigatewayv2_api.alerts_api.api_endpoint}/${aws_apigatewayv2_stage.prod.name}/alert", null)
}

output "runbook_action_lambda_arn" {
  value = aws_lambda_function.runbook_action.arn
}
