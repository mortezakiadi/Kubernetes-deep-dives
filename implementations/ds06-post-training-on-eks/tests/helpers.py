"""Small helpers shared by the scenarios: the launcher, the tracking store in S3, kubectl reads and waits."""
import json
import re
import subprocess
import sys
import time

import boto3

from harness import ENV, KUBECTL, ROOT

PROJECT = "ds06-posttrain"
NS = "post-training"
PTL = [sys.executable, str(ROOT / "launcher" / "ptl.py")]
RECIPES = ROOT / "recipes" / "assistant-v1"
BASE_REV = "7ae557604adf67be50417f59c2c2f167def9a775"


def rel(p):
    return str(p.relative_to(ROOT))


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


_BUCKET = None


def bucket():
    global _BUCKET
    if _BUCKET is None:
        out = subprocess.run(["terraform", "output", "-raw", "bucket"], cwd=ROOT / "terraform" / "20-platform", capture_output=True, text=True, env=ENV)
        _BUCKET = out.stdout.strip()
    return _BUCKET


_S3 = None


def s3():
    global _S3
    if _S3 is None:
        _S3 = boto3.Session(profile_name=ENV.get("AWS_PROFILE", "admin"), region_name="us-east-2").client("s3")
    return _S3


def s3_json(key):
    try:
        return json.loads(s3().get_object(Bucket=bucket(), Key=key)["Body"].read())
    except Exception:
        return None


def s3_text(key):
    try:
        return s3().get_object(Bucket=bucket(), Key=key)["Body"].read().decode()
    except Exception:
        return ""


def s3_keys(prefix):
    out = []
    for page in s3().get_paginator("list_objects_v2").paginate(Bucket=bucket(), Prefix=prefix):
        out += page.get("Contents", [])
    return out


def run_id_of(text):
    m = re.search(r"launched (\S+)", text)
    return m.group(1) if m else None


def status(run):
    return s3_json(f"runs/{run}/status.json") or {}


def wait_run(run, seconds=3600):
    return wait_for(lambda: status(run).get("state") in ("succeeded", "failed") and status(run)["state"], seconds, 15)


def metrics(run):
    return [json.loads(l) for l in s3_text(f"runs/{run}/metrics.jsonl").splitlines() if l.strip()]


def events(run):
    out = []
    for o in sorted(s3_keys(f"runs/{run}/events/"), key=lambda o: o["Key"]):
        out.append(s3_json(o["Key"]))
    return out


def event(run, name):
    return next((e for e in events(run) if e and e.get("ev") == name), {})


def summary(run):
    return s3_json(f"runs/{run}/summary.json") or {}


def evals_of(model_run):
    return s3_json(f"evals/by-model/{model_run}.json") or {}


def registry_entry(run, stage="sft"):
    return s3_json(f"registry/assistant-v1/{stage}/{run}/entry.json") or {}


def workloads():
    return kj("-n", NS, "get", "workloads").get("items", [])


def workload_of(run):
    for w in workloads():
        owner = (w["metadata"].get("ownerReferences") or [{}])[0].get("name", "")
        if owner == run:
            return w
    return {}


def pods_of(run):
    return kj("-n", NS, "get", "pods", "-l", f"ds06/run-id={run}").get("items", [])


def gpu_nodes():
    return kj("get", "nodes", "-l", "ds06/pool=gpu").get("items", [])


def last_json(text):
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {}


def run_in_cluster(sc, display, code, ns=NS, name="probe", image="public.ecr.aws/docker/library/python:3.12-slim", timeout=240, sa=None):
    """Run a Python snippet in a short-lived pod, so it reaches cluster-internal Services, and record what it printed."""
    args = [KUBECTL, "-n", ns, "run", name, "--rm", "-i", "--restart=Never", f"--image={image}", "-q"]
    if sa:
        args.append(f"--overrides={json.dumps({'spec': {'serviceAccountName': sa}})}")
    subprocess.run([KUBECTL, "-n", ns, "delete", "pod", name, "--ignore-not-found", "--wait=true"], capture_output=True, env=ENV)
    return sc.run(display, args + ["--", "python", "-c", code], timeout=timeout, stdin="")
