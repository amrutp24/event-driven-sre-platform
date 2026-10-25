# terraform-aws-event-driven-sre

A Terraform module that adds automated incident response to an EKS cluster. Alertmanager or Datadog alerts are sent to an endpoint on AWS, recorded, and handed to a Step Functions runbook that changes the deployment in response: it switches the service to degraded mode, restarts it, or scales it up, and drains nodes under pressure. The runbook then checks that the alert clears and hands the incident to a person if it doesn't.

The module brings the pipeline (API Gateway, two Lambdas, EventBridge, Step Functions, DynamoDB, SNS, KMS). You bring the cluster. See [Known gaps](#known-gaps) for what's still missing.

![Architecture: alerts from the cluster go through the webhook and the event bus to the incident record, the runbook and notifications; the runbook acts back on the cluster](https://raw.githubusercontent.com/amrutp24/terraform-aws-event-driven-sre/main/docs/architecture/architecture.png)

The diagram is generated from [docs/architecture/diagram.py](docs/architecture/diagram.py) with [Diagrams](https://diagrams.mingrammer.com/).

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

   EventBridge can't write to DynamoDB, so the incident recorder, a small Express state machine, does those writes. With `create_chat_topic` on, the four SNS rules also send to a second topic for Slack or Teams (see [Notifications](#notifications)).
4. The runbook state machine only acts on firing `critical` alerts (`auto_remediate_severities`); the rest just notify. It claims the incident in DynamoDB first. Only one run can claim it, so an incident is remediated once even if the alert is re-sent or EventBridge delivers it twice.
5. It picks an action from the alert name (`runbook_routes`) and invokes the `runbook-action` Lambda, retrying up to three times with backoff. The Lambda runs in your VPC and applies the action through the Kubernetes API, authenticating with an EKS token signed by its own IAM role. The result goes out as an `action.completed` event.
6. It waits a minute, then checks DynamoDB every 30 seconds for the resolved alert. If the alert resolves within 10 minutes of the action, it records the MTTR and publishes `incident.auto_resolved`. If it doesn't, or the action failed, or there's no runbook for the alert, it marks the incident `escalated` and publishes `incident.escalated`. Both go to SNS.

The actions:

| Action | What the Lambda does |
| --- | --- |
| `restart` | Restarts the deployment, like `kubectl rollout restart`. |
| `scale` | Raises the HPA's minimum to the alert's `desired_replicas` or `default_scale_replicas` (4), or scales the deployment when it has no HPA. Never above `remediation_max_replicas`. |
| `degrade` | Sets the SSM parameter `degraded_mode_parameter_name` to `true`, for `degraded_mode_deployment` to read (no restart). Off unless `degraded_mode_deployment` is set. |
| `degrade_or_scale` | `degrade`, then `scale`. |
| `drain_node` | Cordons the node and evicts its pods, respecting PodDisruptionBudgets. Only nodes with `drainable_node_label`, and only if `drain_min_schedulable_nodes` other ones are Ready. Off unless `drainable_node_label` is set. |

`runbook_routes` is empty by default, so nothing is remediated until you route alerts: a critical alert with no route is escalated. An action that isn't configured is rejected and escalated too. [examples/complete](examples/complete) routes the demo's alerts:

| Alert | Action |
| --- | --- |
| `CheckoutHighErrorRate`, `CheckoutHighLatencyP95`, `CheckoutSLOBurnFast` | `degrade_or_scale`: set `/checkout/degraded_mode` to `true` (the demo app reads it within 15 seconds), and raise the HPA's minimum to 4 replicas. |
| `CheckoutDown` | `restart` |
| `NodeUnderPressure` | `drain_node`, for nodes labelled `nodegroup=apps` |
| Any other critical alert | Escalate. |

An alert can override this with a `runbook_action` annotation (`degrade`, `restart`, `scale`, `degrade_or_scale` or `drain_node`, or `notify_only` to skip the runbook), and set the replica count with `desired_replicas`. The target is the alert's `namespace` and `deployment` labels (or Datadog's `kube_namespace` and `kube_deployment` tags), falling back to its `service` label and then `default_deployment` in the first of `remediation_namespaces` (with no `default_deployment`, an alert that names neither is escalated), and for a drain its `node` label (or Datadog's `host` tag). The Lambda refuses namespaces outside `remediation_namespaces` and more than `remediation_max_replicas` replicas.

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

  remediation_namespaces = ["shop"]
  runbook_routes = {
    CartDown         = { action = "restart" }
    CartHighLatency  = { action = "scale" }
    NodeDiskPressure = { action = "drain_node" }
  }

  # Optional: turn on drain_node and degrade
  drainable_node_label     = "nodegroup=apps"
  degraded_mode_deployment = "cart"
}
```

`subnet_ids` are private subnets that can reach the cluster's API endpoint and AWS APIs (through a NAT gateway or VPC endpoints). The module adds a rule to `cluster_security_group_id` that lets the Lambda in on 443.

The cluster needs access entries enabled (authentication mode `API` or `API_AND_CONFIG_MAP`). The module gives the Lambda's role an access entry in the `sre-remediation` group (`kubernetes_group`) with no access policy, so the one manual step is the RBAC that says what that group may do. The module renders it as the `rbac_manifest` output: a Role and RoleBinding in each of `remediation_namespaces`, and a ClusterRole for nodes and evictions only when `drainable_node_label` is set.

```bash
terraform output -raw rbac_manifest | kubectl apply -f -
```

From a root module that calls this one, add an output that passes `module.sre.rbac_manifest` through, as both examples do.

With `degraded_mode_deployment` set, the module creates the SSM parameter `degraded_mode_parameter_name` (default `/<name>/degraded-mode`) with the value `false`. Your app reads it and serves a degraded response while it's `true`; give the app's role `ssm:GetParameter` on `degraded_mode_parameter_arn`.

Point Alertmanager's webhook receiver at `alert_webhook_url` and send `alertmanager_api_key` in the `X-API-Key` header. For Datadog, see [docs/API.md](docs/API.md#datadog-webhook-format).

Neither Lambda has reserved concurrency by default (`-1`). New accounts often have a concurrency limit of 50, and AWS keeps at least 50 unreserved, so any reservation fails there. If your account has room, `runbook_action_reserved_concurrency = 10` caps how many remediations hit the cluster at once.

### Notifications

```hcl
module "sre" {
  # ...
  notification_emails = ["oncall@example.com"]
  create_chat_topic   = true
}
```

- `notification_emails` subscribes each address to the alerts topic (`sns_topic_arn`). SNS sends each one a confirmation email, and nothing arrives until its link is clicked.
- `create_chat_topic` adds a second topic, `chat_topic_arn`, encrypted with the same KMS key, with the same four events in the custom notification format of Amazon Q Developer in chat applications (AWS Chatbot). Subscribe a Slack or Teams channel configuration to it. Titles look like `critical: CheckoutHighErrorRate is firing on checkout`, `Escalated: CheckoutHighErrorRate on checkout needs a person`, `Resolved: ...` and `Auto-resolved: ...`, with the incident ID in the text. The module doesn't create the channel configuration, because the Chatbot API isn't in every region (not in `us-east-1`); [examples/complete](examples/complete/README.md#slack-and-email) does it for Slack with a provider in `us-east-2`.
- PagerDuty isn't built in. A PagerDuty SNS integration would subscribe to `sns_topic_arn`.

## Examples

- [examples/complete](examples/complete): the full demo. A VPC with flow logs, EKS 1.36, IRSA roles, ECR, CloudTrail, optional VPC endpoints, one KMS key passed to the module, optional email and Slack notifications, and the `checkout` demo app with Prometheus alerting on top. Deploy it with its scripts, in order (they run from `examples/complete` wherever they're called from):

  ```bash
  cd examples/complete
  bash scripts/bootstrap.sh       # terraform init and apply, configure kubectl
  bash scripts/install-addons.sh  # AWS Load Balancer Controller, ExternalDNS (only with route53_zone_arns), cert-manager
  bash scripts/build-image.sh     # build apps/checkout and push it to ECR, tagged with the short git SHA
  bash scripts/deploy.sh          # Alertmanager's API key, kube-prometheus-stack, the alert rules, the checkout app and the remediation RBAC
  bash scripts/destroy.sh         # after uninstalling the Helm releases; protections off, then terraform destroy
  ```

  Running continuously in `us-east-1` it costs roughly $460 a month, nearly all of it the cluster, the nodes and the NAT gateways. The breakdown is in [its README](examples/complete/README.md#cost). Tear it down when you're done. [examples/complete/DEMO.md](examples/complete/DEMO.md) walks through breaking the app and watching it get fixed.
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
├── templates/                # RBAC manifest template behind the rbac_manifest output
├── api/                      # JSON schema for the webhook payload
├── stepfunctions/            # runbook and incident recorder definitions, with tests
├── lambda/                   # alert_ingest and runbook_action, each with unit tests; Terraform zips them
├── tests/                    # terraform test against a mocked AWS provider
├── examples/
│   ├── complete/             # VPC, EKS, IRSA, ECR, CloudTrail, VPC endpoints, KMS key, Slack, and the module
│   │   ├── apps/checkout/    # demo Flask service, Dockerfile, unit tests
│   │   ├── helm/             # checkout chart, kube-prometheus-stack values and alert rules, optional Datadog values
│   │   ├── scripts/          # bootstrap.sh, install-addons.sh, build-image.sh, deploy.sh, destroy.sh
│   │   └── DEMO.md           # break the app and watch it get fixed
│   └── existing-cluster/     # the module on a cluster you already have
├── docs/                     # architecture, API, security design
└── .github/                  # CI, release-please, Dependabot
```

## Prerequisites

The module has no provider blocks. It needs an EKS cluster with access entries enabled, and `kubectl` to apply the RBAC manifest.

<!-- The tables between these markers are generated by terraform-docs (settings in .terraform-docs.yml); don't edit them by hand -->
<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
| ---- | ------- |
| terraform | >= 1.5.0 |
| archive | >= 2.4 |
| aws | >= 5.95, < 7.0 |

## Providers

| Name | Version |
| ---- | ------- |
| archive | >= 2.4 |
| aws | >= 5.95, < 7.0 |

## Resources

| Name | Type |
| ---- | ---- |
| [aws_api_gateway_api_key.webhook](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/api_gateway_api_key) | resource |
| [aws_api_gateway_deployment.alerts_api](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/api_gateway_deployment) | resource |
| [aws_api_gateway_gateway_response.errors](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/api_gateway_gateway_response) | resource |
| [aws_api_gateway_integration.webhook_lambda](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/api_gateway_integration) | resource |
| [aws_api_gateway_method.webhook_post](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/api_gateway_method) | resource |
| [aws_api_gateway_method_settings.all](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/api_gateway_method_settings) | resource |
| [aws_api_gateway_model.alert_payload](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/api_gateway_model) | resource |
| [aws_api_gateway_request_validator.body](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/api_gateway_request_validator) | resource |
| [aws_api_gateway_resource.webhook](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/api_gateway_resource) | resource |
| [aws_api_gateway_rest_api.alerts_api](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/api_gateway_rest_api) | resource |
| [aws_api_gateway_rest_api_policy.private_only](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/api_gateway_rest_api_policy) | resource |
| [aws_api_gateway_stage.prod](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/api_gateway_stage) | resource |
| [aws_api_gateway_usage_plan.webhook](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/api_gateway_usage_plan) | resource |
| [aws_api_gateway_usage_plan_key.webhook](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/api_gateway_usage_plan_key) | resource |
| [aws_cloudwatch_event_archive.sre](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_archive) | resource |
| [aws_cloudwatch_event_bus.sre](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_bus) | resource |
| [aws_cloudwatch_event_rule.action_completed](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_rule) | resource |
| [aws_cloudwatch_event_rule.all_events](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_rule) | resource |
| [aws_cloudwatch_event_rule.incident_auto_resolved](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_rule) | resource |
| [aws_cloudwatch_event_rule.incident_created](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_rule) | resource |
| [aws_cloudwatch_event_rule.incident_escalated](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_rule) | resource |
| [aws_cloudwatch_event_rule.incident_resolved](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_rule) | resource |
| [aws_cloudwatch_event_target.action_to_recorder](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_target) | resource |
| [aws_cloudwatch_event_target.all_to_logs](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_target) | resource |
| [aws_cloudwatch_event_target.created_to_recorder](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_target) | resource |
| [aws_cloudwatch_event_target.created_to_runbook](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_target) | resource |
| [aws_cloudwatch_event_target.resolved_to_recorder](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_target) | resource |
| [aws_cloudwatch_event_target.runbook_outcome_to_sns](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_target) | resource |
| [aws_cloudwatch_event_target.to_chat](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_target) | resource |
| [aws_cloudwatch_event_target.to_sns](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_target) | resource |
| [aws_cloudwatch_log_group.alert_ingest](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_log_group) | resource |
| [aws_cloudwatch_log_group.events](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_log_group) | resource |
| [aws_cloudwatch_log_group.incident_recorder](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_log_group) | resource |
| [aws_cloudwatch_log_group.runbook](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_log_group) | resource |
| [aws_cloudwatch_log_group.runbook_action](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_log_group) | resource |
| [aws_cloudwatch_log_resource_policy.events](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_log_resource_policy) | resource |
| [aws_dynamodb_table.incidents](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/dynamodb_table) | resource |
| [aws_eks_access_entry.runbook_action](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/eks_access_entry) | resource |
| [aws_iam_role.events_invoke](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role.incident_recorder](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role.lambda_role](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role.runbook_lambda_role](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role.sfn_role](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role_policy.events_invoke](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_iam_role_policy.incident_recorder](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_iam_role_policy.lambda_policy](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_iam_role_policy.runbook_lambda_policy](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_iam_role_policy.sfn_invoke_lambda](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_iam_role_policy.sfn_policy](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_iam_role_policy_attachment.runbook_vpc_access](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy_attachment) | resource |
| [aws_kms_alias.platform](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/kms_alias) | resource |
| [aws_kms_key.platform](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/kms_key) | resource |
| [aws_lambda_function.alert_ingest](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lambda_function) | resource |
| [aws_lambda_function.runbook_action](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lambda_function) | resource |
| [aws_lambda_permission.apigw](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lambda_permission) | resource |
| [aws_security_group.runbook_lambda](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/security_group) | resource |
| [aws_security_group.webhook_endpoint](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/security_group) | resource |
| [aws_sfn_state_machine.incident_recorder](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sfn_state_machine) | resource |
| [aws_sfn_state_machine.runbook](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sfn_state_machine) | resource |
| [aws_sns_topic.alerts](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sns_topic) | resource |
| [aws_sns_topic.chat](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sns_topic) | resource |
| [aws_sns_topic_policy.alerts](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sns_topic_policy) | resource |
| [aws_sns_topic_policy.chat](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sns_topic_policy) | resource |
| [aws_sns_topic_subscription.email](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sns_topic_subscription) | resource |
| [aws_sqs_queue.events_dlq](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sqs_queue) | resource |
| [aws_sqs_queue_policy.events_dlq](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sqs_queue_policy) | resource |
| [aws_ssm_parameter.degraded_mode](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/ssm_parameter) | resource |
| [aws_vpc_endpoint.webhook](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/vpc_endpoint) | resource |
| [aws_vpc_security_group_ingress_rule.cluster_from_runbook_action](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/vpc_security_group_ingress_rule) | resource |

## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| cluster\_name | EKS cluster that runbook\_action remediates | `string` | n/a | yes |
| cluster\_security\_group\_id | The cluster's security group. The module adds an ingress rule for 443 from runbook\_action's security group. | `string` | n/a | yes |
| remediation\_namespaces | Namespaces runbook\_action may act in. The first is the runbook's target when an alert doesn't name one. | `list(string)` | n/a | yes |
| subnet\_ids | Private subnets for runbook\_action, and for the execute-api endpoint when webhook\_private is true | `list(string)` | n/a | yes |
| vpc\_id | VPC runbook\_action runs in | `string` | n/a | yes |
| alert\_ingest\_reserved\_concurrency | Concurrent executions reserved for alert\_ingest. -1 leaves it unreserved, the default, because AWS keeps at least 50 to 100 executions unreserved per account and new accounts often have a limit of 50. | `number` | `-1` | no |
| auto\_remediate\_severities | Severities the runbook acts on. Others only notify. | `list(string)` | <pre>[<br/>  "critical"<br/>]</pre> | no |
| create\_access\_entry | Create an EKS access entry for runbook\_action's role in kubernetes\_group. The cluster needs access entries enabled (authentication mode API or API\_AND\_CONFIG\_MAP). | `bool` | `true` | no |
| create\_chat\_topic | Also publish incidents to a second topic in the format Amazon Q Developer in chat applications (AWS Chatbot) renders, for a Slack or Teams channel to subscribe to. See examples/complete/chatbot.tf. | `bool` | `false` | no |
| create\_degraded\_mode\_parameter | Create the degraded-mode parameter with the value "false". Turn it off if it already exists. Only used with degraded\_mode\_deployment. | `bool` | `true` | no |
| create\_kms\_key | Create a KMS key for the incidents table, SNS topic, DLQ and log groups. Set it to false and pass kms\_key\_arn to use your own. | `bool` | `true` | no |
| default\_deployment | Deployment the runbook targets when an alert names neither a deployment nor a service. With null, such an alert is escalated. | `string` | `null` | no |
| default\_scale\_replicas | Replicas scale and degrade\_or\_scale go to when the alert has no desired\_replicas annotation. At most remediation\_max\_replicas. | `number` | `4` | no |
| degraded\_mode\_deployment | The only deployment degrade works for; it reads the degraded-mode parameter. With null, degraded mode is off: no parameter, no SSM permissions, and degrade and degrade\_or\_scale are rejected. | `string` | `null` | no |
| degraded\_mode\_parameter\_name | SSM parameter degrade sets to "true". Defaults to /NAME/degraded-mode, with NAME from var.name. Only used with degraded\_mode\_deployment. | `string` | `null` | no |
| drain\_min\_schedulable\_nodes | drain\_node refuses to drain a node unless at least this many other drainable nodes are Ready and schedulable | `number` | `1` | no |
| drainable\_node\_label | drain\_node only drains nodes with this label, as key=value. With null, drain\_node is off: it's rejected, and rbac\_manifest has no node permissions. | `string` | `null` | no |
| incident\_retention\_days | Days after an incident's last activity before DynamoDB deletes it | `number` | `90` | no |
| incidents\_deletion\_protection | Deletion protection on the incidents table. Set it to false before destroying. | `bool` | `true` | no |
| kms\_key\_arn | Existing KMS key to use when create\_kms\_key is false. Its policy has to allow what the module's own key policy allows (see kms.tf). | `string` | `null` | no |
| kubernetes\_group | Kubernetes group runbook\_action's role is mapped to. rbac\_manifest binds it, and names its Roles after it. | `string` | `"sre-remediation"` | no |
| log\_retention\_days | Days the module's log groups keep logs | `number` | `30` | no |
| name | Prefix for every resource name. Letters, digits and hyphens, at most 40 characters (the event archive is named after it with -archive added, and can't be longer than 48). | `string` | `"event-driven-sre"` | no |
| notification\_emails | Email addresses subscribed to the alerts topic. Each one gets a confirmation email from SNS to accept first. | `list(string)` | `[]` | no |
| remediation\_max\_replicas | Highest replica count runbook\_action will scale a deployment to | `number` | `10` | no |
| runbook\_action\_reserved\_concurrency | Concurrent executions reserved for runbook\_action, which caps how many remediations hit the cluster at once (10 is a good cap). -1 leaves it unreserved, the default, because AWS keeps at least 50 to 100 executions unreserved per account and new accounts often have a limit of 50. | `number` | `-1` | no |
| runbook\_routes | Which action the runbook takes for each alertname. An alert at an auto-remediated severity with no route (and no runbook\_action annotation) is escalated, so with the default nothing is remediated. | <pre>map(object({<br/>    action = string<br/>  }))</pre> | `{}` | no |
| stabilization\_seconds | How long the runbook waits after an action before it checks whether the alert resolved | `number` | `60` | no |
| tags | Tags for every resource that takes them | `map(string)` | `{}` | no |
| verify\_poll\_seconds | How often the runbook checks for the resolved alert after the stabilization wait | `number` | `30` | no |
| verify\_timeout\_seconds | How long after the action the alert has to resolve before the incident is escalated. Alertmanager only sends the resolved notification at its group\_interval, so leave room for that. | `number` | `600` | no |
| webhook\_burst\_limit | Burst of requests allowed on the webhook | `number` | `2000` | no |
| webhook\_private | Make the webhook a private API, reachable only through an execute-api VPC endpoint the module creates in vpc\_id and subnet\_ids. Datadog can't reach a private API, so only use this when Alertmanager is the only sender. | `bool` | `false` | no |
| webhook\_rate\_limit | Steady-state requests per second allowed on the webhook | `number` | `1000` | no |
| webhook\_require\_api\_key | Require an X-API-Key header on POST /webhook. Creates one key for Alertmanager and one for Datadog. | `bool` | `true` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| alert\_ingest\_function\_arn | Lambda behind the webhook that validates alerts and puts them on the bus |
| alert\_ingest\_function\_name | Name of the alert\_ingest Lambda |
| alert\_webhook\_url | POST alerts here |
| alertmanager\_api\_key | X-API-Key for Alertmanager; null when webhook\_require\_api\_key is false |
| alerts\_topic\_name | Name of the incident notifications topic |
| chat\_topic\_arn | Topic for a Slack or Teams channel configuration to subscribe to; null when create\_chat\_topic is false |
| datadog\_api\_key | X-API-Key for the Datadog webhook; null when webhook\_require\_api\_key is false |
| degraded\_mode\_parameter\_arn | Grant the degraded-mode deployment ssm:GetParameter on this. Null without degraded\_mode\_deployment. |
| degraded\_mode\_parameter\_name | SSM parameter the degrade action sets to true; the app reads it. Null without degraded\_mode\_deployment. |
| event\_bus\_arn | ARN of the event bus, for rules or targets of your own |
| event\_bus\_name | Custom EventBridge bus every sre.* event goes through |
| events\_archive\_arn | Replay source; events are kept 7 days |
| events\_dlq\_url | Events EventBridge couldn't deliver after retries |
| events\_log\_group | Every event on the bus |
| incident\_recorder\_arn | Express state machine that writes incident events to DynamoDB |
| incident\_recorder\_log\_group | Incident recorder failures. Express workflows have no execution history, so this is where they show up. |
| incidents\_table\_arn | ARN of the incidents table |
| incidents\_table\_name | DynamoDB table with one item per incident; query the by-status index for open or escalated ones |
| kms\_key\_arn | Key for the incidents table, SNS topic, DLQ and log groups: the module's own, or kms\_key\_arn |
| kubernetes\_group | Group runbook\_action's role is in; rbac\_manifest binds it |
| rbac\_manifest | Kubernetes RBAC for kubernetes\_group: a Role and RoleBinding in each of remediation\_namespaces, and a ClusterRole for nodes and evictions when drainable\_node\_label is set. Apply it with terraform output -raw rbac\_manifest \| kubectl apply -f - |
| rest\_api\_execution\_arn | Execution ARN of the webhook's REST API, for execute-api:Invoke permissions |
| rest\_api\_id | ID of the webhook's REST API |
| runbook\_action\_function\_arn | Lambda that carries out remediation actions against the cluster |
| runbook\_action\_function\_name | Name of the runbook\_action Lambda |
| runbook\_action\_role\_arn | IAM role runbook\_action calls the Kubernetes API as |
| runbook\_action\_security\_group\_id | Security group of runbook\_action; the module already allows it to the cluster API on 443 |
| runbook\_log\_group | Runbook execution logs, including state input and output |
| runbook\_state\_machine\_arn | Runbook state machine, started by the incident.created rule |
| sns\_topic\_arn | Incident notifications; subscribe on-call here |
<!-- END_TF_DOCS -->

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
cd examples/complete/apps/checkout && pip install -r requirements-dev.txt && pytest tests/ --cov
```

CI (`.github/workflows/ci.yml`) runs on every pull request and push to `main`:

- the module tests, and the Lambda, state machine and app tests
- `terraform fmt`, and `terraform validate` on the module and both examples with Terraform 1.5.7 (the oldest the module supports) and the latest 1.x
- tflint on the module and `examples/complete`
- Checkov on the module and on `examples/complete`. Settings and skipped checks, with reasons, are in `.checkov.yaml`. It reports findings without failing the build.
- Trivy on the repo (results in the Security tab), and on the checkout image built from `examples/complete/apps/checkout`, which fails on a critical vulnerability that has a fix
- a check that the tables in this README match what terraform-docs generates

`pre-commit run -a` runs the formatting, validation, lint and docs hooks locally; see [CONTRIBUTING.md](CONTRIBUTING.md). Releases are made by release-please from conventional commit titles, and the changes are in [CHANGELOG.md](CHANGELOG.md). To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Known gaps

- **The complete example's EKS public endpoint is open to the internet by default.** `deploy.sh` reaches the cluster through it from your machine. Set `eks_public_access_cidrs` in `examples/complete` to your own address.
- **Remediation isn't reverted automatically.** After an incident resolves, degraded mode stays on, the HPA minimum stays raised, and a drained node stays cordoned. Put them back by hand (in the complete example, re-running `deploy.sh` also resets the HPA minimum, since `helm upgrade` applies the chart's 3):

  ```bash
  aws ssm put-parameter --name "$(terraform output -raw degraded_mode_parameter_name)" --value false --overwrite
  kubectl patch hpa DEPLOYMENT -n NAMESPACE --type merge -p '{"spec":{"minReplicas":3}}'
  kubectl uncordon NODE_NAME
  ```
- **The webhook's TLS version isn't pinned.** It's HTTPS only, with API Gateway's default TLS policy. Setting a TLS 1.2 minimum on the REST API needs AWS provider 6, and the module still supports provider 5 (the complete example's EKS module, ~> 20, keeps it below 6).
- **`degrade` only works for one deployment.** It flips the SSM parameter `degraded_mode_parameter_name`, which only `degraded_mode_deployment` reads.
- **A private webhook shuts out Datadog.** With `webhook_private = true` only callers inside the VPC can reach it, which leaves Alertmanager as the only sender.

## License

MIT. See [LICENSE](LICENSE).
