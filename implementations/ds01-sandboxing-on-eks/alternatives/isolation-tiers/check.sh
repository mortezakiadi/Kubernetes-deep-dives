#!/usr/bin/env bash
# Needs Docker, patch and python3. Checks what can be checked without a cluster or a GPU:
# the variant templates match what make_templates.py derives from the real template, they pass the pinned agent-sandbox CRD
# schemas, the Manager patch applies and compiles, the RuntimeClasses pass kubeconform, and every YAML file parses.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
IMPL="$(cd "$HERE/../.." && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
echo "== Manager patch applies to a copy of platform/ and compiles"
cp -r "$IMPL/platform" "$TMP/" && ( cd "$TMP" && patch -p1 < "$HERE/manager-templates.patch" >/dev/null && python3 -m py_compile platform/ds01/manager.py ) && echo PASS || echo FAIL
echo "== RuntimeClasses against the Kubernetes schemas (kubeconform)"
docker run --rm -v "$HERE:/w:ro" ghcr.io/yannh/kubeconform:latest -summary /w/gvisor/runtimeclass.yaml /w/gpu/runtimeclass.yaml
docker run --rm -v "$IMPL:/impl:ro" -w /impl/alternatives/isolation-tiers python:3.12-slim sh -c '
  pip install -q pyyaml jsonschema >/dev/null 2>&1
  echo "== variant templates are what make_templates.py produces"
  python make_templates.py /tmp/new.yaml >/dev/null && diff /tmp/new.yaml sandbox-templates.yaml && echo PASS
  echo "== variant templates against the pinned CRDs"; python ../validate_agent_sandbox_crds.py sandbox-templates.yaml
  echo "== every YAML file parses (nodepools carry \${placeholders}, which are plain text to YAML)"
  python -c "
import glob, yaml
for f in sorted(glob.glob(\"*/*.yaml\")):
    n = len([d for d in yaml.safe_load_all(open(f)) if d]); print(f, n, \"documents\")
"
'
