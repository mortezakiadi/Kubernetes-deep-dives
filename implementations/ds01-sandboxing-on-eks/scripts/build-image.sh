#!/usr/bin/env bash
# Build the platform image for linux/amd64 and push it to ECR.
# Terraform calls this (see terraform/20-platform/image.tf); it can also be run by hand.
#
#   build-image.sh <account_id> <region> <repository> <tag>
set -euo pipefail

ACCOUNT_ID="$1"; REGION="$2"; REPO="$3"; TAG="$4"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
IMAGE="${REGISTRY}/${REPO}:${TAG}"

# Skip the build when this exact tag is already in the registry.
if aws ecr describe-images --region "$REGION" --repository-name "$REPO" --image-ids imageTag="$TAG" >/dev/null 2>&1; then
  echo "image ${REPO}:${TAG} already in ECR, nothing to build"
  exit 0
fi

aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
docker buildx build --platform linux/amd64 --provenance=false --push -t "$IMAGE" "$HERE/platform"
docker logout "$REGISTRY" >/dev/null 2>&1 || true
echo "pushed ${REPO}:${TAG}"
