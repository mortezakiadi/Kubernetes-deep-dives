#!/usr/bin/env bash
# Writes the local, git-ignored variable files: the account ID and this machine's public address (the only address allowed to reach the
# clusters' API servers). Run it again, and then re-apply layer 2, whenever your public address changes.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ACCOUNT_ID="${1:-$(aws sts get-caller-identity --query Account --output text)}"
MY_IP="$(curl -fsS https://checkip.amazonaws.com | tr -d '\n')"
printf 'account_id = "%s"\n' "$ACCOUNT_ID" > "$ROOT/terraform/10-network/local.auto.tfvars"
printf 'account_id = "%s"\nadmin_cidr = "%s/32"\n' "$ACCOUNT_ID" "$MY_IP" > "$ROOT/terraform/20-clusters/local.auto.tfvars"
echo "admin address updated"
