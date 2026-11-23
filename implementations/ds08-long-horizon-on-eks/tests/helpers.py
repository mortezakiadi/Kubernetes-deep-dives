"""Helpers shared by the scenarios: the launcher, the bucket behind the trajectory store, kubectl reads and waits."""
import json
import re
import subprocess
import sys
import time

import boto3

from harness import ENV, KUBECTL, ROOT

PROJECT = "ds08-longhorizon"
NS = "rl-envs"
LH = [sys.executable, str(ROOT / "launcher" / "lh.py")]
sys.path.insert(0, str(ROOT / "workloads"))
import jobs  # noqa: E402
import metrics  # noqa: E402


def kq(*args, stdin=None, timeout=120):
    p = subprocess.run([KUBECTL, *args], capture_output=True, text=True, env=ENV, input=stdin, timeout=timeout)
    return (p.stdout + (p.stderr if p.returncode else "")).strip()


def kj(*args):
    p = subprocess.run([KUBECTL, *args, "-o", "json"], capture_output=True, text=True, env=ENV)
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return {}


def wait_for(fn, seconds=120, every=5):
    end = time.time() + seconds
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    return None


def cond(obj, kind):
    for c in (obj or {}).get("status", {}).get("conditions", []):
        if c["type"] == kind:
            return c
    return {}


_B, _S3 = None, None


def bucket():
    global _B
    if _B is None:
        _B = subprocess.run(["terraform", "output", "-raw", "bucket"], cwd=ROOT / "terraform" / "20-platform", capture_output=True, text=True, env=ENV).stdout.strip()
    return _B


def s3():
    global _S3
    if _S3 is None:
        _S3 = boto3.Session(profile_name=ENV.get("AWS_PROFILE", "admin"), region_name="us-east-2").client("s3")
    return _S3


def sj(key):
    try:
        return json.loads(s3().get_object(Bucket=bucket(), Key=key)["Body"].read())
    except Exception:
        return None


def skeys(prefix):
    out = []
    for page in s3().get_paginator("list_objects_v2").paginate(Bucket=bucket(), Prefix=prefix):
        out += page.get("Contents", [])
    return out


def roots(run):
    return [t for t in (sj(o["Key"]) for o in skeys(f"trajectories/{run}-") if metrics.is_root(o["Key"])) if t]


def job_state(ns, name):
    j = kj("-n", ns, "get", "job", name)
    for c in j.get("status", {}).get("conditions", []):
        if c["type"] in ("Complete", "Failed") and c["status"] == "True":
            return c["type"]
    return None


def workload_of(ns, job):
    for w in kj("-n", ns, "get", "workloads").get("items", []):
        if (w["metadata"].get("ownerReferences") or [{}])[0].get("name") == job:
            return w
    return {}


def vllm_metrics():
    """Counters from the rollout engine's /metrics, read through the API server's service proxy."""
    raw = kq("get", "--raw", "/api/v1/namespaces/llm/services/vllm:8000/proxy/metrics")
    out = {}
    for line in raw.splitlines():
        m = re.match(r"^(vllm:[a-z_]+)(\{[^}]*\})? ([0-9.e+-]+)$", line)
        if m:
            out[m.group(1)] = out.get(m.group(1), 0.0) + float(m.group(3))
    return out


def store_metrics():
    pod = kq("-n", "platform", "get", "pods", "-l", "app=trajstore", "-o", "jsonpath={.items[0].metadata.name}")
    total = {}
    for p in kj("-n", "platform", "get", "pods", "-l", "app=trajstore").get("items", []):
        o = kq("-n", "platform", "exec", p["metadata"]["name"], "--", "python", "-c",
               "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/metrics').read().decode())")
        try:
            for k, v in json.loads(o.splitlines()[-1]).items():
                t = total.setdefault(k, {})
                for kk, vv in v.items():
                    t[kk] = t.get(kk, 0) + vv
        except Exception:
            pass
    return total


def last_json(text):
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {}
