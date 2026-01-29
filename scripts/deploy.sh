#!/usr/bin/env bash
set -euo pipefail

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo update >/dev/null

echo "[1/3] Install kube-prometheus-stack"
helm upgrade --install mon prometheus-community/kube-prometheus-stack   -f helm/observability/kube-prometheus-stack-values.yaml   -n monitoring --create-namespace

echo "[2/3] Apply alert rules"
kubectl apply -n monitoring -f helm/observability/prom-rules/checkout-alerts.yaml
kubectl apply -n monitoring -f helm/observability/prom-rules/slo-burnrate.yaml

echo "[3/3] Deploy app"
helm upgrade --install checkout helm/checkout -n apps --create-namespace

echo "Deployed."
