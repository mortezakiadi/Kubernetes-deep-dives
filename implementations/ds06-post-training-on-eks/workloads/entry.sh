#!/usr/bin/env bash
# Entry point of every GPU job. The image is the pinned vLLM release image (CUDA, PyTorch, Transformers, vLLM); the few
# pure-Python packages TRL needs are added on top at pinned versions, with the image's own core packages held fixed
# by a constraints file, and the resulting package list is hashed and recorded with the run.
set -euo pipefail
cd /code
python3 -m pip freeze 2>/dev/null | grep -i -E '^(torch|torchvision|torchaudio|transformers|tokenizers|huggingface.hub|numpy|safetensors|accelerate|boto3|botocore)==' > /tmp/constraints.txt
python3 -m pip install --no-cache-dir -q --disable-pip-version-check -c /tmp/constraints.txt \
  trl==1.18.2 datasets==5.2.0 > /tmp/pip.log 2>&1 || { cat /tmp/pip.log; exit 1; }
python3 -m pip freeze 2>/dev/null | sort > /tmp/pip-freeze.txt
echo "PACKAGES sha256 $(sha256sum /tmp/pip-freeze.txt | cut -c1-16) $(grep -i -E '^(torch|transformers|trl|vllm|accelerate|datasets)==' /tmp/pip-freeze.txt | tr '\n' ' ')"
code=0
python3 -u "$@" || code=$?
if [ "$code" -ne 0 ] && [ "$1" != "serve.py" ] && [ -n "${RUN_ID:-}" ]; then
  # A run that crashes still ends with a state in the tracking store, so nothing waits for it forever.
  python3 -c "import sys, time; sys.path.insert(0, '/code'); from common import get_json, put_json, RUN_ID; \
s = get_json(f'runs/{RUN_ID}/status.json') or {}; s.update(state='failed', exit_code=$code, updated=time.time()); \
put_json(f'runs/{RUN_ID}/status.json', s)" || true
fi
exit "$code"
