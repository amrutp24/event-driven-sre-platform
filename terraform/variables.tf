variable "name" {
  type    = string
  default = "event-driven-sre"
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "eks_cluster_name" {
  type    = string
  default = "event-driven-sre-eks"
}

variable "eks_version" {
  type    = string
  default = "1.29"
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "az_count" {
  type    = number
  default = 2
}

variable "node_instance_type_system" {
  type    = string
  default = "m6i.large"
}

variable "node_instance_type_apps" {
  type    = string
  default = "m6i.large"
}

variable "tags" {
  type = map(string)
  default = {
    "env"   = "prod"
    "owner" = "you"
  }
}

# Route53 hosted zone ARNs for ExternalDNS and optionally cert-manager DNS01 solver
variable "route53_zone_arns" {
  type    = list(string)
  default = []
}

variable "cert_manager_route53_enabled" {
  type    = bool
  default = false
}
