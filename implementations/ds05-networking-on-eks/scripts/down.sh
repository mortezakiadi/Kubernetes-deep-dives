#!/usr/bin/env bash
# Removes everything up.sh created, in reverse order, then checks that nothing is left.
#   down.sh          asks for confirmation
#   down.sh --yes    does not ask
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export AWS_PAGER=""
if [ "${1:-}" != "--yes" ]; then
  read -r -p "This destroys both EKS clusters, the core network, every VPC and the DNS setup. Type 'destroy' to continue: " answer
  [ "$answer" = "destroy" ] || { echo "aborted"; exit 1; }
fi
K="$ROOT/.tools/kubectl"
export KUBECONFIG="$ROOT/.kube/config"
if [ -f "$KUBECONFIG" ]; then
  echo "== asking the controllers to remove what they created (load balancers, Lattice services), while they still run"
  $K --context main -n shop delete httproute,iamauthpolicy web --ignore-not-found --wait=true --timeout=120s 2>/dev/null || true
  for ctx in main second; do $K --context $ctx -n shop delete ingress web --ignore-not-found --wait=true --timeout=120s 2>/dev/null || true; done
  for i in $(seq 1 30); do
    n=$(aws vpc-lattice list-services --region us-east-1 --query 'length(items)' --output text 2>/dev/null || echo 0)
    a=$(aws elbv2 describe-load-balancers --names ds05-main --region us-east-1 --query 'length(LoadBalancers)' --output text 2>/dev/null || echo 0)
    b=$(aws elbv2 describe-load-balancers --names ds05-second --region eu-west-1 --query 'length(LoadBalancers)' --output text 2>/dev/null || echo 0)
    [ "${n:-0}" = "0" ] && [ "${a:-0}" = "0" ] && [ "${b:-0}" = "0" ] && break
    sleep 10
  done
fi
(cd "$ROOT/terraform/30-platform" && terraform destroy -input=false -auto-approve)
# The service network may still have the cluster's VPC associated: remove that before the network itself goes.
for sn in $(aws vpc-lattice list-service-networks --region us-east-1 --query "items[?name=='ds05-lattice'].id" --output text 2>/dev/null); do
  for assoc in $(aws vpc-lattice list-service-network-vpc-associations --service-network-identifier "$sn" --region us-east-1 --query 'items[].id' --output text 2>/dev/null); do
    aws vpc-lattice delete-service-network-vpc-association --service-network-vpc-association-identifier "$assoc" --region us-east-1 >/dev/null 2>&1 || true
  done
  aws vpc-lattice delete-service-network --service-network-identifier "$sn" --region us-east-1 >/dev/null 2>&1 || true
done
(cd "$ROOT/terraform/20-clusters" && terraform destroy -input=false -auto-approve) || (cd "$ROOT/terraform/20-clusters" && terraform destroy -input=false -auto-approve)
(cd "$ROOT/terraform/10-network" && terraform destroy -input=false -auto-approve) || (cd "$ROOT/terraform/10-network" && terraform destroy -input=false -auto-approve)
"$ROOT/scripts/verify-clean.sh"
