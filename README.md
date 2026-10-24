# event-driven-sre-platform

Automated incident response for a service running on EKS. Prometheus alerts for a demo `checkout` service are sent to an endpoint on AWS, recorded, and handed to a Step Functions runbook that changes the deployment in response: it switches the service to degraded mode, restarts it, or scales it up, and drains nodes under pressure. The runbook then checks that the alert clears and hands the incident to a person if it doesn't.

The alert path (Alertmanager to API Gateway to Lambda to EventBridge, which routes to DynamoDB, SNS and Step Functions) and remediation are in place and covered by unit tests. See [Known gaps](#known-gaps) for what's still missing.

## How it works

1. Prometheus, installed with kube-prometheus-stack, evaluates five alert rules for `checkout` (`CheckoutHighErrorRate`, `CheckoutHighLatencyP95`, `CheckoutDown`, `CheckoutSLOBurnFast` and `CheckoutSLOBurnSlow`) and one for the cluster's nodes, `NodeUnderPressure` (memory, disk or PID pressure for 5 minutes).
2. Alertmanager posts firing and resolved alerts to `POST /webhook` on an API Gateway REST API, with an `X-API-Key` header. API Gateway checks the body against a JSON schema and rate-limits requests (1000 per second, bursts of 2000). Datadog monitors can send to the same endpoint; see [docs/API.md](docs/API.md).
3. The `alert-ingest` Lambda validates the payload (anything invalid gets a `400` with an error code), normalizes Alertmanager and Datadog alerts into one format, and adds the account, region and receive time. Each alert gets a stable incident id, so a firing alert and its resolution belong to the same incident. It puts each alert on the EventBridge bus as an `incident.created` or `incident.resolved` event and answers `202 Accepted`. It doesn't call anything else.
4. EventBridge rules route the events:

   | Event | Source | Goes to |
   | --- | --- | --- |
   | `incident.created` | `sre.alertmanager`, `sre.datadog` | incident recorder (DynamoDB), SNS, runbook state machine |
   | `incident.resolved` | `sre.alertmanager`, `sre.datadog` | incident recorder (DynamoDB), SNS |
   | `action.completed` | `sre.runbook` | incident recorder (DynamoDB) |
   | `incident.escalated`, `incident.auto_resolved` | `sre.runbook` | SNS |
   | any `sre.*` event | | CloudWatch Logs |

   EventBridge can't write to DynamoDB, so the incident recorder, a small Express state machine, does those writes.
5. The runbook state machine only acts on firing `critical` alerts (`auto_remediate_severities`); warnings and info alerts just notify. It claims the incident in DynamoDB first. Only one run can claim it, so an incident is remediated once even if Alertmanager re-sends the alert or EventBridge delivers it twice.
6. It picks an action from the alert name and invokes the `runbook-action` Lambda with the action and its target, retrying up to three times with backoff. The Lambda applies it through the Kubernetes API, authenticating with an EKS token signed by its own IAM role. The result goes out as an `action.completed` event.
7. It waits a minute, then checks DynamoDB every 30 seconds for the resolved alert. If the alert resolves within 10 minutes of the action, it records the MTTR and publishes `incident.auto_resolved`. If it doesn't, or the action failed, or there's no runbook for the alert, it marks the incident `escalated` and publishes `incident.escalated`. Both go to SNS.

| Alert | Action |
| --- | --- |
| `CheckoutHighErrorRate`, `CheckoutHighLatencyP95`, `CheckoutSLOBurnFast` | Turn on degraded mode by setting the SSM parameter `/checkout/degraded_mode` to `true` (the pods read it within 15 seconds, no restart), and raise the HPA's minimum to 4 replicas. |
| `CheckoutDown` | Restart the deployment. |
| `NodeUnderPressure` | Drain the node: cordon it and evict its pods, respecting PodDisruptionBudgets. Only `apps` nodes, and only if another schedulable `apps` node is left. |
| Any other critical alert | Escalate. |

An alert can override this with a `runbook_action` annotation (`degrade`, `restart`, `scale`, `degrade_or_scale` or `drain_node`, or `notify_only` to skip the runbook), and set the replica count with `desired_replicas`. The target is the alert's `namespace` and `deployment` labels (or Datadog's `kube_namespace` and `kube_deployment` tags), falling back to its `service` label and then `apps/checkout`, and for a drain its `node` label (or Datadog's `host` tag). The Lambda refuses namespaces other than `apps` and more than 10 replicas (`remediation_max_replicas`).

None of this is undone automatically when the incident resolves; see [Known gaps](#known-gaps).

Each incident's DynamoDB record goes from `open` to `remediating`, then to `resolved` when the alert clears, or `escalated` when the runbook gives up. Every action and its result are appended to `actions_taken`, and a remediated incident gets `remediation_result` and `mttr_seconds`. The table has point-in-time recovery and deletion protection on, and deletes incidents 90 days after their last activity.

Every EventBridge target retries for up to an hour, then sends the event to an SQS dead-letter queue. Every event on the bus is also written to the `/aws/events/<name>` log group (kept 30 days) and to an archive (kept 7 days) that can be replayed. Replaying is safe: the recorder only updates records, and the runbook won't claim an incident twice. The runbook logs every state's input and output to `/aws/vendedlogs/states/<name>-runbook`.

The `checkout` app is a small Flask service with `/checkout`, `/metrics`, `/healthz` and `/readyz` endpoints, running as 3 to 10 pods behind an HPA, with a PodDisruptionBudget that keeps at least 2 running. Two environment variables let you break it on purpose: `ERROR_RATE` (0.0 to 1.0) returns random errors and `LATENCY_MS` adds delay. Degraded mode turns both off. It starts from `DEGRADED_MODE` and then follows the SSM parameter, which the pods read through an IRSA role that can only read that parameter.

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
│   ├── eks.tf                # EKS 1.36, "system" and "apps" managed node groups on AL2023, metrics-server
│   ├── irsa.tf               # IAM roles for the ALB controller, ExternalDNS and cert-manager
│   ├── webhook_api.tf        # REST API: POST /webhook, schema validation, API keys, throttling
│   ├── event_pipeline.tf     # both Lambdas, the runbook state machine, DynamoDB, SNS, the event bus
│   ├── event_routing.tf      # EventBridge rules and targets, incident recorder, archive, dead-letter queue, audit log
│   ├── api/                  # JSON schema for the webhook payload
│   ├── stepfunctions/        # runbook and incident recorder definitions
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
bash scripts/deploy.sh          # Alertmanager's API key, kube-prometheus-stack, the alert rules, the checkout app and the remediation RBAC
```

`eks_version` defaults to `1.36`, and both node groups run Amazon Linux 2023. A cluster created from an older version of this repo has to be upgraded one minor version per apply, and changing the AMI type replaces its node groups. The first `deploy.sh` onto an existing checkout release drops `replicas` from the Deployment in favour of the HPA, so it briefly runs 1 pod until the HPA brings it back to 3.

Before `deploy.sh`, set three values in `helm/checkout/values.yaml`:

- `image.repository`. The chart has a placeholder, and nothing in this repo builds or pushes the image, so build `apps/checkout` and push it to a registry the cluster can pull from.
- `ingress.host`
- `ingress.certificateArn`, an ACM certificate for that host

The ingress creates an internet-facing HTTPS load balancer through the AWS Load Balancer Controller. Set `ingress.enabled=false` if you don't want one.

`deploy.sh` also connects Alertmanager to the webhook: it stores `terraform output alertmanager_api_key` in the `alertmanager-webhook-key` secret and fills the URL from `terraform output alert_webhook_url` into `helm/observability/alertmanager-values.yaml`. It passes the checkout service account's IRSA role (`terraform output checkout_role_arn`) and the region to the checkout chart. Run it again after a `terraform apply` that changes any of these.

To send alerts from Datadog, see [docs/API.md](docs/API.md#datadog-webhook-format). Its key is `terraform output -raw datadog_api_key`.

To tear it down, uninstall the Helm releases first. The load balancer the controller created isn't in Terraform state, and it blocks the VPC from being deleted while it exists. `destroy.sh` turns off the incidents table's deletion protection before it runs `terraform destroy`.

```bash
helm uninstall checkout -n apps
helm uninstall mon -n monitoring
bash scripts/destroy.sh         # deletion protection off, then terraform destroy
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
aws logs tail /aws/events/event-driven-sre --follow                 # every event on the bus
aws dynamodb scan --table-name event-driven-sre-incidents --max-items 5
aws stepfunctions list-executions --max-results 5 \
  --state-machine-arn "$(terraform -chdir=terraform output -raw step_functions_arn)"
aws logs tail /aws/vendedlogs/states/event-driven-sre-runbook --follow  # each runbook step
aws logs tail /aws/lambda/event-driven-sre-runbook-action --follow
```

A few minutes after the runbook acts, the incident should read `resolved` with `remediation_result` `auto_resolved`. Escalated incidents, newest first:

```bash
aws dynamodb query --table-name event-driven-sre-incidents --index-name by-status \
  --key-condition-expression "#s = :s" --no-scan-index-forward \
  --expression-attribute-names '{"#s": "status"}' \
  --expression-attribute-values '{":s": {"S": "escalated"}}'
```

Nothing should be in the dead-letter queue:

```bash
aws sqs get-queue-attributes --attribute-names ApproximateNumberOfMessages \
  --queue-url "$(terraform -chdir=terraform output -raw events_dlq_url)"
```

To replay the last hour of events through the rules:

```bash
aws events start-replay --replay-name "replay-$(date +%s)" \
  --event-source-arn "$(terraform -chdir=terraform output -raw events_archive_arn)" \
  --destination "Arn=$(aws events describe-event-bus --name event-driven-sre-bus --query Arn --output text)" \
  --event-start-time "$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --event-end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

The names above assume the default `name` variable, `event-driven-sre`.

## Tests

```bash
# checkout app
cd apps/checkout && pip install -r requirements-dev.txt && pytest tests/ --cov

# Lambdas, with AWS calls mocked by moto
cd terraform/lambda/alert_ingest && pip install -r requirements-dev.txt && AWS_DEFAULT_REGION=us-east-1 pytest tests/ --cov
cd terraform/lambda/runbook_action && pip install -r requirements-dev.txt && AWS_DEFAULT_REGION=us-east-1 pytest tests/ --cov

# Both state machines, run through a local JSONata simulator with the Lambdas on either side
cd terraform/stepfunctions && pip install -r tests/requirements.txt && pytest tests/
```

`tests/integration/` runs the alert path end to end against LocalStack (`setup.sh`, then pytest, then `cleanup.sh`). It isn't part of CI.

CI (`.github/workflows/integration-tests.yml`) runs Checkov on the Terraform, Trivy on the container image, the app, Lambda and state machine tests, and `terraform fmt`, `validate` and `tflint`.

## Known gaps

- **Remediation isn't reverted automatically.** After an incident resolves, degraded mode stays on, the HPA minimum stays raised, and a drained node stays cordoned. Put them back by hand (re-running `deploy.sh` also resets the HPA minimum, since `helm upgrade` applies the chart's 3):

  ```bash
  aws ssm put-parameter --name /checkout/degraded_mode --value false --overwrite
  kubectl patch hpa checkout -n apps --type merge -p '{"spec":{"minReplicas":3}}'
  kubectl uncordon NODE_NAME
  ```

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
