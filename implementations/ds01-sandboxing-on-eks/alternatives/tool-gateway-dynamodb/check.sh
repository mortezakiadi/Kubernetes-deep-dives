#!/usr/bin/env bash
# Runs the store tests against DynamoDB Local in Docker. Needs Docker and nothing else. Starts and removes its own containers.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
NET=ds01-ddb-check
cleanup() { docker rm -f ds01-ddb-local >/dev/null 2>&1; docker network rm $NET >/dev/null 2>&1; }
trap cleanup EXIT
cleanup
docker network create $NET >/dev/null
docker run -d --name ds01-ddb-local --network $NET --network-alias ddb amazon/dynamodb-local:latest -jar DynamoDBLocal.jar -inMemory -sharedDb >/dev/null
sleep 4
docker run --rm --network $NET -v "$HERE:/work" -w /work -e DDB_ENDPOINT=http://ddb:8000 \
  python:3.12-slim sh -c "pip install -q boto3 >/dev/null 2>&1 && python test_store.py"
