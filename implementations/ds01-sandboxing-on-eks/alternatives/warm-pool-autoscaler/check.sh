#!/usr/bin/env bash
# Needs Docker, patch and python3. Runs the sizing tests, applies the Manager patch to a copy of platform/ and compiles it,
# checks keda.yaml against the Kubernetes schemas for the core kinds, and parses all of it. KEDA's own kinds are not schema-checked.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
IMPL="$(cd "$HERE/../.." && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
echo "== sizing tests"; ( cd "$HERE" && python3 test_pool_sizing.py 2>&1 | tail -3 )
echo "== patch applies to a copy of platform/ and compiles"
cp -r "$IMPL/platform" "$TMP/" && cp "$HERE/pool_sizing.py" "$TMP/platform/ds01/" && ( cd "$TMP" && patch -p1 < "$HERE/manager.patch" >/dev/null && python3 -m py_compile platform/ds01/manager.py ) && echo PASS || echo FAIL
echo "== keda.yaml: core kinds against the Kubernetes schemas, KEDA kinds skipped"
docker run --rm -v "$HERE:/w:ro" ghcr.io/yannh/kubeconform:latest -summary -ignore-missing-schemas /w/keda.yaml
