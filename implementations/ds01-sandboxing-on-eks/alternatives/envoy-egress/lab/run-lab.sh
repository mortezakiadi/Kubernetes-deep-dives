#!/usr/bin/env bash
# Reproduces the Envoy egress gateway lab on one machine with Docker. Nothing here touches AWS or the EKS stack.
#   ./run-lab.sh
# It starts: a "sandbox" container, a TLS server that plays three hosts, the policy service and Envoy,
# runs the checks below, prints PASS or FAIL for each, and removes everything it started.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENVOY_DIR="$(dirname "$HERE")"
ENVOY_IMAGE="${ENVOY_IMAGE:-envoyproxy/envoy:v1.40.2}"
PY_IMAGE="${PY_IMAGE:-python:3.12-slim}"
CURL_IMAGE="${CURL_IMAGE:-curlimages/curl:latest}"
NET=ds01-envoy-lab
WORK="$(mktemp -d)"
CONTAINERS="lab-sandbox lab-stranger lab-server lab-authz lab-envoy"
cleanup() { docker rm -f $CONTAINERS >/dev/null 2>&1; docker network rm $NET >/dev/null 2>&1; rm -rf "$WORK"; }
trap cleanup EXIT
cleanup_first() { docker rm -f $CONTAINERS >/dev/null 2>&1; docker network rm $NET >/dev/null 2>&1; }
cleanup_first
docker network create $NET >/dev/null
fail=0
check() {  # check "description" "expected text" "actual text"
  if [[ "$3" == *"$2"* ]]; then echo "PASS  $1"; else echo "FAIL  $1"; echo "      expected to contain: $2"; echo "      got: $3"; fail=1; fi
}
# 1. A private CA and one certificate per lab host (the test hosts end in .test and exist only on the lab network).
mkdir -p "$WORK/certs"; cd "$WORK/certs"
openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.pem -days 2 -subj "/CN=lab-ca" 2>/dev/null
for h in allowed.test metrics.test blocked.test; do
  openssl req -newkey rsa:2048 -nodes -keyout $h.key -out $h.csr -subj "/CN=$h" 2>/dev/null
  printf "subjectAltName=DNS:$h" > $h.ext
  openssl x509 -req -in $h.csr -CA ca.pem -CAkey ca.key -CAcreateserial -extfile $h.ext -out $h.pem -days 2 2>/dev/null
done
chmod 644 *.key
cd "$HERE"
# 2. Envoy accepts the configuration.
out=$(docker run --rm -v "$ENVOY_DIR/envoy.yaml:/etc/envoy/envoy.yaml:ro" -v "$WORK/certs:/certs:ro" "$ENVOY_IMAGE" --mode validate -c /etc/envoy/envoy.yaml 2>&1 | tail -1)
check "Envoy validates the configuration" "configuration" "$out"
# 3. The sandbox, then the session table that names its address.
docker run -d --name lab-sandbox --network $NET --entrypoint sh "$CURL_IMAGE" -c "sleep 600" >/dev/null
SANDBOX_IP=$(docker inspect -f "{{(index .NetworkSettings.Networks \"$NET\").IPAddress}}" lab-sandbox)
cat > "$WORK/sessions.json" <<JSON
{"$SANDBOX_IP": {"session_id": "sess-lab-0001", "allow_hosts": ["allowed.test", "metrics.test"], "inject": {"metrics.test": "metrics-api"}}}
JSON
docker cp "$WORK/certs/ca.pem" lab-sandbox:/tmp/ca.pem >/dev/null
docker run -d --name lab-server --network $NET --network-alias allowed.test --network-alias blocked.test --network-alias metrics.test \
  -v "$WORK/certs:/certs:ro" -v "$HERE/server.py:/server.py:ro" "$PY_IMAGE" python /server.py >/dev/null
docker run -d --name lab-authz --network $NET --network-alias authz \
  -v "$ENVOY_DIR/policy_service.py:/authz.py:ro" -v "$WORK/sessions.json:/sessions.json:ro" "$PY_IMAGE" \
  sh -c "pip install -q grpcio xds-protos >/dev/null 2>&1; python -u /authz.py" >/dev/null
for _ in $(seq 1 60); do docker logs lab-authz 2>&1 | grep -q "authz up" && break; sleep 3; done
docker run -d --name lab-envoy --network $NET --network-alias envoy -v "$ENVOY_DIR/envoy.yaml:/etc/envoy/envoy.yaml:ro" \
  -v "$WORK/certs:/certs:ro" "$ENVOY_IMAGE" -c /etc/envoy/envoy.yaml >/dev/null
sleep 5
via() { docker exec lab-sandbox curl -s -m 10 -x http://envoy:3128 --cacert /tmp/ca.pem "$@"; }
# 4. The checks.
r=$(via https://allowed.test/ 2>&1)
check "an allowlisted host is reached through a tunnel, and the sandbox verifies the site's own certificate" '"server_host": "allowed.test"' "$r"
r=$(via -o /dev/null -w "%{http_connect}" https://blocked.test/ 2>&1)
check "a host that is not on the session's allowlist is refused with 403" "403" "$r"
r=$(via -H "Authorization: Bearer placeholder" https://metrics.test/v1/metrics 2>&1)
check "the internal host receives the real credential although the sandbox sent only a placeholder" "Bearer real-credential-for-sess-lab-0001" "$r"
docker run -d --name lab-stranger --network $NET --entrypoint sh "$CURL_IMAGE" -c "sleep 600" >/dev/null
r=$(docker exec lab-stranger curl -s -m 10 -k -o /dev/null -w "%{http_connect}" -x http://envoy:3128 https://allowed.test/ 2>&1)
check "a source address that owns no session is refused with 403" "403" "$r"
codes=""; for _ in 1 2 3 4 5 6 7 8; do codes="$codes $(via -o /dev/null -w '%{http_connect}' https://allowed.test/ 2>&1)"; done
check "a session over its rate limit is refused with 429 (the lab limit is 5 a minute)" "429" "$codes"
docker exec lab-sandbox curl -s -m 5 -k -o /dev/null https://envoy:8443/ >/dev/null 2>&1; rc=$?
check "the TLS-terminating listener cannot be reached from the network (curl exit code 7)" "7" "$rc"
# Envoy writes access-log lines a little after the request ends, so wait for the last one (up to 30 s).
for _ in $(seq 1 15); do
  logs=$(docker logs lab-envoy 2>&1 | grep '"component"')
  [[ "$logs" == *'"event":"egress.request"'* && "$logs" == *'blocked.test:443'* ]] && break
  sleep 2
done
check "the audit log names the session on a refused request" '"detail":"ext_authz_denied","event":"egress.decision","host":"blocked.test:443","session_id":"sess-lab-0001"' "$logs"
check "the audit log records the injected request" '"event":"egress.request","host":"metrics.test","injected":"true"' "$logs"
echo
if [ $fail -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "SOME CHECKS FAILED"; fi
exit $fail
