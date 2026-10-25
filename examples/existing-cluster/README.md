# Existing cluster

Adds the alert-to-remediation pipeline to an EKS cluster you already have.

## What it needs

- An EKS cluster with access entries turned on (authentication mode `API` or
  `API_AND_CONFIG_MAP`). The module gives the runbook_action Lambda's role an
  access entry in the `sre-remediation` group.
- Private subnets in the cluster's VPC that can reach the cluster's API
  endpoint. The Lambda runs there and also needs a way out to AWS APIs, through
  a NAT gateway or VPC endpoints.
- The cluster security group ID. The module adds a rule that lets the Lambda in
  on 443.
- The RBAC applied after the apply, so the group is allowed to remediate
  (see below).

## Run it

```bash
terraform init
terraform apply \
  -var region=us-east-1 \
  -var cluster_name=my-cluster \
  -var vpc_id=vpc-0123456789abcdef0 \
  -var 'subnet_ids=["subnet-0123456789abcdef0","subnet-0fedcba9876543210"]' \
  -var cluster_security_group_id=sg-0123456789abcdef0   -var 'remediation_namespaces=["apps"]'   -var 'runbook_routes={ MyAppDown = { action = "restart" } }'

# Let the Lambda's group act in remediation_namespaces
terraform output -raw rbac_manifest | kubectl apply -f -

terraform output -raw alert_webhook_url
terraform output -raw alertmanager_api_key
```

`runbook_routes` maps alert names to actions. Without it nothing is
remediated: critical alerts are recorded, notified and escalated to a person.
`MyAppDown` above is a placeholder for one of your own alerts.

Point Alertmanager's webhook receiver at the URL and send the key in the
`X-API-Key` header.

## Notifications

This example doesn't subscribe anyone. Add `notification_emails` to the
module call for email; each address has to click the link in a confirmation
email from SNS before anything arrives. For Slack or Teams, set
`create_chat_topic = true` and subscribe an Amazon Q Developer in chat
applications channel configuration to the module's `chat_topic_arn`.
`examples/complete/chatbot.tf` shows the Slack one.
