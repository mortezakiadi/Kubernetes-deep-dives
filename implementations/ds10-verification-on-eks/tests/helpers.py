"""Helpers shared by the scenarios: the launcher, the bucket behind the store, kubectl reads and waits."""
import json
import re
import subprocess
import sys
import time

import boto3

from harness import ENV, KUBECTL, ROOT

PROJECT = "ds10-verify"
VQ = [sys.executable, str(ROOT / "launcher" / "vq.py")]
sys.path.insert(0, str(ROOT / "workloads"))


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


INFO_PY = "import urllib.request,sys;sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:8080/info',timeout=30).read().decode())"


def info(vid):
    """A verifier service's /info, read inside one of its pods (the rewards NetworkPolicy keeps the API server proxy out)."""
    name = vid.replace("@", "-").replace(".", "-")
    try:
        return json.loads(kq("-n", "rewards", "exec", f"deploy/{name}", "--", "python", "-c", INFO_PY))
    except json.JSONDecodeError:
        return {}


def last_json(text):
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {}


def json_lines(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out
