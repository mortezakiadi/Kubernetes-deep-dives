#!/usr/bin/env bash
# Needs Docker. Validates the Terraform, checks the NetworkPolicies against the Kubernetes schemas, and parses the policy file.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
echo "== terraform validate"; "$HERE/../validate-terraform.sh" network-gaps/terraform
echo "== NetworkPolicies against the Kubernetes schemas (kubeconform)"
docker run --rm -v "$HERE:/w:ro" ghcr.io/yannh/kubeconform:latest -summary /w/egress-policies.yaml
echo "== SecurityGroupPolicy parses (no published schema was checked; the fields come from the AWS page)"
docker run --rm -v "$HERE:/w:ro" python:3.12-slim sh -c 'pip install -q pyyaml >/dev/null 2>&1; python -c "import yaml;print(len(list(yaml.safe_load_all(open(\"/w/securitygrouppolicy.yaml\")))), \"document\")"'
