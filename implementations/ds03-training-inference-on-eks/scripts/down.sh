#!/usr/bin/env bash
# Removes everything up.sh created, in reverse order, then checks that nothing is left.
#   down.sh          asks for confirmation
#   down.sh --yes    does not ask
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1 AWS_PAGER=""
K() { KUBECONFIG="$ROOT/.kube/config" "$ROOT/.tools/kubectl" "$@"; }

if [ "${1:-}" != "--yes" ]; then
  read -r -p "This destroys the EKS cluster, the GPU nodes, the VPC, NAT gateway, bucket and everything in them. Type 'destroy' to continue: " answer
  [ "$answer" = "destroy" ] || { echo "aborted"; exit 1; }
fi

if [ -f "$ROOT/.kube/config" ]; then
  # Stop the workloads first so the GPU nodes they hold are empty when the node pools go.
  for ns in llm-train batch-eval inference research; do
    K -n "$ns" delete trainjobs,jobs,statefulsets,deployments,leaderworkersets --all --wait=false 2>/dev/null || true
  done
  K -n gpu-buffer delete deployments --all --wait=false 2>/dev/null || true
  # The gateway's cloud resources (if any) go with the Gateway object, so remove it while the controllers still run.
  K -n inference-gw delete gateway --all --wait=false 2>/dev/null || true
fi

# Kueue puts finalizers on its own objects, and its controller re-adds them while it runs. Once the controller is
# removed nothing clears them, and deleting the Kueue CRDs then hangs. So while the platform layer is destroyed, a
# helper keeps clearing them.
strip_finalizers() {
  while true; do
    for kind in workloads localqueues clusterqueues resourceflavors admissionchecks topologies workloadpriorityclasses cohorts; do
      for obj in $(K get "$kind.kueue.x-k8s.io" -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"/"}{.metadata.name}{" "}{end}' 2>/dev/null); do
        ns="${obj%%/*}"; name="${obj#*/}"
        args=(patch "$kind.kueue.x-k8s.io" "$name" --type=merge -p '{"metadata":{"finalizers":null}}')
        [ -n "$ns" ] && args+=(-n "$ns")
        K "${args[@]}" >/dev/null 2>&1 || true
      done
    done
    sleep 10
  done
}
if [ -f "$ROOT/.kube/config" ]; then
  strip_finalizers & STRIPPER=$!
  trap 'kill $STRIPPER 2>/dev/null || true' EXIT
fi
(cd "$ROOT/terraform/20-platform" && terraform destroy -input=false -auto-approve)
[ -n "${STRIPPER:-}" ] && kill "$STRIPPER" 2>/dev/null || true
(cd "$ROOT/terraform/10-cluster" && terraform destroy -input=false -auto-approve)
"$ROOT/scripts/verify-clean.sh"
