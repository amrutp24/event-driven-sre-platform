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
  description = "Kubernetes version. An existing cluster has to be upgraded one minor version at a time."
  type        = string
  default     = "1.36"
}

variable "eks_public_access_cidrs" {
  description = "CIDRs allowed to reach the EKS public API endpoint. The default is open to the internet; set it to your own address, e.g. [\"203.0.113.10/32\"]."
  type        = list(string)
  default     = ["0.0.0.0/0"]
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

variable "enable_vpc_endpoints" {
  description = "Interface endpoints for ECR, STS, SSM, EKS and CloudWatch Logs, so that traffic skips the NAT gateways. About $88/month for six endpoints in two AZs. The S3 and DynamoDB gateway endpoints are always on."
  type        = bool
  default     = false
}

variable "enable_cloudtrail" {
  description = "Create a multi-region CloudTrail trail for this account. Turn it off if an organization trail already covers the account; a second copy of management events is billed."
  type        = bool
  default     = true
}

variable "cloudtrail_retention_days" {
  description = "Days CloudTrail log files are kept in the trail bucket"
  type        = number
  default     = 365

  validation {
    condition     = var.cloudtrail_retention_days >= 1 && floor(var.cloudtrail_retention_days) == var.cloudtrail_retention_days
    error_message = "Must be a whole number of days, at least 1."
  }
}

variable "cloudtrail_force_destroy" {
  description = "Let terraform destroy delete the trail bucket with its logs in it. scripts/destroy.sh sets this to true before tearing down."
  type        = bool
  default     = false
}

# Passed to the sre module. The module checks the values; see its variables
# for the rest of its settings.

variable "incidents_deletion_protection" {
  description = "Deletion protection on the incidents table. scripts/destroy.sh sets this to false before tearing down."
  type        = bool
  default     = true
}

variable "webhook_private" {
  description = "Make the webhook a private API, reachable only through an execute-api VPC endpoint. Datadog can't reach a private API, so only use this when Alertmanager is the only sender."
  type        = bool
  default     = false
}

variable "webhook_require_api_key" {
  description = "Require an X-API-Key header on POST /webhook. Creates one key for Alertmanager and one for Datadog."
  type        = bool
  default     = true
}

variable "auto_remediate_severities" {
  description = "Severities the runbook acts on. Others only notify."
  type        = list(string)
  default     = ["critical"]
}

variable "verify_timeout_seconds" {
  description = "How long after the action the alert has to resolve before the incident is escalated"
  type        = number
  default     = 600
}

variable "alert_ingest_reserved_concurrency" {
  description = "Concurrent executions reserved for alert_ingest. AWS keeps 100 unreserved per account, so accounts with a low concurrency limit need -1 (no reservation)."
  type        = number
  default     = 100
}
