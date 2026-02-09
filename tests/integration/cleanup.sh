#!/bin/bash

# Integration Test Cleanup Script for Event-Driven SRE Platform
# Cleans up LocalStack and mock Kubernetes environment

set -e

echo "🧹 Cleaning up integration test environment..."

# Stop and remove LocalStack container
echo "📦 Stopping LocalStack..."
docker stop localstack-sre 2>/dev/null || echo "LocalStack container not running"
docker rm localstack-sre 2>/dev/null || echo "LocalStack container not found"

# Delete kind cluster
echo "🏗️ Removing Kubernetes cluster..."
if kind get clusters | grep -q sre-integration; then
  kind delete cluster --name sre-integration
  echo "✅ Kubernetes cluster removed"
else
  echo "Kubernetes cluster not found"
fi

# Clean up any remaining Docker networks
echo "🌐 Cleaning up Docker networks..."
docker network prune -f 2>/dev/null || echo "No networks to clean"

# Clean up temp files
echo "📁 Cleaning up temporary files..."
rm -rf /tmp/localstack/data 2>/dev/null || echo "No temp data to clean"

echo "✅ Integration test environment cleaned up!"
echo ""
echo "🚀 Run setup again with:"
echo "  tests/integration/setup.sh"