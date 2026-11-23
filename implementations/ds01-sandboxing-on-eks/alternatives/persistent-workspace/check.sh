#!/usr/bin/env bash
# Needs Docker, patch and python3. Runs the unit tests, checks the generated template has not drifted from the original it was
# built from, applies the Manager patch to a copy of platform/ and compiles it, and checks the YAML against the pinned CRDs.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
IMPL="$(cd "$HERE/../.." && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
echo "== patch applies to a copy of platform/ and compiles"
cp -r "$IMPL/platform" "$TMP/" && cp "$HERE/workspace.py" "$TMP/platform/ds01/" && ( cd "$TMP" && patch -p1 < "$HERE/manager.patch" >/dev/null && python3 -m py_compile platform/ds01/manager.py platform/ds01/common.py ) && echo PASS || echo FAIL
docker run --rm -v "$IMPL:/impl:ro" -w /impl/alternatives/persistent-workspace python:3.12-slim sh -c '
  pip install -q pyyaml jsonschema >/dev/null 2>&1
  echo "== unit tests"; python test_workspace.py 2>&1 | tail -3
  echo "== template is what make_template.py produces"; python make_template.py | diff - sandbox-template-persistent.yaml && echo PASS
  echo "== YAML against the pinned CRDs"; python ../validate_agent_sandbox_crds.py sandbox-template-persistent.yaml
  echo "== other YAML parses"; python -c "import yaml;[list(yaml.safe_load_all(open(f))) for f in (\"storage.yaml\",\"manager-rbac.yaml\")];print(\"PASS\")"
'
