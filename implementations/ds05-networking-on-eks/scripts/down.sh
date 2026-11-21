#!/usr/bin/env bash
# Removes everything up.sh created, in reverse order, then checks that nothing is left.
#   down.sh          asks for confirmation
#   down.sh --yes    does not ask
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export AWS_PAGER=""
if [ "${1:-}" != "--yes" ]; then
  read -r -p "This destroys both EKS clusters, the core network, every VPC and the DNS setup. Type 'destroy' to continue: " answer
  [ "$answer" = "destroy" ] || { echo "aborted"; exit 1; }
fi
(cd "$ROOT/terraform/30-platform" && terraform destroy -input=false -auto-approve) || true
(cd "$ROOT/terraform/20-clusters" && terraform destroy -input=false -auto-approve)
(cd "$ROOT/terraform/10-network" && terraform destroy -input=false -auto-approve)
"$ROOT/scripts/verify-clean.sh"
