#!/usr/bin/env bash
# Builds the platform: VPC, standard EKS cluster with a CPU node group and a small real-GPU node group
# (terraform/10-cluster), then Kueue, the second scheduler, KWOK, DCGM and the accounting service (terraform/20-platform),
# then the simulated GPU fleet of 16 KWOK nodes. Safe to re-run.
#   GPU_NODES=0 ./scripts/up.sh     builds everything with an empty GPU pool (nothing GPU-bound can run)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export AWS_REGION=us-east-2 AWS_DEFAULT_REGION=us-east-2 AWS_PAGER=""
GPU_NODES="${GPU_NODES:-2}"
for tool in terraform aws helm python3 curl; do
  command -v "$tool" >/dev/null || { echo "missing prerequisite: $tool"; exit 1; }
done
python3 -c 'import PIL, yaml' 2>/dev/null || { echo "missing Python packages: pip install pillow pyyaml"; exit 1; }
[ -x "$ROOT/.tools/kubectl" ] || "$ROOT/scripts/setup-tools.sh"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "Deploying into the AWS account that ends in ${ACCOUNT_ID: -4}, region ${AWS_REGION}, with ${GPU_NODES} GPU node(s)"
# Some networks send each connection out through a different address of a small pool: allow every one observed.
# Addresses seen by an earlier run of this script are kept, so the list only grows while the build exists.
OLD_IPS="$(sed -n 's/^admin_ips *= *"\(.*\)"/\1/p' "$ROOT/terraform/10-cluster/local.auto.tfvars" 2>/dev/null | tr ',' '\n' || true)"
MY_IPS="$( { echo "$OLD_IPS"; for i in $(seq 1 120); do curl -fsS --max-time 5 https://checkip.amazonaws.com || true; echo; done; } | grep -E '^[0-9.]+$' | sort -u | paste -sd, -)"
echo "Allowing $(echo "$MY_IPS" | tr ',' '\n' | wc -l | tr -d ' ') egress address(es) to reach the cluster API"
printf 'account_id = "%s"\nadmin_ips = "%s"\n' "$ACCOUNT_ID" "$MY_IPS" > "$ROOT/terraform/10-cluster/local.auto.tfvars"
printf 'account_id = "%s"\n' "$ACCOUNT_ID" > "$ROOT/terraform/20-platform/local.auto.tfvars"

cd "$ROOT/terraform/10-cluster"
terraform init -input=false >/dev/null
terraform apply -input=false -auto-approve -var "gpu_nodes=${GPU_NODES}"
PROJECT="$(terraform output -raw project)"

mkdir -p "$ROOT/.kube"; rm -f "$ROOT/.kube/config"
aws eks update-kubeconfig --name "$PROJECT" --alias ds17 --region "$AWS_REGION" --kubeconfig "$ROOT/.kube/config" >/dev/null

cd "$ROOT/terraform/20-platform"
terraform init -input=false >/dev/null
terraform apply -input=false -auto-approve

export KUBECONFIG="$ROOT/.kube/config"
K="$ROOT/.tools/kubectl"
echo
$K get nodes -L ds17/pool,node.kubernetes.io/instance-type
$K get pods -A | grep -v -E "kube-system|Completed" || true
# The simulated fleet: 16 Node objects that KWOK keeps Ready (platform/make_fleet.py).
python3 "$ROOT/platform/make_fleet.py" | $K apply -f - >/dev/null
for i in $(seq 1 30); do [ "$($K get nodes -l ds17/pool=fake-gpu --no-headers 2>/dev/null | grep -c ' Ready')" = 16 ] && break; sleep 5; done
$K get nodes -l ds17/pool=fake-gpu -L network.ds17.io/block,network.ds17.io/rack --no-headers | awk '{print $1, $2, $6, $7}' | head -3
echo "fake GPU nodes Ready: $($K get nodes -l ds17/pool=fake-gpu --no-headers | grep -c ' Ready') of 16"
# The real GPU nodes must both be Ready and advertising their GPU, and stay so for a minute: when EC2 refuses a type for
# capacity, the node group can replace an instance after the apply returns, and S02 needs both GPUs to stay put.
stable=0
for i in $(seq 1 90); do
  n="$($K get nodes -l ds17/pool=gpu -o jsonpath='{range .items[*]}{.status.allocatable.nvidia\.com/gpu}{" "}{end}' 2>/dev/null | tr ' ' '\n' | grep -c '^1$')"
  if [ "$n" = "${GPU_NODES}" ]; then stable=$((stable+1)); else stable=0; fi
  [ "$stable" -ge 6 ] && break
  sleep 10
done
echo "real GPU nodes Ready with a GPU: $n of ${GPU_NODES}"
$K -n kube-system rollout status deploy/ds17-scheduler --timeout=300s
$K -n platform rollout status deploy/accounting --timeout=300s
echo "Ready. Run: python3 tests/run_scenarios.py all"
