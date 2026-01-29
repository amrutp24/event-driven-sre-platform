# AWS Load Balancer Controller IRSA
module "alb_controller_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"

  role_name                              = "${var.name}-alb-controller"
  attach_load_balancer_controller_policy = true

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["kube-system:aws-load-balancer-controller"]
    }
  }

  tags = var.tags
}

# ExternalDNS IRSA (Route53)
module "external_dns_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"

  role_name                     = "${var.name}-external-dns"
  attach_external_dns_policy     = true
  external_dns_hosted_zone_arns  = var.route53_zone_arns

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["kube-system:external-dns"]
    }
  }

  tags = var.tags
}

# Cert-manager IRSA is optional depending on DNS01 integrations (Route53)
module "cert_manager_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"

  role_name = "${var.name}-cert-manager"

  # Minimal Route53 permissions for DNS01 challenges (optional; enable if you use Route53 solver)
  create_role = var.cert_manager_route53_enabled
  role_policy_arns = var.cert_manager_route53_enabled ? [] : []

  oidc_providers = var.cert_manager_route53_enabled ? {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["cert-manager:cert-manager"]
    }
  } : {}

  tags = var.tags
}

resource "aws_iam_policy" "cert_manager_route53" {
  count       = var.cert_manager_route53_enabled ? 1 : 0
  name        = "${var.name}-cert-manager-route53"
  description = "Route53 permissions for cert-manager DNS01"
  policy      = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect = "Allow",
        Action = [
          "route53:ChangeResourceRecordSets"
        ],
        Resource = var.route53_zone_arns
      },
      {
        Effect = "Allow",
        Action = [
          "route53:ListHostedZones",
          "route53:ListResourceRecordSets",
          "route53:GetChange"
        ],
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "cert_manager_route53_attach" {
  count      = var.cert_manager_route53_enabled ? 1 : 0
  role       = module.cert_manager_irsa.iam_role_name
  policy_arn = aws_iam_policy.cert_manager_route53[0].arn
}
