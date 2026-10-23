#!/usr/bin/env bash
set -euo pipefail

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo update >/dev/null

WEBHOOK_URL="$(terraform -chdir=terraform output -raw alert_webhook_url)"
API_KEY="$(terraform -chdir=terraform output -raw alertmanager_api_key)"

echo "[1/5] Store the webhook API key for Alertmanager"
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic alertmanager-webhook-key -n monitoring \
  --from-literal=api-key="$API_KEY" --dry-run=client -o yaml | kubectl apply -f -

echo "[2/5] Install kube-prometheus-stack, sending alerts to $WEBHOOK_URL"
AM_VALUES="$(mktemp)"
trap 'rm -f "$AM_VALUES"' EXIT
sed "s#<WEBHOOK_URL>#${WEBHOOK_URL}#" helm/observability/alertmanager-values.yaml > "$AM_VALUES"
helm upgrade --install mon prometheus-community/kube-prometheus-stack \
  -f helm/observability/kube-prometheus-stack-values.yaml \
  -f "$AM_VALUES" \
  -n monitoring

echo "[3/5] Apply alert rules"
kubectl apply -n monitoring -f helm/observability/prom-rules/checkout-alerts.yaml
kubectl apply -n monitoring -f helm/observability/prom-rules/slo-burnrate.yaml

echo "[4/5] Deploy app"
helm upgrade --install checkout helm/checkout -n apps --create-namespace

echo "[5/5] Allow the runbook Lambda to remediate in apps"
kubectl apply -f k8s/rbac/remediation-role.yaml

echo "Deployed."
