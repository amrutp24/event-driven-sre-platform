# Changelog

All notable changes to this module are recorded here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the module follows
[Semantic Versioning](https://semver.org/). New entries are written by
release-please from conventional commit titles.

## [1.0.0](https://github.com/amrutp24/terraform-aws-event-driven-sre/releases/tag/v1.0.0) (2026-10-24)

First release of the module.

### Added

- `POST /webhook` on an API Gateway REST API for Alertmanager and Datadog alerts, with a JSON schema, API keys, throttling, and an optional private endpoint.
- The `alert-ingest` Lambda, which validates and normalizes alerts, gives each one a stable incident id, and puts `incident.created` and `incident.resolved` events on a custom EventBridge bus.
- EventBridge rules to an incident recorder (an Express state machine writing to DynamoDB), SNS, the runbook state machine and CloudWatch Logs, with retries, an SQS dead-letter queue and a 7 day archive.
- A Step Functions runbook that claims each incident once, runs the action routed for the alert (`degrade`, `restart`, `scale`, `degrade_or_scale`, `drain_node`), waits for the alert to resolve, and records the MTTR or escalates.
- The `runbook-action` Lambda, which runs in the VPC and acts on the cluster through the Kubernetes API with an EKS token signed by its own role, limited to `remediation_namespaces` and `remediation_max_replicas`.
- An EKS access entry for the Lambda's role, and the matching RBAC as the `rbac_manifest` output.
- A KMS key for the table, topics, queue and log groups (or bring your own), email subscriptions, and an optional second topic in the AWS Chatbot format for Slack or Teams.
- `examples/complete` (VPC, EKS, IRSA, ECR, CloudTrail, a demo app with Prometheus alerting and deploy scripts) and `examples/existing-cluster`.
