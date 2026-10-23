# Alert webhook

Alertmanager sends alerts to a single endpoint on an API Gateway HTTP API. The `alert-ingest` Lambda handles it.

## Endpoint

```
POST https://<api-id>.execute-api.<region>.amazonaws.com/prod/alert
```

Get the full URL with:

```bash
terraform -chdir=terraform output -raw alert_webhook_url
```

HTTP APIs only accept HTTPS.

## Authentication

None. Anyone with the URL can post an alert and start a remediation run. This is listed under [Known gaps](../README.md#known-gaps).

## Request

The body is Alertmanager's webhook payload. `alert-ingest` only reads the `alerts` array. For each alert it uses:

| Field | Used for | Default |
| --- | --- | --- |
| `labels.alertname` | which runbook action to take | `UnknownAlert` |
| `labels.severity` | DynamoDB record, SNS subject | `ticket` |
| `labels.service` | DynamoDB record, SNS subject | `unknown` |
| `status` | DynamoDB record, SNS subject | `firing` |
| `annotations.runbook_action` | overrides the action picked from the alert name | |
| `annotations.desired_replicas` | replica count for `scale` | `4` |

Other fields are ignored. Base64-encoded bodies are decoded first.

Minimal example:

```json
{
  "alerts": [
    {
      "status": "firing",
      "labels": {
        "alertname": "CheckoutHighErrorRate",
        "severity": "page",
        "service": "checkout"
      },
      "annotations": {
        "summary": "Checkout 5xx rate > 2% (5m)"
      }
    }
  ]
}
```

## What happens for each alert

1. A record is written to the `event-driven-sre-incidents` table with `incident_id` (a new UUID), `ts`, `service`, `alertname`, `severity`, `status` and the full normalized record under `payload`.
2. The record is put on the `event-driven-sre-bus` EventBridge bus with source `prometheus.alertmanager` and detail type `SREAlert`.
3. It's published to the `event-driven-sre-alerts` SNS topic with the subject `[PAGE] checkout - CheckoutHighErrorRate (firing)`.
4. A `event-driven-sre-runbook` Step Functions execution starts with the record as input.

Resolved alerts go through the same steps, including step 4.

The normalized record:

```json
{
  "incident_id": "6f1c2c1e-5d1a-4f0e-9a43-2b8f6a0c9e71",
  "timestamp": 1761180000,
  "status": "firing",
  "severity": "page",
  "service": "checkout",
  "alertname": "CheckoutHighErrorRate",
  "labels": { "alertname": "CheckoutHighErrorRate", "severity": "page", "service": "checkout" },
  "annotations": { "summary": "Checkout 5xx rate > 2% (5m)" },
  "runbook_action_arn": "arn:aws:lambda:us-east-1:123456789012:function:event-driven-sre-runbook-action"
}
```

## Response

`200` with one entry per alert processed:

```json
{"ok": true, "processed": [{"incident_id": "6f1c2c1e-5d1a-4f0e-9a43-2b8f6a0c9e71", "alertname": "CheckoutHighErrorRate"}]}
```

A payload with no `alerts` returns `200` with an empty `processed` list.

If the body isn't valid JSON, or a call to DynamoDB, EventBridge, SNS or Step Functions fails, the Lambda raises and API Gateway returns `500`. Alerts earlier in the same request have already been processed by then.

## Sending a test alert

```bash
curl -s -X POST "$(terraform -chdir=terraform output -raw alert_webhook_url)" \
  -H "Content-Type: application/json" \
  -d '{"alerts":[{"status":"firing","labels":{"alertname":"TestAlert","severity":"ticket","service":"checkout"}}]}'
```

`TestAlert` isn't in the runbook's routing table, so it's handled as `notify_only` and nothing in the cluster changes.

## Alertmanager configuration

From `helm/observability/alertmanager-values.yaml`, with the placeholder replaced by the URL above:

```yaml
alertmanager:
  config:
    route:
      receiver: "aws-webhook"
    receivers:
      - name: "aws-webhook"
        webhook_configs:
          - url: "https://<api-id>.execute-api.<region>.amazonaws.com/prod/alert"
            send_resolved: true
```

## Checking that a request went through

```bash
aws logs tail /aws/lambda/event-driven-sre-alert-ingest --since 15m
aws dynamodb scan --table-name event-driven-sre-incidents --max-items 5

SFN_ARN=$(aws stepfunctions list-state-machines \
  --query "stateMachines[?name=='event-driven-sre-runbook'].stateMachineArn" --output text)
aws stepfunctions list-executions --state-machine-arn "$SFN_ARN" --max-results 5

aws logs tail /aws/lambda/event-driven-sre-runbook-action --since 15m
```

The SNS topic has no subscriptions by default. Add one to receive the notifications:

```bash
aws sns subscribe --protocol email --notification-endpoint you@example.com \
  --topic-arn "$(aws sns list-topics --query "Topics[?ends_with(TopicArn, ':event-driven-sre-alerts')].TopicArn" --output text)"
```
