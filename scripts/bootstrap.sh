#!/usr/bin/env bash
set -euo pipefail

# Production bootstrap: builds Lambda zips, applies Terraform, configures kubeconfig.
# Requires: aws cli, terraform, kubectl, helm, zip, python3.

echo "[1/4] Build Lambda packages"
bash terraform/lambda/build.sh
bash terraform/lambda/build_runbook_action.sh

echo "[2/4] Terraform init/apply"
terraform -chdir=terraform init
terraform -chdir=terraform apply -auto-approve

echo "[3/4] Configure kubectl"
CLUSTER="$(terraform -chdir=terraform output -raw eks_cluster_name)"
REGION="$(terraform -chdir=terraform output -raw aws_region)"
aws eks update-kubeconfig --name "$CLUSTER" --region "$REGION"

echo "[4/4] Cluster nodes"
kubectl get nodes
echo "Bootstrap complete. Next: ./scripts/install-addons.sh"
