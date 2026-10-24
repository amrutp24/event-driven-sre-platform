provider "aws" {
  region = var.region
}

module "sre" {
  source = "../.."

  cluster_name              = var.cluster_name
  vpc_id                    = var.vpc_id
  subnet_ids                = var.subnet_ids
  cluster_security_group_id = var.cluster_security_group_id
}
