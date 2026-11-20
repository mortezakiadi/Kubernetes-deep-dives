#!/usr/bin/env bash
# Builds the agent image for linux/amd64 and pushes it to the project's ECR repository. Prints the full image reference.
# The tag is a hash of the application files, so the same code always gets the same tag and a changed file gets a new one.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1 AWS_PAGER=""
REPO="$(cd "$ROOT/terraform/10-cluster" && terraform output -raw ecr_repository)"
TAG="$(cd "$ROOT/app" && find . -type f -not -name '*.pyc' -not -path '*/__pycache__/*' | sort | xargs shasum -a 256 | shasum -a 256 | cut -c1-12)"
REGISTRY="${REPO%%/*}"
aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
if aws ecr describe-images --repository-name "${REPO#*/}" --image-ids imageTag="$TAG" >/dev/null 2>&1; then
  echo "image $TAG is already in ECR" >&2
else
  docker buildx build --platform linux/amd64 --load -t "$REPO:$TAG" "$ROOT/app" >&2
  docker push "$REPO:$TAG" >&2
fi
echo "$REPO:$TAG"
