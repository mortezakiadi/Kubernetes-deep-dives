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
  for ns in team-llm team-vision team-research research-sweeps research-paper nokueue gpu-real; do
    $K -n $ns delete jobs,pods --all --wait=false 2>/dev/null || true
  done
  $K delete nodes -l ds17/pool=fake-gpu --wait=false 2>/dev/null || true
fi
if [ -f "$ROOT/terraform/20-platform/terraform.tfstate" ]; then
  (cd "$ROOT/terraform/20-platform" && terraform destroy -input=false -auto-approve)
fi
# The VPC CNI can leave an unattached network interface behind for a terminated node, and EKS leaves the cluster security
# group; either blocks the subnet and VPC deletes. Remove them, but only inside this build's VPC (found by its Project tag).
cleanup_leftovers() {
  local vpc
  vpc="$(aws ec2 describe-vpcs --filters "Name=tag:Project,Values=ds17-sched" --query 'Vpcs[0].VpcId' --output text 2>/dev/null)"
  [ -n "$vpc" ] && [ "$vpc" != "None" ] || return 0
  for eni in $(aws ec2 describe-network-interfaces --filters "Name=vpc-id,Values=$vpc" "Name=status,Values=available" --query 'NetworkInterfaces[].NetworkInterfaceId' --output text); do
    echo "removing leftover network interface $eni"; aws ec2 delete-network-interface --network-interface-id "$eni" || true
  done
  for sg in $(aws ec2 describe-security-groups --filters "Name=vpc-id,Values=$vpc" "Name=tag:aws:eks:cluster-name,Values=ds17-sched" --query 'SecurityGroups[].GroupId' --output text); do
    echo "removing leftover EKS security group $sg"; aws ec2 delete-security-group --group-id "$sg" || true
  done
}
if ! (cd "$ROOT/terraform/10-cluster" && terraform destroy -input=false -auto-approve); then
  cleanup_leftovers
  (cd "$ROOT/terraform/10-cluster" && terraform destroy -input=false -auto-approve)
fi
"$ROOT/scripts/verify-clean.sh"
