#!/usr/bin/env bash
set -euo pipefail

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo update >/dev/null

WEBHOOK_URL="$(terraform -chdir=examples/complete output -raw alert_webhook_url)"
API_KEY="$(terraform -chdir=examples/complete output -raw alertmanager_api_key)"
CHECKOUT_ROLE_ARN="$(terraform -chdir=examples/complete output -raw checkout_role_arn)"
REGION="$(terraform -chdir=examples/complete output -raw aws_region)"
IMAGE_REPO="$(terraform -chdir=examples/complete output -raw checkout_image_repository)"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD)}"

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
kubectl apply -n monitoring -f helm/observability/prom-rules/node-alerts.yaml

echo "[4/5] Deploy app"
# The service account's IRSA role lets the app read its degraded-mode flag from SSM.
# The image tag has to be pushed already, by scripts/build-image.sh.
helm upgrade --install checkout helm/checkout -n apps --create-namespace \
  --set image.repository="$IMAGE_REPO" \
  --set image.tag="$IMAGE_TAG" \
  --set serviceAccount.roleArn="$CHECKOUT_ROLE_ARN" \
  --set env.AWS_REGION="$REGION"

echo "[5/5] Allow the runbook Lambda to remediate in apps"
kubectl apply -f k8s/rbac/remediation-role.yaml

echo "Deployed."
