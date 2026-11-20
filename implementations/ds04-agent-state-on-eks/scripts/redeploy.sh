#!/usr/bin/env bash
# After changing the application code: build and push a new image, point the platform at it, and let the Deployments roll.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1 AWS_PAGER=""
IMAGE="$("$ROOT/scripts/build-image.sh")"
F="$ROOT/terraform/20-platform/local.auto.tfvars"
grep -v '^agent_image' "$F" > "$F.tmp" && printf 'agent_image = "%s"\n' "$IMAGE" >> "$F.tmp" && mv "$F.tmp" "$F"
cd "$ROOT/terraform/20-platform" && terraform apply -input=false -auto-approve
# The workers and the tester are created once and then managed by the scenarios, so they are rolled here.
export KUBECONFIG="$ROOT/.kube/config"
K="$ROOT/.tools/kubectl"
for d in worker-v1 worker-v2 tester; do $K -n agent-platform set image deploy/$d "$( [ $d = tester ] && echo main || echo worker )=$IMAGE" >/dev/null; done
