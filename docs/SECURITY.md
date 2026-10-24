# Security Documentation - Event-Driven SRE Platform

## Table of Contents

1. [Security Overview](#security-overview)
2. [IAM Security Model](#iam-security-model)
3. [IRSA (IAM Roles for Service Accounts)](#irsa-iam-roles-for-service-accounts)
4. [Kubernetes RBAC](#kubernetes-rbac)
5. [Network Security](#network-security)
6. [Data Encryption](#data-encryption)
7. [Secrets Management](#secrets-management)
8. [Audit & Logging](#audit--logging)
9. [Security Best Practices Implemented](#security-best-practices-implemented)
10. [Security Checklist for Production](#security-checklist-for-production)
11. [Threat Model](#threat-model)
12. [Incident Response](#incident-response)
13. [Compliance Considerations](#compliance-considerations)

---

## Security Overview

This platform implements defense-in-depth security principles across multiple layers:

- **Identity & Access Management**: Least-privilege IAM policies, IRSA for temporary credentials
- **Network Security**: Private subnets, security groups, VPC endpoints
- **Data Protection**: Encryption at rest and in transit, KMS key management
- **Audit & Compliance**: CloudTrail logging, CloudWatch audit trails, DynamoDB incident history
- **Application Security**: Container image scanning, Terraform security scanning, dependency management

---

## IAM Security Model

### Principle: Least Privilege

All IAM roles and policies follow the principle of least privilege—granting only the minimum permissions required to perform their function.

### Lambda Execution Roles

#### 1. alert_ingest Lambda Role

**Purpose**: Receive alerts from Alertmanager, validate, enrich, and publish to EventBridge

**Permissions**:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:REGION:ACCOUNT_ID:log-group:/aws/lambda/PLATFORM_NAME-alert-ingest:*"
    },
    {
      "Effect": "Allow",
      "Action": ["events:PutEvents"],
      "Resource": "arn:aws:events:REGION:ACCOUNT_ID:event-bus/PLATFORM_NAME-bus"
    }
  ]
}
```

**Why This Is Secure**:
- ✅ No wildcards (`*`) for application resources
- ✅ Scoped to specific log group (not all CloudWatch Logs)
- ✅ Only write access to specific event bus (not all EventBridge buses)
- ✅ No access to DynamoDB, SNS or Step Functions at all; EventBridge rules deliver the events
- ✅ Cannot read incidents or delete data

#### 2. runbook_action Lambda Role

**Purpose**: Execute remediation actions against EKS and SSM Parameter Store

**Permissions**:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:REGION:*:log-group:/aws/lambda/PLATFORM_NAME-runbook-action:*"
    },
    {
      "Effect": "Allow",
      "Action": ["eks:DescribeCluster"],
      "Resource": "arn:aws:eks:REGION:*:cluster/CLUSTER_NAME"
    },
    {
      "Effect": "Allow",
      "Action": ["ssm:PutParameter", "ssm:GetParameter"],
      "Resource": "arn:aws:ssm:REGION:*:parameter/checkout/*"
    },
    {
      "Effect": "Allow",
      "Action": ["sts:GetCallerIdentity"],
      "Resource": "*"
    }
  ]
}
```

**Why This Is Secure**:
- ✅ Only describe specific EKS cluster (not all clusters)
- ✅ SSM access limited to `/checkout/*` namespace (not all parameters)
- ✅ Cannot delete SSM parameters or modify other application configs
- ✅ `sts:GetCallerIdentity` required to sign the EKS authentication token (cannot be scoped)

**Note**: The actual Kubernetes access is controlled by RBAC (see below), not IAM. The role also has the AWS-managed `AWSLambdaVPCAccessExecutionRole` policy so the Lambda can run in the private subnets.

**Input checks**: the Lambda only runs what the state machine sends it, and checks that first. It raises `InvalidRequest` for an action other than `degrade`, `restart`, `scale`, `degrade_or_scale` or `drain_node`, a namespace that isn't in `ALLOWED_NAMESPACES` (`apps`), a deployment name that isn't a valid Kubernetes name, replicas outside 1 to `MAX_REPLICAS` (`remediation_max_replicas`, default 10), or `degrade` for anything but checkout. For `drain_node` it refuses an invalid or unknown node name, a node without the `nodegroup=apps` label, and a drain that would leave no other Ready, schedulable apps node (`drain_min_schedulable_nodes`, default 1). The state machine doesn't retry `InvalidRequest`; it escalates the incident instead. So an alert can't point the Lambda at another namespace, scale a deployment without limit, or drain a system node or the last apps node, even though the target comes from the alert's labels.

### Step Functions Execution Role

**Purpose**: Orchestrate runbook workflow and invoke Lambda functions

**Permissions**:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogDelivery",
        "logs:GetLogDelivery",
        "logs:UpdateLogDelivery",
        "logs:DeleteLogDelivery",
        "logs:ListLogDeliveries",
        "logs:PutResourcePolicy",
        "logs:DescribeResourcePolicies",
        "logs:DescribeLogGroups"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["lambda:InvokeFunction"],
      "Resource": "arn:aws:lambda:REGION:ACCOUNT_ID:function:PLATFORM_NAME-runbook-action"
    },
    {
      "Effect": "Allow",
      "Action": ["dynamodb:UpdateItem", "dynamodb:GetItem"],
      "Resource": "arn:aws:dynamodb:REGION:ACCOUNT_ID:table/PLATFORM_NAME-incidents"
    },
    {
      "Effect": "Allow",
      "Action": ["events:PutEvents"],
      "Resource": "arn:aws:events:REGION:ACCOUNT_ID:event-bus/PLATFORM_NAME-bus"
    }
  ]
}
```

**Why This Is Secure**:
- ✅ CloudWatch Logs permissions required for Step Functions logging (cannot be scoped further)
- ✅ Only invoke specific Lambda function (not all functions)
- ✅ `UpdateItem` and `GetItem` on the incidents table only: to claim an incident, check whether it resolved, and mark it resolved or escalated. No scans, queries or deletes.
- ✅ `PutEvents` on the platform's bus only, for `action.completed`, `incident.auto_resolved` and `incident.escalated`

### Incident Recorder Role

**Purpose**: The `incident-recorder` Express state machine writes incident events to DynamoDB.

**Permissions**: `dynamodb:UpdateItem` on `PLATFORM_NAME-incidents`, plus the same log delivery permissions as the runbook role so it can log failed writes. It can't read or delete records.

### EventBridge Target Role

**Purpose**: Lets the EventBridge rules start the two state machines.

**Permissions**: `states:StartExecution` on `PLATFORM_NAME-runbook` and `PLATFORM_NAME-incident-recorder`.

The other targets grant EventBridge access through resource policies, each limited with an `aws:SourceArn` condition where the service supports it:

| Resource | Allows `events.amazonaws.com` to | Limited to |
| --- | --- | --- |
| SNS topic `PLATFORM_NAME-alerts` | `sns:Publish` | the `incident-created`, `incident-resolved`, `incident-escalated` and `incident-auto-resolved` rules |
| SQS queue `PLATFORM_NAME-events-dlq` | `sqs:SendMessage` | the platform's six rules |
| Log group `/aws/events/PLATFORM_NAME` | `logs:CreateLogStream`, `logs:PutLogEvents` | that log group |

---

## IRSA (IAM Roles for Service Accounts)

### What Is IRSA?

IRSA allows Kubernetes pods to assume IAM roles via the EKS OIDC provider, eliminating the need for long-lived credentials.

### How It Works

```
1. Pod starts with ServiceAccount annotation: eks.amazonaws.com/role-arn=<IAM_ROLE_ARN>
2. EKS mutating webhook injects AWS credentials as environment variables
3. Pod calls AWS STS AssumeRoleWithWebIdentity using OIDC token
4. STS returns temporary credentials (valid for 1 hour)
5. Pod uses temporary credentials to call AWS APIs
```

### Security Benefits

- **No Static Credentials**: No kubeconfig files or access keys stored in secrets
- **Automatic Rotation**: Credentials expire after 1 hour and are automatically refreshed
- **Audit Trail**: Every AssumeRoleWithWebIdentity call logged in CloudTrail
- **Least Privilege**: ServiceAccount bound to specific IAM role and Kubernetes Role

### The checkout app's role

The checkout pods read the degraded-mode flag from SSM. The chart's `checkout` service account carries the role from `terraform output checkout_role_arn` (`terraform/irsa.tf`, passed in by `scripts/deploy.sh`), and the role trusts only `apps:checkout`. Its one permission:

```json
{
  "Effect": "Allow",
  "Action": ["ssm:GetParameter"],
  "Resource": "arn:aws:ssm:REGION:ACCOUNT_ID:parameter/checkout/degraded_mode"
}
```

It can't write the flag or read any other parameter. Only the runbook_action Lambda writes it.

The container runs from `python:3.13-slim` as a non-root user (UID 10001).

### How the runbook_action Lambda authenticates

IRSA only works for pods, and the Lambda isn't a pod. Instead it calls the Kubernetes API as its own IAM role, and the cluster maps that role to a Kubernetes group with an EKS access entry (`terraform/eks.tf`):

```hcl
access_entries = {
  runbook_action = {
    principal_arn     = aws_iam_role.runbook_lambda_role.arn
    kubernetes_groups = ["checkout-remediation"]
  }
}
```

1. The Lambda calls `eks:DescribeCluster` for the API endpoint and CA certificate.
2. It presigns an STS `GetCallerIdentity` request with its role's temporary credentials and sends it as a bearer token.
3. EKS verifies the token with STS, finds the access entry for the role, and treats the caller as a member of `checkout-remediation`.
4. Kubernetes RBAC decides what that group may do (next section).

The Lambda runs in the private subnets and reaches the cluster's private API endpoint. The cluster security group allows HTTPS from the Lambda's security group only.

**Security Properties**:
- ✅ No kubeconfig, static token or long-lived key is stored anywhere
- ✅ Only this IAM role is mapped to the `checkout-remediation` group
- ✅ Outside the `apps` namespace, the group can only look at and cordon nodes, list pods and evict them (for `drain_node`)

---

## Kubernetes RBAC

### Principle: Namespace Isolation

The runbook_action Lambda (through its access entry) can change workloads only in the `apps` namespace. The one exception is `drain_node`: nodes are cluster-scoped and a node runs pods from every namespace, so cordoning one and evicting its pods needs a ClusterRole. That ClusterRole can't change any workload, only nodes and evictions. The Role, the ClusterRole and their bindings are in `k8s/rbac/remediation-role.yaml` and applied by `scripts/deploy.sh`.

### Role Definition

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: checkout-remediation
  namespace: apps
rules:
  # Read, scale and restart deployments
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list", "patch"]

  # scale raises the HPA's minimum instead of fighting it over replicas
  - apiGroups: ["autoscaling"]
    resources: ["horizontalpodautoscalers"]
    verbs: ["get", "patch"]

  # Look at pods to verify a rollout
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list"]

  # Read app config
  - apiGroups: [""]
    resources: ["configmaps"]
    verbs: ["get", "list"]
```

### ClusterRole for drain_node

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: checkout-remediation-nodes
rules:
  # Check the node's labels and readiness, then cordon it
  - apiGroups: [""]
    resources: ["nodes"]
    verbs: ["get", "list", "patch"]

  # Find the pods on the node
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["list"]

  - apiGroups: [""]
    resources: ["pods/eviction"]
    verbs: ["create"]
```

It's bound to the same `checkout-remediation` group with a ClusterRoleBinding of the same name. Eviction goes through the Eviction API, which respects PodDisruptionBudgets; there's no permission to delete pods outright. The Lambda also limits which nodes it will drain (see the input checks above), but RBAC can't express "only `apps` nodes", so the cluster-wide grant is the backstop, not the limit.

### RoleBinding

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: checkout-remediation
  namespace: apps
subjects:
  - kind: Group
    name: checkout-remediation
    apiGroup: rbac.authorization.k8s.io
roleRef:
  kind: Role
  name: checkout-remediation
  apiGroup: rbac.authorization.k8s.io
```

### What the Lambda CANNOT Do

- ❌ Delete deployments, pods, nodes or other resources (evicting is allowed, deleting isn't)
- ❌ Create new deployments or pods
- ❌ Modify deployments or other workloads in other namespaces (e.g., `kube-system`, `monitoring`)
- ❌ Access secrets or service accounts
- ❌ Modify RBAC rules (roles, rolebindings, clusterroles)
- ❌ Exec into pods or access pod logs
- ❌ Modify cluster-level resources other than nodes. It can patch nodes, and the Lambda only uses that to cordon them.

### Security Validation

**Test RBAC Permissions**:
```bash
# Verify the remediation group can patch deployments
kubectl auth can-i patch deployments -n apps --as=runbook-action --as-group=checkout-remediation
# Expected: yes

# Verify it CANNOT delete deployments
kubectl auth can-i delete deployments -n apps --as=runbook-action --as-group=checkout-remediation
# Expected: no

# Verify it CANNOT change workloads in other namespaces
kubectl auth can-i patch deployments -n kube-system --as=runbook-action --as-group=checkout-remediation
# Expected: no

# drain_node: it can cordon nodes and evict pods anywhere, but not delete them
kubectl auth can-i patch nodes --as=runbook-action --as-group=checkout-remediation
# Expected: yes
kubectl auth can-i create pods/eviction -n kube-system --as=runbook-action --as-group=checkout-remediation
# Expected: yes
kubectl auth can-i delete pods -n apps --as=runbook-action --as-group=checkout-remediation
# Expected: no
```

---

## Network Security

### VPC Design

- **Public Subnets**: the ALB and a NAT gateway per AZ
- **Private Subnets**: EKS nodes, the runbook_action Lambda, and the interface endpoints when they're on

### Security Groups

#### 1. EKS Node Security Group

Created by the EKS module with its recommended rules:

**Inbound Rules**:
- From the control plane: HTTPS (443), the kubelet (10250) and the webhook ports
- From other nodes: DNS and the ephemeral port range (pod-to-pod traffic)
- From the ALB to the pods; the AWS Load Balancer Controller adds this rule

**Outbound Rules**:
- All traffic, which leaves through the NAT gateways (images, AWS APIs) unless an endpoint handles it

#### 2. ALB Security Group

Created by the AWS Load Balancer Controller for the internet-facing checkout ingress.

**Inbound Rules**:
- HTTPS (443) from anywhere. Only allowing CloudFront is not built yet; there's no CloudFront in front of it today.

**Outbound Rules**:
- To the checkout pods

The listener is HTTPS only, with the `ELBSecurityPolicy-TLS13-1-2-2021-06` policy: TLS 1.2 minimum, TLS 1.3 preferred.

#### 3. runbook_action Lambda Security Group

**Inbound Rules**:
- None (Lambda functions do not accept incoming connections)

**Outbound Rules**:
- HTTPS (443) only: the cluster's private API endpoint, and AWS APIs (EKS, SSM, STS) through the NAT gateways or the interface endpoints

The cluster security group allows HTTPS from this group only.

#### 4. VPC Endpoint Security Group

Created when any interface endpoint is.

**Inbound Rules**:
- HTTPS (443) from the VPC CIDR

### VPC Endpoints

- **Gateway endpoints, always on** (they're free): S3 (ECR image layers) and DynamoDB, on the private route tables
- **Interface endpoints, off by default** (`enable_vpc_endpoints`, about $88/month for six in two AZs): `ecr.api`, `ecr.dkr`, `sts`, `ssm`, `eks` and `logs`, in the private subnets with private DNS, so the usual hostnames resolve to them inside the VPC
- **execute-api**, created whenever `webhook_private` is on, since a private API can only be reached through it. With private DNS on, other public REST APIs' default URLs stop resolving from inside the VPC.

**Security Benefit**: Traffic to those services stays on the AWS network instead of going out through the NAT gateways.

### Private Webhook

`webhook_private = true` makes the webhook a private REST API. Its resource policy allows invokes only through the VPC's execute-api endpoint (`aws:SourceVpce`), so only callers inside the VPC, Alertmanager among them, can reach it. Datadog can't reach a private API, so this is for setups where Alertmanager is the only sender. The invoke URL stays the same; private DNS resolves it inside the VPC.

---

## Data Encryption

### Encryption at Rest

The platform has one customer managed key, `alias/PLATFORM_NAME` (`terraform/kms.tf`, output `kms_key_arn`), with yearly rotation on and a 30 day deletion window.

| Data | Encryption | Key |
|------|------------|-----|
| **DynamoDB incidents table** | Enabled | Platform key |
| **SNS topic** | Enabled | Platform key |
| **SQS dead-letter queue** | Enabled | Platform key |
| **Platform log groups** (both Lambdas, both state machines, the event log) | Enabled | Platform key |
| **CloudTrail** (the trail and its S3 bucket, with an S3 bucket key) | Enabled | Platform key |
| **Kubernetes secrets** | Enabled | The key the EKS module created for the cluster |
| **ECR images** | Enabled | AES-256 |
| **SSM `/checkout/degraded_mode`** | Not encrypted with KMS | A String parameter; the flag isn't sensitive |

EKS secrets stay on the module's key because a cluster can't be moved to a different key in place. The EKS control plane log group is the EKS module's own, kept 90 days, and not on the platform key.

**Key policy**: besides the account root, it lets these use the key:
- DynamoDB, SNS, SQS and CloudWatch Logs on behalf of this account's roles (`kms:ViaService` and `kms:CallerAccount`)
- CloudWatch Logs itself, only for this account's log groups (encryption context `aws:logs:arn`)
- EventBridge, to publish to the encrypted topic and dead-letter to the encrypted queue
- CloudTrail, only for this platform's trail (`aws:SourceArn`)

```hcl
resource "aws_dynamodb_table" "incidents" {
  # ...
  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.platform.arn
  }

  point_in_time_recovery {
    enabled = true
  }
}
```

### Encryption in Transit

- **ALB**: HTTPS only, with an ACM certificate and the `ELBSecurityPolicy-TLS13-1-2-2021-06` policy (TLS 1.2 minimum, TLS 1.3 preferred)
- **Webhook (API Gateway)**: HTTPS only, with API Gateway's default TLS policy for the default endpoint. Pinning a TLS 1.2 minimum there (`security_policy` on the REST API) needs AWS provider 6, and the EKS module (~> 20) holds the provider below 6. That comes with the move to EKS module v21.
- **SNS, the dead-letter queue and the CloudTrail bucket**: their resource policies deny any request without TLS (`aws:SecureTransport` false)
- **EKS API server**: TLS, managed by AWS
- **Inside the cluster**: plain HTTP, ALB to pods included. There's no service mesh.

---

## Secrets Management

### Where Secrets Are Stored

The only secrets are the two webhook API keys.

| Item | Storage | Access Method |
|------|---------|---------------|
| **Alertmanager's webhook API key** | Kubernetes Secret `alertmanager-webhook-key` in `monitoring`, encrypted with the EKS secrets key | `scripts/deploy.sh` writes it from `terraform output alertmanager_api_key`; Alertmanager mounts it as a file |
| **Datadog's webhook API key** | A hidden custom variable in Datadog's webhook integration | Added by hand from `terraform output datadog_api_key` |
| **Degraded-mode flag** (not a secret) | SSM String parameter `/checkout/degraded_mode` | The runbook_action Lambda writes it; checkout pods read it through their IRSA role |
| **TLS certificate** | AWS Certificate Manager | The ALB fetches it |
| **Image pulls** | No secret | Nodes pull from ECR with their node role |

Both API key values are also in the Terraform state, as sensitive outputs, so keep the state bucket encrypted and access to it limited.

### Secret Rotation

**Webhook API keys**: replace one and redeploy:

```bash
terraform -chdir=terraform apply -replace='aws_api_gateway_api_key.webhook["alertmanager"]'
bash scripts/deploy.sh   # writes the new key to Alertmanager's secret
```

For Datadog, replace `aws_api_gateway_api_key.webhook["datadog"]` and paste the new value into the Datadog integration.

**Degraded-mode flag**: doesn't need rotation; it isn't sensitive.

### Secrets in Code

**Never commit secrets to Git**:
- ❌ Hardcoded API keys in Lambda code
- ❌ Database passwords in Terraform files
- ❌ AWS credentials in Docker images

**Where they come from instead**:
- ✅ API keys are generated by Terraform and handed to Alertmanager through a Kubernetes Secret
- ✅ AWS access is through IAM roles (Lambda execution roles, IRSA), never stored keys

---

## Audit & Logging

### CloudTrail

`terraform/cloudtrail.tf` creates a multi-region trail, `PLATFORM_NAME-trail`, unless `enable_cloudtrail = false` (for accounts an organization trail already covers).

**What Is Logged**:
- Management events in every region: IAM, Lambda configuration and code updates, EKS, KMS, SSM writes and so on, plus global services
- AssumeRoleWithWebIdentity calls (IRSA token exchanges)
- Not data events (DynamoDB item reads and writes, S3 object access, Lambda invokes); those are billed per event
- Kubernetes API calls are in the EKS audit logs, not CloudTrail (see below)

**Storage**: the S3 bucket `PLATFORM_NAME-cloudtrail-ACCOUNT_ID` (output `cloudtrail_bucket`), encrypted with the platform key, versioned, with all public access blocked, ownership enforced, and a policy that only lets this trail write and denies non-TLS requests. Log file validation is on, so `aws cloudtrail validate-logs` can show whether a file was changed or deleted.

**Retention**: 365 days (`cloudtrail_retention_days`), then the lifecycle rule deletes them. The bucket can't be deleted while it holds logs unless `cloudtrail_force_destroy` is set, which `scripts/destroy.sh` does.

**Use Cases**:
- **Forensics**: Who invoked which Lambda at what time?
- **Compliance**: Prove that only authorized users modified production resources
- **Threat Detection**: Detect unusual API call patterns (e.g., delete all DynamoDB items)

### CloudWatch Logs

**Log Groups**:
- `/aws/lambda/PLATFORM_NAME-alert-ingest`
- `/aws/lambda/PLATFORM_NAME-runbook-action`
- `/aws/vendedlogs/states/PLATFORM_NAME-runbook` (every runbook state's input and output)
- `/aws/vendedlogs/states/PLATFORM_NAME-incident-recorder` (failed incident writes)
- `/aws/events/PLATFORM_NAME` (every event on the bus)
- `/aws/eks/CLUSTER_NAME/cluster` (EKS control plane logs)

**Retention**: 30 days for the five platform log groups, all encrypted with the platform key. Terraform creates the two Lambda log groups too, so Lambda doesn't make its own unencrypted ones that never expire. The EKS module keeps its log group 90 days.

Both state machines log execution data, so these log groups hold alert payloads: labels, annotations, and the action results. Alerts carry no secrets, and the webhook API keys never reach the state machines, but treat the log groups like the incident table when granting read access.

**Metric Filters**:
- Extract custom metrics (e.g., remediation success rate, alert volume)
- Create CloudWatch alarms on errors or anomalies

### DynamoDB Incident History

Every incident is logged with:
- **incident_id**: Unique identifier (UUID, derived from the alert so a firing alert and its resolution share it)
- **first_seen_at**, **last_seen_at**: When the platform first and last received the alert (ISO 8601)
- **alertname**: Name of the alert (e.g., `CheckoutHighErrorRate`)
- **severity**: `critical`, `warning`, `info`
- **status**: `open`, `remediating`, `resolved`, `escalated`
- **payload**: The full normalized alert as JSON, Prometheus labels included (service, namespace, etc.)
- **actions_taken**: List of remediation actions executed, with their result and the execution that ran them
- **resolved_at**: When the alert resolved (ISO 8601)
- **remediation_result**, **mttr_seconds**, **escalation_reason**: How the runbook ended

**Retention**: 90 days after the incident's last activity (TTL on `expires_at`), then auto-deleted. Point-in-time recovery is on, and deletion protection stops the table being deleted by accident, `terraform destroy` included.

**Use Cases**:
- **Analytics**: MTTR trends, incident frequency by service
- **Compliance**: Prove that all incidents were handled according to policy
- **Runbook Tuning**: Identify which remediation actions are most effective

### EKS Control Plane Logs

**Enabled Log Types**:
- `api`: Kubernetes API server logs (who called which API, when)
- `audit`: Detailed audit logs (every API call with request/response)
- `authenticator`: IAM authentication logs (IRSA token exchanges)
- `controllerManager`: Controller logs (deployment scaling, etc.)
- `scheduler`: Pod scheduling decisions

**Use Cases**:
- **RBAC Debugging**: Why did ServiceAccount get access denied?
- **Audit**: What deployments were patched in the last 24 hours?
- **Threat Detection**: Detect unauthorized access attempts

---

## Security Best Practices Implemented

### ✅ Implemented

1. **Least-Privilege IAM**: All roles have specific ARNs, no wildcards for application resources
2. **IRSA and EKS access entries**: No long-lived credentials stored
3. **Kubernetes RBAC**: the remediation group is limited to the `apps` namespace and specific operations
4. **Encryption at Rest**: the platform KMS key for the incidents table, SNS, the dead-letter queue, the log groups and CloudTrail; the EKS module's key for Kubernetes secrets; AES-256 for ECR
5. **Encryption in Transit**: TLS 1.2+ policy on the ALB; SNS, the dead-letter queue and the CloudTrail bucket deny non-TLS requests. The webhook is HTTPS only, but its TLS policy waits on AWS provider 6.
6. **Network Segmentation**: Private subnets for EKS and the remediation Lambda, security groups for isolation, an optional private webhook
7. **CloudTrail Logging**: Multi-region management events with log file validation, kept 365 days
8. **CloudWatch Logs**: Lambda, Step Functions and event logs kept 30 days, encrypted with the platform key
9. **DynamoDB Incident History**: Complete audit trail
10. **No Secrets in SSM**: the degraded flag is a plain String; the webhook API keys are in a Kubernetes Secret that EKS encrypts
11. **Container Image Scanning**: ECR scans on push, and Trivy scans the image in CI
12. **Infrastructure Scanning**: Checkov and tflint run on the Terraform in CI. Checkov's settings and every skipped check, each with its reason, are in `.checkov.yaml`; it reports but doesn't fail the build.
13. **VPC Endpoints**: S3 and DynamoDB gateway endpoints always; interface endpoints optional (`enable_vpc_endpoints`)
14. **API Gateway Throttling**: Rate limiting to prevent abuse
15. **Non-root Container**: the checkout image runs as an unprivileged user
16. **Immutable Image Tags**: each build is tagged with its git SHA, and ECR refuses to overwrite a tag
17. **VPC Flow Logs**: all traffic, to CloudWatch Logs, encrypted with the platform key and kept 30 days
18. **No Node Credentials for Pods**: nodes require IMDSv2 with a hop limit of 1, so pods can't use the node's IAM role; pods that need AWS use IRSA
19. **Remediation Concurrency Cap**: runbook_action runs at most 10 at once (`runbook_action_reserved_concurrency`)

### 🚧 Recommended for Production

20. **Restrict the EKS Public Endpoint**: it's open to `0.0.0.0/0` by default; set `eks_public_access_cidrs` to your own address
21. **Multi-Factor Authentication (MFA)**: Require MFA for all AWS console access
22. **Service Control Policies (SCPs)**: Prevent deletion of CloudTrail, KMS keys
23. **GuardDuty**: Threat detection for AWS accounts
24. **Security Hub**: Centralized security findings
25. **Config Rules**: Continuous compliance monitoring (e.g., ensure encryption enabled)
26. **Secrets Rotation**: Rotate the webhook API keys on a schedule
27. **WAF Rules**: Protect the ALB and API Gateway from common attacks (SQL injection, XSS)
28. **DDoS Protection**: AWS Shield Standard (free) or Advanced (paid)
29. **TLS Policy on the Webhook**: once the repo moves to AWS provider 6

---

## Security Checklist for Production

Use this checklist before deploying to production:

### IAM & Access Control
- [ ] All IAM roles have least-privilege policies (no wildcards for app resources)
- [ ] IRSA configured for all pods that need AWS access
- [ ] Kubernetes RBAC limits Lambda to specific namespace
- [ ] MFA enabled for all AWS console users
- [ ] IAM Access Analyzer enabled to detect over-privileged policies

### Network Security
- [ ] EKS nodes in private subnets
- [ ] Security groups allow only required traffic
- [ ] VPC endpoints configured for AWS services
- [ ] VPC Flow Logs enabled
- [ ] EKS public endpoint limited to your own CIDRs (`eks_public_access_cidrs`)
- [ ] ALB has TLS certificate from ACM
- [ ] WAF rules enabled on API Gateway (if public)

### Data Protection
- [ ] DynamoDB encrypted with customer managed KMS key
- [ ] CloudWatch Logs encrypted
- [ ] EKS secrets encrypted with customer managed KMS key
- [ ] Any secret you add to SSM uses SecureString (the degraded flag isn't secret and doesn't need it)
- [ ] S3 buckets for logs have encryption and versioning enabled
- [ ] DynamoDB point-in-time recovery enabled

### Logging & Monitoring
- [ ] CloudTrail enabled in all regions
- [ ] EKS control plane logs enabled (api, audit, authenticator)
- [ ] Lambda function logs retained for at least 30 days
- [ ] CloudWatch alarms for Lambda errors, Step Functions failures
- [ ] SNS topic subscribed for security alerts

### Application Security
- [ ] Container images scanned with Trivy (no HIGH or CRITICAL vulnerabilities)
- [ ] Terraform scanned with Checkov, and Checkov set to fail the build (it's soft-fail today)
- [ ] Flask app dependencies updated (no known vulnerabilities)
- [ ] API Gateway throttling configured (prevent abuse)
- [ ] Rate limiting enabled on alert webhook endpoint

### Compliance & Audit
- [ ] CloudTrail logs archived to S3 with lifecycle policy
- [ ] DynamoDB incident history retained for compliance period (90+ days)
- [ ] Regular security audits scheduled (quarterly)
- [ ] Incident response plan documented and tested

---

## Threat Model

### Threat 1: Malicious Alert Injection

**Attack Vector**: Attacker sends crafted alerts to API Gateway webhook

**Impact**: Trigger unauthorized remediation actions (e.g., delete all pods)

**Mitigations**:
- ✅ API Gateway validates JSON schema (reject malformed requests)
- ✅ alert_ingest Lambda validates alert structure and source
- ✅ RBAC limits Lambda to patching deployments and HPAs, cordoning nodes and evicting pods (cannot delete anything)
- ✅ The state machine only runs four known actions, and the Lambda rejects other namespaces, invalid deployment names and replica counts over `MAX_REPLICAS`
- ✅ An incident is remediated once, however many times the alert is sent
- ✅ API Gateway throttling prevents alert flood

**Residual Risk**: Low (requires bypassing multiple validation layers)

### Threat 2: Compromised IAM Credentials

**Attack Vector**: Attacker gains access to Lambda IAM role credentials

**Impact**: Read incident history, publish fake alerts, invoke Step Functions

**Mitigations**:
- ✅ Least-privilege IAM policies limit damage
- ✅ CloudTrail logs management API calls in every region (detect unusual activity)
- ❌ GuardDuty isn't set up by this repo; turn it on for the account
- ✅ Lambda execution role cannot delete resources
- ✅ Credentials are temporary (Lambda execution context, 6-hour max)

**Residual Risk**: Medium (could still cause availability issues)

### Threat 3: Kubernetes RBAC Bypass

**Attack Vector**: Attacker exploits Kubernetes vulnerability to escalate privileges

**Impact**: Modify resources outside `apps` namespace, delete production pods

**Mitigations**:
- ✅ Keep EKS version up-to-date (patches security issues)
- ✅ RBAC limits the remediation group's workload changes to the `apps` namespace; outside it, only cordons and evictions
- ❌ Pod Security Standards (PSS) aren't enforced yet
- ❌ No network policies between namespaces yet
- ✅ Audit logs capture unauthorized access attempts

**Residual Risk**: Low (requires Kubernetes 0-day exploit)

### Threat 4: Lambda Function Code Injection

**Attack Vector**: Attacker modifies Lambda function code in S3 or ECR

**Impact**: Execute arbitrary code with Lambda IAM role permissions

**Mitigations**:
- ✅ Function code only changes through Terraform; no role in the platform has `lambda:UpdateFunctionCode`
- ✅ CloudTrail logs every function code and configuration update
- ✅ The checkout image tags are immutable, so a deployed tag can't be swapped for different code
- ❌ Lambda code signing isn't set up

**Residual Risk**: Very Low (requires AWS account compromise)

### Threat 5: DynamoDB Data Exfiltration

**Attack Vector**: Attacker reads incident history from DynamoDB

**Impact**: Exposure of incident details, alert patterns, service topology

**Mitigations**:
- ✅ IAM policies prevent read access (only the incident recorder and the runbook state machine can write, and the runbook can only read single items)
- ✅ DynamoDB encrypted at rest with the platform key
- ✅ VPC gateway endpoint for DynamoDB (traffic from the VPC stays on the AWS network)
- ❌ CloudTrail doesn't log item reads and writes (data events), only table-level calls

**Residual Risk**: Low (requires IAM role compromise)

---

## Incident Response

### Security Incident Playbook

**Scenario 1: Unauthorized Lambda Invocation Detected**

1. **Detect**: CloudWatch alarm on Lambda invocations spike
2. **Triage**: Check CloudTrail for source IP and IAM principal
3. **Contain**: Disable Lambda function (update IAM policy to deny all)
4. **Investigate**: Review Lambda logs for malicious activity
5. **Remediate**: Rotate IAM credentials, patch vulnerability
6. **Recover**: Re-enable Lambda with updated policy
7. **Post-Mortem**: Document lessons learned, update runbooks

**Scenario 2: Suspicious EKS API Calls**

1. **Detect**: EKS audit logs show unusual API calls (e.g., delete all pods)
2. **Triage**: Identify ServiceAccount and IAM role
3. **Contain**: Revoke RBAC permissions (delete RoleBinding)
4. **Investigate**: Review CloudTrail for AssumeRoleWithWebIdentity calls
5. **Remediate**: Patch compromised pod or revoke IAM role
6. **Recover**: Re-create ServiceAccount with tighter RBAC
7. **Post-Mortem**: Update RBAC policies, add monitoring

**Scenario 3: DynamoDB Data Breach**

1. **Detect**: GuardDuty alert on unusual DynamoDB access
2. **Triage**: Identify IAM principal and IP address
3. **Contain**: Revoke IAM role or add deny policy
4. **Investigate**: Determine what data was accessed
5. **Remediate**: Rotate exposed credentials, notify affected parties
6. **Recover**: Enable DynamoDB point-in-time recovery, restore if needed
7. **Post-Mortem**: Implement additional access controls (VPC endpoint, IAM conditions)

---

## Compliance Considerations

### SOC 2 Type II

**Requirements**:
- Encryption at rest and in transit (✅ Implemented)
- Access controls and least privilege (✅ Implemented)
- Audit logs retained for at least 1 year (✅ CloudTrail to S3)
- Incident response plan documented (✅ See above)
- Regular security reviews and penetration tests (🚧 To be scheduled)

### PCI DSS (if handling payment data)

**Requirements**:
- Network segmentation (✅ Private subnets, security groups)
- Encryption of cardholder data (✅ KMS encryption)
- Access control (✅ RBAC, IAM)
- Monitoring and logging (✅ CloudTrail, CloudWatch Logs)
- Regular security testing (🚧 Quarterly scans required)

**Note**: This platform does not store cardholder data, but alerts may contain metadata that references payment transactions.

### HIPAA (if handling health data)

**Requirements**:
- Encryption at rest and in transit (✅ Implemented)
- Audit controls (✅ CloudTrail, EKS audit logs)
- Access controls (✅ IAM, RBAC)
- Data retention policies (✅ DynamoDB TTL)
- Business Associate Agreement (BAA) with AWS (🚧 Required for compliance)

**Note**: Ensure alerts do not contain Protected Health Information (PHI) in labels or annotations.

---

## Security Contact

For security issues or vulnerabilities, please report to:

- **Email**: security@example.com (replace with your actual contact)
- **Bug Bounty Program**: (if applicable)

**Please DO NOT** open public GitHub issues for security vulnerabilities.

---

## References

- [AWS Security Best Practices](https://aws.amazon.com/architecture/security-identity-compliance/)
- [EKS Best Practices Guide - Security](https://aws.github.io/aws-eks-best-practices/security/docs/)
- [Kubernetes Security and Disclosure](https://kubernetes.io/docs/reference/issues-security/security/)
- [OWASP Top 10](https://owasp.org/www-project-top-ten/)
- [CIS Benchmarks for AWS and Kubernetes](https://www.cisecurity.org/cis-benchmarks/)

---

**Last Updated**: 2026-02-02
**Reviewed By**: SRE Platform Team
**Next Review**: 2026-05-02 (quarterly)
