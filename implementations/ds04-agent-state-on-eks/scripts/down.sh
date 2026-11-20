#!/usr/bin/env bash
# Removes everything up.sh created, in reverse order, then checks that nothing is left.
#   down.sh          asks for confirmation
#   down.sh --yes    does not ask
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1 AWS_PAGER=""
if [ "${1:-}" != "--yes" ]; then
  read -r -p "This destroys the EKS cluster, the database, the VPC, NAT gateway, bucket and everything in them. Type 'destroy' to continue: " answer
  [ "$answer" = "destroy" ] || { echo "aborted"; exit 1; }
fi
if [ -f "$ROOT/.kube/config" ]; then
  # Remove the sandboxes and workers first so no node is held by a pod when the cluster goes.
  KUBECONFIG="$ROOT/.kube/config" "$ROOT/.tools/kubectl" -n sandboxes delete pods --all --wait=false 2>/dev/null || true
fi
(cd "$ROOT/terraform/20-platform" && terraform destroy -input=false -auto-approve)
(cd "$ROOT/terraform/10-cluster" && terraform destroy -input=false -auto-approve)
"$ROOT/scripts/verify-clean.sh"
