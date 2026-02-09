# CI/CD Terraform Variables
# Used for GitHub Actions testing and validation
name                      = "event-driven-sre-ci"
aws_region                = "us-east-1"
eks_cluster_name          = "event-driven-sre-ci-eks"
eks_version               = "1.29"
vpc_cidr                  = "10.40.0.0/16"
az_count                  = 2
node_instance_type_system = "t3.medium"
node_instance_type_apps   = "t3.medium"
tags = {
  env         = "ci"
  owner       = "sre-team"
  project     = "event-driven-sre-platform"
  cost_center = "engineering"
}

# Route53 hosted zone ARNs for ExternalDNS and optionally cert-manager DNS01 solver
route53_zone_arns            = []
cert_manager_route53_enabled = false