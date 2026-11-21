#!/usr/bin/env bash
# Builds the Design 05 network in three layers: the network (IPAM, VPCs, core network, DNS), the two clusters, and what runs on them.
# Safe to re-run. Takes about 40 minutes the first time.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export AWS_PAGER=""
for tool in terraform aws helm python3; do
  command -v "$tool" >/dev/null || { echo "missing prerequisite: $tool"; exit 1; }
done
[ -x "$ROOT/.tools/kubectl" ] || "$ROOT/scripts/setup-tools.sh"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "Deploying into the AWS account that ends in ${ACCOUNT_ID: -4}"
"$ROOT/scripts/update-my-ip.sh" "$ACCOUNT_ID"
echo "== layer 1: network"
cd "$ROOT/terraform/10-network"
terraform init -input=false
# IPAM refuses many pool changes at once ("ConcurrentMutationLimitExceeded"); a second pass finishes whatever was refused.
terraform apply -input=false -auto-approve || terraform apply -input=false -auto-approve
echo "== layer 2: clusters"
cd "$ROOT/terraform/20-clusters"
terraform init -input=false
# The Kubernetes providers read the clusters' endpoints, so the clusters are created first and everything that needs them second.
terraform apply -input=false -auto-approve -target=module.cluster_main -target=module.cluster_second
terraform apply -input=false -auto-approve
mkdir -p "$ROOT/.kube"; rm -f "$ROOT/.kube/config"
aws eks update-kubeconfig --name ds05-networking-main --region us-east-1 --alias main --kubeconfig "$ROOT/.kube/config" >/dev/null
aws eks update-kubeconfig --name ds05-networking-second --region eu-west-1 --alias second --kubeconfig "$ROOT/.kube/config" >/dev/null
echo "== layer 3: platform"
cd "$ROOT/terraform/30-platform"
printf 'account_id = "%s"\n' "$ACCOUNT_ID" > local.auto.tfvars
terraform init -input=false
# The load balancers are created by the controller while this runs, and Terraform reads them to make the DNS records, so a second pass
# finishes whatever the first could not.
terraform apply -input=false -auto-approve || terraform apply -input=false -auto-approve
echo "Ready. Run: python3 tests/run_scenarios.py all"
