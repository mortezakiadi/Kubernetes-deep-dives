"""The rollout engine of an RL job: a vLLM server (TRL's `trl vllm-serve`, which adds the weight-transfer endpoints
the trainer pushes new weights through) on its own GPU, in its own pod.

It loads the same starting checkpoint as the trainer, then serves until the trainer's run is over: the trainer's
status in the tracking store is the signal, so the rollout pod ends cleanly however the trainer ends.
"""
import json
import os
import subprocess
import sys
import time

from common import RUN_ID, get_json, model_path, resolved


def main():
    r = resolved()
    ro = r["spec"]["rollout"]
    model = model_path(r["base"])
    cmd = ["trl", "vllm-serve", "--model", model, "--host", "0.0.0.0", "--port", os.environ.get("ROLLOUT_PORT", "8000"),
           "--gpu_memory_utilization", str(ro.get("server_gpu_memory", 0.85)), "--max_model_len", str(ro.get("max_model_len", 1024)),
           "--enable_prefix_caching", "true"]
    print("ROLLOUT " + json.dumps({"cmd": cmd}), flush=True)
    proc = subprocess.Popen(cmd)
    while proc.poll() is None:
        st = get_json(f"runs/{RUN_ID}/status.json") or {}
        if st.get("state") in ("succeeded", "failed"):
            print(f"trainer finished ({st['state']}); stopping the rollout engine", flush=True)
            proc.terminate()
            try:
                proc.wait(60)
            except subprocess.TimeoutExpired:
                proc.kill()
            sys.exit(0)
        time.sleep(10)
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
