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

### POST /alert

**Description**: Receives alerts from Prometheus Alertmanager or Datadog, validates the payload, enriches with metadata, and publishes to EventBridge.

**URL**: `https://{api-gateway-id}.execute-api.{region}.amazonaws.com/prod/alert`

**Method**: `POST`

**Content-Type**: `application/json`

**Timeout**: 30 seconds

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
| `alerts[].annotations.runbook_action` | string | Override default action: `degrade`, `scale`, `restart`, `degrade_or_scale`, `notify_only` |
| `alerts[].startsAt` | string (ISO 8601) | When the alert started firing |
| `alerts[].endsAt` | string (ISO 8601) | When the alert resolved (0001-01-01 means still firing) |
| `alerts[].generatorURL` | string | Link to Prometheus query |
| `alerts[].fingerprint` | string | Unique identifier for this alert |

### Datadog Webhook Format

Datadog sends alerts in a different format:

```json
{
  "body": "High error rate detected on checkout service",
  "title": "CheckoutHighErrorRate on checkout service",
  "priority": "normal",
  "tags": [
    "service:checkout",
    "namespace:apps",
    "severity:critical"
  ],
  "alert_type": "error",
  "date_happened": 1738491000,
  "alert_id": "12345678",
  "alert_status": "alert",
  "alert_transition": "Triggered",
  "event_url": "https://app.datadoghq.com/monitors/12345678"
}
```

**Note**: The `alert_ingest` Lambda normalizes both formats into a common schema before publishing to EventBridge.

---

## Response Schema

### Success Response (202 Accepted)

```json
{
  "status": "accepted",
  "message": "Alert received and queued for processing",
  "alert_count": 1,
  "correlation_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | Always `accepted` for success |
| `message` | string | Human-readable confirmation |
| `alert_count` | integer | Number of alerts processed in this request |
| `correlation_id` | string (UUID) | Unique identifier for tracking this request through the pipeline |

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

### API Key (Optional)

For production deployments, enable API key authentication on API Gateway:

**Header**: `X-API-Key: YOUR_API_KEY`

**Example**:
```bash
curl -X POST "https://abc123.execute-api.us-west-2.amazonaws.com/prod/alert" \
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
| `RATE_LIMIT_EXCEEDED` | 429 | Too many requests in a short time | Reduce alert frequency or request throttle increase |
| `INTERNAL_ERROR` | 500 | Lambda function encountered an error | Check CloudWatch Logs for details |
| `EVENTBRIDGE_ERROR` | 502 | Failed to publish event to EventBridge | Check EventBridge service health |
| `TIMEOUT` | 504 | Lambda function timed out (> 30 seconds) | Retry request; contact support if persistent |

---

## Rate Limiting

**Default Limits**:
- **Requests per second**: 1000 (burst: 2000)
- **Concurrent requests**: 100

**Behavior**:
- If rate limit exceeded, API Gateway returns `429 Too Many Requests`
- Alertmanager automatically retries with exponential backoff (default: 3 attempts)

**Increasing Limits**:
- Contact AWS Support to request higher API Gateway throttle limits
- Typical production limit: 10,000 requests/second

---

## Examples

### Example 1: Test Alert (cURL)

Send a simple test alert to verify webhook configuration:

```bash
curl -X POST "https://abc123.execute-api.us-west-2.amazonaws.com/prod/alert" \
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
  "correlation_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

### Example 2: High Error Rate Alert (Python)

```python
import requests
import json

url = "https://abc123.execute-api.us-west-2.amazonaws.com/prod/alert"
headers = {"Content-Type": "application/json"}

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
curl -X POST "https://abc123.execute-api.us-west-2.amazonaws.com/prod/alert" \
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
curl -X POST "https://abc123.execute-api.us-west-2.amazonaws.com/prod/alert" \
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

**alertmanager.yml**:
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
      - url: 'https://abc123.execute-api.us-west-2.amazonaws.com/prod/alert'
        send_resolved: true
        http_config:
          # Optional: Add API key if enabled
          # authorization:
          #   type: Bearer
          #   credentials: YOUR_API_KEY
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
curl -X POST "https://abc123.execute-api.us-west-2.amazonaws.com/prod/alert" \
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
Received alert: CheckoutHighErrorRate, severity: critical
Published event to EventBridge: event_id=550e8400-e29b-41d4-a716-446655440000
```

### Check EventBridge Events

Verify events are being published to EventBridge:

```bash
aws events put-events --entries file://test-event.json
```

### Check DynamoDB Incident Records

Verify incidents are being stored:

```bash
aws dynamodb scan \
  --table-name PLATFORM_NAME-incidents \
  --max-items 5 \
  --query 'Items[*].[incident_id.S, alertname.S, status.S, timestamp.S]' \
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

---

## Monitoring

### Key Metrics to Track

1. **Alert Ingestion Rate**: Number of alerts received per minute
   - CloudWatch metric: `AWS/Lambda Invocations` for `alert-ingest` function
   - Target: < 1000/minute (default throttle limit)

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
6. **Test webhook with `notify_only` action** before enabling auto-remediation
7. **Enable API key authentication** in production
8. **Use VPC endpoint** for maximum security
9. **Configure Alertmanager retry** for transient failures

---

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-02-02 | Initial API documentation |

---

## Support

For API issues or questions:
- Check CloudWatch Logs: `/aws/lambda/PLATFORM_NAME-alert-ingest`
- Open GitHub issue: https://github.com/YOUR_ORG/YOUR_REPO/issues
- Contact: sre-platform@example.com

---

**Last Updated**: 2026-02-02
**API Version**: 1.0
