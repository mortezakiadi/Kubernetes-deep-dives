#!/usr/bin/env bash
# Builds the platform: VPC, standard EKS cluster with a CPU node group, a GPU node group and snapshot-capable storage
# (terraform/10-cluster), the task bank, then everything that runs on the cluster (terraform/20-platform). Safe to re-run.
#   GPU_NODES=0 ./scripts/up.sh     builds everything with an empty GPU pool (nothing GPU-bound can run)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export AWS_REGION=us-east-2 AWS_DEFAULT_REGION=us-east-2 AWS_PAGER=""
GPU_NODES="${GPU_NODES:-2}"
for tool in terraform aws helm python3 curl; do
  command -v "$tool" >/dev/null || { echo "missing prerequisite: $tool"; exit 1; }
done
python3 -c 'import boto3, PIL' 2>/dev/null || { echo "missing Python packages: pip install boto3 pillow"; exit 1; }
[ -x "$ROOT/.tools/kubectl" ] || "$ROOT/scripts/setup-tools.sh"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "Deploying into the AWS account that ends in ${ACCOUNT_ID: -4}, region ${AWS_REGION}, with ${GPU_NODES} GPU node(s)"
MY_IP="$(curl -fsS https://checkip.amazonaws.com | tr -d '\n')"
printf 'account_id = "%s"\nadmin_cidr = "%s/32"\n' "$ACCOUNT_ID" "$MY_IP" > "$ROOT/terraform/10-cluster/local.auto.tfvars"
printf 'account_id = "%s"\n' "$ACCOUNT_ID" > "$ROOT/terraform/20-platform/local.auto.tfvars"

cd "$ROOT/terraform/10-cluster"
terraform init -input=false >/dev/null
terraform apply -input=false -auto-approve -var "gpu_nodes=${GPU_NODES}"
PROJECT="$(terraform output -raw project)"

mkdir -p "$ROOT/.kube"; rm -f "$ROOT/.kube/config"
aws eks update-kubeconfig --name "$PROJECT" --alias ds08 --region "$AWS_REGION" --kubeconfig "$ROOT/.kube/config" >/dev/null

# The task bank is generated, not downloaded, and checked: every task's hidden tests fail as shipped and pass after
# the reference fix.
python3 "$ROOT/tasks/make_tasks.py" "$ROOT/.build/tasks"
python3 "$ROOT/tasks/check_tasks.py" "$ROOT/.build/tasks" | tail -1

cd "$ROOT/terraform/20-platform"
terraform init -input=false >/dev/null
terraform apply -input=false -auto-approve

export KUBECONFIG="$ROOT/.kube/config"
K="$ROOT/.tools/kubectl"
echo
$K get nodes -L ds08/pool,node.kubernetes.io/instance-type
$K get pods -A | grep -v -E "kube-system|Completed" || true
echo "Waiting for the rollout engine to load the model"
$K -n llm rollout status deploy/vllm --timeout=1200s
echo "Ready. Run: python3 tests/run_scenarios.py all"
