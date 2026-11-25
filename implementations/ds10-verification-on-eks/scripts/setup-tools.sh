#!/usr/bin/env bash
# Downloads a kubectl that matches the cluster (Kubernetes 1.37) into .tools/, with a checksum check.
# Nothing is installed system-wide.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${KUBECTL_VERSION:-v1.37.2}"
OS="$(uname | tr '[:upper:]' '[:lower:]')"
case "$(uname -m)" in arm64|aarch64) ARCH=arm64 ;; *) ARCH=amd64 ;; esac
mkdir -p "$ROOT/.tools"
cd "$ROOT/.tools"
curl -fsSLO "https://dl.k8s.io/release/${VERSION}/bin/${OS}/${ARCH}/kubectl"
curl -fsSL "https://dl.k8s.io/release/${VERSION}/bin/${OS}/${ARCH}/kubectl.sha256" -o kubectl.sha256
echo "$(cat kubectl.sha256)  kubectl" | shasum -a 256 -c -
chmod +x kubectl && rm kubectl.sha256
./kubectl version --client
