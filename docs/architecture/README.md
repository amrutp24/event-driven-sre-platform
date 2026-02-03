# Event-Driven SRE Platform - Architecture Documentation

## Overview

This document provides a comprehensive view of the event-driven SRE platform architecture, showing how alerts flow through the system from detection to automated remediation.

## Table of Contents

1. [High-Level Architecture](#high-level-architecture)
2. [Alert Lifecycle Flow](#alert-lifecycle-flow)
3. [Incident Response Sequence](#incident-response-sequence)
4. [Security Architecture](#security-architecture)
5. [Component Details](#component-details)

---

## High-Level Architecture

```mermaid
graph TB
    subgraph "User Traffic & Edge"
        Users[Users/Clients]
        R53[Route53 DNS]
        CF[CloudFront + WAF]
    end

    subgraph "AWS VPC"
        ALB[Application Load Balancer<br/>AWS Load Balancer Controller]

        subgraph "Amazon EKS Cluster"
            subgraph "apps namespace"
                APP[Checkout Service Pods<br/>Flask + Gunicorn<br/>/checkout /metrics /healthz]
            end

            subgraph "monitoring namespace"
                PROM[Prometheus Operator<br/>Metrics Storage + TSDB]
                AM[Alertmanager<br/>Alert Routing]
                GRAF[Grafana<br/>Dashboards + Visualization]
                SM[ServiceMonitor<br/>Scrape Config]
            end
        end

        subgraph "Event Pipeline"
            APIGW[API Gateway<br/>Alert Webhook Endpoint]
            L1[alert_ingest Lambda<br/>Validation + Enrichment]
            EB[EventBridge<br/>Event Router]
            SF[Step Functions<br/>Runbook Orchestration]
            L2[runbook_action Lambda<br/>K8s Remediation]
        end

        subgraph "Storage & Notifications"
            DDB[(DynamoDB<br/>Incident Records)]
            SNS[SNS<br/>Notifications]
            CW[CloudWatch Logs<br/>Audit Trail]
        end
    end

    subgraph "External Monitoring"
        DD[Datadog<br/>Logs + APM + SLOs]
    end

    %% Traffic Flow
    Users -->|HTTPS| R53
    R53 --> CF
    CF -->|TLS| ALB
    ALB -->|HTTP| APP

    %% Observability Flow
    APP -->|Expose Metrics| SM
    SM -->|Scrape :8080/metrics| PROM
    PROM -->|Query| GRAF
    PROM -->|Alert Rules| AM
    APP -->|Logs + Traces| DD

    %% Alert Flow
    AM -->|Webhook POST| APIGW
    DD -->|Monitor Alerts| APIGW
    APIGW -->|Invoke| L1
    L1 -->|Put Events| EB

    %% Event Routing
    EB -->|Route: incident.created| DDB
    EB -->|Route: incident.created| SNS
    EB -->|Route: incident.created| SF
    EB -->|Log All Events| CW

    %% Remediation Flow
    SF -->|Execute Action| L2
    L2 -->|IRSA + RBAC| APP
    L2 -->|Update SSM| SSM[SSM Parameter Store<br/>Feature Flags]
    SSM -.->|Read Config| APP

    %% Styling
    classDef aws fill:#FF9900,stroke:#232F3E,stroke-width:2px,color:#fff
    classDef k8s fill:#326CE5,stroke:#fff,stroke-width:2px,color:#fff
    classDef storage fill:#3F8624,stroke:#fff,stroke-width:2px,color:#fff
    classDef external fill:#632CA6,stroke:#fff,stroke-width:2px,color:#fff

    class APIGW,L1,L2,EB,SF,ALB,R53,CF,SNS,CW aws
    class APP,PROM,AM,GRAF,SM k8s
    class DDB,SSM storage
    class DD external
```

---

## Alert Lifecycle Flow

This diagram shows the numbered sequence of events from alert detection through automated remediation.

```mermaid
flowchart TD
    Start([Alert Condition Detected]) --> Step1

    Step1[1. Prometheus evaluates alert rules<br/>PromQL: rate errors > 5%] --> Step2
    Step2[2. Alert enters FIRING state<br/>Alertmanager receives alert] --> Step3
    Step3[3. Alertmanager routes alert<br/>Applies labels + annotations] --> Step4

    Step4[4. POST webhook to API Gateway<br/>JSON payload with alert details] --> Step5
    Step5[5. alert_ingest Lambda triggered<br/>Validates schema + enriches metadata] --> Step6

    Step6{Alert Valid?} -->|No| Reject[Log error + return 400]
    Step6 -->|Yes| Step7

    Step7[6. Normalize alert format<br/>Add: timestamp, severity, context] --> Step8
    Step8[7. Put event to EventBridge<br/>Source: sre.alertmanager] --> Step9

    Step9[8. EventBridge routes to 4 targets] --> Multi{Fan-out}

    Multi --> Target1[8a. DynamoDB PutItem<br/>Store incident record]
    Multi --> Target2[8b. SNS Publish<br/>Notify on-call engineer]
    Multi --> Target3[8c. CloudWatch Logs<br/>Audit trail]
    Multi --> Target4[8d. Step Functions StartExecution<br/>Begin runbook automation]

    Target4 --> Step10[9. Step Functions orchestrates remediation<br/>Evaluates alert severity + labels]

    Step10 --> Decision{Runbook Available?}
    Decision -->|No| Manual[Escalate to human<br/>Log to incident channel]
    Decision -->|Yes| Step11

    Step11[10. Invoke runbook_action Lambda<br/>Pass: namespace, deployment, action] --> Step12
    Step12[11. Lambda authenticates to EKS<br/>IRSA: AssumeRoleWithWebIdentity] --> Step13
    Step13[12. Execute remediation action] --> ActionType{Action Type}

    ActionType -->|Scale| Scale[kubectl scale deployment<br/>Increase replicas]
    ActionType -->|Restart| Restart[kubectl rollout restart<br/>Rolling restart pods]
    ActionType -->|Degrade| Degrade[Update SSM parameter<br/>Enable degraded mode]
    ActionType -->|Drain| Drain[kubectl drain node<br/>Evict pods safely]

    Scale --> Step14
    Restart --> Step14
    Degrade --> Step14
    Drain --> Step14

    Step14[13. Update incident status<br/>DynamoDB: status=remediated] --> Step15
    Step15[14. Verify alert resolves<br/>Wait for RESOLVED state] --> Step16

    Step16{Alert Resolved?} -->|Yes| Success([MTTR: seconds<br/>Incident closed])
    Step16 -->|No after 5min| Escalate([Escalate to on-call<br/>Manual intervention needed])

    Reject --> End([End: Alert dropped])
    Manual --> End
    Success --> End
    Escalate --> End

    classDef detect fill:#FF6B6B,stroke:#C92A2A,stroke-width:2px,color:#fff
    classDef process fill:#4ECDC4,stroke:#0B7285,stroke-width:2px,color:#fff
    classDef remediate fill:#95E1D3,stroke:#087F5B,stroke-width:2px,color:#000
    classDef result fill:#FFE66D,stroke:#E67700,stroke-width:2px,color:#000

    class Step1,Step2,Step3 detect
    class Step4,Step5,Step6,Step7,Step8,Step9,Multi,Target1,Target2,Target3,Target4 process
    class Step10,Step11,Step12,Step13,Step14,ActionType,Scale,Restart,Degrade,Drain remediate
    class Step15,Step16,Success,Escalate result
```

---

## Incident Response Sequence

This sequence diagram shows the interactions between components during an incident.

```mermaid
sequenceDiagram
    participant App as Checkout Service
    participant Prom as Prometheus
    participant AM as Alertmanager
    participant APIGW as API Gateway
    participant L1 as alert_ingest Lambda
    participant EB as EventBridge
    participant DDB as DynamoDB
    participant SNS as SNS Topic
    participant SF as Step Functions
    participant L2 as runbook_action Lambda
    participant K8s as Kubernetes API
    participant SSM as SSM Parameter Store

    Note over App,Prom: Detection Phase
    App->>Prom: Expose metrics at /metrics<br/>error_rate=0.08 (8%)
    Prom->>Prom: Evaluate alert rule every 30s<br/>RULE: error_rate > 5%
    Prom->>AM: Fire alert: CheckoutHighErrorRate

    Note over AM,APIGW: Notification Phase
    AM->>AM: Apply routing rules<br/>Match: severity=critical
    AM->>APIGW: POST /webhook<br/>JSON: {alert_name, labels, annotations}

    Note over APIGW,EB: Ingestion Phase
    APIGW->>L1: Invoke with event payload
    L1->>L1: Validate JSON schema<br/>Enrich: account_id, region, timestamp
    L1->>EB: PutEvents: source=sre.alertmanager<br/>detail-type=AlertFired
    L1-->>APIGW: Return 202 Accepted
    APIGW-->>AM: Return 200 OK

    Note over EB,SNS: Fan-out Phase
    par Parallel Targets
        EB->>DDB: PutItem: incident record<br/>{id, status=open, timestamp}
        EB->>SNS: Publish notification<br/>Alert on-call engineer
        EB->>SF: StartExecution: incident-response-runbook
    end

    Note over SF,K8s: Remediation Phase
    SF->>SF: Evaluate runbook logic<br/>IF severity=critical THEN auto-remediate
    SF->>L2: Invoke with task input<br/>{action: enable_degraded_mode}
    L2->>K8s: Authenticate via IRSA<br/>AssumeRole: checkout-remediation-role
    L2->>SSM: PutParameter: /checkout/degraded_mode=true
    SSM-->>L2: Success
    L2->>K8s: Get Deployment: apps/checkout
    K8s-->>L2: Deployment manifest
    L2->>K8s: Patch Deployment: restart annotation
    K8s-->>L2: Success: rollout triggered
    L2-->>SF: Return: {status: success, action: completed}

    Note over SF,DDB: Verification Phase
    SF->>DDB: UpdateItem: status=remediating
    SF->>SF: Wait 60 seconds for metrics to stabilize

    Note over App,Prom: Recovery Verification
    App->>App: Read SSM parameter<br/>degraded_mode=true
    App->>App: Skip optional operations<br/>Reduced error rate
    App->>Prom: Expose metrics<br/>error_rate=0.01 (1%)
    Prom->>AM: Resolve alert: CheckoutHighErrorRate
    AM->>APIGW: POST /webhook<br/>status=resolved
    APIGW->>L1: Process resolved event
    L1->>EB: PutEvents: AlertResolved
    EB->>DDB: UpdateItem: status=resolved<br/>resolved_at=timestamp

    Note over SNS,DDB: Completion
    EB->>SNS: Publish: Incident auto-resolved<br/>MTTR: 90 seconds
```

---

## Security Architecture

```mermaid
graph TB
    subgraph "IAM Roles & Policies"
        R1[alert_ingest Lambda Role<br/>Permissions:<br/>- eventbridge:PutEvents]
        R2[runbook_action Lambda Role<br/>Permissions:<br/>- eks:DescribeCluster<br/>- ssm:GetParameter<br/>- ssm:PutParameter<br/>- sts:AssumeRole]
        R3[EventBridge Role<br/>Permissions:<br/>- dynamodb:PutItem<br/>- sns:Publish<br/>- states:StartExecution<br/>- logs:PutLogEvents]
        R4[Step Functions Role<br/>Permissions:<br/>- lambda:InvokeFunction<br/>- dynamodb:UpdateItem]
    end

    subgraph "IRSA (IAM Roles for Service Accounts)"
        SA[Kubernetes ServiceAccount<br/>checkout-remediation<br/>Namespace: apps]
        OIDC[EKS OIDC Provider<br/>Federated Identity]
        Trust[Trust Policy:<br/>AssumeRoleWithWebIdentity]
    end

    subgraph "Kubernetes RBAC"
        Role[Role: checkout-remediation<br/>Namespace: apps<br/>Permissions:<br/>- get/patch deployments<br/>- list pods]
        RB[RoleBinding<br/>ServiceAccount -> Role]
    end

    subgraph "Network Security"
        SG1[Security Group: EKS Nodes<br/>Ingress: ALB only<br/>Egress: Internet via NAT]
        SG2[Security Group: ALB<br/>Ingress: 443 from CloudFront<br/>Egress: EKS nodes]
        SG3[Security Group: VPC Endpoints<br/>Ingress: EKS nodes<br/>Private Link for AWS APIs]
    end

    subgraph "Data Security"
        KMS[KMS Key<br/>Encrypt:<br/>- DynamoDB tables<br/>- SNS topics<br/>- CloudWatch Logs<br/>- EKS secrets]
        SSM_Sec[SSM Parameter Store<br/>SecureString type<br/>Encrypted with KMS]
    end

    R2 -->|AssumeRole| OIDC
    OIDC -->|Federate| SA
    SA -->|Bound by| RB
    RB -->|Grants| Role

    R1 -.->|Least Privilege| R3
    R3 -.->|Least Privilege| R4
    R4 -.->|Invokes| R2

    SG2 -->|Allow 443| SG1
    SG1 -->|Private| SG3

    KMS -.->|Encrypts| SSM_Sec

    classDef iam fill:#DD344C,stroke:#fff,stroke-width:2px,color:#fff
    classDef k8s fill:#326CE5,stroke:#fff,stroke-width:2px,color:#fff
    classDef network fill:#FF9900,stroke:#232F3E,stroke-width:2px,color:#fff
    classDef encryption fill:#5C6AC4,stroke:#fff,stroke-width:2px,color:#fff

    class R1,R2,R3,R4,OIDC,Trust iam
    class SA,Role,RB k8s
    class SG1,SG2,SG3 network
    class KMS,SSM_Sec encryption
```

---

## Component Details

### 1. Detection Layer

**Prometheus Operator**
- Scrapes metrics from `/metrics` endpoint every 15 seconds
- Evaluates PromQL alert rules every 30 seconds
- Alert rules defined in PrometheusRule CRD
- TSDB retention: 15 days
- High availability: 2 replicas with remote write to external storage

**Alertmanager**
- Groups related alerts (reduces noise)
- Routes alerts based on severity labels
- Deduplicates alerts (5-minute window)
- Webhook integration with API Gateway
- Retry logic: 3 attempts with exponential backoff

**Datadog Monitoring** (Optional)
- APM traces for distributed tracing
- Log aggregation with structured logging
- Custom SLO monitors (availability, latency, error budget)
- Synthetics for external endpoint monitoring

### 2. Ingestion Layer

**API Gateway**
- REST API endpoint: `POST /webhook`
- Request validation: JSON schema enforcement
- Throttling: 1000 requests/second
- Authentication: API key (optional) or VPC endpoint
- Integration: Lambda proxy integration

**alert_ingest Lambda**
- Runtime: Python 3.11
- Memory: 256 MB
- Timeout: 30 seconds
- Concurrency: 100 reserved concurrent executions
- Functions:
  - Validate alert payload schema
  - Enrich with AWS metadata (account, region, timestamp)
  - Normalize alert format across sources (Alertmanager, Datadog)
  - Transform to EventBridge event format
  - Error handling: invalid alerts logged to CloudWatch

### 3. Event Routing Layer

**EventBridge**
- Custom event bus: `sre-platform-events`
- Event sources: `sre.alertmanager`, `sre.datadog`
- Event patterns for routing:
  - `incident.created` -> DynamoDB, SNS, Step Functions
  - `incident.resolved` -> DynamoDB, SNS
  - `action.completed` -> DynamoDB
- Archive: 7-day event replay capability
- Dead-letter queue: Capture failed events for retry

### 4. Orchestration Layer

**Step Functions**
- State machine: `incident-response-runbook`
- Steps:
  1. **Evaluate Alert**: Check severity and runbook eligibility
  2. **Log Incident**: Update DynamoDB status
  3. **Choose Action**: Map alert type to remediation action
  4. **Execute Action**: Invoke runbook_action Lambda
  5. **Wait for Stabilization**: 60-second delay
  6. **Verify Resolution**: Check if alert resolved
  7. **Update Status**: Mark incident as resolved or escalated
- Error handling: Exponential retry with 3 attempts
- Execution history: 90 days

**runbook_action Lambda**
- Runtime: Python 3.11
- Memory: 512 MB
- Timeout: 120 seconds
- VPC: Attached to private subnets (access to EKS API)
- Functions:
  - **enable_degraded_mode**: Update SSM parameter + rolling restart
  - **scale_deployment**: Increase replica count
  - **restart_deployment**: Force pod recreation
  - **drain_node**: Safely evict pods from unhealthy node
- IRSA: Assumes Kubernetes ServiceAccount via OIDC
- RBAC: Limited to `apps` namespace, `deployments` resource

### 5. Storage & Notification Layer

**DynamoDB**
- Table: `sre-incidents`
- Primary key: `incident_id` (UUID)
- Sort key: `timestamp` (ISO 8601)
- Attributes:
  - `alert_name`: String
  - `severity`: String (critical, warning, info)
  - `status`: String (open, remediating, resolved, escalated)
  - `labels`: Map
  - `actions_taken`: List
  - `resolved_at`: Number (Unix timestamp)
- Encryption: KMS
- Point-in-time recovery: Enabled
- TTL: 90 days (auto-delete old incidents)

**SNS Topic**
- Topic: `sre-incident-notifications`
- Subscriptions:
  - Email: On-call engineer
  - Slack webhook: Incident channel
  - PagerDuty: Critical alerts only (filtered)
- Message filtering: Severity-based routing
- Delivery retry: 3 attempts over 10 minutes

**CloudWatch Logs**
- Log groups:
  - `/aws/lambda/alert-ingest`
  - `/aws/lambda/runbook-action`
  - `/aws/states/incident-response-runbook`
- Retention: 30 days
- Insights queries: Pre-built dashboards for MTTR, alert volume
- Metric filters: Extract custom metrics (remediation success rate)

### 6. Application Layer

**Checkout Service**
- Framework: Flask + Gunicorn
- Replicas: 3 (horizontal pod autoscaling)
- Resources:
  - Requests: 100m CPU, 128Mi memory
  - Limits: 500m CPU, 512Mi memory
- Health checks:
  - Liveness: `/healthz` (timeout 5s)
  - Readiness: `/healthz` (failure threshold 3)
- Metrics: Prometheus client library
  - Counters: `checkout_requests_total`, `checkout_errors_total`
  - Histograms: `checkout_request_duration_seconds`
  - Gauges: `checkout_degraded_mode_enabled`
- Chaos engineering:
  - `LATENCY_MS`: Inject artificial latency
  - `ERROR_RATE`: Random error injection (0.0-1.0)
  - `DEGRADED_MODE`: Disable optional features

---

## Key Design Decisions

### 1. Serverless Event Pipeline
**Decision**: Use Lambda + EventBridge instead of EKS-hosted services
**Rationale**:
- Lower operational overhead (no pods to manage)
- Auto-scaling without capacity planning
- Pay-per-use pricing for bursty alert traffic
- Built-in retry and error handling

**Trade-offs**:
- Cold start latency (mitigated with reserved concurrency)
- Vendor lock-in to AWS
- Debugging across distributed services

### 2. IRSA for Kubernetes Access
**Decision**: Use IAM Roles for Service Accounts instead of long-lived credentials
**Rationale**:
- No secrets to rotate or leak
- Temporary credentials (1-hour expiration)
- Audit trail in CloudTrail
- Fine-grained permissions per namespace

**Trade-offs**:
- Complex initial setup (OIDC provider)
- Requires EKS 1.13+

### 3. EventBridge as Central Bus
**Decision**: Route all events through EventBridge instead of direct integrations
**Rationale**:
- Decouples producers and consumers
- Easy to add new targets without code changes
- Built-in event filtering and transformation
- Event replay for debugging

**Trade-offs**:
- Additional latency (10-50ms)
- Event payload size limit (256 KB)

### 4. Step Functions for Orchestration
**Decision**: Use Step Functions instead of custom Lambda orchestration
**Rationale**:
- Visual workflow editor
- Built-in error handling and retries
- State persistence (no external database needed)
- Execution history for compliance

**Trade-offs**:
- Cost at high scale ($25 per 1M state transitions)
- Limited to 25,000 execution history
- ASL (Amazon States Language) learning curve

---

## Observability & Monitoring

### Metrics Collected
- **Application metrics**: Request rate, error rate, latency (p50, p95, p99)
- **Pipeline metrics**: Alert ingestion rate, remediation success rate, MTTR
- **Infrastructure metrics**: Pod CPU/memory, node capacity, Lambda duration

### Dashboards
- **SRE Dashboard**: Real-time incident status, alert volume, remediation actions
- **Application Dashboard**: Service health, error rate trends, latency heatmaps
- **Cost Dashboard**: Lambda invocations, EventBridge events, DynamoDB consumption

### Alerts on the Alerting System
- **Meta-monitoring**: Alert if alert_ingest Lambda errors > 1%
- **Pipeline health**: Alert if EventBridge delivery failures > 5%
- **Remediation failures**: Alert if runbook_action Lambda fails 3 times in 10 minutes

---

## Disaster Recovery

### Failure Scenarios

**Scenario 1: alert_ingest Lambda fails**
- Impact: Alerts not ingested, no remediation
- Detection: CloudWatch alarm on Lambda errors
- Mitigation: Alertmanager retries webhook (3 attempts), DLQ captures failed events
- Recovery: Replay events from EventBridge archive

**Scenario 2: EKS API unreachable**
- Impact: Remediation actions fail
- Detection: runbook_action Lambda timeout
- Mitigation: Step Functions retry (3 attempts, exponential backoff)
- Escalation: SNS notification to on-call after retries exhausted

**Scenario 3: EventBridge target failure**
- Impact: Incidents not logged to DynamoDB
- Detection: CloudWatch metric filter on failed PutItem
- Mitigation: EventBridge DLQ captures failed events
- Recovery: Reprocess events from DLQ

**Scenario 4: DynamoDB table deleted**
- Impact: No incident history
- Detection: Terraform state drift
- Mitigation: Point-in-time recovery (restore from backup)
- Prevention: Enable deletion protection on table

---

## Scalability Considerations

### Current Limits
- Alert ingestion: 1000 alerts/second (API Gateway throttle)
- Concurrent remediations: 100 (Lambda reserved concurrency)
- Event throughput: 10,000 events/second (EventBridge soft limit)
- Incident storage: Unlimited (DynamoDB auto-scaling)

### Scaling Strategies
1. **Horizontal scaling**: Increase Lambda concurrency
2. **Batching**: Group alerts in 5-second windows to reduce invocations
3. **Caching**: Cache EKS cluster info in Lambda environment variables
4. **Sharding**: Separate event buses per region or environment

---

## Cost Optimization

### Monthly Cost Estimate (1000 alerts/day)
- API Gateway: 30M requests × $3.50/M = $105
- Lambda (alert_ingest): 30M invocations × 100ms × $0.20/M = $6
- Lambda (runbook_action): 1000 invocations × 5s × $0.20/M = $0.10
- EventBridge: 30M events × $1.00/M = $30
- Step Functions: 1000 executions × 5 transitions × $25/M = $0.13
- DynamoDB: 1000 writes/day × $1.25/M = $0.04
- SNS: 1000 notifications × $0.50/M = $0.0005
- **Total: ~$141/month**

### Optimization Techniques
- Use VPC endpoints to avoid API Gateway costs (NAT Gateway cheaper at high volume)
- Reduce Lambda memory if not needed (alert_ingest can use 128 MB)
- Use EventBridge input transformers to reduce Step Functions state transitions
- Archive old DynamoDB incidents to S3 (Glacier for long-term storage)

---

## Security Best Practices Implemented

- ✅ Least-privilege IAM policies (no wildcards for application resources)
- ✅ Encryption at rest (KMS for all data stores)
- ✅ Encryption in transit (TLS 1.2+ for all endpoints)
- ✅ IRSA instead of long-lived credentials
- ✅ Kubernetes RBAC (namespace-scoped roles)
- ✅ VPC security groups (allow-list only)
- ✅ CloudTrail logging (API audit trail)
- ✅ Secrets management (SSM Parameter Store with KMS)
- ✅ Container image scanning (ECR scan on push)
- ✅ Network segmentation (private subnets for EKS)

---

## Future Enhancements

### Short-term (Next Sprint)
- Add more runbook actions (canary deployment, traffic shift)
- Implement alert correlation (group related alerts)
- Add Slack bot for interactive remediation approval
- Create Terraform module for multi-region deployment

### Medium-term (Next Quarter)
- Machine learning for anomaly detection
- Automated rollback if remediation causes further degradation
- SLO-based alerting (error budget exhaustion)
- Cost anomaly detection and auto-scaling

### Long-term (Next Year)
- Multi-cloud support (GCP, Azure)
- Chaos engineering platform integration (Gremlin, Chaos Mesh)
- Self-healing infrastructure (auto-replace unhealthy nodes)
- Predictive alerting (forecast incidents before they occur)

---

## References

- [AWS Lambda Best Practices](https://docs.aws.amazon.com/lambda/latest/dg/best-practices.html)
- [EKS IRSA Documentation](https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html)
- [EventBridge Event Patterns](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-event-patterns.html)
- [Prometheus Alert Rules](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/)
- [Step Functions Error Handling](https://docs.aws.amazon.com/step-functions/latest/dg/concepts-error-handling.html)

---

**Last Updated**: 2026-02-02
**Maintained By**: SRE Platform Team
