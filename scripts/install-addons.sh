#!/usr/bin/env bash
set -euo pipefail

# Installs AWS Load Balancer Controller, ExternalDNS, and cert-manager.
# Uses IRSA roles created by Terraform (annotates service accounts).

CLUSTER="$(terraform -chdir=terraform output -raw eks_cluster_name)"
REGION="$(terraform -chdir=terraform output -raw aws_region)"
ALB_ROLE_ARN="$(terraform -chdir=terraform output -raw alb_controller_role_arn)"
DNS_ROLE_ARN="$(terraform -chdir=terraform output -raw external_dns_role_arn)"

echo "Cluster: $CLUSTER  Region: $REGION"

helm repo add eks https://aws.github.io/eks-charts >/dev/null
helm repo add bitnami https://charts.bitnami.com/bitnami >/dev/null
helm repo add jetstack https://charts.jetstack.io >/dev/null
helm repo update >/dev/null

echo "[1/3] AWS Load Balancer Controller"
kubectl -n kube-system create serviceaccount aws-load-balancer-controller --dry-run=client -o yaml | kubectl apply -f -
kubectl -n kube-system annotate serviceaccount aws-load-balancer-controller eks.amazonaws.com/role-arn="$ALB_ROLE_ARN" --overwrite
helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller   -n kube-system   --set clusterName="$CLUSTER"   --set serviceAccount.create=false   --set serviceAccount.name=aws-load-balancer-controller

echo "[2/3] ExternalDNS (Route53)"
kubectl -n kube-system create serviceaccount external-dns --dry-run=client -o yaml | kubectl apply -f -
kubectl -n kube-system annotate serviceaccount external-dns eks.amazonaws.com/role-arn="$DNS_ROLE_ARN" --overwrite
helm upgrade --install external-dns bitnami/external-dns   -n kube-system   --set provider=aws   --set aws.region="$REGION"   --set serviceAccount.create=false   --set serviceAccount.name=external-dns

echo "[3/3] cert-manager"
kubectl get ns cert-manager >/dev/null 2>&1 || kubectl create ns cert-manager
helm upgrade --install cert-manager jetstack/cert-manager   -n cert-manager   --set installCRDs=true

echo "Add-ons installed."
