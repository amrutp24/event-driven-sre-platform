#!/usr/bin/env bash
set -euo pipefail

# Applies examples/complete and configures kubectl.
# Terraform zips the Lambda code itself, so there's no build step.
# Requires: aws cli, terraform, kubectl, helm.

# Run from examples/complete, wherever the script is called from
cd "$(dirname "$0")/.."

echo "[1/3] Terraform init/apply"
terraform init
terraform apply -auto-approve

echo "[2/3] Configure kubectl"
CLUSTER="$(terraform output -raw eks_cluster_name)"
REGION="$(terraform output -raw aws_region)"
aws eks update-kubeconfig --name "$CLUSTER" --region "$REGION"

echo "[3/3] Cluster nodes"
kubectl get nodes
echo "Bootstrap complete. Next: scripts/install-addons.sh"
