#!/usr/bin/env bash
# Builds the platform: VPC, standard EKS cluster with a CPU node group, a GPU node group and a gVisor sandbox node group
# (terraform/10-cluster), the datasets and the pinned verifier wheels, then everything that runs on the cluster
# (terraform/20-platform). Safe to re-run.
#   GPU_NODES=0 ./scripts/up.sh     builds everything with an empty GPU pool (nothing GPU-bound can run)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export AWS_REGION=us-east-2 AWS_DEFAULT_REGION=us-east-2 AWS_PAGER=""
GPU_NODES="${GPU_NODES:-3}"
for tool in terraform aws helm python3 curl; do
  command -v "$tool" >/dev/null || { echo "missing prerequisite: $tool"; exit 1; }
done
python3 -c 'import boto3, PIL' 2>/dev/null || { echo "missing Python packages: pip install boto3 pillow"; exit 1; }
[ -x "$ROOT/.tools/kubectl" ] || "$ROOT/scripts/setup-tools.sh"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "Deploying into the AWS account that ends in ${ACCOUNT_ID: -4}, region ${AWS_REGION}, with ${GPU_NODES} GPU node(s)"
# Some networks send each connection out through a different address of a small pool: allow every one observed.
# Addresses seen by an earlier run of this script are kept, so the list only grows while the build exists.
OLD_IPS="$(sed -n 's/^admin_ips *= *"\(.*\)"/\1/p' "$ROOT/terraform/10-cluster/local.auto.tfvars" 2>/dev/null | tr ',' '\n')"
MY_IPS="$( { echo "$OLD_IPS"; for i in $(seq 1 120); do curl -fsS https://checkip.amazonaws.com; echo; done; } | grep -E '^[0-9.]+$' | sort -u | paste -sd, -)"
echo "Allowing $(echo "$MY_IPS" | tr ',' '\n' | wc -l | tr -d ' ') egress address(es) to reach the cluster API"
printf 'account_id = "%s"\nadmin_ips = "%s"\n' "$ACCOUNT_ID" "$MY_IPS" > "$ROOT/terraform/10-cluster/local.auto.tfvars"
printf 'account_id = "%s"\n' "$ACCOUNT_ID" > "$ROOT/terraform/20-platform/local.auto.tfvars"

cd "$ROOT/terraform/10-cluster"
terraform init -input=false >/dev/null
terraform apply -input=false -auto-approve -var "gpu_nodes=${GPU_NODES}"
PROJECT="$(terraform output -raw project)"

mkdir -p "$ROOT/.kube"; rm -f "$ROOT/.kube/config"
aws eks update-kubeconfig --name "$PROJECT" --alias ds10 --region "$AWS_REGION" --kubeconfig "$ROOT/.kube/config" >/dev/null

# The datasets are generated, not downloaded; the self-test checks the strict verifiers against the gold sets locally.
python3 "$ROOT/datasets/make_data.py" "$ROOT/.build/data"
python3 -c 'import sympy' 2>/dev/null && python3 "$ROOT/datasets/selftest.py" "$ROOT/.build/data" | tail -1
# The verifiers' only dependency, downloaded once here and checked against the sha256 in terraform/20-platform.
mkdir -p "$ROOT/.build/wheels"
python3 -m pip download -q --only-binary=:all: --no-deps sympy==1.14.0 mpmath==1.3.0 -d "$ROOT/.build/wheels"

cd "$ROOT/terraform/20-platform"
terraform init -input=false >/dev/null
terraform apply -input=false -auto-approve

export KUBECONFIG="$ROOT/.kube/config"
K="$ROOT/.tools/kubectl"
echo
$K get nodes -L ds10/pool,node.kubernetes.io/instance-type
$K get pods -A | grep -v -E "kube-system|Completed" || true
echo "Waiting for the verifiers and the two judges"
$K -n rewards rollout status deploy --timeout=900s
for j in judge-a judge-b; do $K -n judges rollout status deploy/$j --timeout=1800s; done
echo "Ready. Run: python3 tests/run_scenarios.py all"
