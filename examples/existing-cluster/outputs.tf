output "alert_webhook_url" {
  description = "POST alerts here"
  value       = module.sre.alert_webhook_url
}

output "alertmanager_api_key" {
  description = "X-API-Key for Alertmanager"
  value       = module.sre.alertmanager_api_key
  sensitive   = true
}

output "rbac_manifest" {
  description = "Apply with terraform output -raw rbac_manifest | kubectl apply -f -"
  value       = module.sre.rbac_manifest
}
