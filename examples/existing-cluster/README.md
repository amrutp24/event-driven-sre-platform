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
- The RBAC manifest applied, so the group is allowed to remediate:

  ```bash
  kubectl apply -f ../../k8s/rbac/remediation-role.yaml
  ```

## Run it

```bash
terraform init
terraform apply \
  -var region=us-east-1 \
  -var cluster_name=my-cluster \
  -var vpc_id=vpc-0123456789abcdef0 \
  -var 'subnet_ids=["subnet-0123456789abcdef0","subnet-0fedcba9876543210"]' \
  -var cluster_security_group_id=sg-0123456789abcdef0

terraform output -raw alert_webhook_url
terraform output -raw alertmanager_api_key
```

Point Alertmanager's webhook receiver at the URL and send the key in the
`X-API-Key` header.
