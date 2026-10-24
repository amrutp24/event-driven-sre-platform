# API Documentation - Event-Driven SRE Platform

## Overview

This document describes the API endpoints exposed by the event-driven SRE platform. The primary integration point is the alert webhook endpoint, which receives alerts from Prometheus Alertmanager or Datadog.

---

## Table of Contents

1. [Alert Webhook Endpoint](#alert-webhook-endpoint)
2. [Request Schema](#request-schema)
3. [Response Schema](#response-schema)
4. [Authentication](#authentication)
5. [Error Codes](#error-codes)
6. [Rate Limiting](#rate-limiting)
7. [Examples](#examples)

---

## Alert Webhook Endpoint

### POST /webhook

**Description**: Receives alerts from Prometheus Alertmanager or Datadog, validates the payload, enriches with metadata, and publishes to EventBridge.

**URL**: `https://{api-gateway-id}.execute-api.{region}.amazonaws.com/prod/webhook`

**Method**: `POST`

**Content-Type**: `application/json`

**Authentication**: `X-API-Key` header (see [Authentication](#authentication))

**Timeout**: 30 seconds for `alert_ingest`; API Gateway stops waiting after 29 seconds and returns `504`

---

## Request Schema

### Alertmanager Webhook Format

Prometheus Alertmanager sends alerts in the following format:

```json
{
  "version": "4",
  "groupKey": "string",
  "truncatedAlerts": 0,
  "status": "firing",
  "receiver": "sre-webhook",
  "groupLabels": {
    "alertname": "CheckoutHighErrorRate",
    "service": "checkout"
  },
  "commonLabels": {
    "alertname": "CheckoutHighErrorRate",
    "severity": "critical",
    "service": "checkout",
    "namespace": "apps"
  },
  "commonAnnotations": {
    "summary": "High error rate detected on checkout service",
    "description": "Error rate is 8.5% over the last 5 minutes (threshold: 5%)",
    "runbook_url": "https://runbooks.example.com/checkout-high-error-rate"
  },
  "externalURL": "https://alertmanager.example.com",
  "alerts": [
    {
      "status": "firing",
      "labels": {
        "alertname": "CheckoutHighErrorRate",
        "severity": "critical",
        "service": "checkout",
        "namespace": "apps",
        "pod": "checkout-5d7c8b9f4d-abc12"
      },
      "annotations": {
        "summary": "High error rate detected on checkout service",
        "description": "Error rate is 8.5% over the last 5 minutes (threshold: 5%)",
        "runbook_url": "https://runbooks.example.com/checkout-high-error-rate",
        "runbook_action": "degrade_or_scale"
      },
      "startsAt": "2026-02-02T10:30:00.000Z",
      "endsAt": "0001-01-01T00:00:00Z",
      "generatorURL": "https://prometheus.example.com/graph?g0.expr=...",
      "fingerprint": "a1b2c3d4e5f6"
    }
  ]
}
```

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `alerts` | array | Array of alert objects (required, min length 1) |
| `alerts[].status` | string | Alert status: `firing` or `resolved` |
| `alerts[].labels` | object | Key-value pairs identifying the alert |
| `alerts[].labels.alertname` | string | Name of the alert (e.g., `CheckoutHighErrorRate`) |
| `alerts[].labels.severity` | string | Severity level: `critical`, `warning`, `info` |

### Optional Fields

| Field | Type | Description |
|-------|------|-------------|
| `alerts[].annotations` | object | Human-readable metadata (summary, description, runbook_url) |
| `alerts[].annotations.runbook_action` | string | Override the default action: `degrade`, `scale`, `restart`, `degrade_or_scale` or `drain_node`. `notify_only` skips the runbook: the alert is recorded and sent to SNS, nothing more. Any other value escalates a critical alert. |
| `alerts[].annotations.desired_replicas` | string | Replica count for `scale` and `degrade_or_scale` (default 4, at most `remediation_max_replicas`). With an HPA, this becomes the HPA's minimum instead. |
| `alerts[].labels.namespace`, `alerts[].labels.deployment` | string | What the runbook acts on. Without them it uses the `service` label as the deployment, then `apps/checkout`. Namespaces other than `apps` are refused. |
| `alerts[].labels.node` | string | The node `drain_node` drains. `NodeUnderPressure` carries it from kube-state-metrics. Only nodes labelled `nodegroup=apps` are drained. |
| `alerts[].startsAt` | string (ISO 8601) | When the alert started firing |
| `alerts[].endsAt` | string (ISO 8601) | When the alert resolved (0001-01-01 means still firing) |
| `alerts[].generatorURL` | string | Link to Prometheus query |
| `alerts[].fingerprint` | string | Unique identifier for this alert |

API Gateway checks the body's structure against a JSON schema (`terraform/api/alert_payload.schema.json`). `alert_ingest` then checks the values above, so a missing `alertname` or an unknown severity comes back with its own error code.

The incident id is derived from `fingerprint` and `startsAt`, which Alertmanager keeps the same while an alert fires and when it resolves. A resolved notification therefore updates the incident its firing alert created. Alerts without both fields get a new incident id each time.

### Datadog Webhook Format

Datadog's webhook payload is whatever you define in the Webhooks integration, so set it to this (the `$` values are Datadog template variables):

```json
{
  "title": "$EVENT_TITLE",
  "body": "$TEXT_ONLY_MSG",
  "priority": "$ALERT_PRIORITY",
  "tags": "$TAGS",
  "date_happened": "$DATE_POSIX",
  "alert_id": "$ALERT_ID",
  "alert_status": "$ALERT_STATUS",
  "alert_transition": "$ALERT_TRANSITION",
  "alert_cycle_key": "$ALERT_CYCLE_KEY",
  "event_url": "$LINK"
}
```

and add a custom header with the key from `terraform output -raw datadog_api_key`, stored as a hidden custom variable:

```json
{"X-API-Key": "$WEBHOOK_API_KEY"}
```

What it becomes:

| Datadog field | Normalized as |
| --- | --- |
| `alert_transition` | `Recovered` is `resolved`; `Triggered`, `Re-Triggered`, `Warn`, `No Data` and `Renotify` are `firing` |
| `alert_cycle_key` | the incident id, so the trigger and its recovery are one incident. Required for that to work. |
| `tags` (`"a:b,c:d"` or a JSON array) | labels. An `alertname:` tag sets the alert name, otherwise `title` is used. `service:` sets the service. The runbook takes its target from `kube_namespace:` and `kube_deployment:` tags when there are no `namespace`/`deployment` tags, and the node to drain from a `host:` tag when there's no `node:` tag. |
| `severity:` tag | the severity (`critical`, `warning` or `info`); otherwise `priority` P1 and P2 are `critical`, P3 is `warning`, P4 and P5 are `info`, and anything else is `warning` |
| `date_happened` | epoch seconds or milliseconds |

A Datadog payload is recognized by its `alert_transition` field, and it's published to EventBridge with source `sre.datadog` (Alertmanager's is `sre.alertmanager`).

**Note**: The `alert_ingest` Lambda normalizes both formats into a common schema before publishing to EventBridge.

---

## Response Schema

### Success Response (202 Accepted)

```json
{
  "status": "accepted",
  "message": "Alert received and queued for processing",
  "alert_count": 1,
  "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
  "incidents": [
    {
      "incident_id": "1f8a2b52-3c0e-5d6f-9a1b-7c2d3e4f5a6b",
      "alertname": "CheckoutHighErrorRate",
      "status": "firing"
    }
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | Always `accepted` for success |
| `message` | string | Human-readable confirmation |
| `alert_count` | integer | Number of alerts processed in this request |
| `correlation_id` | string (UUID) | Unique identifier for tracking this request through the pipeline |
| `incidents` | array | One entry per alert, with its incident id. The alerts are on the EventBridge bus by the time this returns; remediation runs asynchronously. |

### Error Response (400 Bad Request)

```json
{
  "status": "error",
  "message": "Invalid alert payload: missing required field 'alerts'",
  "error_code": "INVALID_PAYLOAD",
  "details": {
    "field": "alerts",
    "expected": "array",
    "received": "null"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | Always `error` for failures |
| `message` | string | Human-readable error message |
| `error_code` | string | Machine-readable error code (see [Error Codes](#error-codes)) |
| `details` | object | Additional context about the error |

---

## Authentication

### API Key

On by default (`webhook_require_api_key = true`). Terraform creates one key for Alertmanager and one for Datadog, so either can be rotated or revoked on its own:

```bash
terraform -chdir=terraform output -raw alertmanager_api_key
terraform -chdir=terraform output -raw datadog_api_key
```

`scripts/deploy.sh` gives Alertmanager its key. Set `webhook_require_api_key = false` to accept requests without one.

**Header**: `X-API-Key: YOUR_API_KEY`

**Example**:
```bash
curl -X POST "https://abc123.execute-api.us-west-2.amazonaws.com/prod/webhook" \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d @alert.json
```

### VPC Endpoint (Recommended for Production)

For maximum security, use a VPC endpoint to restrict access to the API Gateway:

1. Create VPC endpoint for API Gateway
2. Configure API Gateway resource policy to allow only VPC endpoint
3. Alertmanager/Datadog webhooks must originate from within VPC

**Resource Policy Example**:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": "*",
      "Action": "execute-api:Invoke",
      "Resource": "arn:aws:execute-api:REGION:ACCOUNT_ID:API_ID/*",
      "Condition": {
        "StringEquals": {
          "aws:SourceVpce": "vpce-1234567890abcdef0"
        }
      }
    }
  ]
}
```

---

## Error Codes

| Error Code | HTTP Status | Description | Resolution |
|------------|-------------|-------------|------------|
| `INVALID_PAYLOAD` | 400 | Request body is not valid JSON or missing required fields | Check JSON syntax and required fields |
| `MISSING_ALERT_NAME` | 400 | Alert is missing `alertname` label | Add `alertname` label to Prometheus alert rule |
| `INVALID_SEVERITY` | 400 | Severity is not one of `critical`, `warning`, `info` | Use valid severity values |
| `INVALID_API_KEY` | 403 | `X-API-Key` header missing or wrong | Use the key from `terraform output` |
| `RATE_LIMIT_EXCEEDED` | 429 | Too many requests in a short time | Reduce alert frequency or request throttle increase |
| `INTERNAL_ERROR` | 500 | Lambda function encountered an error | Check CloudWatch Logs for details |
| `EVENTBRIDGE_ERROR` | 502 | Failed to publish event to EventBridge | Check EventBridge service health |
| `TIMEOUT` | 504 | API Gateway stopped waiting for the Lambda (29 seconds) | Retry the request; check CloudWatch Logs if it keeps happening |

`INVALID_PAYLOAD` comes from API Gateway when the body doesn't match the schema, and from `alert_ingest` otherwise. Errors from API Gateway have `details.request_id` instead of field details. `INTERNAL_ERROR` and `EVENTBRIDGE_ERROR` include `details.correlation_id`.

---

## Rate Limiting

**Default Limits**:
- **Requests per second**: 1000 (burst: 2000), set by `webhook_rate_limit` and `webhook_burst_limit`
- **Concurrent requests**: 100, the `alert_ingest` reserved concurrency (`alert_ingest_reserved_concurrency`)

**Behavior**:
- If rate limit exceeded, API Gateway returns `429 Too Many Requests`
- Alertmanager automatically retries with exponential backoff (default: 3 attempts)

**Increasing Limits**:
- Contact AWS Support to request higher API Gateway throttle limits
- Typical production limit: 10,000 requests/second

---

## Examples

The examples read the URL and key from Terraform:

```bash
WEBHOOK_URL=$(terraform -chdir=terraform output -raw alert_webhook_url)
API_KEY=$(terraform -chdir=terraform output -raw alertmanager_api_key)
```

### Example 1: Test Alert (cURL)

Send a simple test alert to verify webhook configuration:

```bash
curl -X POST "$WEBHOOK_URL" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "alerts": [
      {
        "status": "firing",
        "labels": {
          "alertname": "TestAlert",
          "severity": "critical",
          "service": "test"
        },
        "annotations": {
          "summary": "This is a test alert",
          "runbook_action": "notify_only"
        },
        "startsAt": "2026-02-02T10:00:00Z"
      }
    ]
  }'
```

**Expected Response**:
```json
{
  "status": "accepted",
  "message": "Alert received and queued for processing",
  "alert_count": 1,
  "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
  "incidents": [
    {"incident_id": "6f1c2c1e-5d1a-4f0e-9a43-2b8f6a0c9e71", "alertname": "TestAlert", "status": "firing"}
  ]
}
```

`TestAlert` has `runbook_action: notify_only`, so the runbook execution ends at its first step (`NotAutoRemediated`): it doesn't claim the incident, call the Lambda or escalate. The alert is still recorded in DynamoDB and sent to SNS.

### Example 2: High Error Rate Alert (Python)

```python
import json
import os

import requests

url = os.environ["WEBHOOK_URL"]
headers = {"Content-Type": "application/json", "X-API-Key": os.environ["API_KEY"]}

payload = {
    "alerts": [
        {
            "status": "firing",
            "labels": {
                "alertname": "CheckoutHighErrorRate",
                "severity": "critical",
                "service": "checkout",
                "namespace": "apps"
            },
            "annotations": {
                "summary": "Error rate is 8.5% over the last 5 minutes",
                "description": "Threshold: 5%",
                "runbook_url": "https://runbooks.example.com/checkout-error-rate",
                "runbook_action": "degrade_or_scale"
            },
            "startsAt": "2026-02-02T10:30:00Z",
            "fingerprint": "a1b2c3d4e5f6"
        }
    ]
}

response = requests.post(url, headers=headers, data=json.dumps(payload))

if response.status_code == 202:
    print(f"Alert sent successfully: {response.json()}")
else:
    print(f"Error: {response.status_code} - {response.text}")
```

### Example 3: Resolved Alert

Send a resolved alert to close an incident:

```bash
curl -X POST "$WEBHOOK_URL" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "alerts": [
      {
        "status": "resolved",
        "labels": {
          "alertname": "CheckoutHighErrorRate",
          "severity": "critical",
          "service": "checkout"
        },
        "annotations": {
          "summary": "Error rate has returned to normal"
        },
        "startsAt": "2026-02-02T10:30:00Z",
        "endsAt": "2026-02-02T10:32:00Z",
        "fingerprint": "a1b2c3d4e5f6"
      }
    ]
  }'
```

### Example 4: Multiple Alerts in One Request

Alertmanager groups related alerts and sends them in a single request:

```bash
curl -X POST "$WEBHOOK_URL" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "alerts": [
      {
        "status": "firing",
        "labels": {
          "alertname": "CheckoutHighErrorRate",
          "severity": "critical",
          "pod": "checkout-5d7c8b9f4d-abc12"
        }
      },
      {
        "status": "firing",
        "labels": {
          "alertname": "CheckoutHighErrorRate",
          "severity": "critical",
          "pod": "checkout-5d7c8b9f4d-def34"
        }
      }
    ]
  }'
```

### Example 5: Alertmanager Configuration

Configure Alertmanager to send alerts to the webhook:

`helm/observability/alertmanager-values.yaml` sets this up through kube-prometheus-stack; the equivalent plain **alertmanager.yml** is:
```yaml
global:
  resolve_timeout: 5m

route:
  receiver: 'sre-webhook'
  group_by: ['alertname', 'service']
  group_wait: 10s
  group_interval: 10s
  repeat_interval: 12h

receivers:
  - name: 'sre-webhook'
    webhook_configs:
      - url: 'https://abc123.execute-api.us-west-2.amazonaws.com/prod/webhook'
        send_resolved: true
        http_config:
          http_headers:
            X-API-Key:
              files:
                - /etc/alertmanager/secrets/alertmanager-webhook-key/api-key
```

### Example 6: Test with Fake Alert Data File

**alert.json**:
```json
{
  "version": "4",
  "groupKey": "{}:{alertname=\"CheckoutHighErrorRate\"}",
  "truncatedAlerts": 0,
  "status": "firing",
  "receiver": "sre-webhook",
  "groupLabels": {
    "alertname": "CheckoutHighErrorRate"
  },
  "commonLabels": {
    "alertname": "CheckoutHighErrorRate",
    "severity": "critical",
    "service": "checkout"
  },
  "commonAnnotations": {
    "summary": "Error rate exceeded threshold"
  },
  "externalURL": "http://alertmanager:9093",
  "alerts": [
    {
      "status": "firing",
      "labels": {
        "alertname": "CheckoutHighErrorRate",
        "severity": "critical",
        "service": "checkout"
      },
      "annotations": {
        "summary": "Error rate exceeded threshold",
        "runbook_action": "degrade"
      },
      "startsAt": "2026-02-02T10:30:00.000Z",
      "endsAt": "0001-01-01T00:00:00Z"
    }
  ]
}
```

**Send with cURL**:
```bash
curl -X POST "$WEBHOOK_URL" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d @alert.json
```

---

## Debugging

### Check CloudWatch Logs

If alerts are not being processed, check Lambda function logs:

```bash
aws logs tail /aws/lambda/PLATFORM_NAME-alert-ingest --follow --format short
```

**Common log entries**:
```
Accepted 1 alert(s), correlation_id 550e8400-e29b-41d4-a716-446655440000
Rejected payload (INVALID_SEVERITY): Severity must be one of critical, warning, info {'field': 'alerts[0].labels.severity', ...}
```

### Check EventBridge Events

Every event on the bus is logged, and events EventBridge couldn't deliver end up in the dead-letter queue:

```bash
aws logs tail /aws/events/PLATFORM_NAME --follow
aws sqs get-queue-attributes --attribute-names ApproximateNumberOfMessages \
  --queue-url "$(terraform -chdir=terraform output -raw events_dlq_url)"
```

### Check DynamoDB Incident Records

Verify incidents are being stored:

```bash
aws dynamodb scan \
  --table-name PLATFORM_NAME-incidents \
  --max-items 5 \
  --query 'Items[*].[incident_id.S, alertname.S, status.S, first_seen_at.S, last_action.S]' \
  --output table
```

Escalated incidents, newest first, from the `by-status` index (or `open`, `remediating`, `resolved`):

```bash
aws dynamodb query \
  --table-name PLATFORM_NAME-incidents \
  --index-name by-status \
  --key-condition-expression "#s = :s" \
  --expression-attribute-names '{"#s": "status"}' \
  --expression-attribute-values '{":s": {"S": "escalated"}}' \
  --no-scan-index-forward \
  --query 'Items[*].[incident_id.S, alertname.S, first_seen_at.S, escalation_reason.S]' \
  --output table
```

### Check Step Functions Executions

Verify runbook workflows are starting:

```bash
aws stepfunctions list-executions \
  --state-machine-arn arn:aws:states:REGION:ACCOUNT_ID:stateMachine:PLATFORM_NAME-runbook \
  --max-results 10 \
  --query 'executions[*].[name, status, startDate]' \
  --output table
```

`SUCCEEDED` covers alerts that resolved after remediation, alerts the runbook skipped (not critical, or `notify_only`) and duplicates. `FAILED` with error `Escalated` means the incident was handed to a person; the execution's cause is the reason. Every state's input and output is in `/aws/vendedlogs/states/PLATFORM_NAME-runbook`.

---

## Monitoring

### Key Metrics to Track

1. **Alert Ingestion Rate**: Number of alerts received per minute
   - CloudWatch metric: `AWS/Lambda Invocations` for `alert-ingest` function
   - Target: well under 1000/second (the throttle limit)

2. **Alert Processing Latency**: Time from webhook to EventBridge publish
   - CloudWatch metric: `AWS/Lambda Duration` for `alert-ingest` function
   - Target: < 500ms (p95)

3. **Error Rate**: Percentage of failed alert ingestion attempts
   - CloudWatch metric: `AWS/Lambda Errors` for `alert-ingest` function
   - Target: < 1%

4. **EventBridge Delivery Failures**: Failed deliveries to targets
   - CloudWatch metric: `AWS/Events FailedInvocations`
   - Target: 0

### CloudWatch Alarms

**Alarm 1: High Alert Ingestion Error Rate**
```bash
aws cloudwatch put-metric-alarm \
  --alarm-name AlertIngestHighErrorRate \
  --alarm-description "Alert ingestion Lambda error rate > 1%" \
  --metric-name Errors \
  --namespace AWS/Lambda \
  --statistic Average \
  --period 300 \
  --evaluation-periods 2 \
  --threshold 0.01 \
  --comparison-operator GreaterThanThreshold \
  --dimensions Name=FunctionName,Value=PLATFORM_NAME-alert-ingest \
  --treat-missing-data notBreaching
```

**Alarm 2: EventBridge Delivery Failures**
```bash
aws cloudwatch put-metric-alarm \
  --alarm-name EventBridgeDeliveryFailures \
  --alarm-description "EventBridge failed to deliver events to targets" \
  --metric-name FailedInvocations \
  --namespace AWS/Events \
  --statistic Sum \
  --period 300 \
  --evaluation-periods 1 \
  --threshold 5 \
  --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching
```

---

## Best Practices

1. **Always send `send_resolved: true`** in Alertmanager config to close incidents automatically
2. **Use `runbook_action` annotation** to override default remediation actions
3. **Include `runbook_url` annotation** for human escalation scenarios
4. **Monitor alert ingestion error rate** to detect misconfigurations
5. **Use correlation_id** from response to track alerts through the pipeline
6. **Test the webhook with `notify_only` or a `warning` alert** before letting critical alerts through, since both stop before the runbook changes anything
7. **Enable API key authentication** in production
8. **Use VPC endpoint** for maximum security
9. **Configure Alertmanager retry** for transient failures

---

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-02-02 | Initial API documentation |
| 1.4 | 2026-10-23 | `drain_node` action and the `node` label (or Datadog `host` tag). `desired_replicas` raises the HPA's minimum when the deployment has one. |
| 1.3 | 2026-10-23 | The runbook state machine picks the action; `runbook_action: notify_only` skips the runbook and other unknown values escalate. `namespace`/`deployment` labels and `desired_replicas` set the target. |
| 1.2 | 2026-10-23 | `alert_ingest` publishes to EventBridge only; rules route to DynamoDB, SNS and Step Functions. `remediation_started` removed from the response. |
| 1.1 | 2026-10-23 | `POST /webhook` on a REST API with schema validation, API keys and throttling; Datadog payloads; `202` and `400` responses as documented |

---

## Support

For API issues or questions:
- Check CloudWatch Logs: `/aws/lambda/PLATFORM_NAME-alert-ingest`
- Open an issue: https://github.com/amrutp24/event-driven-sre-platform/issues

---

**Last Updated**: 2026-10-23
**API Version**: 1.4
