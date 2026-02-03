# Event-Driven SRE Platform - Demo Script

## Overview

This is a step-by-step demo script designed to showcase the event-driven SRE platform in **5-7 minutes**. It demonstrates the complete incident response loop from alert detection to automated remediation.

**Demo Flow**: Error injection → Alert fires → Automated remediation → Alert resolves → System recovers

---

## Prerequisites Checklist

Before starting the demo, verify:

```bash
# 1. AWS credentials configured
aws sts get-caller-identity

# 2. kubectl configured for EKS cluster
kubectl cluster-info

# 3. Terraform outputs available
cd terraform
terraform output

# 4. Required CLI tools installed
which kubectl helm aws jq curl
```

Expected: All commands succeed without errors.

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

# Check current error rate (should be ~0%)
curl http://localhost:8080/metrics | grep checkout_errors_total
```

Expected: Error rate near 0, all targets healthy.

---

## Demo Script (5-7 minutes)

### Phase 1: Show Healthy State (30 seconds)

**Script**: "Let me show you the system in a healthy state. We have a checkout microservice running on EKS, monitored by Prometheus."

```bash
# Show running pods
kubectl get pods -n apps -l app=checkout

# Expected output:
# NAME                        READY   STATUS    RESTARTS   AGE
# checkout-5d7c8b9f4d-abc12   1/1     Running   0          10m
# checkout-5d7c8b9f4d-def34   1/1     Running   0          10m
# checkout-5d7c8b9f4d-ghi56   1/1     Running   0          10m

# Show current metrics
curl http://localhost:8080/metrics | grep -E "checkout_requests_total|checkout_errors_total"

# Expected: requests increasing, errors near 0
```

**Talking point**: "We have 3 replicas running, exposing Prometheus metrics. Error rate is currently 0%."

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

**Script**: "Within 30 seconds, Prometheus evaluates the alert rule and fires an alert."

```bash
# Open Grafana in browser (or use port-forward)
# Navigate to: Alerting → Alert Rules
# Look for: CheckoutHighErrorRate

# Or check Alertmanager directly
kubectl port-forward -n monitoring svc/alertmanager-operated 9093:9093 &
# Open http://localhost:9093/#/alerts
```

**Expected behavior**:
- Alert transitions from `Pending` (30s) to `Firing`
- Alert shows labels: `severity=critical`, `service=checkout`

**Talking point**: "Prometheus detected the error rate exceeded our 5% threshold. The alert is now in FIRING state and Alertmanager is routing it to our webhook."

**Show alert payload** (optional, for technical audience):

```bash
# Tail CloudWatch logs to see webhook received
aws logs tail /aws/lambda/alert-ingest --follow --format short

# Expected log entry:
# Received alert: CheckoutHighErrorRate, severity: critical
```

---

### Phase 4: Show Automated Remediation (1 minute)

**Script**: "The alert webhook triggers our event-driven pipeline. Let's watch the automation in action."

```bash
# Check EventBridge events
aws events describe-event-bus --name sre-platform-events

# Check Step Functions execution started
aws stepfunctions list-executions \
  --state-machine-arn $(terraform output -raw step_functions_arn) \
  --max-results 1 \
  --query 'executions[0].[name,status,startDate]' \
  --output table

# Expected: New execution in RUNNING state

# Watch Step Functions in AWS Console
# Navigate to: Step Functions → State machines → incident-response-runbook → Executions
```

**Show in AWS Console**:
- Execution graph: visual flow through states
- Input/output: alert details
- Current step: "ExecuteRunbookAction"

**Talking point**: "Step Functions orchestrates the remediation workflow. It's now invoking the runbook_action Lambda to enable degraded mode."

```bash
# Watch Lambda logs
aws logs tail /aws/lambda/runbook-action --follow --format short

# Expected log entries:
# Authenticating to EKS cluster: my-cluster
# Updating SSM parameter: /checkout/degraded_mode = true
# Patching deployment: apps/checkout
# Remediation successful
```

---

### Phase 5: Verify Remediation Applied (1 minute)

**Script**: "The Lambda function updated a feature flag and triggered a rolling restart. Let's verify the changes."

```bash
# Check SSM parameter updated
aws ssm get-parameter --name /checkout/degraded_mode --query 'Parameter.Value' --output text

# Expected output: true

# Check deployment has been patched (restart annotation added)
kubectl get deployment checkout -n apps -o jsonpath='{.spec.template.metadata.annotations.restartedAt}'

# Expected: Recent timestamp

# Watch pods rolling restart
kubectl get pods -n apps -l app=checkout -w

# Expected: Pods terminating and new pods starting
```

**Talking point**: "Degraded mode disables optional features like recommendation engines, reducing load and error rate. The deployment is rolling out the new configuration."

---

### Phase 6: Observe Error Rate Drop (1 minute)

**Script**: "As the new pods come online, the error rate drops below the threshold and the alert resolves."

```bash
# Check current metrics
curl http://localhost:8080/metrics | grep -E "checkout_errors_total|checkout_degraded_mode_enabled"

# Expected:
# checkout_errors_total{status="500"} 15  (stopped increasing)
# checkout_degraded_mode_enabled 1

# Check Prometheus query
# Open http://localhost:9090/graph
# Query: rate(checkout_errors_total[1m]) / rate(checkout_requests_total[1m])
# Expected: Graph shows spike then drop below 0.05 (5%)
```

**Show in Grafana**:
- Error rate panel: spike followed by recovery
- Degraded mode gauge: 0 → 1

**Talking point**: "The error rate has dropped from 10% to under 1%. The alert will resolve automatically within the next evaluation cycle."

---

### Phase 7: Show Incident Record (1 minute)

**Script**: "All incident data is stored in DynamoDB for audit and analysis."

```bash
# Query recent incidents
aws dynamodb scan \
  --table-name sre-incidents \
  --max-items 1 \
  --query 'Items[0]' \
  --output json | jq '.'

# Expected output (formatted JSON):
# {
#   "incident_id": "550e8400-e29b-41d4-a716-446655440000",
#   "alert_name": "CheckoutHighErrorRate",
#   "severity": "critical",
#   "status": "resolved",
#   "timestamp": "2026-02-02T10:30:00Z",
#   "labels": {
#     "service": "checkout",
#     "namespace": "apps"
#   },
#   "actions_taken": ["enable_degraded_mode"],
#   "resolved_at": 1738491090
# }
```

**Show in DynamoDB Console** (optional):
- Navigate to: DynamoDB → Tables → sre-incidents → Items
- Sort by timestamp (descending)
- Show latest incident record

**Talking point**: "We have a complete audit trail: when the alert fired, what actions were taken, and when it resolved. Mean Time To Resolution (MTTR) was approximately 90 seconds, compared to several minutes with manual intervention."

---

### Phase 8: Show Alert Resolved (30 seconds)

**Script**: "The alert has now resolved, completing the automated incident response cycle."

```bash
# Check Alertmanager
# Open http://localhost:9093/#/alerts
# Expected: CheckoutHighErrorRate no longer in firing state

# Check Step Functions execution completed
aws stepfunctions list-executions \
  --state-machine-arn $(terraform output -raw step_functions_arn) \
  --max-results 1 \
  --query 'executions[0].status' \
  --output text

# Expected: SUCCEEDED

# Check SNS notification sent (if email configured)
aws sns list-subscriptions-by-topic \
  --topic-arn $(terraform output -raw sns_topic_arn) \
  --query 'Subscriptions[0].Endpoint'

# Expected: Your email address (check inbox for notification)
```

**Talking point**: "The system is now back to healthy state. The on-call engineer received a notification, but no manual intervention was needed. This is the power of event-driven automation."

---

## Demo Wrap-Up (1 minute)

### Key Metrics to Highlight

```bash
# Calculate MTTR
# Time from alert firing to resolution: ~90 seconds

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
5. **Remediation**: Lambda uses IRSA to call Kubernetes API securely
6. **Verification**: System monitors for alert resolution

---

## Reset Procedure (Post-Demo, 1 minute)

To reset the system for another demo or return to normal state:

```bash
# Step 1: Disable error injection
kubectl set env deployment/checkout -n apps ERROR_RATE-

# Verify removed
kubectl get deployment checkout -n apps -o jsonpath='{.spec.template.spec.containers[0].env}'

# Step 2: Disable degraded mode
aws ssm put-parameter \
  --name /checkout/degraded_mode \
  --value "false" \
  --overwrite

# Step 3: Restart deployment to pick up config
kubectl rollout restart deployment/checkout -n apps

# Step 4: Wait for rollout to complete
kubectl rollout status deployment/checkout -n apps

# Step 5: Verify healthy state
curl http://localhost:8080/metrics | grep checkout_errors_total
# Expected: Error count stopped increasing

# Step 6: Clear old incident records (optional)
aws dynamodb delete-item \
  --table-name sre-incidents \
  --key '{"incident_id": {"S": "INCIDENT_ID_FROM_SCAN"}}'
```

**Expected time**: 60-90 seconds for system to return to healthy state.

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
# Query: rate(checkout_errors_total[1m]) / rate(checkout_requests_total[1m])
# Expected: Value > 0.05
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

# Test webhook manually
WEBHOOK_URL=$(cd terraform && terraform output -raw alert_webhook_url)

curl -X POST "$WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -d '{
    "alerts": [{
      "labels": {"alertname": "TestAlert", "severity": "critical"},
      "annotations": {"summary": "Test alert"},
      "status": "firing"
    }]
  }'

# Expected: HTTP 202 Accepted
```

**Fix**: If 403 Forbidden, check API Gateway resource policy:
```bash
cd terraform
terraform apply -target=aws_api_gateway_rest_api.alert_webhook
```

---

### Issue 3: Step Functions Not Starting

**Symptoms**: EventBridge receives event, but no execution in Step Functions

**Debug steps**:
```bash
# Check EventBridge rule
aws events list-rules --event-bus-name sre-platform-events

# Check rule targets
aws events list-targets-by-rule \
  --rule incident-created-rule \
  --event-bus-name sre-platform-events

# Check CloudWatch Logs for EventBridge errors
aws logs tail /aws/events/sre-platform-events --follow
```

**Fix**: If IAM error, check EventBridge execution role:
```bash
cd terraform
terraform plan -target=aws_iam_role.eventbridge_role
# Verify: Role has states:StartExecution permission
```

---

### Issue 4: Remediation Lambda Fails

**Symptoms**: Step Functions shows Lambda failure

**Debug steps**:
```bash
# Check Lambda logs
aws logs tail /aws/lambda/runbook-action --follow --format short

# Common errors:
# - "EKS cluster not accessible" → Check VPC configuration
# - "AccessDeniedException" → Check IRSA and RBAC
# - "Deployment not found" → Check namespace and deployment name

# Test EKS connectivity from Lambda
aws lambda invoke \
  --function-name runbook-action \
  --payload '{"action": "test_connection"}' \
  /tmp/response.json
```

**Fix**: If RBAC error, verify ServiceAccount and Role:
```bash
kubectl get sa -n apps checkout-remediation
kubectl get role -n apps checkout-remediation
kubectl get rolebinding -n apps checkout-remediation
```

---

### Issue 5: Degraded Mode Not Working

**Symptoms**: SSM parameter updated, but app still has errors

**Debug steps**:
```bash
# Check app is reading SSM
kubectl logs -n apps -l app=checkout | grep -i "degraded"

# Expected: Log entry showing parameter read

# Check parameter value
aws ssm get-parameter --name /checkout/degraded_mode --query 'Parameter.Value' --output text

# Check IAM permissions for pods
# Pods should have IRSA role to read SSM
kubectl describe pod -n apps -l app=checkout | grep -i "AWS_ROLE_ARN"
```

**Fix**: If pods can't read SSM, check IAM policy:
```bash
cd terraform
terraform plan -target=aws_iam_role.checkout_pod_role
# Verify: Policy allows ssm:GetParameter
```

---

## Advanced Demo Variations

### Variation 1: Show Multiple Simultaneous Alerts

```bash
# Inject both errors and latency
kubectl set env deployment/checkout -n apps \
  ERROR_RATE=0.10 \
  LATENCY_MS=500

# Result: Two alerts fire (CheckoutHighErrorRate, CheckoutHighLatency)
# Step Functions handles both in parallel
```

### Variation 2: Show Manual Override

```bash
# Start auto-remediation, then cancel
aws stepfunctions stop-execution \
  --execution-arn EXECUTION_ARN \
  --cause "Manual intervention required"

# Update incident status to escalated
aws dynamodb update-item \
  --table-name sre-incidents \
  --key '{"incident_id": {"S": "INCIDENT_ID"}}' \
  --update-expression "SET #status = :escalated" \
  --expression-attribute-names '{"#status": "status"}' \
  --expression-attribute-values '{":escalated": {"S": "escalated"}}'
```

### Variation 3: Show Alert Resolution Without Remediation

```bash
# Inject error, let alert fire
kubectl set env deployment/checkout -n apps ERROR_RATE=0.10

# Wait for alert to fire (30-60 seconds)

# Manually fix the issue (remove error injection)
kubectl set env deployment/checkout -n apps ERROR_RATE-

# Watch alert auto-resolve
# EventBridge receives resolved event, updates DynamoDB
```

---

## Demo Timing Breakdown

| Phase | Duration | Actions |
|-------|----------|---------|
| Phase 1: Show healthy state | 30s | Show pods, metrics |
| Phase 2: Inject failure | 30s | Set ERROR_RATE env var |
| Phase 3: Alert fires | 1m | Show Alertmanager, webhook logs |
| Phase 4: Auto-remediation | 1m | Show Step Functions, Lambda logs |
| Phase 5: Verify remediation | 1m | Show SSM, deployment patch |
| Phase 6: Error rate drops | 1m | Show metrics recovery |
| Phase 7: Incident record | 1m | Show DynamoDB entry |
| Phase 8: Alert resolves | 30s | Show Alertmanager resolved |
| **Total** | **6m 30s** | |

---

## Pre-Demo Checklist

Print this checklist and verify before each demo:

- [ ] AWS credentials configured and valid
- [ ] kubectl context set to correct EKS cluster
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
terraform output dynamodb_table_name
```

---

**Last Updated**: 2026-02-02
**Demo Version**: 1.0
**Estimated Duration**: 5-7 minutes
