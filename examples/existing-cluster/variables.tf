variable "region" {
  description = "Region the cluster is in"
  type        = string
}

variable "cluster_name" {
  description = "EKS cluster the runbook remediates"
  type        = string
}

variable "vpc_id" {
  description = "VPC the cluster is in"
  type        = string
}

variable "subnet_ids" {
  description = "Private subnets that can reach the cluster's API endpoint, for the runbook_action Lambda"
  type        = list(string)
}

variable "cluster_security_group_id" {
  description = "The cluster security group. The module adds a 443 ingress rule to it for the Lambda."
  type        = string
}

variable "remediation_namespaces" {
  description = "Namespaces the runbook may change. The first is the target when an alert doesn't name one."
  type        = list(string)
}

variable "runbook_routes" {
  description = "Action for each alertname, e.g. { MyAppDown = { action = \"restart\" } }"
  type = map(object({
    action = string
  }))
  default = {}
}
