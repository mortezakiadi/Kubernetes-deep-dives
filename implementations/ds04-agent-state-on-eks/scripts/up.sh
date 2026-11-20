#!/usr/bin/env bash
# Builds the platform: VPC, Multi-AZ PostgreSQL and the EKS Auto Mode cluster (terraform/10-cluster), the agent image,
# then everything that runs on the cluster (terraform/20-platform). Safe to re-run.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1 AWS_PAGER=""
for tool in terraform aws helm python3 docker; do
  command -v "$tool" >/dev/null || { echo "missing prerequisite: $tool"; exit 1; }
done
[ -x "$ROOT/.tools/kubectl" ] || "$ROOT/scripts/setup-tools.sh"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "Deploying into the AWS account that ends in ${ACCOUNT_ID: -4}, region ${AWS_REGION}"
MY_IP="$(curl -fsS https://checkip.amazonaws.com | tr -d '\n')"
printf 'account_id = "%s"\nadmin_cidr = "%s/32"\n' "$ACCOUNT_ID" "$MY_IP" > "$ROOT/terraform/10-cluster/local.auto.tfvars"
cd "$ROOT/terraform/10-cluster"
terraform init -input=false
terraform apply -input=false -auto-approve
PROJECT="$(terraform output -raw project)"
DB_HOST="$(terraform output -raw db_host)"
DB_PASSWORD="$(terraform output -raw db_password)"
mkdir -p "$ROOT/.kube"; rm -f "$ROOT/.kube/config"
aws eks update-kubeconfig --name "$PROJECT" --alias ds04 --region "$AWS_REGION" --kubeconfig "$ROOT/.kube/config" >/dev/null
IMAGE="$("$ROOT/scripts/build-image.sh")"
printf 'account_id = "%s"\ndb_host = "%s"\ndb_password = "%s"\nagent_image = "%s"\n' "$ACCOUNT_ID" "$DB_HOST" "$DB_PASSWORD" "$IMAGE" > "$ROOT/terraform/20-platform/local.auto.tfvars"
cd "$ROOT/terraform/20-platform"
terraform init -input=false
terraform apply -input=false -auto-approve
export KUBECONFIG="$ROOT/.kube/config"
K="$ROOT/.tools/kubectl"
echo; $K get pods -A | grep -v -E "kube-system|Completed"
echo "Ready. Run: python3 tests/run_scenarios.py all"
