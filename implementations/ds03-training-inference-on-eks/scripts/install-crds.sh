#!/usr/bin/env bash
# The Gateway API and the Inference Extension ship as plain manifests. They are applied once, at pinned versions, before
# the platform layer, because the gateway charts need these CRDs to exist when Helm validates them.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export KUBECONFIG="$ROOT/.kube/config"
K="$ROOT/.tools/kubectl"
GATEWAY_API_VERSION="${GATEWAY_API_VERSION:-v1.6.0}"
INFERENCE_EXTENSION_VERSION="${INFERENCE_EXTENSION_VERSION:-v1.5.0}"
$K apply --server-side -f "https://github.com/kubernetes-sigs/gateway-api/releases/download/${GATEWAY_API_VERSION}/standard-install.yaml"
$K apply --server-side -f "https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/download/${INFERENCE_EXTENSION_VERSION}/manifests.yaml"
