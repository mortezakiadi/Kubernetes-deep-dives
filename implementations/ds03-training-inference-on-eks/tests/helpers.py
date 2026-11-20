"""Small helpers shared by the scenarios: kubectl wrappers, golden-path rendering, S3 and Prometheus reads."""
import json
import pathlib
import re
import subprocess
import time
from string import Template

import boto3

from harness import ENV, KUBECTL, ROOT, redact

GOLDEN = ROOT / "golden-paths"
CODE = ROOT / "workloads"
VLLM = "v0.34.0"
PY = "public.ecr.aws/docker/library/python:3.12-slim"
TORCH = "pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime"
MODEL = "qwen2.5-0.5b-instruct"
SERVED = "qwen2.5-0.5b"
PROJECT = "ds03-trainserve"


def kq(*args, stdin=None, timeout=120):
    p = subprocess.run([KUBECTL, *args], capture_output=True, text=True, env=ENV, input=stdin, timeout=timeout)
    return (p.stdout + (p.stderr if p.returncode else "")).strip()


def kj(*args):
    p = subprocess.run([KUBECTL, *args, "-o", "json"], capture_output=True, text=True, env=ENV)
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return {}


def wait_for(fn, seconds=120, every=4):
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


def render(tmpl, **kw):
    return Template((GOLDEN / tmpl).read_text()).substitute(**kw)


def apply(sc, manifest, display):
    return sc.run(display, [KUBECTL, "create", "-f", "-"], stdin=manifest)


def bucket():
    out = subprocess.run(["terraform", "output", "-raw", "bucket"], cwd=ROOT / "terraform" / "20-platform", capture_output=True, text=True, env=ENV)
    return out.stdout.strip()


_S3 = None


def s3():
    global _S3
    if _S3 is None:
        _S3 = boto3.Session(profile_name=ENV.get("AWS_PROFILE", "admin"), region_name="us-east-1").client("s3")
    return _S3


def s3_json(key):
    try:
        return json.loads(s3().get_object(Bucket=bucket(), Key=key)["Body"].read())
    except Exception:
        return None


def s3_list(prefix):
    out = []
    for page in s3().get_paginator("list_objects_v2").paginate(Bucket=bucket(), Prefix=prefix):
        out += page.get("Contents", [])
    return out


def run_events(run_id):
    """Every training event the run wrote to S3, in time order, across all attempts and ranks."""
    ev = []
    for o in s3_list(f"runs/{run_id}/events/"):
        body = s3().get_object(Bucket=bucket(), Key=o["Key"])["Body"].read().decode()
        ev += [json.loads(l) for l in body.splitlines() if l.strip()]
    return sorted(ev, key=lambda e: e["t"])


def prom(query):
    """One instant query against the cluster's Prometheus, through the API server's service proxy."""
    from urllib.parse import quote
    raw = kq("get", "--raw", f"/api/v1/namespaces/monitoring/services/prometheus-server:80/proxy/api/v1/query?query={quote(query)}")
    try:
        return json.loads(raw)["data"]["result"]
    except Exception:
        return []


def pod_logs(ns, pod, container=None, tail=None, tries=8):
    """kubectl logs by pod name, retrying while a new node's log endpoint warms up."""
    args = ["-n", ns, "logs", pod] + (["-c", container] if container else []) + (["--tail", str(tail)] if tail else [])
    out = ""
    for _ in range(tries):
        out = kq(*args)
        if not out.startswith("Error from server") and "tls: internal error" not in out:
            return out
        time.sleep(6)
    return out


def pods_of(ns, selector):
    return kj("-n", ns, "get", "pods", "-l", selector).get("items", [])


def job_pod(ns, job):
    ps = pods_of(ns, f"job-name={job}")
    return ps[0]["metadata"]["name"] if ps else None


def job_done(ns, job):
    j = kj("-n", ns, "get", "job", job)
    for c in j.get("status", {}).get("conditions", []):
        if c["type"] in ("Complete", "Failed") and c["status"] == "True":
            return c["type"]
    return None


def job_log(ns, job, seconds=900):
    wait_for(lambda: job_done(ns, job), seconds, 8)
    pod = job_pod(ns, job)
    return pod_logs(ns, pod) if pod else ""


def last_json(text):
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {}


def gpu_instances():
    """GPU instances of this cluster that are pending or running, from EC2 (the cost-relevant fact)."""
    p = subprocess.run(["aws", "ec2", "describe-instances", "--filters", f"Name=tag:eks:eks-cluster-name,Values={PROJECT}",
                        "Name=instance-state-name,Values=pending,running", "Name=instance-type,Values=g*",
                        "--query", "Reservations[].Instances[].[InstanceId,InstanceType,Placement.AvailabilityZone]", "--output", "text"],
                       capture_output=True, text=True, env=ENV)
    return [l for l in p.stdout.splitlines() if l.strip()]


def gpu_nodes():
    return [n for n in kj("get", "nodes").get("items", []) if "nvidia.com/gpu" in n.get("status", {}).get("capacity", {})]


def run_py_in_cluster(code, name="probe", ns="platform", timeout=300):
    """Run a Python snippet inside the cluster (so it can reach cluster-internal Services) and return what it printed."""
    p = subprocess.run([KUBECTL, "-n", ns, "run", name, "--rm", "-i", "--restart=Never", f"--image={PY}", "-q", "--", "python", "-c", code],
                       capture_output=True, text=True, env=ENV, timeout=timeout, input="")
    return p.stdout.strip()


def secs(ts_a, ts_b):
    f = lambda t: __import__("datetime").datetime.fromisoformat(t.replace("Z", "+00:00"))
    return (f(ts_b) - f(ts_a)).total_seconds()
