#!/usr/bin/env bash
set -euo pipefail

# Production bootstrap: applies examples/complete, configures kubeconfig.
# Terraform zips the Lambda code itself, so there's no build step.
# Requires: aws cli, terraform, kubectl, helm.

echo "[1/3] Terraform init/apply"
terraform -chdir=examples/complete init
terraform -chdir=examples/complete apply -auto-approve

echo "[2/3] Configure kubectl"
CLUSTER="$(terraform -chdir=examples/complete output -raw eks_cluster_name)"
REGION="$(terraform -chdir=examples/complete output -raw aws_region)"
aws eks update-kubeconfig --name "$CLUSTER" --region "$REGION"

echo "[3/3] Cluster nodes"
kubectl get nodes
echo "Bootstrap complete. Next: ./scripts/install-addons.sh"
