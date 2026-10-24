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

**Input checks**: the Lambda only runs what the state machine sends it, and checks that first. It raises `InvalidRequest` for an action other than `degrade`, `restart`, `scale` or `degrade_or_scale`, a namespace that isn't in `ALLOWED_NAMESPACES` (`apps`), a deployment name that isn't a valid Kubernetes name, or replicas outside 1 to `MAX_REPLICAS` (`remediation_max_replicas`, default 10). The state machine doesn't retry `InvalidRequest`; it escalates the incident instead. So an alert can't point the Lambda at another namespace or scale a deployment without limit, even though the target comes from the alert's labels.

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
- ✅ The group has no permissions outside the `apps` namespace

---

## Kubernetes RBAC

### Principle: Namespace Isolation

The runbook_action Lambda (through its access entry) has limited Kubernetes permissions scoped to the `apps` namespace. The Role and RoleBinding are in `k8s/rbac/remediation-role.yaml` and applied by `scripts/deploy.sh`.

### Role Definition

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: checkout-remediation
  namespace: apps
rules:
  # Allow getting and patching deployments
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list", "patch"]

  # Allow listing pods (for verification)
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list"]

  # Allow reading configmaps (if needed for config)
  - apiGroups: [""]
    resources: ["configmaps"]
    verbs: ["get", "list"]
```

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

- ❌ Delete deployments, pods, or other resources
- ❌ Create new deployments or pods
- ❌ Modify resources in other namespaces (e.g., `kube-system`, `monitoring`)
- ❌ Access secrets or service accounts
- ❌ Modify RBAC rules (roles, rolebindings, clusterroles)
- ❌ Exec into pods or access pod logs
- ❌ Delete nodes or modify cluster-level resources

### Security Validation

**Test RBAC Permissions**:
```bash
# Verify the remediation group can patch deployments
kubectl auth can-i patch deployments -n apps --as=runbook-action --as-group=checkout-remediation
# Expected: yes

# Verify it CANNOT delete deployments
kubectl auth can-i delete deployments -n apps --as=runbook-action --as-group=checkout-remediation
# Expected: no

# Verify it CANNOT access other namespaces
kubectl auth can-i get pods -n kube-system --as=runbook-action --as-group=checkout-remediation
# Expected: no
```

---

## Network Security

### VPC Design

- **Public Subnets**: ALB, NAT Gateway
- **Private Subnets**: EKS worker nodes, RDS (if used)
- **Isolated Subnets**: Lambda functions (if VPC-attached)

### Security Groups

#### 1. EKS Node Security Group

**Inbound Rules**:
- Allow HTTPS (443) from ALB security group
- Allow all traffic from same security group (pod-to-pod communication)
- Allow HTTPS (443) from EKS control plane security group (kubelet)

**Outbound Rules**:
- Allow HTTPS (443) to internet via NAT Gateway (pull images, call AWS APIs)
- Allow all traffic to same security group

#### 2. ALB Security Group

**Inbound Rules**:
- Allow HTTPS (443) from CloudFront IP ranges (or 0.0.0.0/0 if no CloudFront)

**Outbound Rules**:
- Allow HTTPS (443) to EKS node security group

#### 3. Lambda Security Group (if VPC-attached)

**Inbound Rules**:
- None (Lambda functions do not accept incoming connections)

**Outbound Rules**:
- Allow HTTPS (443) to EKS control plane (API calls)
- Allow HTTPS (443) to VPC endpoints (SSM, CloudWatch Logs)

### VPC Endpoints

To reduce data transfer costs and improve security, use VPC endpoints for:

- **SSM**: `com.amazonaws.REGION.ssm`
- **CloudWatch Logs**: `com.amazonaws.REGION.logs`
- **DynamoDB**: `com.amazonaws.REGION.dynamodb` (Gateway endpoint, no cost)
- **EventBridge**: `com.amazonaws.REGION.events`
- **SNS**: `com.amazonaws.REGION.sns`

**Security Benefit**: Traffic never leaves AWS network, reducing exposure to internet-based attacks.

---

## Data Encryption

### Encryption at Rest

All data stores are encrypted using AWS KMS:

| Service | Encryption | KMS Key |
|---------|------------|---------|
| **DynamoDB** | Enabled | AWS managed key or customer managed key |
| **SNS** | Enabled | AWS managed key |
| **CloudWatch Logs** | Enabled | AWS managed key or customer managed key |
| **EKS Secrets** | Enabled | Customer managed key (recommended) |
| **SSM Parameter Store** | Enabled (SecureString) | AWS managed key or customer managed key |

Today the incidents table uses DynamoDB's default AWS owned key, with point-in-time recovery and deletion protection on. The customer managed key below is still to come.

**Configuration Example (Terraform)**:
```hcl
resource "aws_kms_key" "sre_platform" {
  description             = "SRE Platform encryption key"
  deletion_window_in_days = 30
  enable_key_rotation     = true
}

resource "aws_dynamodb_table" "incidents" {
  name         = "${var.name}-incidents"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "incident_id"

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.sre_platform.arn
  }

  point_in_time_recovery {
    enabled = true
  }
}
```

### Encryption in Transit

All network traffic is encrypted:

- **API Gateway**: TLS 1.2+ (configured by default)
- **ALB**: TLS 1.2+ with ACM certificate
- **EKS API Server**: TLS 1.2+ (configured by default)
- **Inter-pod communication**: mTLS via service mesh (optional: Istio, Linkerd)

---

## Secrets Management

### Where Secrets Are Stored

| Secret Type | Storage | Access Method |
|-------------|---------|---------------|
| **Feature Flags** | SSM Parameter Store (SecureString) | Lambda reads via AWS SDK |
| **Database Passwords** | AWS Secrets Manager | Pod reads via IRSA + CSI driver |
| **API Keys** | AWS Secrets Manager | Injected as environment variables |
| **TLS Certificates** | AWS Certificate Manager | ALB auto-fetches |
| **Image Pull Secrets** | Kubernetes Secrets | EKS auto-creates for ECR |

### Secret Rotation

**SSM Parameter Store**:
- Feature flags do not require rotation (not sensitive)
- For sensitive parameters, use AWS Secrets Manager with auto-rotation

**Secrets Manager** (if used):
- Enable automatic rotation for RDS passwords (Lambda-based)
- Rotate API keys every 90 days (manual or automated)

### Secrets in Code

**Never commit secrets to Git**:
- ❌ Hardcoded API keys in Lambda code
- ❌ Database passwords in Terraform files
- ❌ AWS credentials in Docker images

**Use environment variables and parameter stores**:
- ✅ Lambda reads SSM at runtime
- ✅ Pods read secrets via Kubernetes Secrets or CSI driver
- ✅ Terraform reads secrets from SSM or Secrets Manager

---

## Audit & Logging

### CloudTrail

**What Is Logged**:
- All API calls to AWS services (IAM, Lambda, EKS, DynamoDB, SSM, etc.)
- AssumeRoleWithWebIdentity calls (IRSA token exchanges)
- EKS control plane audit logs (Kubernetes API calls)

**Retention**: 90 days in CloudTrail, then archived to S3 with lifecycle policy (e.g., move to Glacier after 1 year)

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

**Retention**: 30 days for the state machine and event logs (configurable, consider cost vs compliance requirements)

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
4. **Encryption at Rest**: KMS for all data stores
5. **Encryption in Transit**: TLS 1.2+ for all endpoints
6. **Network Segmentation**: Private subnets for EKS, security groups for isolation
7. **CloudTrail Logging**: All API calls audited
8. **CloudWatch Logs**: All Lambda and Step Functions logs retained
9. **DynamoDB Incident History**: Complete audit trail
10. **SSM Parameter Store**: SecureString for sensitive configuration
11. **Container Image Scanning**: Trivy scans on every build
12. **Infrastructure Scanning**: tfsec validates Terraform
13. **VPC Endpoints**: Private connectivity to AWS services (optional, recommended)
14. **API Gateway Throttling**: Rate limiting to prevent abuse

### 🚧 Recommended for Production

15. **Multi-Factor Authentication (MFA)**: Require MFA for all AWS console access
16. **Service Control Policies (SCPs)**: Prevent deletion of CloudTrail, KMS keys
17. **GuardDuty**: Threat detection for AWS accounts
18. **Security Hub**: Centralized security findings
19. **Config Rules**: Continuous compliance monitoring (e.g., ensure encryption enabled)
20. **Secrets Rotation**: Auto-rotate database passwords, API keys
21. **WAF Rules**: Protect API Gateway from common attacks (SQL injection, XSS)
22. **DDoS Protection**: AWS Shield Standard (free) or Advanced (paid)
23. **VPC Flow Logs**: Network traffic analysis
24. **S3 Bucket Policies**: Prevent public access to logs and artifacts

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
- [ ] ALB has TLS certificate from ACM
- [ ] WAF rules enabled on API Gateway (if public)

### Data Protection
- [ ] DynamoDB encrypted with customer managed KMS key
- [ ] CloudWatch Logs encrypted
- [ ] EKS secrets encrypted with customer managed KMS key
- [ ] SSM parameters use SecureString type
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
- [ ] Terraform scanned with tfsec (no MEDIUM or higher issues)
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
- ✅ RBAC limits Lambda to patch deployments only (cannot delete)
- ✅ The state machine only runs four known actions, and the Lambda rejects other namespaces, invalid deployment names and replica counts over `MAX_REPLICAS`
- ✅ An incident is remediated once, however many times the alert is sent
- ✅ API Gateway throttling prevents alert flood

**Residual Risk**: Low (requires bypassing multiple validation layers)

### Threat 2: Compromised IAM Credentials

**Attack Vector**: Attacker gains access to Lambda IAM role credentials

**Impact**: Read incident history, publish fake alerts, invoke Step Functions

**Mitigations**:
- ✅ Least-privilege IAM policies limit damage
- ✅ CloudTrail logs all API calls (detect unusual activity)
- ✅ GuardDuty alerts on anomalous IAM usage
- ✅ Lambda execution role cannot delete resources
- ✅ Credentials are temporary (Lambda execution context, 6-hour max)

**Residual Risk**: Medium (could still cause availability issues)

### Threat 3: Kubernetes RBAC Bypass

**Attack Vector**: Attacker exploits Kubernetes vulnerability to escalate privileges

**Impact**: Modify resources outside `apps` namespace, delete production pods

**Mitigations**:
- ✅ Keep EKS version up-to-date (patches security issues)
- ✅ RBAC limits the remediation group to the `apps` namespace
- ✅ Pod Security Standards (PSS) enforce best practices
- ✅ Network policies isolate namespaces
- ✅ Audit logs capture unauthorized access attempts

**Residual Risk**: Low (requires Kubernetes 0-day exploit)

### Threat 4: Lambda Function Code Injection

**Attack Vector**: Attacker modifies Lambda function code in S3 or ECR

**Impact**: Execute arbitrary code with Lambda IAM role permissions

**Mitigations**:
- ✅ Lambda deployment packages signed (integrity check)
- ✅ S3 bucket for Lambda code has versioning and MFA delete
- ✅ IAM policy prevents unauthorized updates to Lambda code
- ✅ CloudTrail logs all Lambda function updates

**Residual Risk**: Very Low (requires AWS account compromise)

### Threat 5: DynamoDB Data Exfiltration

**Attack Vector**: Attacker reads incident history from DynamoDB

**Impact**: Exposure of incident details, alert patterns, service topology

**Mitigations**:
- ✅ IAM policies prevent read access (only the incident recorder and the runbook state machine can write, and the runbook can only read single items)
- ✅ DynamoDB encrypted at rest
- ✅ VPC endpoint for DynamoDB (traffic stays on AWS network)
- ✅ CloudTrail logs all DynamoDB API calls

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
