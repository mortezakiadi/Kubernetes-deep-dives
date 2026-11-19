#!/usr/bin/env bash
# Builds everything: VPC and EKS Auto Mode cluster (terraform/10-cluster), then the platform on top of it
# (terraform/20-platform). Safe to re-run.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1 AWS_PAGER=""

for tool in terraform aws docker python3; do
  command -v "$tool" >/dev/null || { echo "missing prerequisite: $tool"; exit 1; }
done
docker info >/dev/null 2>&1 || { echo "Docker is installed but not running. Start Docker Desktop and retry."; exit 1; }
[ -x "$ROOT/.tools/kubectl" ] || "$ROOT/scripts/setup-tools.sh"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "Deploying into the AWS account that ends in ${ACCOUNT_ID: -4}, region ${AWS_REGION}"
MY_IP="$(curl -fsS https://checkip.amazonaws.com | tr -d '\n')"

# Local, git-ignored values. The account ID makes Terraform refuse to run against any other account.
printf 'account_id = "%s"\nadmin_cidr = "%s/32"\n' "$ACCOUNT_ID" "$MY_IP" > "$ROOT/terraform/10-cluster/local.auto.tfvars"
printf 'account_id = "%s"\n' "$ACCOUNT_ID" > "$ROOT/terraform/20-platform/local.auto.tfvars"

cd "$ROOT/terraform/10-cluster"
terraform init -input=false
terraform apply -input=false -auto-approve

CLUSTER="$(terraform output -raw cluster_name)"
mkdir -p "$ROOT/.kube"
aws eks update-kubeconfig --name "$CLUSTER" --region "$AWS_REGION" --kubeconfig "$ROOT/.kube/config" >/dev/null

cd "$ROOT/terraform/20-platform"
terraform init -input=false
terraform apply -input=false -auto-approve

export KUBECONFIG="$ROOT/.kube/config"
"$ROOT/.tools/kubectl" -n sandboxes wait --for=jsonpath='{.status.readyReplicas}'=2 sandboxwarmpool/python-untrusted-pool --timeout=600s
"$ROOT/.tools/kubectl" -n sandboxes get sandboxwarmpool
echo "Ready. Run: python3 tests/run_scenarios.py all"
