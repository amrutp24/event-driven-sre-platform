variable "name" {
  description = "Prefix for every resource name. Letters, digits and hyphens, at most 40 characters (the event archive's name, <name>-archive, can't be longer than 48)."
  type        = string
  default     = "event-driven-sre"

  validation {
    condition     = length(var.name) <= 40 && can(regex("^[A-Za-z0-9-]+$", var.name))
    error_message = "Use only letters, digits and hyphens, at most 40 characters."
  }
}

variable "tags" {
  description = "Tags for every resource that takes them"
  type        = map(string)
  default     = {}
}

# ------------------------------------------------------------------ cluster and network

variable "cluster_name" {
  description = "EKS cluster that runbook_action remediates"
  type        = string
}

variable "vpc_id" {
  description = "VPC runbook_action runs in"
  type        = string

  validation {
    condition     = can(regex("^vpc-[0-9a-f]+$", var.vpc_id))
    error_message = "Must be a VPC ID, vpc-..."
  }
}

variable "subnet_ids" {
  description = "Private subnets for runbook_action, and for the execute-api endpoint when webhook_private is true"
  type        = list(string)

  validation {
    condition     = length(var.subnet_ids) > 0
    error_message = "Give at least one subnet."
  }
}

variable "cluster_security_group_id" {
  description = "The cluster's security group. The module adds an ingress rule for 443 from runbook_action's security group."
  type        = string

  validation {
    condition     = can(regex("^sg-[0-9a-f]+$", var.cluster_security_group_id))
    error_message = "Must be a security group ID, sg-..."
  }
}

variable "create_access_entry" {
  description = "Create an EKS access entry for runbook_action's role in kubernetes_group. The cluster needs access entries enabled (authentication mode API or API_AND_CONFIG_MAP)."
  type        = bool
  default     = true
}

variable "kubernetes_group" {
  description = "Kubernetes group runbook_action's role is mapped to. rbac_manifest binds it, and names its Roles after it."
  type        = string
  default     = "sre-remediation"

  validation {
    condition     = can(regex("^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$", var.kubernetes_group))
    error_message = "Must be a lowercase DNS label (letters, digits and hyphens, at most 63 characters), since rbac_manifest names its Roles after it."
  }
}

# ------------------------------------------------------------------ remediation

variable "remediation_namespaces" {
  description = "Namespaces runbook_action may act in. The first is the runbook's target when an alert doesn't name one."
  type        = list(string)

  validation {
    condition     = length(var.remediation_namespaces) > 0
    error_message = "Give at least one namespace."
  }
}

variable "default_deployment" {
  description = "Deployment the runbook targets when an alert names neither a deployment nor a service. With null, such an alert is escalated."
  type        = string
  default     = null
}

variable "runbook_routes" {
  description = "Which action the runbook takes for each alertname. An alert at an auto-remediated severity with no route (and no runbook_action annotation) is escalated, so with the default nothing is remediated."
  type = map(object({
    action = string
  }))
  default = {}

  validation {
    condition     = alltrue([for r in values(var.runbook_routes) : contains(["degrade", "restart", "scale", "degrade_or_scale", "drain_node"], r.action)])
    error_message = "Each action must be degrade, restart, scale, degrade_or_scale or drain_node."
  }
}

variable "degraded_mode_deployment" {
  description = "The only deployment degrade works for; it reads the degraded-mode parameter. With null, degraded mode is off: no parameter, no SSM permissions, and degrade and degrade_or_scale are rejected."
  type        = string
  default     = null
}

variable "degraded_mode_parameter_name" {
  description = "SSM parameter degrade sets to \"true\". Defaults to /<name>/degraded-mode. Only used with degraded_mode_deployment."
  type        = string
  default     = null
}

variable "create_degraded_mode_parameter" {
  description = "Create the degraded-mode parameter with the value \"false\". Turn it off if it already exists. Only used with degraded_mode_deployment."
  type        = bool
  default     = true
}

variable "drainable_node_label" {
  description = "drain_node only drains nodes with this label, as key=value. With null, drain_node is off: it's rejected, and rbac_manifest has no node permissions."
  type        = string
  default     = null

  validation {
    condition     = var.drainable_node_label == null || can(regex("^[^=]+=.*$", var.drainable_node_label))
    error_message = "Must be key=value."
  }
}

variable "drain_min_schedulable_nodes" {
  description = "drain_node refuses to drain a node unless at least this many other drainable nodes are Ready and schedulable"
  type        = number
  default     = 1

  validation {
    condition     = var.drain_min_schedulable_nodes >= 1
    error_message = "Must be at least 1."
  }
}

# Checked against remediation_max_replicas by a precondition on the runbook
variable "default_scale_replicas" {
  description = "Replicas scale and degrade_or_scale go to when the alert has no desired_replicas annotation. At most remediation_max_replicas."
  type        = number
  default     = 4

  validation {
    condition     = var.default_scale_replicas >= 1 && floor(var.default_scale_replicas) == var.default_scale_replicas
    error_message = "Must be a whole number, at least 1."
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

variable "runbook_action_reserved_concurrency" {
  description = "Concurrent executions reserved for runbook_action, which caps how many remediations hit the cluster at once (10 is a good cap). -1 leaves it unreserved, the default, because AWS keeps at least 50 to 100 executions unreserved per account and new accounts often have a limit of 50."
  type        = number
  default     = -1

  validation {
    condition     = var.runbook_action_reserved_concurrency >= -1 && floor(var.runbook_action_reserved_concurrency) == var.runbook_action_reserved_concurrency
    error_message = "Must be a whole number, -1 or more."
  }
}

variable "alert_ingest_reserved_concurrency" {
  description = "Concurrent executions reserved for alert_ingest. -1 leaves it unreserved, the default, because AWS keeps at least 50 to 100 executions unreserved per account and new accounts often have a limit of 50."
  type        = number
  default     = -1

  validation {
    condition     = var.alert_ingest_reserved_concurrency >= -1 && floor(var.alert_ingest_reserved_concurrency) == var.alert_ingest_reserved_concurrency
    error_message = "Must be a whole number, -1 or more."
  }
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

# ------------------------------------------------------------------ incidents and logs

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
  description = "Deletion protection on the incidents table. Set it to false before destroying."
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "Days the module's log groups keep logs"
  type        = number
  default     = 30

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.log_retention_days)
    error_message = "Must be a retention period CloudWatch Logs supports, e.g. 7, 30, 90 or 365."
  }
}

# ------------------------------------------------------------------ encryption

variable "create_kms_key" {
  description = "Create a KMS key for the incidents table, SNS topic, DLQ and log groups. Set it to false and pass kms_key_arn to use your own."
  type        = bool
  default     = true
}

# Checked against create_kms_key by a precondition on the incidents table;
# a validation can't refer to another variable before Terraform 1.9.
variable "kms_key_arn" {
  description = "Existing KMS key to use when create_kms_key is false. Its policy has to allow what the module's own key policy allows (see kms.tf)."
  type        = string
  default     = null

  validation {
    condition     = var.kms_key_arn == null || can(regex("^arn:[^:]+:kms:", var.kms_key_arn))
    error_message = "Must be a KMS key ARN."
  }
}

# ------------------------------------------------------------------ webhook

variable "webhook_require_api_key" {
  description = "Require an X-API-Key header on POST /webhook. Creates one key for Alertmanager and one for Datadog."
  type        = bool
  default     = true
}

variable "webhook_rate_limit" {
  description = "Steady-state requests per second allowed on the webhook"
  type        = number
  default     = 1000

  validation {
    condition     = var.webhook_rate_limit > 0
    error_message = "Must be more than 0."
  }
}

variable "webhook_burst_limit" {
  description = "Burst of requests allowed on the webhook"
  type        = number
  default     = 2000

  validation {
    condition     = var.webhook_burst_limit > 0 && floor(var.webhook_burst_limit) == var.webhook_burst_limit
    error_message = "Must be a whole number, more than 0."
  }
}

variable "webhook_private" {
  description = "Make the webhook a private API, reachable only through an execute-api VPC endpoint the module creates in vpc_id and subnet_ids. Datadog can't reach a private API, so only use this when Alertmanager is the only sender."
  type        = bool
  default     = false
}

variable "notification_emails" {
  description = "Email addresses subscribed to the alerts topic. Each one gets a confirmation email from SNS to accept first."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for e in var.notification_emails : can(regex("^[^@ ]+@[^@ ]+[.][^@ ]+$", e))])
    error_message = "Each notification email must be an email address."
  }
}

variable "create_chat_topic" {
  description = "Also publish incidents to a second topic in the format Amazon Q Developer in chat applications (AWS Chatbot) renders, for a Slack or Teams channel to subscribe to. See examples/complete/chatbot.tf."
  type        = bool
  default     = false
}
