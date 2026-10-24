# Complete

The full demo of the module: everything it needs around it, and a demo app to break and fix.

## What it creates

- A VPC across two AZs with a NAT gateway per AZ, flow logs, the S3 and DynamoDB gateway endpoints, and optional interface endpoints (`enable_vpc_endpoints`)
- An EKS 1.36 cluster with "system" and "apps" managed node groups on Amazon Linux 2023, and metrics-server
- IRSA roles for the AWS Load Balancer Controller, ExternalDNS, cert-manager, the EBS CSI driver and the checkout app (which can only read the degraded-mode parameter)
- An ECR repository for the checkout image
- A multi-region CloudTrail trail and its bucket (`enable_cloudtrail`)
- One KMS key for the module's data, the flow logs and the CloudTrail bucket, passed to the module with `create_kms_key = false`
- The module itself, wired to the VPC and the cluster

State is local; there's no backend block.

## Prerequisites

- The AWS CLI, Terraform, kubectl, Helm, Docker and git
- In `helm/checkout/values.yaml`, `ingress.host` and `ingress.certificateArn` (an ACM certificate for that host), or `ingress.enabled=false` for no load balancer
- `eks_public_access_cidrs` set to your own address; the default is open to the internet

## Deploy

From the repo root, in this order:

```bash
bash scripts/bootstrap.sh       # terraform init and apply here, then configure kubectl
bash scripts/install-addons.sh  # AWS Load Balancer Controller, ExternalDNS, cert-manager
bash scripts/build-image.sh     # build apps/checkout and push it to ECR, tagged with the short git SHA
bash scripts/deploy.sh          # Alertmanager's API key, kube-prometheus-stack, the alert rules, the checkout app and the remediation RBAC
```

`build-image.sh` refuses to build uncommitted changes in `apps/checkout` unless you set `IMAGE_TAG`; if you do, set the same `IMAGE_TAG` for `deploy.sh`. Re-run `deploy.sh` after an apply that changes the webhook URL, the API key or the checkout role.

[docs/DEMO.md](../../docs/DEMO.md) walks through breaking the app and watching the runbook fix it.

## Destroy

Uninstall the Helm releases first. The load balancer the controller created isn't in Terraform state, and it blocks the VPC from being deleted while it exists.

```bash
helm uninstall checkout -n apps
helm uninstall mon -n monitoring
bash scripts/destroy.sh         # deletion protection and the trail bucket's protection off, then terraform destroy
```

## Cost

Roughly $460 a month running continuously in `us-east-1`, plus $88 with `enable_vpc_endpoints`. The breakdown is in [docs/architecture/README.md](../../docs/architecture/README.md#monthly-cost-estimate).
