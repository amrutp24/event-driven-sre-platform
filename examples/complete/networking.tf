locals {
  azs = slice(data.aws_availability_zones.available.names, 0, var.az_count)
}

data "aws_availability_zones" "available" {}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.7"

  name = "${var.name}-vpc"
  cidr = var.vpc_cidr

  azs             = local.azs
  private_subnets = [for i, az in local.azs : cidrsubnet(var.vpc_cidr, 4, i)]
  public_subnets  = [for i, az in local.azs : cidrsubnet(var.vpc_cidr, 8, 100 + i)]

  enable_nat_gateway     = true
  single_nat_gateway     = false
  one_nat_gateway_per_az = true

  enable_dns_hostnames = true
  enable_dns_support   = true

  # Flow logs to CloudWatch, encrypted, 30 days. Billed per GB ingested.
  enable_flow_log                                 = true
  create_flow_log_cloudwatch_log_group            = true
  create_flow_log_cloudwatch_iam_role             = true
  flow_log_max_aggregation_interval               = 60
  flow_log_cloudwatch_log_group_retention_in_days = 30
  flow_log_cloudwatch_log_group_kms_key_id        = aws_kms_key.platform.arn

  # Required by AWS Load Balancer Controller for subnet discovery
  public_subnet_tags = {
    "kubernetes.io/role/elb"                        = "1"
    "kubernetes.io/cluster/${var.eks_cluster_name}" = "shared"
  }

  private_subnet_tags = {
    "kubernetes.io/role/internal-elb"               = "1"
    "kubernetes.io/cluster/${var.eks_cluster_name}" = "shared"
  }

  tags = merge(var.tags, {
    "project" = var.name
  })
}

# ------------------------------------------------------------------ VPC endpoints

# S3 (ECR image layers) and DynamoDB stay off the NAT gateways. Gateway
# endpoints are free, so they're always on.
resource "aws_vpc_endpoint" "gateway" {
  for_each = toset(["s3", "dynamodb"])

  vpc_id            = module.vpc.vpc_id
  service_name      = "com.amazonaws.${var.aws_region}.${each.key}"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = module.vpc.private_route_table_ids

  tags = merge(var.tags, { Name = "${var.name}-${each.key}" })
}

# Interface endpoints cost per AZ per hour (about $88/month for the six here in
# two AZs), so they're opt-in. The execute-api endpoint for a private webhook
# is made by the sre module.
locals {
  interface_endpoints = var.enable_vpc_endpoints ? toset(["ecr.api", "ecr.dkr", "sts", "ssm", "eks", "logs"]) : toset([])
}

resource "aws_security_group" "vpc_endpoints" {
  #checkov:skip=CKV2_AWS_5:attached to aws_vpc_endpoint.interface
  count = var.enable_vpc_endpoints ? 1 : 0

  name        = "${var.name}-vpc-endpoints"
  description = "Interface VPC endpoints"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description = "HTTPS from the VPC"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  tags = var.tags
}

# With private DNS on, the services' normal hostnames resolve to these
# endpoints inside the VPC.
resource "aws_vpc_endpoint" "interface" {
  for_each = local.interface_endpoints

  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.vpc.private_subnets
  security_group_ids  = [aws_security_group.vpc_endpoints[0].id]
  private_dns_enabled = true

  tags = merge(var.tags, { Name = "${var.name}-${each.key}" })
}
