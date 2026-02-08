#!/bin/bash

# Integration Test Setup Script for Event-Driven SRE Platform
# Sets up LocalStack and mock Kubernetes environment for integration testing

set -e

echo "🚀 Setting up integration test environment..."

# Check dependencies
command -v docker >/dev/null 2>&1 || { echo "❌ Docker is required but not installed."; exit 1; }
command -v localstack >/dev/null 2>&1 || { echo "❌ LocalStack is required but not installed."; exit 1; }
command -v kubectl >/dev/null 2>&1 || { echo "❌ kubectl is required but not installed."; exit 1; }

# Start LocalStack
echo "📦 Starting LocalStack..."
docker run -d --rm \
  --name localstack-sre \
  -p 4566:4566 \
  -p 4510-4559:4510-4559 \
  -e SERVICES=lambda,events,stepfunctions,dynamodb,sns,apigateway,iam \
  -e DEBUG=1 \
  -e DATA_DIR=/tmp/localstack/data \
  localstack/localstack:latest

# Wait for LocalStack to be ready
echo "⏳ Waiting for LocalStack to be ready..."
timeout 60 bash -c 'until curl -f http://localhost:4566/health; do sleep 2; done' || {
  echo "❌ LocalStack failed to start within 60 seconds"
  exit 1
}

echo "✅ LocalStack is ready"

# Create mock Kubernetes environment using kind
echo "🏗️ Setting up mock Kubernetes environment..."
if ! command -v kind >/dev/null 2>&1; then
  echo "Installing kind..."
  curl -Lo ./kind https://kind.sigs.k8s.io/dl/v0.20.0/kind-linux-amd64
  chmod +x ./kind
  sudo mv ./kind /usr/local/bin/kind
fi

# Create kind cluster if it doesn't exist
if ! kind get clusters | grep -q sre-integration; then
  kind create cluster --name sre-integration --wait 300s
  echo "✅ Kubernetes cluster created"
else
  echo "✅ Kubernetes cluster already exists"
fi

# Set up kubectl context
kubectl cluster-info --context kind-sre-integration

# Create test namespace and deployment
echo "📋 Setting up test Kubernetes resources..."
kubectl create namespace sre-test --dry-run=client -o yaml | kubectl apply -f -

# Deploy mock checkout service
kubectl apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: checkout
  namespace: sre-test
  labels:
    app: checkout
spec:
  replicas: 3
  selector:
    matchLabels:
      app: checkout
  template:
    metadata:
      labels:
        app: checkout
    spec:
      containers:
      - name: checkout
        image: nginx:alpine
        ports:
        - containerPort: 80
        env:
        - name: DEGRADED_MODE
          value: "false"
        - name: ERROR_RATE
          value: "0.0"
        - name: LATENCY_MS
          value: "0"
---
apiVersion: v1
kind: Service
metadata:
  name: checkout
  namespace: sre-test
  labels:
    app: checkout
spec:
  selector:
    app: checkout
  ports:
  - port: 80
    targetPort: 80
  type: ClusterIP
EOF

echo "🔧 Configuring IAM role for Lambda functions..."
# Create test IAM role for Lambda functions (simplified for LocalStack)
aws --endpoint-url=http://localhost:4566 iam create-role \
  --role-name test-lambda-role \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole"
      }
    ]
  }' || echo "IAM role might already exist"

echo "🧪 Installing test dependencies..."
cd tests/integration
pip install -r requirements-dev.txt

echo "✅ Integration test environment is ready!"
echo ""
echo "📊 Environment Summary:"
echo "  LocalStack: http://localhost:4566"
echo "  Kubernetes: kind-sre-integration"
echo "  Test Namespace: sre-test"
echo ""
echo "🧪 Run integration tests with:"
echo "  AWS_DEFAULT_REGION=us-east-1 python -m pytest tests/integration/ -v"
echo ""
echo "🧹 Cleanup with:"
echo "  docker stop localstack-sre"
echo "  kind delete cluster --name sre-integration"