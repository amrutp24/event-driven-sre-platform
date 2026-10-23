# Security

## Reporting a vulnerability

Please don't open a public issue. Email amrut.pagidipally@gmail.com with what you found and how to reproduce it.

## How it's set up

Resource names assume the default `name` variable, `event-driven-sre`.

### IAM

Each component has its own role, and permissions are limited to specific resources:

| Role | Can do |
| --- | --- |
| `event-driven-sre-alert-ingest-role` | write its own log group; `dynamodb:PutItem` on the incidents table; `events:PutEvents` on the bus; `sns:Publish` on the topic; `states:StartExecution` on the state machine |
| `event-driven-sre-sfn-role` | `lambda:InvokeFunction` on the runbook Lambda; CloudWatch log delivery actions |
| `event-driven-sre-runbook-action-role` | write its own log group; `eks:DescribeCluster` on the cluster; `ssm:GetParameter` and `ssm:PutParameter` on `parameter/checkout/*`; `sts:GetCallerIdentity` |

Two permissions use `Resource: "*"` because AWS doesn't support narrower resources for them: `sts:GetCallerIdentity`, and the Step Functions log delivery actions. Step Functions logging isn't turned on yet, so the log delivery permissions are unused for now.

### Kubernetes access

- Add-ons that run as pods use IRSA, with roles for the AWS Load Balancer Controller, ExternalDNS (limited to the hosted zones in `route53_zone_arns`), cert-manager (Route 53 access only when `cert_manager_route53_enabled` is set) and the EBS CSI driver.
- The runbook Lambda isn't a pod. It authenticates to the Kubernetes API with a token signed by its IAM role. That role has no EKS access entry or RBAC binding, so today it has no permissions in the cluster. When it gets one, it should be limited to patching deployments in the `apps` namespace.

### Network

- Nodes run in private subnets and reach the internet through a NAT gateway in each zone.
- The EKS API endpoint is reachable privately and publicly. The public endpoint is on for the first bootstrap; restrict it with `cluster_endpoint_public_access_cidrs` or turn it off afterwards.
- The Lambdas don't run in the VPC. The runbook Lambda reaches the cluster through the public endpoint.
- The alert webhook is public and HTTPS-only.

### Encryption

- Terraform state is stored in S3 with encryption on (`terraform/backend.tf`).
- DynamoDB and CloudWatch Logs encrypt data at rest by default with AWS-managed keys. No customer-managed KMS keys are configured.
- The SNS topic has no server-side encryption configured.
- All traffic to API Gateway, the EKS API and AWS service APIs uses TLS.

### Logging

- All five EKS control plane log types are enabled.
- Both Lambdas log to CloudWatch, and every alert leaves a record in DynamoDB.
- This repo doesn't create a CloudTrail trail. API activity is recorded by the account's own trail, if it has one.

### CI

Pushes to `main` and pull requests run Checkov on the Terraform and Trivy on the repository, with Trivy's results uploaded to the Security tab.

## Known gaps

- **The webhook has no authentication.** Anyone with the URL can post an alert and trigger remediation. Options are a shared secret checked by `alert-ingest`, a Lambda authorizer, or IAM auth with Alertmanager signing requests.
- **No throttling or access logging on the API stage** beyond API Gateway's defaults.
- **The EKS public endpoint is open to all addresses.**
- **The SNS topic isn't encrypted** and has no subscriptions.

Functional gaps are listed in the [README](../README.md#known-gaps).
