#!/usr/bin/env bash
set -euo pipefail

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo update >/dev/null

echo "[1/4] Install kube-prometheus-stack"
helm upgrade --install mon prometheus-community/kube-prometheus-stack   -f helm/observability/kube-prometheus-stack-values.yaml   -n monitoring --create-namespace

echo "[2/4] Apply alert rules"
kubectl apply -n monitoring -f helm/observability/prom-rules/checkout-alerts.yaml
kubectl apply -n monitoring -f helm/observability/prom-rules/slo-burnrate.yaml

echo "[3/4] Deploy app"
helm upgrade --install checkout helm/checkout -n apps --create-namespace

echo "[4/4] Allow the runbook Lambda to remediate in apps"
kubectl apply -f k8s/rbac/remediation-role.yaml

echo "Deployed."
