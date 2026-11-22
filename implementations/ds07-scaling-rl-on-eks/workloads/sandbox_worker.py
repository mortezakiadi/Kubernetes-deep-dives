"""A worker of the environment tier: takes a job from the queue, runs the answer's code against the tests, returns the result.

This is a throwaway pod running a few lines of arithmetic, not a hardened sandbox (Design 01 builds that). The code runs in a child
process with a time limit and a memory limit, and the result is a number between 0 and 1.
"""
import json
import os
import resource
import subprocess
import sys
import time

import redis

r = redis.Redis(host=os.environ.get("REDIS_HOST", "redis"), port=6379, decode_responses=True)
LATENCY = float(os.environ.get("TOOL_LATENCY_MS", "0")) / 1000.0

HARNESS = """
import json, sys
ns = {}
exec(compile(sys.argv[1], "answer", "exec"), ns)
tests = json.loads(sys.argv[2])
f = ns.get("f")
ok = 0
for x, want in tests:
    try:
        ok += int(f(x) == want)
    except Exception:
        pass
print(json.dumps({"passed": ok / len(tests)}))
"""


def limits():
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (3, 3))


def run(job):
    try:
        p = subprocess.run([sys.executable, "-c", HARNESS, job["code"], json.dumps(job["tests"])], capture_output=True, text=True,
                           timeout=5, preexec_fn=limits)
        return json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {"passed": 0.0}
    except Exception:
        return {"passed": 0.0}


while True:
    item = r.blpop("sandbox:queue", timeout=5)
    if not item:
        continue
    job = json.loads(item[1])
    started = time.time()
    if LATENCY:
        time.sleep(LATENCY)  # stands in for a slow tool: a browser, a build, a test suite
    res = run(job)
    res["queue_wait_s"] = started - job.get("ts", started)
    r.lpush(f"sandbox:result:{job['id']}", json.dumps(res))
    r.expire(f"sandbox:result:{job['id']}", 300)
    r.lpush("sandbox:waits", f"{res['queue_wait_s']:.3f}")
    r.ltrim("sandbox:waits", 0, 20000)
    r.incr("sandbox:done")
