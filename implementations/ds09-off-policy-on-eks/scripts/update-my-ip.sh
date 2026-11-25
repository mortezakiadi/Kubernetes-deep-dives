#!/usr/bin/env bash
# Writes the local, git-ignored variable files: the account ID and this machine's public address (the only address allowed to reach the
# cluster's API server). Run it again, and then re-apply layer 1, whenever your public address changes.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ACCOUNT_ID="${1:-$(aws sts get-caller-identity --query Account --output text)}"
MY_IP="$(curl -fsS https://checkip.amazonaws.com | tr -d '\n')"
printf 'account_id = "%s"\nadmin_cidr = "%s/32"\n' "$ACCOUNT_ID" "$MY_IP" > "$ROOT/terraform/10-cluster/local.auto.tfvars"
printf 'account_id = "%s"\n' "$ACCOUNT_ID" > "$ROOT/terraform/20-platform/local.auto.tfvars"
echo "admin address updated"
