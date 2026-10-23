# event-driven-sre-platform

Automated incident response for a service running on EKS. Prometheus alerts for a demo `checkout` service are sent to an endpoint on AWS, recorded, and passed to a Lambda function that changes the deployment in response: it switches the service to degraded mode, restarts it, or scales it up.

The alert path (Alertmanager to API Gateway to Lambda to DynamoDB, SNS and Step Functions) is in place and covered by unit tests. Remediation against a real cluster still needs one piece the repo doesn't include; see [Known gaps](#known-gaps).

## How it works

1. Prometheus, installed with kube-prometheus-stack, evaluates five alert rules for `checkout`: `CheckoutHighErrorRate`, `CheckoutHighLatencyP95`, `CheckoutDown`, `CheckoutSLOBurnFast` and `CheckoutSLOBurnSlow`.
2. Alertmanager posts firing and resolved alerts to an API Gateway HTTP API at `POST /alert`.
3. The `alert-ingest` Lambda handles each alert in the payload. It writes an incident record to DynamoDB, publishes a notification to SNS, puts an event on an EventBridge bus, and starts a Step Functions execution.
4. The Step Functions state machine invokes the `runbook-action` Lambda, retrying up to three times with backoff.
5. `runbook-action` picks an action from the alert name and applies it through the Kubernetes API. It authenticates with an EKS token signed by its own IAM role.

| Alert | Action |
| --- | --- |
| `CheckoutHighErrorRate`, `CheckoutHighLatencyP95`, `CheckoutSLOBurnFast` | Set `DEGRADED_MODE=true` on the deployment, restart it, and scale it to 4 replicas. Also sets the SSM parameter `/checkout/degraded_mode` to `true` as a record. |
| `CheckoutDown` | Restart the deployment. |
| Anything else | Notification only. |

An alert can override this with a `runbook_action` annotation (`degrade`, `restart`, `scale`, `degrade_or_scale` or `notify_only`), and set the replica count with `desired_replicas`.

The `checkout` app is a small Flask service with `/checkout`, `/metrics`, `/healthz` and `/readyz` endpoints. Three environment variables let you break it on purpose: `ERROR_RATE` (0.0 to 1.0) returns random errors, `LATENCY_MS` adds delay, and `DEGRADED_MODE=true` turns both off.

## What's in the repo

```
.
├── apps/checkout/            # Flask service, Dockerfile, unit tests
├── helm/
│   ├── checkout/             # chart: Deployment, Service, Ingress, ServiceMonitor, ConfigMap
│   ├── observability/        # kube-prometheus-stack and Alertmanager values, alert rules
│   └── datadog/              # optional Datadog agent values
├── terraform/
│   ├── networking.tf         # VPC, two AZs, a NAT gateway per AZ
│   ├── eks.tf                # EKS 1.29, "system" and "apps" managed node groups
│   ├── irsa.tf               # IAM roles for the ALB controller, ExternalDNS and cert-manager
│   ├── event_pipeline.tf     # API Gateway, both Lambdas, Step Functions, DynamoDB, SNS, EventBridge
│   ├── stepfunctions/        # state machine definition
│   └── lambda/               # alert_ingest and runbook_action, each with unit tests
├── scripts/                  # bootstrap.sh, install-addons.sh, deploy.sh, destroy.sh
├── tests/integration/        # end-to-end tests against LocalStack
└── Jenkinsfile
```

Terraform state goes to an S3 backend (`terraform/backend.tf`).

## Deploy

Requires the AWS CLI, Terraform, kubectl, Helm, Python 3, Docker and `zip`.

```bash
bash scripts/bootstrap.sh       # build the Lambdas, create the VPC, EKS and event pipeline, configure kubectl
bash scripts/install-addons.sh  # AWS Load Balancer Controller, ExternalDNS, cert-manager
bash scripts/deploy.sh          # kube-prometheus-stack, the alert rules, and the checkout app
```

Before `deploy.sh`, set three values in `helm/checkout/values.yaml`:

- `image.repository`. The chart has a placeholder, and nothing in this repo builds or pushes the image, so build `apps/checkout` and push it to a registry the cluster can pull from.
- `ingress.host`
- `ingress.certificateArn`, an ACM certificate for that host

The ingress creates an internet-facing HTTPS load balancer through the AWS Load Balancer Controller. Set `ingress.enabled=false` if you don't want one.

Then point Alertmanager at the webhook. Get the URL:

```bash
terraform -chdir=terraform output -raw alert_webhook_url
```

Put it in `helm/observability/alertmanager-values.yaml` in place of the placeholder URL, and upgrade the release:

```bash
helm upgrade mon prometheus-community/kube-prometheus-stack \
  -f helm/observability/kube-prometheus-stack-values.yaml \
  -f helm/observability/alertmanager-values.yaml \
  -n monitoring
```

To tear it down, uninstall the Helm releases first. The load balancer the controller created isn't in Terraform state, and it blocks the VPC from being deleted while it exists.

```bash
helm uninstall checkout -n apps
helm uninstall mon -n monitoring
bash scripts/destroy.sh         # terraform destroy
```

## Trying it

Make the service fail 10% of requests:

```bash
kubectl set env deployment/checkout -n apps ERROR_RATE=0.10
```

Watch for `CheckoutHighErrorRate` in Alertmanager:

```bash
kubectl port-forward -n monitoring svc/alertmanager-operated 9093:9093
# then open http://localhost:9093
```

Once it fires, check that it reached AWS:

```bash
aws dynamodb scan --table-name event-driven-sre-incidents --max-items 5
aws logs tail /aws/lambda/event-driven-sre-alert-ingest --follow
aws logs tail /aws/lambda/event-driven-sre-runbook-action --follow
```

The names above assume the default `name` variable, `event-driven-sre`.

## Tests

```bash
# checkout app
cd apps/checkout && pip install -r requirements-dev.txt && pytest tests/ --cov

# Lambdas, with AWS calls mocked by moto
cd terraform/lambda/alert_ingest && pip install -r requirements-dev.txt && AWS_DEFAULT_REGION=us-east-1 pytest tests/ --cov
cd terraform/lambda/runbook_action && pip install -r requirements-dev.txt && AWS_DEFAULT_REGION=us-east-1 pytest tests/ --cov
```

`tests/integration/` runs the alert path end to end against LocalStack (`setup.sh`, then pytest, then `cleanup.sh`). It isn't part of CI.

CI (`.github/workflows/integration-tests.yml`) runs Checkov on the Terraform, Trivy on the container image, the app and Lambda unit tests, and `terraform fmt`, `validate` and `tflint`.

## Known gaps

- **The runbook Lambda can't reach the cluster yet.** Its IAM role, `event-driven-sre-runbook-action-role`, isn't granted anything inside EKS: there's no access entry and no RBAC binding. Its Kubernetes API calls are rejected until it gets an EKS access entry with permission to patch deployments in the `apps` namespace.
- **Resolved alerts trigger remediation again.** Alertmanager sends resolved notifications (`send_resolved: true`), and `alert-ingest` starts a Step Functions run for every alert regardless of status. When an error-rate alert clears, the service is degraded and scaled again.
- **The webhook has no authentication.** Anyone with the URL can post an alert and trigger an action.
- **Nothing consumes the EventBridge events.** Every alert is put on the bus, but no rules are attached to it.
- **Remediation always targets one deployment**, set by `TARGET_DEPLOYMENT` (`checkout`), not the alert's `service` label.
- **Incident records aren't updated.** The DynamoDB record is written when the alert arrives; the action taken and the outcome aren't added to it.
- **The default Kubernetes version is retired.** `eks_version` defaults to `1.29`, which reached the end of EKS extended support in March 2026, so a new cluster needs a newer version.

## Cost

Running continuously in `us-east-1` at on-demand prices, roughly $435 a month, nearly all of it fixed:

| | Monthly |
| --- | --- |
| EKS control plane | $73 |
| 4 × m6i.large nodes (2 per node group) | $280 |
| 2 NAT gateways | $66 |
| Load balancer for the checkout ingress | about $16 plus usage |

API Gateway, Lambda, Step Functions, DynamoDB and SNS add very little at the scale of a few thousand alerts a month. Tear it down when you're done.

## License

MIT. See [LICENSE](LICENSE).
