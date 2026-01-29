data "aws_caller_identity" "current" {}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = var.eks_cluster_name
  cluster_version = var.eks_version

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  # Private endpoint recommended; public enabled for ease of first bootstrap.
  cluster_endpoint_private_access = true
  cluster_endpoint_public_access  = true

  cluster_enabled_log_types = ["api", "audit", "authenticator", "controllerManager", "scheduler"]

  enable_irsa = true

  # Managed node groups (production baseline)
  eks_managed_node_groups = {
    system = {
      name           = "${var.eks_cluster_name}-system"
      instance_types = [var.node_instance_type_system]
      min_size       = 2
      max_size       = 6
      desired_size   = 2

      labels = {
        "nodegroup" = "system"
      }

      taints = [{
        key    = "dedicated"
        value  = "system"
        effect = "NO_SCHEDULE"
      }]

      subnet_ids = module.vpc.private_subnets
    }

    apps = {
      name           = "${var.eks_cluster_name}-apps"
      instance_types = [var.node_instance_type_apps]
      min_size       = 2
      max_size       = 10
      desired_size   = 2

      labels = {
        "nodegroup" = "apps"
      }

      subnet_ids = module.vpc.private_subnets
    }
  }

  # Add-ons
  cluster_addons = {
    coredns    = { most_recent = true }
    kube-proxy = { most_recent = true }
    vpc-cni    = { most_recent = true }
  }

  tags = merge(var.tags, {
    "project" = var.name
  })
}

# Optional: EBS CSI driver (recommended for stateful)
module "ebs_csi_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"

  role_name             = "${var.name}-ebs-csi"
  attach_ebs_csi_policy = true

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["kube-system:ebs-csi-controller-sa"]
    }
  }

  tags = var.tags
}
