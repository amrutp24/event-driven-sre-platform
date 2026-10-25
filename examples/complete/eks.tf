module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.29"

  cluster_name    = var.eks_cluster_name
  cluster_version = var.eks_version

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  # The public endpoint is how deploy.sh reaches the cluster from a laptop.
  # Narrow eks_public_access_cidrs to your own address.
  cluster_endpoint_private_access      = true
  cluster_endpoint_public_access       = true
  cluster_endpoint_public_access_cidrs = var.eks_public_access_cidrs

  cluster_enabled_log_types = ["api", "audit", "authenticator", "controllerManager", "scheduler"]

  enable_irsa = true

  # Whoever runs terraform apply gets cluster admin through an access entry,
  # so bootstrap.sh and deploy.sh can use kubectl straight away
  authentication_mode                      = "API_AND_CONFIG_MAP"
  enable_cluster_creator_admin_permissions = true

  # The sre module adds runbook_action's access entry and its 443 rule on the
  # cluster security group, so neither is set here.

  # The metrics-server add-on listens on 10251, which the module's default
  # node rules don't open. Without it the HPA never gets CPU metrics.
  node_security_group_additional_rules = {
    metrics_server = {
      description                   = "Control plane to metrics-server"
      protocol                      = "tcp"
      from_port                     = 10251
      to_port                       = 10251
      type                          = "ingress"
      source_cluster_security_group = true
    }
  }

  # IMDSv2 only, with a hop limit of 1 so pods can't reach the node's IAM role
  # through instance metadata. Pods that need AWS access use IRSA.
  eks_managed_node_group_defaults = {
    metadata_options = {
      http_endpoint               = "enabled"
      http_tokens                 = "required"
      http_put_response_hop_limit = 1
    }
  }

  # Managed node groups (production baseline). EKS stopped publishing Amazon
  # Linux 2 AMIs after Kubernetes 1.32, so both groups run AL2023.
  eks_managed_node_groups = {
    system = {
      name     = "${var.eks_cluster_name}-system"
      ami_type = "AL2023_x86_64_STANDARD"
      # The module's default role name prefix runs past IAM's 38 character limit
      iam_role_name            = "${var.eks_cluster_name}-system-node"
      iam_role_use_name_prefix = false
      instance_types           = [var.node_instance_type_system]
      min_size                 = 2
      max_size                 = 6
      desired_size             = 2

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
      name     = "${var.eks_cluster_name}-apps"
      ami_type = "AL2023_x86_64_STANDARD"
      # The module's default role name prefix runs past IAM's 38 character limit
      iam_role_name            = "${var.eks_cluster_name}-apps-node"
      iam_role_use_name_prefix = false
      instance_types           = [var.node_instance_type_apps]
      min_size                 = 2
      max_size                 = 10
      desired_size             = 2

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
    # Community add-on; the checkout HPA needs the metrics API
    metrics-server = { most_recent = true }
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
