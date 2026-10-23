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

variable "webhook_require_api_key" {
  description = "Require an X-API-Key header on POST /webhook. Creates one key for Alertmanager and one for Datadog."
  type        = bool
  default     = true
}

variable "webhook_rate_limit" {
  description = "Steady-state requests per second allowed on the webhook"
  type        = number
  default     = 1000
}

variable "webhook_burst_limit" {
  description = "Burst of requests allowed on the webhook"
  type        = number
  default     = 2000
}

variable "alert_ingest_reserved_concurrency" {
  description = "Concurrent executions reserved for alert_ingest. AWS keeps 100 unreserved per account, so accounts with a low concurrency limit need -1 (no reservation)."
  type        = number
  default     = 100
}
