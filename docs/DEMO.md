# Demo walkthrough

Breaks the `checkout` service on purpose and follows the alert through the platform. Takes about 15 minutes, most of it waiting for the alert's 5-minute window.

## Before you start

- Everything is deployed as described in the [README](../README.md#deploy), including the Alertmanager webhook URL.
- To get the notification email, subscribe to the SNS topic (see [API.md](API.md#checking-that-a-request-went-through)) and confirm the subscription.
- Resource names below assume the default `name` variable, `event-driven-sre`.

Check that the app and monitoring stack are running:

```bash
kubectl get pods -n apps
kubectl get pods -n monitoring
```

## 1. Send traffic

The alert rules measure the error rate of requests to `/checkout`. With no traffic there's nothing to measure, so start a loop inside the cluster and leave it running:

```bash
kubectl run load -n apps --image=curlimages/curl --restart=Never -- \
  sh -c 'while true; do curl -s -o /dev/null http://checkout/checkout; sleep 0.2; done'
```

## 2. Break the service

Make 10% of requests fail:

```bash
kubectl set env deployment/checkout -n apps ERROR_RATE=0.10
```

The pods restart with the new setting.

## 3. Watch the alert fire

```bash
kubectl port-forward -n monitoring svc/alertmanager-operated 9093:9093
```

Open http://localhost:9093. `CheckoutHighErrorRate` fires once more than 2% of requests have failed for 5 minutes. `CheckoutSLOBurnFast` fires around the same time, because its threshold is much lower.

## 4. Follow it through AWS

```bash
aws logs tail /aws/lambda/event-driven-sre-alert-ingest --since 15m
aws dynamodb scan --table-name event-driven-sre-incidents --max-items 5

SFN_ARN=$(aws stepfunctions list-state-machines \
  --query "stateMachines[?name=='event-driven-sre-runbook'].stateMachineArn" --output text)
aws stepfunctions list-executions --state-machine-arn "$SFN_ARN" --max-results 5
```

You should see one incident record and one Step Functions execution per alert, and a notification email if you subscribed.

## 5. Remediation

```bash
aws logs tail /aws/lambda/event-driven-sre-runbook-action --since 15m
```

Both alerts map to `degrade_or_scale`.

**As the repo stands, this step fails.** The runbook Lambda's role has no access inside the cluster, so the Kubernetes API rejects the patch and the log shows `K8s API PATCH ... failed: 401`. Step Functions retries three times and then marks the execution as failed. See [Known gaps](../README.md#known-gaps).

Once the role has an EKS access entry that can patch deployments in `apps`, the same run:

1. sets `/checkout/degraded_mode` to `true` in SSM
2. sets `DEGRADED_MODE=true` on the deployment and restarts it
3. scales it to 4 replicas

In degraded mode the app stops injecting errors, so the error rate falls and the alert resolves. The resolved notification currently starts the same remediation again.

## 6. Reset

```bash
kubectl delete pod load -n apps
kubectl set env deployment/checkout -n apps ERROR_RATE=0 DEGRADED_MODE=false
kubectl scale deployment/checkout -n apps --replicas=2
aws ssm put-parameter --name /checkout/degraded_mode --value false --overwrite
```
