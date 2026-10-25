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

Setting both IDs turns on the module's `chat_notifications`, and `chatbot.tf` subscribes the channel to `chat_topic_arn`. The Chatbot API isn't in `us-east-1`, so `chatbot.tf` uses a second provider in `chatbot_region` (default `us-east-2`). The channel's role has no permissions, and the channel has a `ReadOnlyAccess` guardrail. Messages look like:

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

Roughly $460 a month running continuously in `us-east-1`, plus $88 with `enable_vpc_endpoints`. The breakdown is in [docs/architecture/README.md](../../docs/architecture/README.md#monthly-cost-estimate).
