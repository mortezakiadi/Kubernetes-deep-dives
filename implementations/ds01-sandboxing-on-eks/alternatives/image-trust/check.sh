#!/usr/bin/env bash
# Shows the signing mechanics of the image-trust alternative on one machine, with a local registry and a throwaway key:
# an image that was signed verifies, the same image fails against another key, and an unsigned image fails.
# It does not test the admission policy (kyverno-policy.yaml), which needs a cluster. Needs Docker. Cleans up after itself.
set -uo pipefail
NET=ds01-trust-check
WORK="$(mktemp -d)"
COSIGN=ghcr.io/sigstore/cosign/cosign:latest
cleanup() { docker rm -f ds01-trust-reg >/dev/null 2>&1; docker network rm $NET >/dev/null 2>&1; rm -rf "$WORK"; }
trap cleanup EXIT
cleanup_first() { docker rm -f ds01-trust-reg >/dev/null 2>&1; docker network rm $NET >/dev/null 2>&1; }
cleanup_first
docker network create $NET >/dev/null
docker run -d --name ds01-trust-reg --network $NET --network-alias reg -p 127.0.0.1:5055:5000 registry:2 >/dev/null
sleep 3
fail=0
check() { if [ "$2" = "0" ]; then echo "PASS  $1"; else echo "FAIL  $1"; fail=1; fi; }
cs() { docker run --rm --network $NET -v "$WORK:/w" -e COSIGN_PASSWORD="" "$COSIGN" "$@"; }
# two images in the registry: one that will be signed and one that will not
docker tag busybox:latest 127.0.0.1:5055/sandbox/signed:1 && docker tag busybox:latest 127.0.0.1:5055/sandbox/unsigned:1
push() { docker push "$1" 2>&1 | sed -n 's/.*digest: \(sha256:[0-9a-f]*\).*/\1/p' | head -1; }
SIGNED_DIGEST=$(push 127.0.0.1:5055/sandbox/signed:1)
UNSIGNED_DIGEST=$(push 127.0.0.1:5055/sandbox/unsigned:1)
cs generate-key-pair --output-key-prefix /w/good >/dev/null 2>&1
cs generate-key-pair --output-key-prefix /w/other >/dev/null 2>&1
cs sign --key /w/good.key --yes --use-signing-config=false --tlog-upload=false --allow-http-registry "reg:5000/sandbox/signed@$SIGNED_DIGEST" >/dev/null 2>&1
V="--insecure-ignore-tlog --allow-http-registry"
cs verify --key /w/good.pub $V "reg:5000/sandbox/signed@$SIGNED_DIGEST" >/dev/null 2>&1; pos=$?
check "an image signed with the key verifies against the matching public key" "$pos"
# The two refusals only mean something if the positive case works, so they are checked only then.
cs verify --key /w/other.pub $V "reg:5000/sandbox/signed@$SIGNED_DIGEST" >/dev/null 2>&1; r=$?
check "the same image does not verify against a different key" "$([ $pos -eq 0 ] && [ $r -ne 0 ] && echo 0 || echo 1)"
cs verify --key /w/good.pub $V "reg:5000/sandbox/unsigned@$UNSIGNED_DIGEST" >/dev/null 2>&1; r=$?
check "an unsigned image does not verify" "$([ $pos -eq 0 ] && [ $r -ne 0 ] && echo 0 || echo 1)"
echo
if [ $fail -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "SOME CHECKS FAILED"; fi
exit $fail
