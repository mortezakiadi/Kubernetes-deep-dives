#!/usr/bin/env bash
# Removes everything up.sh created, in reverse order, then checks that nothing is left.
#   down.sh          asks for confirmation
#   down.sh --yes    does not ask
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1 AWS_PAGER=""

if [ "${1:-}" != "--yes" ]; then
  read -r -p "This destroys four EKS clusters, the VPC, NAT gateway, bucket and everything in them. Type 'destroy' to continue: " answer
  [ "$answer" = "destroy" ] || { echo "aborted"; exit 1; }
fi

# Remove running jobs first so the GPU and CPU nodes they hold are empty when the pools go.
if [ -f "$ROOT/.kube/config" ]; then
  for c in hub cpu-a cpu-b gpu; do
    for ns in llm vision research; do
      KUBECONFIG="$ROOT/.kube/config" "$ROOT/.tools/kubectl" --context "$c" -n "$ns" delete jobs --all --wait=false 2>/dev/null || true
    done
  done

fi

# Kueue puts finalizers on its own objects, and its controller re-adds them while it runs. Once the
# controller is removed nothing clears them, and deleting the Kueue CRDs then hangs. So while the
# platform layer is destroyed, a helper keeps clearing them on every cluster.
strip_finalizers() {
  while true; do
    for c in hub cpu-a cpu-b gpu; do
      for kind in workloads localqueues clusterqueues resourceflavors admissionchecks \
                  multikueueclusters multikueueconfigs workloadpriorityclasses; do
        for obj in $(KUBECONFIG="$ROOT/.kube/config" "$ROOT/.tools/kubectl" --context "$c" get "$kind.kueue.x-k8s.io" -A \
            -o jsonpath='{range .items[*]}{.metadata.namespace}{"/"}{.metadata.name}{" "}{end}' 2>/dev/null); do
          ns="${obj%%/*}"; name="${obj#*/}"
          args=(--context "$c" patch "$kind.kueue.x-k8s.io" "$name" --type=merge -p '{"metadata":{"finalizers":null}}')
          [ -n "$ns" ] && args+=(-n "$ns")
          KUBECONFIG="$ROOT/.kube/config" "$ROOT/.tools/kubectl" "${args[@]}" >/dev/null 2>&1 || true
        done
      done
    done
    sleep 10
  done
}
strip_finalizers & STRIPPER=$!
trap 'kill $STRIPPER 2>/dev/null || true' EXIT
(cd "$ROOT/terraform/20-platform" && terraform destroy -input=false -auto-approve)
kill $STRIPPER 2>/dev/null || true
cd "$ROOT/terraform/10-fleet" && terraform destroy -input=false -auto-approve
"$ROOT/scripts/verify-clean.sh"
