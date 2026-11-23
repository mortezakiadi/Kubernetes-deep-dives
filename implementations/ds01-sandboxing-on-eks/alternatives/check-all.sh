#!/usr/bin/env bash
# Runs every check in this folder that can run on one machine with Docker and nothing else: no AWS account, no cluster, no GPU.
# Needs Docker, python3 and patch. Takes a few minutes the first time because it pulls the images.
# What it cannot do is listed in README.md: those alternatives are written from documentation and were never run.
set -uo pipefail
cd "$(dirname "$0")"
fail=0
run() { echo; echo "######## $1"; shift; "$@" || fail=1; }
run "10.1 tool gateway on DynamoDB"           tool-gateway-dynamodb/check.sh
run "10.1 its Terraform"                      ./validate-terraform.sh tool-gateway-dynamodb/terraform
run "10.2 audit store (Terraform)"            ./validate-terraform.sh audit-store/terraform
run "10.3 boundary monitoring (Terraform)"    ./validate-terraform.sh boundary-monitoring/terraform
run "10.4 image trust"                        image-trust/check.sh
run "10.4 its Terraform"                      ./validate-terraform.sh image-trust/terraform
run "10.5 per-session CA"                     session-ca/check.sh
run "10.6 warm-pool autoscaler"               warm-pool-autoscaler/check.sh
run "10.7 persistent workspace"               persistent-workspace/check.sh
run "10.8-10.10 isolation tiers"              isolation-tiers/check.sh
run "10.11 network gaps"                      network-gaps/check.sh
echo; [ "$fail" = 0 ] && echo "ALL RAN" || echo "SOMETHING FAILED, read the output above"
exit $fail
