# terraform-aws-event-driven-sre

A Terraform module that adds automated incident response to an EKS cluster. Alertmanager or Datadog alerts are sent to an endpoint on AWS, recorded, and handed to a Step Functions runbook that changes the deployment in response: it switches the service to degraded mode, restarts it, or scales it up, and drains nodes under pressure. The runbook then checks that the alert clears and hands the incident to a person if it doesn't.

The module brings the pipeline (API Gateway, two Lambdas, EventBridge, Step Functions, DynamoDB, SNS, KMS). You bring the cluster. See [Known gaps](#known-gaps) for what's still missing.

## How it works

1. Alertmanager posts firing and resolved alerts to `POST /webhook` on an API Gateway REST API, with an `X-API-Key` header. API Gateway checks the body against a JSON schema and rate-limits requests (1000 per second, bursts of 2000). Datadog monitors can send to the same endpoint; see [docs/API.md](docs/API.md).
2. The `alert-ingest` Lambda validates the payload (anything invalid gets a `400` with an error code), normalizes Alertmanager and Datadog alerts into one format, and gives each alert a stable incident id, so a firing alert and its resolution belong to the same incident. It puts each alert on the EventBridge bus as an `incident.created` or `incident.resolved` event and answers `202 Accepted`.
3. EventBridge rules route the events:

   | Event | Source | Goes to |
   | --- | --- | --- |
   | `incident.created` | `sre.alertmanager`, `sre.datadog` | incident recorder (DynamoDB), SNS, runbook state machine |
   | `incident.resolved` | `sre.alertmanager`, `sre.datadog` | incident recorder (DynamoDB), SNS |
   | `action.completed` | `sre.runbook` | incident recorder (DynamoDB) |
   | `incident.escalated`, `incident.auto_resolved` | `sre.runbook` | SNS |
   | any `sre.*` event | | CloudWatch Logs |

   EventBridge can't write to DynamoDB, so the incident recorder, a small Express state machine, does those writes. With `chat_notifications` on, the four SNS rules also send to a second topic for Slack or Teams (see [Notifications](#notifications)).
4. The runbook state machine only acts on firing `critical` alerts (`auto_remediate_severities`); the rest just notify. It claims the incident in DynamoDB first. Only one run can claim it, so an incident is remediated once even if the alert is re-sent or EventBridge delivers it twice.
5. It picks an action from the alert name (`runbook_routes`) and invokes the `runbook-action` Lambda, retrying up to three times with backoff. The Lambda runs in your VPC and applies the action through the Kubernetes API, authenticating with an EKS token signed by its own IAM role. The result goes out as an `action.completed` event.
6. It waits a minute, then checks DynamoDB every 30 seconds for the resolved alert. If the alert resolves within 10 minutes of the action, it records the MTTR and publishes `incident.auto_resolved`. If it doesn't, or the action failed, or there's no runbook for the alert, it marks the incident `escalated` and publishes `incident.escalated`. Both go to SNS.

With the default `runbook_routes`:

| Alert | Action |
| --- | --- |
| `CheckoutHighErrorRate`, `CheckoutHighLatencyP95`, `CheckoutSLOBurnFast` | `degrade_or_scale`: turn on degraded mode by setting the SSM parameter `/checkout/degraded_mode` to `true` (the demo app reads it within 15 seconds, no restart), and raise the HPA's minimum to 4 replicas. |
| `CheckoutDown` | `restart`: restart the deployment. |
| `NodeUnderPressure` | `drain_node`: cordon the node and evict its pods, respecting PodDisruptionBudgets. Only nodes labelled `nodegroup=apps`, and only if another schedulable one is left. |
| Any other critical alert | Escalate. |

An alert can override this with a `runbook_action` annotation (`degrade`, `restart`, `scale`, `degrade_or_scale` or `drain_node`, or `notify_only` to skip the runbook), and set the replica count with `desired_replicas`. The target is the alert's `namespace` and `deployment` labels (or Datadog's `kube_namespace` and `kube_deployment` tags), falling back to its `service` label and then `default_deployment` in the first of `remediation_namespaces`, and for a drain its `node` label (or Datadog's `host` tag). The Lambda refuses namespaces outside `remediation_namespaces` and more than `remediation_max_replicas` replicas.

None of this is undone automatically when the incident resolves; see [Known gaps](#known-gaps).

Each incident's DynamoDB record goes from `open` to `remediating`, then to `resolved` or `escalated`, with every action and its result in `actions_taken`. Every EventBridge target retries for up to an hour, then sends the event to an SQS dead-letter queue. Every event on the bus is also written to the `/aws/events/<name>` log group and to an archive (kept 7 days) that can be replayed safely. More detail is in [docs/architecture/README.md](docs/architecture/README.md).

## Usage

```hcl
module "sre" {
  source  = "amrutp24/event-driven-sre/aws"
  version = "~> 1.0"

  cluster_name              = "my-cluster"
  vpc_id                    = "vpc-0123456789abcdef0"
  subnet_ids                = ["subnet-0123456789abcdef0", "subnet-0fedcba9876543210"]
  cluster_security_group_id = "sg-0123456789abcdef0"
}
```

`subnet_ids` are private subnets that can reach the cluster's API endpoint and AWS APIs (through a NAT gateway or VPC endpoints). The module adds a rule to `cluster_security_group_id` that lets the Lambda in on 443.

The cluster needs access entries enabled (authentication mode `API` or `API_AND_CONFIG_MAP`). The module gives the Lambda's role an access entry in the `sre-remediation` group (`kubernetes_group`) with no access policy, so the one manual step is the RBAC that says what that group may do:

```bash
kubectl apply -f k8s/rbac/remediation-role.yaml
```

The manifest's Role is for the `apps` namespace; add a copy for each other namespace in `remediation_namespaces`.

Point Alertmanager's webhook receiver at `alert_webhook_url` and send `alertmanager_api_key` in the `X-API-Key` header. For Datadog, see [docs/API.md](docs/API.md#datadog-webhook-format).

Neither Lambda has reserved concurrency by default (`-1`). New accounts often have a concurrency limit of 50, and AWS keeps at least 50 unreserved, so any reservation fails there. If your account has room, `runbook_action_reserved_concurrency = 10` caps how many remediations hit the cluster at once.

### Notifications

```hcl
module "sre" {
  # ...
  notification_emails = ["oncall@example.com"]
  chat_notifications  = true
}
```

- `notification_emails` subscribes each address to the alerts topic (`sns_topic_arn`). SNS sends each one a confirmation email, and nothing arrives until its link is clicked.
- `chat_notifications` adds a second topic, `chat_topic_arn`, encrypted with the same KMS key, with the same four events in the custom notification format of Amazon Q Developer in chat applications (AWS Chatbot). Subscribe a Slack or Teams channel configuration to it. Titles look like `critical: CheckoutHighErrorRate is firing on checkout`, `Escalated: CheckoutHighErrorRate on checkout needs a person`, `Resolved: ...` and `Auto-resolved: ...`, with the incident ID in the text. The module doesn't create the channel configuration, because the Chatbot API isn't in every region (not in `us-east-1`); [examples/complete](examples/complete/README.md#slack-and-email) does it for Slack with a provider in `us-east-2`.
- PagerDuty isn't built in. A PagerDuty SNS integration would subscribe to `sns_topic_arn`.

## Examples

- [examples/complete](examples/complete): the full demo. A VPC with flow logs, EKS 1.36, IRSA roles, ECR, CloudTrail, optional VPC endpoints, one KMS key passed to the module, optional email and Slack notifications, and the `checkout` demo app with Prometheus alerting on top. Deploy it with the scripts, in order:

  ```bash
  bash scripts/bootstrap.sh       # terraform apply in examples/complete, configure kubectl
  bash scripts/install-addons.sh  # AWS Load Balancer Controller, ExternalDNS (only with route53_zone_arns), cert-manager
  bash scripts/build-image.sh     # build apps/checkout and push it to ECR, tagged with the short git SHA
  bash scripts/deploy.sh          # Alertmanager's API key, kube-prometheus-stack, the alert rules, the checkout app and the remediation RBAC
  bash scripts/destroy.sh         # after uninstalling the Helm releases; protections off, then terraform destroy
  ```

  Running continuously in `us-east-1` it costs roughly $460 a month, nearly all of it the cluster, the nodes and the NAT gateways. The breakdown is in [docs/architecture/README.md](docs/architecture/README.md#monthly-cost-estimate). Tear it down when you're done. [docs/DEMO.md](docs/DEMO.md) walks through breaking the app and watching it get fixed.
- [examples/existing-cluster](examples/existing-cluster): the minimal call, for a cluster you already have. On its own the module costs about $14 a month at 1000 alerts a day, plus $1 for its KMS key.

## What's in the repo

```
.
├── main.tf, versions.tf      # the module: region, account and partition lookups, provider requirements
├── variables.tf, outputs.tf
├── webhook_api.tf            # REST API: POST /webhook, schema validation, API keys, throttling, private option
├── event_pipeline.tf         # both Lambdas, the runbook state machine, DynamoDB, SNS, the event bus, the EKS access entry
├── event_routing.tf          # EventBridge rules and targets, incident recorder, archive, dead-letter queue, audit log
├── kms.tf                    # the module's KMS key (create_kms_key)
├── notifications.tf          # email subscriptions and the optional chat topic
├── api/                      # JSON schema for the webhook payload
├── stepfunctions/            # runbook and incident recorder definitions, with tests
├── lambda/                   # alert_ingest and runbook_action, each with unit tests; Terraform zips them
├── k8s/rbac/                 # the remediation Role and ClusterRole for the sre-remediation group
├── examples/
│   ├── complete/             # VPC, EKS, IRSA, ECR, CloudTrail, VPC endpoints, KMS key, Slack, and the module
│   └── existing-cluster/     # the module on a cluster you already have
├── apps/checkout/            # demo Flask service, Dockerfile, unit tests
├── helm/                     # checkout chart, kube-prometheus-stack values and alert rules, optional Datadog values
├── scripts/                  # bootstrap.sh, install-addons.sh, build-image.sh, deploy.sh, destroy.sh (examples/complete)
├── tests/integration/        # end-to-end tests against LocalStack
└── docs/                     # architecture, API, security, demo walkthrough
```

## Requirements

| Name | Version |
| --- | --- |
| terraform | >= 1.5.0 |
| aws | >= 5.95, < 7.0 |
| archive | >= 2.4 |

The module has no provider blocks. It needs an EKS cluster with access entries enabled, and `kubectl` to apply the RBAC manifest.

<!-- BEGIN_TF_TABLES -->
## Inputs

| Name | Description | Type | Default | Required |
| --- | --- | --- | --- | :---: |
| `name` | Prefix for every resource name | `string` | `"event-driven-sre"` | no |
| `tags` | Tags for every resource that takes them | `map(string)` | `{}` | no |
| `cluster_name` | EKS cluster that runbook_action remediates | `string` | n/a | yes |
| `vpc_id` | VPC runbook_action runs in | `string` | n/a | yes |
| `subnet_ids` | Private subnets for runbook_action, and for the execute-api endpoint when webhook_private is true | `list(string)` | n/a | yes |
| `cluster_security_group_id` | The cluster's security group. The module adds an ingress rule for 443 from runbook_action's security group. | `string` | n/a | yes |
| `create_access_entry` | Create an EKS access entry for runbook_action's role in kubernetes_group. The cluster needs access entries enabled (authentication mode API or API_AND_CONFIG_MAP). | `bool` | `true` | no |
| `kubernetes_group` | Kubernetes group runbook_action's role is mapped to; k8s/rbac/remediation-role.yaml binds it | `string` | `"sre-remediation"` | no |
| `remediation_namespaces` | Namespaces runbook_action may act in. The first is the runbook's target when an alert doesn't name one. | `list(string)` | `["apps"]` | no |
| `default_deployment` | Deployment the runbook targets when an alert doesn't name one | `string` | `"checkout"` | no |
| `runbook_routes` | Which action the runbook takes for each alertname | `map(object({action = string}))` | `{ CheckoutHighErrorRate = { action = "degrade_or_scale" }, CheckoutHighLatencyP95 = { action = "degrade_or_scale" }, CheckoutSLOBurnFast = { action = "degrade_or_scale" }, CheckoutDown = { action = "restart" }, NodeUnderPressure = { action = "drain_node" } }` | no |
| `degraded_mode_deployment` | The only deployment degrade works for; it reads the degraded-mode parameter | `string` | `"checkout"` | no |
| `degraded_mode_parameter_name` | SSM parameter degrade sets to "true" | `string` | `"/checkout/degraded_mode"` | no |
| `create_degraded_mode_parameter` | Create the degraded-mode parameter with the value "false". Turn it off if it already exists. | `bool` | `true` | no |
| `drainable_node_label` | drain_node only drains nodes with this label, as key=value | `string` | `"nodegroup=apps"` | no |
| `drain_min_schedulable_nodes` | drain_node refuses to drain a node unless at least this many other drainable nodes are Ready and schedulable | `number` | `1` | no |
| `remediation_max_replicas` | Highest replica count runbook_action will scale a deployment to | `number` | `10` | no |
| `runbook_action_reserved_concurrency` | Concurrent executions reserved for runbook_action, which caps how many remediations hit the cluster at once (10 is a good cap). -1 leaves it unreserved, the default, because AWS keeps at least 50 to 100 executions unreserved per account and new accounts often have a limit of 50. | `number` | `-1` | no |
| `alert_ingest_reserved_concurrency` | Concurrent executions reserved for alert_ingest. -1 leaves it unreserved, the default, because AWS keeps at least 50 to 100 executions unreserved per account and new accounts often have a limit of 50. | `number` | `-1` | no |
| `auto_remediate_severities` | Severities the runbook acts on. Others only notify. | `list(string)` | `["critical"]` | no |
| `stabilization_seconds` | How long the runbook waits after an action before it checks whether the alert resolved | `number` | `60` | no |
| `verify_poll_seconds` | How often the runbook checks for the resolved alert after the stabilization wait | `number` | `30` | no |
| `verify_timeout_seconds` | How long after the action the alert has to resolve before the incident is escalated. Alertmanager only sends the resolved notification at its group_interval, so leave room for that. | `number` | `600` | no |
| `incident_retention_days` | Days after an incident's last activity before DynamoDB deletes it | `number` | `90` | no |
| `incidents_deletion_protection` | Deletion protection on the incidents table. Set it to false before destroying. | `bool` | `true` | no |
| `log_retention_days` | Days the module's log groups keep logs | `number` | `30` | no |
| `create_kms_key` | Create a KMS key for the incidents table, SNS topic, DLQ and log groups. Set it to false and pass kms_key_arn to use your own. | `bool` | `true` | no |
| `kms_key_arn` | Existing KMS key to use when create_kms_key is false. Its policy has to allow what the module's own key policy allows (see kms.tf). | `string` | `null` | no |
| `webhook_require_api_key` | Require an X-API-Key header on POST /webhook. Creates one key for Alertmanager and one for Datadog. | `bool` | `true` | no |
| `webhook_rate_limit` | Steady-state requests per second allowed on the webhook | `number` | `1000` | no |
| `webhook_burst_limit` | Burst of requests allowed on the webhook | `number` | `2000` | no |
| `webhook_private` | Make the webhook a private API, reachable only through an execute-api VPC endpoint the module creates in vpc_id and subnet_ids. Datadog can't reach a private API, so only use this when Alertmanager is the only sender. | `bool` | `false` | no |
| `notification_emails` | Email addresses subscribed to the alerts topic. Each one gets a confirmation email from SNS to accept first. | `list(string)` | `[]` | no |
| `chat_notifications` | Also publish incidents to a second topic in the format Amazon Q Developer in chat applications (AWS Chatbot) renders, for a Slack or Teams channel to subscribe to. See examples/complete/chatbot.tf. | `bool` | `false` | no |

## Outputs

| Name | Description |
| --- | --- |
| `alert_webhook_url` | POST alerts here |
| `alertmanager_api_key` | X-API-Key for Alertmanager; empty when webhook_require_api_key is false (sensitive) |
| `datadog_api_key` | X-API-Key for the Datadog webhook; empty when webhook_require_api_key is false (sensitive) |
| `event_bus_name` | Custom EventBridge bus every sre.* event goes through |
| `event_bus_arn` | ARN of the event bus, for rules or targets of your own |
| `runbook_state_machine_arn` | Runbook state machine, started by the incident.created rule |
| `incident_recorder_arn` | Express state machine that writes incident events to DynamoDB |
| `incidents_table_name` | DynamoDB table with one item per incident; query the by-status index for open or escalated ones |
| `incidents_table_arn` | ARN of the incidents table |
| `sns_topic_arn` | Incident notifications; subscribe on-call here |
| `events_dlq_url` | Events EventBridge couldn't deliver after retries |
| `events_log_group` | Every event on the bus |
| `events_archive_arn` | Replay source; events are kept 7 days |
| `runbook_log_group` | Runbook execution logs, including state input and output |
| `incident_recorder_log_group` | Incident recorder failures. Express workflows have no execution history, so this is where they show up. |
| `kms_key_arn` | Key for the incidents table, SNS topic, DLQ and log groups: the module's own, or kms_key_arn |
| `runbook_action_function_arn` | Lambda that carries out remediation actions against the cluster |
| `runbook_action_role_arn` | IAM role runbook_action calls the Kubernetes API as |
| `runbook_action_security_group_id` | Security group of runbook_action; the module already allows it to the cluster API on 443 |
| `kubernetes_group` | Group runbook_action's role is in; bind it with k8s/rbac/remediation-role.yaml |
| `degraded_mode_parameter_name` | SSM parameter the degrade action sets to true; the app reads it |
| `degraded_mode_parameter_arn` | Grant the degraded-mode deployment ssm:GetParameter on this |
| `chat_topic_arn` | Topic for a Slack or Teams channel configuration to subscribe to; empty when chat_notifications is false |
<!-- END_TF_TABLES -->

## Tests

```bash
# The module, with terraform test against a mocked AWS provider (Terraform 1.7+)
terraform init -backend=false && terraform test

# Lambdas, with AWS calls mocked by moto
cd lambda/alert_ingest && pip install -r requirements-dev.txt && AWS_DEFAULT_REGION=us-east-1 pytest tests/ --cov
cd lambda/runbook_action && pip install -r requirements-dev.txt && AWS_DEFAULT_REGION=us-east-1 pytest tests/ --cov

# Both state machines, run through a local JSONata simulator with the Lambdas on either side
cd stepfunctions && pip install -r tests/requirements.txt && pytest tests/

# The demo app
cd apps/checkout && pip install -r requirements-dev.txt && pytest tests/ --cov
```

`tests/integration/` runs the alert path end to end against LocalStack (`setup.sh`, then pytest, then `cleanup.sh`). It isn't part of CI.

CI (`.github/workflows/integration-tests.yml`) runs Checkov on the module and on `examples/complete` (settings and skipped checks, with reasons, in `.checkov.yaml`), Trivy on the container image, the module tests, the app, Lambda and state machine tests, `terraform fmt`, `terraform validate` on the module and both examples, and `tflint` on the module and `examples/complete`.

## Known gaps

- **The complete example's EKS public endpoint is open to the internet by default.** `deploy.sh` reaches the cluster through it from your machine. Set `eks_public_access_cidrs` in `examples/complete` to your own address.
- **Remediation isn't reverted automatically.** After an incident resolves, degraded mode stays on, the HPA minimum stays raised, and a drained node stays cordoned. Put them back by hand (in the complete example, re-running `deploy.sh` also resets the HPA minimum, since `helm upgrade` applies the chart's 3):

  ```bash
  aws ssm put-parameter --name /checkout/degraded_mode --value false --overwrite
  kubectl patch hpa checkout -n apps --type merge -p '{"spec":{"minReplicas":3}}'
  kubectl uncordon NODE_NAME
  ```
- **The webhook's TLS version isn't pinned.** It's HTTPS only, with API Gateway's default TLS policy. Setting a TLS 1.2 minimum on the REST API needs AWS provider 6, and the module still supports provider 5 (the complete example's EKS module, ~> 20, keeps it below 6).
- **`degrade` only works for one deployment.** It flips the SSM parameter `degraded_mode_parameter_name`, which only `degraded_mode_deployment` reads.
- **A private webhook shuts out Datadog.** With `webhook_private = true` only callers inside the VPC can reach it, which leaves Alertmanager as the only sender.

## License

MIT. See [LICENSE](LICENSE).
