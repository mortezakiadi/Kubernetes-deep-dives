#!/usr/bin/env bash
# Runs test_session_ca.py in a Python container that also has the OpenSSL command line, so the certificates are checked by two
# independent verifiers. Needs Docker and nothing else.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
docker run --rm -v "$HERE:/work" -w /work python:3.12-slim sh -c \
  "apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq openssl >/dev/null 2>&1 && pip install -q cryptography >/dev/null 2>&1 && OPENSSL_CMD=openssl python test_session_ca.py"
