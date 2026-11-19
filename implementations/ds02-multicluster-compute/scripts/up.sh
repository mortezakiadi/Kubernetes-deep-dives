#!/usr/bin/env bash
# Builds the fleet: VPC and four EKS Auto Mode clusters (terraform/10-fleet), then the platform on top
# (terraform/20-platform). Safe to re-run.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1 AWS_PAGER=""

for tool in terraform aws helm python3; do
  command -v "$tool" >/dev/null || { echo "missing prerequisite: $tool"; exit 1; }
done
[ -x "$ROOT/.tools/kubectl" ] || "$ROOT/scripts/setup-tools.sh"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "Deploying into the AWS account that ends in ${ACCOUNT_ID: -4}, region ${AWS_REGION}"
MY_IP="$(curl -fsS https://checkip.amazonaws.com | tr -d '\n')"

printf 'account_id = "%s"\nadmin_cidr = "%s/32"\n' "$ACCOUNT_ID" "$MY_IP" > "$ROOT/terraform/10-fleet/local.auto.tfvars"
printf 'account_id = "%s"\n' "$ACCOUNT_ID" > "$ROOT/terraform/20-platform/local.auto.tfvars"

cd "$ROOT/terraform/10-fleet"
terraform init -input=false
terraform apply -input=false -auto-approve

PROJECT="$(terraform output -raw project)"
mkdir -p "$ROOT/.kube"; rm -f "$ROOT/.kube/config"
for c in hub cpu-a cpu-b gpu; do
  aws eks update-kubeconfig --name "${PROJECT}-${c}" --alias "$c" --region "$AWS_REGION" --kubeconfig "$ROOT/.kube/config" >/dev/null
done

cd "$ROOT/terraform/20-platform"
terraform init -input=false
terraform apply -input=false -auto-approve

export KUBECONFIG="$ROOT/.kube/config"
K="$ROOT/.tools/kubectl"
echo; $K --context hub get multikueuecluster
echo; $K --context hub get clusterqueue
echo "Ready. Run: python3 tests/run_scenarios.py all"
