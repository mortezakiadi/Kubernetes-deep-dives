#!/usr/bin/env bash
# Removes everything up.sh created, in reverse order, then checks that nothing is left.
#   down.sh          asks for confirmation
#   down.sh --yes    does not ask
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export AWS_REGION=us-east-2 AWS_DEFAULT_REGION=us-east-2 AWS_PAGER=""
if [ "${1:-}" != "--yes" ]; then
  read -r -p "This destroys the EKS cluster, both node groups (GPUs included), the VPC, NAT gateway, bucket and everything in them. Type 'destroy' to continue: " answer
  [ "$answer" = "destroy" ] || { echo "aborted"; exit 1; }
fi
if [ -f "$ROOT/.kube/config" ]; then
  # Stop every run first, so no pod is still writing to the bucket while it is emptied and deleted.
  K="$ROOT/.tools/kubectl"; export KUBECONFIG="$ROOT/.kube/config"
  $K -n rl-envs delete jobs --all --wait=true --timeout=180s 2>/dev/null || true
  # CSI snapshots and volumes are EBS objects outside Terraform: delete them through Kubernetes so the driver
  # removes them in AWS, and wait, before the cluster (and with it the driver) is destroyed.
  $K delete volumesnapshots --all -A --wait=true --timeout=600s 2>/dev/null || true
  $K delete pvc --all -A --wait=true --timeout=600s 2>/dev/null || true
  for i in $(seq 1 60); do [ -z "$($K get pv -o name 2>/dev/null)" ] && break; sleep 10; done
fi
if [ -f "$ROOT/terraform/20-platform/terraform.tfstate" ]; then
  (cd "$ROOT/terraform/20-platform" && terraform destroy -input=false -auto-approve)
fi
(cd "$ROOT/terraform/10-cluster" && terraform destroy -input=false -auto-approve)
"$ROOT/scripts/verify-clean.sh"
