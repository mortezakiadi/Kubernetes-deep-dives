"""Helpers shared by the scenarios: driving the cluster the way an operator would, starting runs, and reading what they wrote.

A run is a JobSet in the cluster that writes a metrics file; the file is read back from the state bucket. Every pass or fail
of a check is computed from those real records and from the output of real commands.
"""
import hashlib
import json
import math
import pathlib
import subprocess
import sys
import time

from harness import ENV, KUBECTL, ROOT, Step, redact

sys.path.insert(0, str(ROOT / "workloads"))
import manifests  # noqa: E402

NS = "rl-scale"
PROJECT = "ds07-scaling-rl"
REGION = "us-east-1"
_OUT = {}


def tf_out(name, layer="10-cluster"):
    key = (layer, name)
    if key not in _OUT:
        p = subprocess.run(["terraform", "output", "-json", name], cwd=ROOT / "terraform" / layer, capture_output=True, text=True, env=ENV)
        _OUT[key] = json.loads(p.stdout)
    return _OUT[key]


def bucket():
    return tf_out("state_bucket")


def aws(*args, timeout=120):
    p = subprocess.run(["aws", *args, "--region", REGION], capture_output=True, text=True, env=ENV, timeout=timeout)
    return p.stdout.strip() if p.returncode == 0 else (p.stdout + p.stderr).strip()


def kube(*args, timeout=120, stdin=None):
    p = subprocess.run([KUBECTL, *args], capture_output=True, text=True, env=ENV, timeout=timeout, input=stdin or "")
    return p.returncode, (p.stdout + (p.stderr if p.returncode else "")).strip()


def kj(*args):
    p = subprocess.run([KUBECTL, *args, "-o", "json"], capture_output=True, text=True, env=ENV)
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return {}


def wait_for(fn, seconds=120, every=3):
    end = time.time() + seconds
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    return None


def show(sc, display, output):
    """Record a step whose output was computed or formatted from real data by this program."""
    sc.steps.append(Step(redact(display), redact(output), 0, 0))


# ---------------------------------------------------------------------------------------------------------------
# What runs in the cluster
# ---------------------------------------------------------------------------------------------------------------
def apply_yaml(text):
    return kube("apply", "-f", "-", stdin=text)


def ensure_configs():
    """The trainer's and the sandbox worker's code live in ConfigMaps, so a change to them is one apply away."""
    for name, f in (("trainer-app", "train.py"), ("sandbox-app", "sandbox_worker.py")):
        p = subprocess.run([KUBECTL, "-n", NS, "create", "configmap", name, f"--from-file={ROOT / 'workloads' / f}", "--dry-run=client", "-o", "yaml"],
                           capture_output=True, text=True, env=ENV)
        kube("apply", "-f", "-", stdin=p.stdout)


def rollout_ready(engines):
    pods = kj("-n", NS, "get", "pods", "-l", "leaderworkerset.sigs.k8s.io/name=rollout").get("items", [])
    ready = [p for p in pods if p["status"].get("phase") == "Running" and all(c.get("ready") for c in p["status"].get("containerStatuses", []))]
    return len(ready) == engines and len(pods) == engines


def ensure_rollout(engines=2, wait=900, **kw):
    """Start (or replace) the rollout fleet with these settings and wait until every engine is serving."""
    spec = manifests.rollout_lws(engines=engines, **kw)
    cur = kube("-n", NS, "get", "lws", "rollout", "-o", "jsonpath={.metadata.annotations.ds07-spec}")[1]
    want = hashlib.sha256(spec.encode()).hexdigest()[:12]
    if cur != want:
        kube("-n", NS, "delete", "lws", "rollout", "--ignore-not-found", "--wait=true", timeout=300)
        wait_for(lambda: not kj("-n", NS, "get", "pods", "-l", "leaderworkerset.sigs.k8s.io/name=rollout").get("items"), 180, 3)
        apply_yaml(spec)
        kube("-n", NS, "annotate", "lws", "rollout", f"ds07-spec={want}", "--overwrite")
    ok = wait_for(lambda: rollout_ready(engines) and kube("-n", NS, "exec", "rollout-0", "--", "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "localhost:8000/health")[1].endswith("200"), wait, 10)
    return bool(ok)


def stop_runs():
    kube("-n", NS, "delete", "jobset", "--all", "--ignore-not-found", "--wait=true", timeout=300)


def start_run(run_id, env, max_restarts=0, **kw):
    ensure_configs()
    kube("-n", NS, "delete", "jobset", f"trainer-{run_id}", "--ignore-not-found", "--wait=true", timeout=300)
    return apply_yaml(manifests.trainer_jobset(run_id, env, bucket=bucket(), max_restarts=max_restarts, **kw))


def run_status(run_id):
    js = kj("-n", NS, "get", "jobset", f"trainer-{run_id}")
    conds = {c["type"]: c["status"] for c in js.get("status", {}).get("conditions", [])}
    return {"completed": conds.get("Completed") == "True", "failed": conds.get("Failed") == "True", "restarts": js.get("status", {}).get("restarts", 0)}


def metrics(run_id):
    """Every record the run has written to the bucket so far."""
    p = subprocess.run(["aws", "s3", "cp", f"s3://{bucket()}/runs/{run_id}/metrics.jsonl", "-", "--region", REGION], capture_output=True, text=True, env=ENV)
    out = []
    for line in p.stdout.splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def wait_run(run_id, timeout=3600, every=20, progress=None):
    """Wait until the run's JobSet completes or fails. Returns the final status."""
    end = time.time() + timeout
    while time.time() < end:
        st = run_status(run_id)
        if st["completed"] or st["failed"]:
            return st
        if progress:
            progress(run_id)
        time.sleep(every)
    return {"completed": False, "failed": False, "timeout": True, **run_status(run_id)}


def trainer_log(run_id, tail=200):
    return kube("-n", NS, "logs", "-l", f"run={run_id}", "--tail", str(tail), "--all-containers")[1]


def train_rows(recs):
    return [r for r in recs if r.get("kind") == "train"]


def eval_rows(recs):
    return [r for r in recs if r.get("kind") == "eval"]


# ---------------------------------------------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------------------------------------------
def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else float("nan")


def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * p / 100
    f, c = math.floor(k), math.ceil(k)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def sigmoid(C, A, B, C_mid, R0):
    """Held-out pass rate as a function of compute C (GPU hours), with the design's saturating curve."""
    return R0 + (A - R0) / (1.0 + (C_mid / C) ** B)


def fit_sigmoid(hours, rate, skip=0.0):
    import numpy as np
    from scipy.optimize import curve_fit
    h, r = np.array(hours, float), np.array(rate, float)
    R0 = float(r[0])
    keep = h > skip
    f = lambda C, A, B, C_mid: sigmoid(C, A, B, C_mid, R0)  # noqa: E731
    (A, B, C_mid), cov = curve_fit(f, h[keep], r[keep], p0=[max(r) + 0.05, 1.0, max(h) / 4], bounds=([R0, 0.1, 1e-4], [1.0, 6.0, 1e3]), maxfev=20000)
    return {"A": float(A), "B": float(B), "C_mid": float(C_mid), "R0": R0, "A_std": float(np.sqrt(abs(cov[0, 0])))}
