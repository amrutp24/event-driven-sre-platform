# Event-Driven SRE Platform - Demo Script

## Overview

This is a step-by-step demo script for the event-driven SRE platform. It takes about **15 minutes**, most of it waiting on the alert rule's 5 minute windows, and shows the whole loop from alert detection to automated remediation.

**Demo Flow**: Error injection → Alert fires → Automated remediation → Alert resolves → Runbook confirms it and records the MTTR

The commands assume the default `name` variable, `event-driven-sre`, and are run from the repo root.

---

## Prerequisites Checklist

Before starting the demo, verify:

```bash
# 1. AWS credentials configured
aws sts get-caller-identity

# 2. kubectl configured for EKS cluster
kubectl cluster-info

# 3. Terraform outputs available
terraform -chdir=terraform output

# 4. Required CLI tools installed
which kubectl helm aws jq curl docker git

# 5. The checkout app is built and deployed from ECR. build-image.sh pushes
#    the image tagged with the short git SHA, and deploy.sh deploys that tag.
bash scripts/build-image.sh
bash scripts/deploy.sh
kubectl get deployment checkout -n apps -o jsonpath='{.spec.template.spec.containers[0].image}{"
"}'
```

Expected: All commands succeed without errors, and the image is `ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/event-driven-sre/checkout:<git sha>`. Tags are immutable, so build again after committing changes to `apps/checkout`; to build uncommitted changes, set the same `IMAGE_TAG` for both scripts.

---

## Demo Setup (Pre-Demo, 2 minutes)

### Step 1: Open Required Terminals/Browser Tabs

**Terminal 1**: Command execution
**Terminal 2**: Log watching (optional)
**Browser Tab 1**: Grafana dashboard
**Browser Tab 2**: AWS DynamoDB console
**Browser Tab 3**: AWS Step Functions console

### Step 2: Get Important URLs

```bash
cd terraform

# Get API Gateway webhook URL
terraform output -raw alert_webhook_url
# Example: https://abc123.execute-api.us-west-2.amazonaws.com/prod/webhook

# Get Grafana URL (if using LoadBalancer)
kubectl get svc -n monitoring mon-grafana -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'
# Or port-forward: kubectl port-forward -n monitoring svc/mon-grafana 3000:80

# Get checkout service URL
kubectl get ingress -n apps checkout -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'
# Or port-forward: kubectl port-forward -n apps svc/checkout 8080:80
```

Save these URLs for quick access during demo.

### Step 3: Verify System is Healthy

```bash
# Check all pods are running
kubectl get pods -n apps
kubectl get pods -n monitoring

# Expected: All pods in Running state, READY 1/1 or higher

# Check Prometheus is scraping metrics
kubectl port-forward -n monitoring svc/mon-kube-prometheus-stack-prometheus 9090:9090 &
# Open http://localhost:9090/targets and verify checkout target is UP

# Check current error rate (should be ~0%): no code="5xx" series yet
curl -s http://localhost:8080/metrics | grep 'http_requests_total{'
```

Expected: Error rate near 0, all targets healthy.

---

## Demo Script (about 15 minutes)

### Phase 1: Show Healthy State (30 seconds)

**Script**: "Let me show you the system in a healthy state. We have a checkout microservice running on EKS, monitored by Prometheus."

```bash
# Show running pods, and the autoscaler that keeps 3 to 10 of them
kubectl get pods -n apps -l app=checkout
kubectl get hpa checkout -n apps

# Expected output:
# NAME                        READY   STATUS    RESTARTS   AGE
# checkout-5d7c8b9f4d-abc12   1/1     Running   0          10m
# checkout-5d7c8b9f4d-def34   1/1     Running   0          10m
# checkout-5d7c8b9f4d-ghi56   1/1     Running   0          10m

# Show current metrics
curl -s http://localhost:8080/metrics | grep 'http_requests_total{'

# Expected: route="/checkout",code="200" increasing, no 5xx
```

The alert needs traffic on `/checkout`. If nothing is calling it, start a loop in another terminal:

```bash
while true; do curl -s -o /dev/null http://localhost:8080/checkout; sleep 0.2; done
```

**Talking point**: "The checkout service is running and exposing Prometheus metrics. Error rate is currently 0%."

---

### Phase 2: Inject Failure (30 seconds)

**Script**: "Now I'll simulate a production incident by injecting errors into the service."

```bash
# Inject 10% error rate
kubectl set env deployment/checkout -n apps ERROR_RATE=0.10

# Verify environment variable set
kubectl get deployment checkout -n apps -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="ERROR_RATE")].value}'

# Expected output: 0.10

# Watch pods restart (rolling update)
kubectl rollout status deployment/checkout -n apps
```

**Talking point**: "I'm setting ERROR_RATE to 10%, which will cause the service to return errors for 10% of requests. This simulates a real-world issue like a database connection problem."

---

### Phase 3: Observe Alert Firing (1 minute)

**Script**: "Prometheus watches the 5xx ratio over a 5 minute window. Once it's been above 5% for 5 minutes, the alert fires." With 10% errors that's about 7 to 8 minutes after the injection.

```bash
# Open Grafana in browser (or use port-forward)
# Navigate to: Alerting → Alert Rules
# Look for: CheckoutHighErrorRate

# Or check Alertmanager directly
kubectl port-forward -n monitoring svc/alertmanager-operated 9093:9093 &
# Open http://localhost:9093/#/alerts
```

**Expected behavior**:
- Alert goes `Pending` once the ratio crosses 5%, then `Firing` 5 minutes later
- Alert shows labels: `severity=critical`, `service=checkout`
- Alertmanager sends it to the webhook after its 30 second `group_wait`

**Talking point**: "Prometheus detected the error rate exceeded our 5% threshold. The alert is now in FIRING state and Alertmanager is routing it to our webhook."

**Show alert payload** (optional, for technical audience):

```bash
# Tail CloudWatch logs to see webhook received
aws logs tail /aws/lambda/event-driven-sre-alert-ingest --follow --format short

# Expected log entry:
# Accepted 1 alert(s), correlation_id 550e8400-e29b-41d4-a716-446655440000
```

---

### Phase 4: Show Automated Remediation (1 minute)

**Script**: "The webhook put the alert on EventBridge, which started the runbook state machine. Let's watch it."

```bash
# Events on the bus: incident.created, then action.completed from the runbook
aws logs tail /aws/events/event-driven-sre --since 10m

# The runbook execution
aws stepfunctions list-executions \
  --state-machine-arn "$(terraform -chdir=terraform output -raw step_functions_arn)" \
  --max-results 1 \
  --query 'executions[0].[name,status,startDate]' \
  --output table

# Expected: New execution in RUNNING state

# Every step's input and output
aws logs tail /aws/vendedlogs/states/event-driven-sre-runbook --follow --format short
```

**Show in AWS Console** (Step Functions → State machines → event-driven-sre-runbook → Executions):
- The graph runs EvaluateAlert → ShouldRemediate → LogIncident → ChooseAction → RunbookAvailable → ExecuteAction → ReportActionCompleted, then sits in WaitForStabilization
- ExecuteAction's details show what was sent to the Lambda: `action: degrade_or_scale`, `namespace: apps`, `deployment: checkout`, `replicas: 4`
- Its output is what the Lambda returned, including `degraded: true`, `scaled_to: 4` and `scaled_via: hpa`

**Talking point**: "Step Functions claimed the incident in DynamoDB, picked degrade_or_scale for this alert, and had the runbook_action Lambda apply it. If Alertmanager re-sends the alert, the next run sees the claim and stops."

```bash
# runbook_action's own logs, where Kubernetes API errors would show up
aws logs tail /aws/lambda/event-driven-sre-runbook-action --follow --format short
```

---

### Phase 5: Verify Remediation Applied (1 minute)

**Script**: "The Lambda turned on degraded mode through SSM and raised the autoscaler's minimum. Let's verify the changes."

```bash
# The SSM parameter the pods read
aws ssm get-parameter --name /checkout/degraded_mode --query 'Parameter.Value' --output text

# Expected output: true

# The pod's own view. Requests and scrapes re-read the parameter at most every 15 seconds.
curl -s http://localhost:8080/metrics | grep checkout_degraded_mode_enabled

# Expected output: checkout_degraded_mode_enabled 1.0

# The HPA minimum the Lambda raised
kubectl get hpa checkout -n apps

# Expected: MINPODS 4, MAXPODS 10

# Watch the extra pod start
kubectl get pods -n apps -l app=checkout -w

# Expected: a fourth pod starting. The others keep running; nothing restarts.
```

**Talking point**: "In degraded mode the service skips the injected errors and latency. The pods picked the flag up on their own, without a restart, and the extra replica adds headroom while things settle."

---

### Phase 6: Observe Error Rate Drop (1 minute)

**Script**: "Once the pods see the flag, they stop failing requests, the error rate drops below the threshold and the alert resolves."

```bash
# 5xx counts stop increasing
curl -s http://localhost:8080/metrics | grep 'http_requests_total{route="/checkout"'

# The alert's own expression
# Open http://localhost:9090/graph
# Query: sum(rate(http_requests_total{route="/checkout",code=~"5.."}[5m])) / sum(rate(http_requests_total{route="/checkout"}[5m]))
# Expected: climbs to about 0.10, then falls once degraded mode is on and crosses 0.05 a couple of minutes later
```

**Talking point**: "New requests aren't failing any more, but the alert looks at a 5 minute window, so the ratio takes a couple of minutes to drop under 5%. Then Alertmanager sends the resolved notification on its next 2 minute `group_interval`. The runbook is checking DynamoDB every 30 seconds in the meantime, and gives it up to 10 minutes from the action."

---

### Phase 7: Show Incident Record (1 minute)

**Script**: "All incident data is stored in DynamoDB for audit and analysis."

```bash
# The most recent resolved incident, from the by-status index
aws dynamodb query \
  --table-name event-driven-sre-incidents \
  --index-name by-status \
  --key-condition-expression "#s = :s" \
  --expression-attribute-names '{"#s": "status"}' \
  --expression-attribute-values '{":s": {"S": "resolved"}}' \
  --no-scan-index-forward \
  --max-items 1 \
  --query 'Items[0]' \
  --output json | jq '.'

# Expected output, DynamoDB types and payload left out:
# {
#   "incident_id": "3b9f5c1e-8d2a-5f4b-9c1e-7a6d2e4f8b10",
#   "alertname": "CheckoutHighErrorRate",
#   "service": "checkout",
#   "severity": "critical",
#   "status": "resolved",
#   "first_seen_at": "2026-10-23T10:30:00Z",
#   "remediation_started_at": "2026-10-23T10:30:01.412Z",
#   "actions_taken": [
#     {"action": "degrade_or_scale", "result": "success", "at": "2026-10-23T10:30:04Z", "execution": "arn:aws:states:..."}
#   ],
#   "resolved_at": "2026-10-23T10:35:30Z",
#   "remediation_result": "auto_resolved",
#   "remediated_by": "degrade_or_scale",
#   "mttr_seconds": 330,
#   "notification_count": 1,
#   "expires_at": 1800527730
# }
```

Swap `resolved` for `open`, `remediating` or `escalated` to list those instead.

**Show in DynamoDB Console** (optional):
- Navigate to: DynamoDB → Tables → event-driven-sre-incidents → Explore items
- Query the `by-status` index for `resolved`, newest first
- Show latest incident record

**Talking point**: "We have the whole history: when the alert was first seen, what the runbook did and when, and when it resolved. `mttr_seconds` runs from when the platform first saw the alert to when it saw the resolution, here five and a half minutes, and nobody had to touch it."

---

### Phase 8: Show Alert Resolved (30 seconds)

**Script**: "The alert has now resolved, completing the automated incident response cycle."

```bash
# Check Alertmanager
# Open http://localhost:9093/#/alerts
# Expected: CheckoutHighErrorRate no longer in firing state

# Check Step Functions execution completed
aws stepfunctions list-executions \
  --state-machine-arn "$(terraform -chdir=terraform output -raw step_functions_arn)" \
  --max-results 1 \
  --query 'executions[0].status' \
  --output text

# Expected: SUCCEEDED, ending in AutoResolved (an escalation ends as FAILED, error Escalated)

# Check SNS notification sent (subscribe an email before the demo with
# aws sns subscribe --protocol email --notification-endpoint you@example.com --topic-arn ...)
aws sns list-subscriptions-by-topic \
  --topic-arn "$(terraform -chdir=terraform output -raw sns_topic_arn)" \
  --query 'Subscriptions[0].Endpoint'
```

**Expected emails**, in order:
- `[critical] checkout - CheckoutHighErrorRate is firing. ...` when the alert came in
- `[critical] checkout - CheckoutHighErrorRate is resolved. ...` when it cleared
- `[AUTO-RESOLVED] checkout - CheckoutHighErrorRate recovered after degrade_or_scale, 330s after it was first seen ...` from the runbook

If the alert hadn't cleared in time, the last one would be `[ESCALATED] checkout - CheckoutHighErrorRate needs a person: ...` with the reason.

**Talking point**: "The system is back to a healthy state. On-call got the notifications, but nobody had to do anything."

---

## Demo Wrap-Up (1 minute)

### Key Metrics to Highlight

```bash
# MTTR is on the incident record
# mttr_seconds: time from when the platform first saw the alert to when it saw the resolution

# Show cost efficiency
echo "Estimated cost per incident: $0.001 (Lambda + EventBridge + DynamoDB)"

# Show scalability
echo "System capacity: 1000 alerts/second, 100 concurrent remediations"
```

### Architecture Summary

**Script**: "Let me quickly show you the architecture that makes this possible."

**Open**: `docs/architecture/README.md` in browser or show diagram

**Highlight**:
1. **Detection**: Prometheus scrapes metrics every 15s, evaluates rules every 30s
2. **Ingestion**: API Gateway → Lambda validates and enriches alerts
3. **Routing**: EventBridge fans out to 4 targets (DynamoDB, SNS, Step Functions, CloudWatch)
4. **Orchestration**: Step Functions executes runbook workflow
5. **Remediation**: Lambda calls the Kubernetes API with its own IAM role, mapped to an RBAC group by an EKS access entry
6. **Verification**: the state machine checks DynamoDB for the resolved alert, and escalates if it hasn't resolved 10 minutes after the action

---

## Reset Procedure (Post-Demo, 1 minute)

To reset the system for another demo or return to normal state:

The runbook doesn't undo its own changes, so this is also how to put things back after a real incident.

```bash
# Step 1: Turn off error injection (the chart's default)
kubectl set env deployment/checkout -n apps ERROR_RATE=0.00

# Verify
kubectl get deployment checkout -n apps -o jsonpath='{.spec.template.spec.containers[0].env}'

# Step 2: Turn off degraded mode. The pods pick it up within 15 seconds; no restart needed.
aws ssm put-parameter \
  --name /checkout/degraded_mode \
  --value "false" \
  --overwrite

# Step 3: Put the HPA minimum back to the chart's 3 (re-running deploy.sh does this too)
kubectl patch hpa checkout -n apps --type merge -p '{"spec":{"minReplicas":3}}'

# Step 4: Wait for rollout to complete
kubectl rollout status deployment/checkout -n apps

# Step 5: Verify healthy state
curl -s http://localhost:8080/metrics | grep -E 'checkout_degraded_mode_enabled|http_requests_total\{route="/checkout"'
# Expected: checkout_degraded_mode_enabled 0.0, and 5xx counts stopped increasing

# Step 6: Clear old incident records (optional)
aws dynamodb delete-item \
  --table-name event-driven-sre-incidents \
  --key '{"incident_id": {"S": "INCIDENT_ID_FROM_QUERY"}}'
```

**Expected time**: 60-90 seconds for system to return to healthy state. Clearing records isn't needed to run the demo again: the next alert has a new `startsAt`, so it's a new incident.

---

## Troubleshooting Common Issues

### Issue 1: Alert Doesn't Fire

**Symptoms**: Error rate high, but no alert in Alertmanager

**Debug steps**:
```bash
# Check Prometheus scraping
kubectl port-forward -n monitoring svc/mon-kube-prometheus-stack-prometheus 9090:9090
# Open http://localhost:9090/targets
# Verify: checkout target shows UP

# Check alert rule is loaded
# Open http://localhost:9090/alerts
# Verify: CheckoutHighErrorRate rule is present

# Check PromQL query manually
# Query: sum(rate(http_requests_total{route="/checkout",code=~"5.."}[5m])) / sum(rate(http_requests_total{route="/checkout"}[5m]))
# Expected: Value > 0.05, for 5 minutes
```

**Fix**: If target is DOWN, check ServiceMonitor:
```bash
kubectl get servicemonitor -n apps checkout -o yaml
# Verify: selector matches service labels
```

---

### Issue 2: Webhook Not Received

**Symptoms**: Alert fires, but no Lambda logs in CloudWatch

**Debug steps**:
```bash
# Check Alertmanager config
kubectl get secret -n monitoring alertmanager-mon-kube-prometheus-stack-alertmanager -o jsonpath='{.data.alertmanager\.yaml}' | base64 -d

# Verify: webhook_configs contains correct URL

# Test webhook manually. A warning only notifies, so nothing in the cluster changes.
WEBHOOK_URL=$(terraform -chdir=terraform output -raw alert_webhook_url)
API_KEY=$(terraform -chdir=terraform output -raw alertmanager_api_key)

curl -X POST "$WEBHOOK_URL" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "alerts": [{
      "labels": {"alertname": "TestAlert", "severity": "warning"},
      "annotations": {"summary": "Test alert"},
      "status": "firing"
    }]
  }'

# Expected: HTTP 202 Accepted
```

**Fix**: A 403 means the `X-API-Key` header is missing or wrong. Re-run `scripts/deploy.sh` so Alertmanager's secret has the current key.

---

### Issue 3: Step Functions Not Starting

**Symptoms**: EventBridge receives event, but no execution in Step Functions

**Debug steps**:
```bash
# Check EventBridge rule
aws events list-rules --event-bus-name event-driven-sre-bus

# Check rule targets
aws events list-targets-by-rule \
  --rule event-driven-sre-incident-created \
  --event-bus-name event-driven-sre-bus

# Did the event reach the bus?
aws logs tail /aws/events/event-driven-sre --since 30m

# Events EventBridge couldn't deliver end up here
aws sqs get-queue-attributes --attribute-names ApproximateNumberOfMessages \
  --queue-url "$(terraform -chdir=terraform output -raw events_dlq_url)"
```

An execution that starts and stops at `NotAutoRemediated` means the alert wasn't critical or was annotated `runbook_action: notify_only`. One that stops at `AlreadyHandled` means another run already claimed the incident, or it had already resolved.

**Fix**: If IAM error, check EventBridge execution role:
```bash
cd terraform
terraform plan -target=aws_iam_role.events_invoke
# Verify: Role has states:StartExecution permission
```

---

### Issue 4: Remediation Lambda Fails

**Symptoms**: The execution goes ExecuteAction → ReportActionFailed → Escalate and ends as FAILED with error `Escalated`

**Debug steps**:
```bash
# The escalation reason, e.g. "degrade_or_scale failed: InvalidRequest"
aws stepfunctions describe-execution --execution-arn EXECUTION_ARN --query cause --output text

# The Lambda's full error is in ExecuteAction's details in the console, and in the
# action.completed event (result failed) in /aws/events/event-driven-sre
aws logs tail /aws/lambda/event-driven-sre-runbook-action --since 30m --format short

# Common errors:
# - InvalidRequest → the namespace isn't in ALLOWED_NAMESPACES (apps), the deployment name isn't valid,
#   or replicas is outside 1 to MAX_REPLICAS. Not retried.
# - Timeouts → the Lambda can't reach the cluster API; check its security group and the cluster security group rule
# - A Kubernetes API 403 → check the access entry and RBAC
# - A Kubernetes API 404 → no such deployment in that namespace

# Run the Lambda directly with the payload the state machine sends.
# This really restarts the checkout deployment.
aws lambda invoke \
  --function-name event-driven-sre-runbook-action \
  --cli-binary-format raw-in-base64-out \
  --payload '{"incident_id": "manual-test", "alertname": "ManualTest", "severity": "critical", "action": "restart", "namespace": "apps", "deployment": "checkout", "replicas": 4}' \
  /tmp/response.json && cat /tmp/response.json
```

**Fix**: If RBAC error, verify the access entry, Role and RoleBinding:
```bash
aws eks list-access-entries --cluster-name "$(terraform -chdir=terraform output -raw eks_cluster_name)"
kubectl get role,rolebinding -n apps checkout-remediation
kubectl auth can-i patch deployments -n apps --as=runbook-action --as-group=checkout-remediation
```

---

### Issue 5: Degraded Mode Not Working

**Symptoms**: SSM parameter is `true`, but the app still returns errors

**Debug steps**:
```bash
# Check parameter value
aws ssm get-parameter --name /checkout/degraded_mode --query 'Parameter.Value' --output text

# The pod's own view (0.0 or 1.0), re-read at most every 15 seconds.
curl -s http://localhost:8080/metrics | grep checkout_degraded_mode_enabled

# A pod that can't read SSM logs one warning and keeps its last value
kubectl logs -n apps -l app=checkout | grep "Can't read"

# The service account should carry the IRSA role, and the pods should have AWS_ROLE_ARN
kubectl get sa checkout -n apps -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}{"\n"}'
kubectl describe pod -n apps -l app=checkout | grep -i "AWS_ROLE_ARN"
```

**Fix**: The role ARN should match `terraform -chdir=terraform output -raw checkout_role_arn`. If it's missing or different, re-run `scripts/deploy.sh`, which passes it to the chart, then restart the pods so they get the new credentials:
```bash
kubectl rollout restart deployment/checkout -n apps
```

---

## Advanced Demo Variations

### Variation 1: Show Multiple Simultaneous Alerts

```bash
# Inject both errors and latency (the latency alert needs p95 over 1s)
kubectl set env deployment/checkout -n apps \
  ERROR_RATE=0.10 \
  LATENCY_MS=1500

# Result: Two alerts fire (CheckoutHighErrorRate, CheckoutHighLatencyP95)
# Each is its own incident with its own execution; both apply degrade_or_scale
```

### Variation 2: Show an Escalation

A critical alert with no runbook is escalated right after it's logged:

```bash
WEBHOOK_URL=$(terraform -chdir=terraform output -raw alert_webhook_url)
API_KEY=$(terraform -chdir=terraform output -raw alertmanager_api_key)

curl -X POST "$WEBHOOK_URL" -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"alerts": [{"status": "firing",
       "labels": {"alertname": "CheckoutQueueBacklog", "severity": "critical", "service": "checkout"},
       "annotations": {"summary": "Demo escalation"},
       "fingerprint": "demo-escalation-1", "startsAt": "2026-10-23T10:00:00Z"}]}'

# The execution goes LogIncident → ChooseAction → RunbookAvailable → Escalate and ends FAILED
# The record has status escalated and escalation_reason "no runbook for CheckoutQueueBacklog"
# SNS sends "[ESCALATED] checkout - CheckoutQueueBacklog needs a person: no runbook for CheckoutQueueBacklog ..."
```

To stop a run that's already going, `aws stepfunctions stop-execution --execution-arn EXECUTION_ARN`. The incident stays `remediating` until the alert resolves.

### Variation 3: Show a Warning That Only Notifies

Warnings go to SNS and DynamoDB, but the runbook stops at its first step:

```bash
curl -X POST "$WEBHOOK_URL" -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"alerts": [{"status": "firing",
       "labels": {"alertname": "DemoWarning", "severity": "warning", "service": "checkout"},
       "annotations": {"summary": "Demo warning"},
       "fingerprint": "demo-warning-1", "startsAt": "2026-10-23T10:00:00Z"}]}'

# The execution ends at NotAutoRemediated; the record is open with no actions_taken.
# Send the same body with "status": "resolved" and the record goes to resolved.
```

### Variation 4: Drain a Node

The runbook drains an `apps` node when `NodeUnderPressure` fires. To try it without real memory or disk pressure, send the alert yourself with a real node name. This really cordons the node and moves its pods.

```bash
NODE=$(kubectl get nodes -l nodegroup=apps -o jsonpath='{.items[0].metadata.name}')

curl -X POST "$WEBHOOK_URL" -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"alerts": [{"status": "firing",
       "labels": {"alertname": "NodeUnderPressure", "severity": "critical", "service": "cluster",
                  "node": "'"$NODE"'", "condition": "MemoryPressure"},
       "annotations": {"summary": "Demo drain"},
       "fingerprint": "demo-drain-1", "startsAt": "2026-10-23T10:00:00Z"}]}'

kubectl get nodes -l nodegroup=apps     # the node shows SchedulingDisabled
kubectl get pods -n apps -o wide -w     # checkout pods move off it, never fewer than 2 running

# ExecuteAction's output has cordoned: true, evicted, skipped, unmanaged and still_blocked.
# A system node, or the last schedulable apps node, is refused and the incident escalates.
```

This alert won't resolve on its own, so send the same body with `"status": "resolved"` (or the runbook escalates after 10 minutes). The node stays cordoned either way:

```bash
kubectl uncordon "$NODE"
```

---

## Demo Timing Breakdown

| Phase | Duration | Actions |
|-------|----------|---------|
| Phase 1: Show healthy state | 30s | Show pods, metrics |
| Phase 2: Inject failure | 30s | Set ERROR_RATE env var |
| Phase 3: Alert fires | 7-8m | Ratio crosses 5% after about 2.5m, then the rule's 5m `for` |
| Phase 4: Auto-remediation | 1m | Show Step Functions, Lambda logs |
| Phase 5: Verify remediation | 1m | Show SSM, the degraded-mode gauge, the HPA minimum |
| Phase 6: Error rate drops | 3-5m | 5m rate window, then Alertmanager's 2m `group_interval` |
| Phase 7: Incident record | 1m | Show DynamoDB entry |
| Phase 8: Alert resolves | 30s | Show Alertmanager resolved, SNS emails |
| **Total** | **about 15m** | Talk through the architecture while phases 3 and 6 wait |

---

## Pre-Demo Checklist

Print this checklist and verify before each demo:

- [ ] AWS credentials configured and valid
- [ ] kubectl context set to correct EKS cluster
- [ ] Checkout image built and pushed with `scripts/build-image.sh`, then deployed with `scripts/deploy.sh`
- [ ] All pods in `apps` and `monitoring` namespaces are Running
- [ ] Prometheus targets are UP (check /targets)
- [ ] Alertmanager webhook URL configured correctly
- [ ] Grafana dashboard accessible (port-forward if needed)
- [ ] Terraform outputs available (alert_webhook_url, sns_topic_arn, step_functions_arn)
- [ ] Error rate currently at 0% (healthy state)
- [ ] Degraded mode currently false
- [ ] CloudWatch Logs Insights dashboards bookmarked
- [ ] AWS Console tabs open (DynamoDB, Step Functions)
- [ ] Demo script open and reviewed
- [ ] Backup plan if live demo fails (recorded video or screenshots)

---

## Post-Demo Discussion Points

After the demo, be prepared to discuss:

1. **Architecture decisions**: Why serverless? Why EventBridge vs direct Lambda invocations?
2. **Security**: How is IRSA more secure than long-lived credentials?
3. **Cost**: What's the monthly cost at scale? How to optimize?
4. **Testing**: How do you test this without triggering real alerts?
5. **Limitations**: What scenarios can't be automated? When do you need humans?
6. **Scalability**: What happens at 10,000 alerts/second?
7. **Multi-region**: How would you extend this to multiple regions?
8. **Rollback**: What if remediation makes things worse?
9. **Observability**: How do you monitor the monitoring system (meta-monitoring)?
10. **Evolution**: What would you build next if you had 3 more months?

---

## Quick Command Reference

**Port Forwards** (run in separate terminals):
```bash
kubectl port-forward -n monitoring svc/mon-kube-prometheus-stack-prometheus 9090:9090
kubectl port-forward -n monitoring svc/alertmanager-operated 9093:9093
kubectl port-forward -n monitoring svc/mon-grafana 3000:80
kubectl port-forward -n apps svc/checkout 8080:80
```

**Key URLs**:
- Prometheus: http://localhost:9090
- Alertmanager: http://localhost:9093
- Grafana: http://localhost:3000 (admin/prom-operator)
- Checkout metrics: http://localhost:8080/metrics
- AWS Step Functions: https://console.aws.amazon.com/states/home
- AWS DynamoDB: https://console.aws.amazon.com/dynamodbv2/home

**Terraform Outputs**:
```bash
cd terraform
terraform output alert_webhook_url
terraform output sns_topic_arn
terraform output step_functions_arn
terraform output incidents_table
terraform output runbook_log_group
```

---

**Last Updated**: 2026-02-02
**Demo Version**: 1.0
**Estimated Duration**: about 15 minutes
