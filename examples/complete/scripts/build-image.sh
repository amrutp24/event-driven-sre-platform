#!/usr/bin/env bash
set -euo pipefail

# Builds the checkout image and pushes it to the ECR repository Terraform made.
# Tags are immutable, so each build is tagged with the short git SHA, or
# $IMAGE_TAG if set. deploy.sh deploys the same tag.

# Run from examples/complete, wherever the script is called from
cd "$(dirname "$0")/.."

REPO="$(terraform output -raw checkout_image_repository)"
REGION="$(terraform output -raw aws_region)"
REGISTRY="${REPO%%/*}"

if [ -z "${IMAGE_TAG:-}" ] && ! git diff --quiet HEAD -- apps/checkout; then
  echo "apps/checkout has uncommitted changes. Commit them, or set IMAGE_TAG." >&2
  exit 1
fi
TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD)}"

echo "[1/3] Log in to $REGISTRY"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"

echo "[2/3] Build $REPO:$TAG"
# No provenance or SBOM attestations: Docker Desktop would push those under the
# same tag first, and the repo's immutable tags then refuse the image itself
docker build --platform linux/amd64 --provenance=false --sbom=false -t "$REPO:$TAG" apps/checkout

echo "[3/3] Push"
docker push "$REPO:$TAG"

echo "Pushed $REPO:$TAG"
