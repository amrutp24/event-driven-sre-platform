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

variable "auto_remediate_severities" {
  description = "Severities the runbook acts on. Others only notify."
  type        = list(string)
  default     = ["critical"]

  validation {
    condition     = alltrue([for s in var.auto_remediate_severities : contains(["critical", "warning", "info"], s)])
    error_message = "Each severity must be critical, warning or info."
  }
}

variable "stabilization_seconds" {
  description = "How long the runbook waits after an action before it checks whether the alert resolved"
  type        = number
  default     = 60

  validation {
    condition     = var.stabilization_seconds >= 1 && floor(var.stabilization_seconds) == var.stabilization_seconds
    error_message = "Must be a whole number of seconds, at least 1."
  }
}

variable "verify_poll_seconds" {
  description = "How often the runbook checks for the resolved alert after the stabilization wait"
  type        = number
  default     = 30

  validation {
    condition     = var.verify_poll_seconds >= 5 && floor(var.verify_poll_seconds) == var.verify_poll_seconds
    error_message = "Must be a whole number of seconds, at least 5."
  }
}

variable "verify_timeout_seconds" {
  description = "How long after the action the alert has to resolve before the incident is escalated. Alertmanager only sends the resolved notification at its group_interval, so leave room for that."
  type        = number
  default     = 600

  validation {
    condition     = var.verify_timeout_seconds >= 60
    error_message = "Must be at least 60 seconds."
  }
}

variable "incident_retention_days" {
  description = "Days after an incident's last activity before DynamoDB deletes it"
  type        = number
  default     = 90

  validation {
    condition     = var.incident_retention_days >= 1 && floor(var.incident_retention_days) == var.incident_retention_days
    error_message = "Must be a whole number of days, at least 1."
  }
}

variable "incidents_deletion_protection" {
  description = "Deletion protection on the incidents table. scripts/destroy.sh sets this to false before tearing down."
  type        = bool
  default     = true
}

variable "drain_min_schedulable_nodes" {
  description = "drain_node refuses to drain an apps node unless at least this many other apps nodes are Ready and schedulable"
  type        = number
  default     = 1

  validation {
    condition     = var.drain_min_schedulable_nodes >= 1
    error_message = "Must be at least 1."
  }
}

variable "remediation_max_replicas" {
  description = "Highest replica count runbook_action will scale a deployment to"
  type        = number
  default     = 10

  validation {
    condition     = var.remediation_max_replicas >= 1
    error_message = "Must be at least 1."
  }
}
