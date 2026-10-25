# Complete

The full demo of the module: everything it needs around it, and a demo app to break and fix.

## What it creates

- A VPC across two AZs with a NAT gateway per AZ, flow logs, the S3 and DynamoDB gateway endpoints, and optional interface endpoints (`enable_vpc_endpoints`)
- An EKS 1.36 cluster with "system" and "apps" managed node groups on Amazon Linux 2023, and metrics-server (with its port, 10251, opened from the control plane to the nodes)
- IRSA roles for the AWS Load Balancer Controller, cert-manager, the EBS CSI driver and the checkout app (which can only read the degraded-mode parameter), and for ExternalDNS when `route53_zone_arns` is set
- An ECR repository for the checkout image
- A multi-region CloudTrail trail and its bucket (`enable_cloudtrail`)
- One KMS key for the module's data, the flow logs and the CloudTrail bucket, passed to the module with `create_kms_key = false`
- The module itself, wired to the VPC and the cluster
- Optionally, a Slack channel for incident messages (`chatbot.tf`, see [Slack and email](#slack-and-email))

State is local; there's no backend block.

## Prerequisites

- The AWS CLI, Terraform, kubectl, Helm, Docker and git
- In `helm/checkout/values.yaml`, `ingress.host` and `ingress.certificateArn` (an ACM certificate for that host), or `ingress.enabled=false` for no load balancer
- `eks_public_access_cidrs` set to your own address; the default is open to the internet
- `route53_zone_arns` if you want ExternalDNS to manage records. Without it there's no ExternalDNS role and `install-addons.sh` skips ExternalDNS.

## Deploy

From this directory, in this order (the scripts also work from anywhere else, since they change to this directory first):

```bash
bash scripts/bootstrap.sh       # terraform init and apply here, then configure kubectl
bash scripts/install-addons.sh  # AWS Load Balancer Controller, ExternalDNS (with route53_zone_arns), cert-manager
bash scripts/build-image.sh     # build apps/checkout and push it to ECR, tagged with the short git SHA
bash scripts/deploy.sh          # Alertmanager's API key, kube-prometheus-stack, the alert rules, the checkout app and the remediation RBAC
```

`install-addons.sh` waits for the load balancer controller to be ready before going on, since its webhook checks every new Service. `build-image.sh` refuses to build uncommitted changes in `apps/checkout` unless you set `IMAGE_TAG`; if you do, set the same `IMAGE_TAG` for `deploy.sh`. It builds without provenance and SBOM attestations, because Docker Desktop would push those under the tag first and the repository's immutable tags would then refuse the image. Re-run `deploy.sh` after an apply that changes the webhook URL, the API key or the checkout role.

Both Lambdas run without reserved concurrency by default (`alert_ingest_reserved_concurrency` and `runbook_action_reserved_concurrency` are -1). New accounts often have a concurrency limit of 50, and AWS keeps at least 50 unreserved, so any reservation fails there. If your account has room, set `runbook_action_reserved_concurrency` (10 is a good cap) to limit how many remediations hit the cluster at once.

[DEMO.md](DEMO.md) walks through breaking the app and watching the runbook fix it.

## Slack and email

Both are off until you set them, in `terraform.tfvars` or with `-var`:

```hcl
notification_emails = ["oncall@example.com"]
slack_workspace_id  = "T0123456789"
slack_channel_id    = "C0123456789"
```

Email: each address gets a confirmation email from SNS after the apply, and nothing is delivered until the link in it is clicked. Messages are one line each, like `[critical] checkout - CheckoutHighErrorRate is firing. ...`.

Slack goes through Amazon Q Developer in chat applications (AWS Chatbot). Before the apply:

1. In the AWS console, open Amazon Q Developer in chat applications, choose Configure new client, pick Slack, and allow the workspace.
2. Open the channel in Slack in a browser. The URL is `https://app.slack.com/client/T.../C...`: the `T...` part is `slack_workspace_id` and the `C...` part is `slack_channel_id`.

After the apply, run `/invite @Amazon Q` in the channel. Without that the configuration exists but nothing posts.

Setting both IDs turns on the module's `create_chat_topic`, and `chatbot.tf` subscribes the channel to `chat_topic_arn`. The Chatbot API isn't in `us-east-1`, so `chatbot.tf` uses a second provider in `chatbot_region` (default `us-east-2`). The channel's role has no permissions, and the channel has a `ReadOnlyAccess` guardrail. Messages look like:

- `critical: CheckoutHighErrorRate is firing on checkout`
- `Escalated: CheckoutHighErrorRate on checkout needs a person`
- `Resolved: CheckoutHighErrorRate on checkout`
- `Auto-resolved: CheckoutHighErrorRate on checkout`

with the summary or reason and the incident ID in the text.

PagerDuty isn't wired up. A PagerDuty SNS integration would subscribe to the `sns_topic_arn` output.

## Notes

- On Windows Git Bash, prefix `aws` commands that take a `/path` argument with `MSYS_NO_PATHCONV=1`, or Git Bash rewrites the path into a Windows one:

  ```bash
  MSYS_NO_PATHCONV=1 aws ssm get-parameter --name /checkout/degraded_mode
  ```

- If an apply fails after creating `alert_ingest` and leaves it tainted, untaint it before applying again:

  ```bash
  terraform untaint module.sre.aws_lambda_function.alert_ingest
  ```

  Otherwise Terraform replaces it, and because the API deployment uses `create_before_destroy`, it tries to create a second function with the same name, which fails.

## Destroy

Uninstall the Helm releases first. The load balancer the controller created isn't in Terraform state, and it blocks the VPC from being deleted while it exists.

```bash
helm uninstall checkout -n apps
helm uninstall mon -n monitoring
bash scripts/destroy.sh         # deletion protection and the trail bucket's protection off, then terraform destroy
```

## Cost

Roughly $460 a month running continuously, at `us-east-1` on-demand prices with the default variables, before free tiers. The cluster is nearly all of it; the alert pipeline is small even at 1000 alerts a day, and it plus the KMS key line is what the module costs on its own. Tear it down when you're done.

| Item | Monthly |
| --- | --- |
| EKS control plane ($0.10/hour) | $73 |
| 4 × m6i.large nodes, 2 per node group ($0.096/hour each) | $280 |
| EBS root volumes, 4 × 20 GB gp3 | $6 |
| 2 NAT gateways ($0.045/hour each, plus $0.045/GB processed) | $66 plus data |
| Load balancer for the checkout ingress ($0.0225/hour plus LCUs) | about $16 plus usage |
| KMS key ($1, plus $0.03 per 10,000 requests) | about $1 |
| CloudTrail management events (the first trail in an account is free) and its S3 bucket | under $1 |
| S3 and DynamoDB gateway endpoints | $0 |
| EKS control plane logs, all five types ($0.50/GB ingested) | a few dollars, depending on cluster activity |
| Alert pipeline at 1000 alerts/day (below) | about $14 |
| **Total** | **about $460** |
| Optional: 6 interface endpoints in 2 AZs (`enable_vpc_endpoints`, $0.01/hour each per AZ, plus $0.01/GB) | add $88 |
| Optional: the execute-api endpoint for `webhook_private` (2 AZs) | add $15 |

The alert pipeline line assumes 1000 webhook calls a day (30,000 a month), one alert each, half of them firing alerts that each start a runbook run of about 30 state transitions (remediate, then poll for up to 10 minutes):

- API Gateway (REST): 30,000 requests × $3.50/M = $0.11
- Lambda: alert_ingest (256 MB, about 100 ms) and runbook_action (512 MB, about 2 s per action): about $0.30
- EventBridge: about 60,000 custom events × $1/M = $0.06, plus a few cents for the archive
- Step Functions: 15,000 runbook runs × 30 transitions = 450,000 × $25/M = $11.25; the Express incident recorder, 45,000 runs: about $0.05
- DynamoDB on demand: about 150,000 writes (index included) and 135,000 reads: about $0.12
- SNS: 45,000 publishes, $0.02; email deliveries, $2 per 100,000 after the first 1,000: about $0.90. The chat topic doubles the publishes; Amazon Q Developer in chat applications itself is free.
- CloudWatch Logs for the pipeline, about 1 GB at $0.50/GB: $0.50
