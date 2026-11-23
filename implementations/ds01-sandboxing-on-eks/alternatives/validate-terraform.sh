#!/usr/bin/env bash
# Syntax and schema check for one alternative's Terraform, using the HashiCorp image. No AWS account and no credentials needed:
#   ./validate-terraform.sh tool-gateway-dynamodb/terraform
# It runs "terraform init -backend=false" and "terraform validate" in a throwaway copy, so nothing is written next to the files.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DIR="${1:?usage: validate-terraform.sh <directory under alternatives>}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
ls "$HERE/$DIR"/*.tf >/dev/null 2>&1 || { echo "FAIL  $DIR: no .tf files there (give a path under alternatives/)"; exit 1; }
cp -R "$HERE/$DIR/." "$WORK/"
docker run --rm -v "$WORK:/tf" -w /tf hashicorp/terraform:1.9 init -backend=false -input=false -no-color >"$WORK/init.log" 2>&1 \
  || { echo "FAIL  $DIR: terraform init failed"; tail -15 "$WORK/init.log"; exit 1; }
out=$(docker run --rm -v "$WORK:/tf" -w /tf hashicorp/terraform:1.9 validate -no-color 2>&1)
if echo "$out" | grep -q "Success! The configuration is valid"; then echo "PASS  $DIR: terraform validate"; else echo "FAIL  $DIR"; echo "$out"; exit 1; fi
