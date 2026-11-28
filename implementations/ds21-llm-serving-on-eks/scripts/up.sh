#!/usr/bin/env bash
# Builds Design 21 in two layers: the cluster (network, EKS, the CPU and GPU node pools, the state bucket) and what runs on it (device plugin,
# KEDA, Prometheus, the GPU exporter). Safe to re-run. Takes about 25 minutes the first time.
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
echo "== layer 1: cluster"
cd "$ROOT/terraform/10-cluster"
terraform init -input=false
terraform apply -input=false -auto-approve
mkdir -p "$ROOT/.kube"; rm -f "$ROOT/.kube/config"
aws eks update-kubeconfig --name ds21-llm-serving --region us-east-1 --alias ds21 --kubeconfig "$ROOT/.kube/config" >/dev/null
echo "== layer 2: platform"
cd "$ROOT/terraform/20-platform"
terraform init -input=false
# The first pass can reach a Helm release before the namespace it needs exists; a second pass finishes whatever the first could not.
terraform apply -input=false -auto-approve || terraform apply -input=false -auto-approve
echo "Ready. Run: python3 tests/run_scenarios.py all"
