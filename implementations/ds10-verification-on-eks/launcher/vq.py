#!/usr/bin/env python3
"""vq: the operator's command line for the reward-quality platform.

    vq.py job NAME -- <tools.py arguments>          run one platform job (quality, fuzz, regress, strength, rescore, review)
    vq.py register  VERIFIER --gold KEY --fuzz KEY --regress KEY [--policy KEY]
                                                    apply the training gate and write the registry entry
    vq.py registry                                  list registry entries
    vq.py rl --run R --verifier V [--steps N] [--no-stop] [--no-wait]
                                                    launch a red-team RL run pinned to V; refused unless the registry
                                                    approved V for training (exit 3)
    vq.py fuzz-now                                  start the nightly fuzz CronJob now, as a Job
    vq.py submit REVIEWER|decisions --run R --file F  upload what people returned for a run's review queue

The registry (registry/ in the bucket) is written only here, with the operator's credentials: the cluster's store
refuses writes to it, so nothing inside the cluster can approve a verifier.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

import boto3

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workloads"))
from quality import GATE, gate  # noqa: E402

KUBECTL = str(ROOT / ".tools" / "kubectl")
ENV = {**os.environ, "KUBECONFIG": str(ROOT / ".kube" / "config"), "AWS_PAGER": ""}
REGION = "us-east-2"
PY_IMAGE = "public.ecr.aws/docker/library/python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
VLLM_IMAGE = "docker.io/vllm/vllm-openai@sha256:91ee972ab751fb7180bc71c175865e36b091686600dca783d52b2511399810b3"
POLICY = {"repo": "Qwen/Qwen2.5-0.5B-Instruct", "revision": "7ae557604adf67be50417f59c2c2f167def9a775"}
AUDIT, EVAL = "strict-reference-math@1.0.0", "eval-math@1.0.0"
_S3, _B = None, None


def s3():
    global _S3
    if _S3 is None:
        _S3 = boto3.Session(profile_name=os.environ.get("AWS_PROFILE"), region_name=REGION).client("s3")
    return _S3


def tf_out(name):
    return subprocess.run(["terraform", "output", "-raw", name], cwd=ROOT / "terraform" / "20-platform", capture_output=True, text=True, env=ENV).stdout.strip()


def bucket():
    global _B
    _B = _B or tf_out("bucket")
    return _B


def get(key):
    try:
        return json.loads(s3().get_object(Bucket=bucket(), Key=key)["Body"].read())
    except s3().exceptions.NoSuchKey:
        return None


def put(key, obj):
    s3().put_object(Bucket=bucket(), Key=key, Body=json.dumps(obj, indent=1).encode())


def keys(prefix):
    out = []
    for page in s3().get_paginator("list_objects_v2").paginate(Bucket=bucket(), Prefix=prefix):
        out += [o["Key"] for o in page.get("Contents", [])]
    return out


NET_ERRORS = ("Unable to connect to the server", "dial tcp", "i/o timeout", "TLS handshake timeout", "connection refused", "connection reset")


def kubectl(*args, stdin=None):
    """kubectl, retried when the API endpoint could not be reached (this network rotates its egress address)."""
    for attempt in range(6):
        p = subprocess.run([KUBECTL, *args], capture_output=True, text=True, env=ENV, input=stdin)
        out = (p.stdout + p.stderr).strip()
        if p.returncode == 0 or not any(e in out for e in NET_ERRORS):
            return p.returncode, out
        time.sleep(3 + 2 * attempt)
    return p.returncode, out


def create(obj):
    code, out = kubectl("create", "-f", "-", stdin=json.dumps(obj))
    if code:
        sys.exit("kubectl create failed: " + out)
    print(out, flush=True)


def wait_job(ns, name, seconds=5400):
    end = time.time() + seconds
    while time.time() < end:
        code, out = kubectl("-n", ns, "get", "job", name, "-o", "json")
        if code == 0:
            for c in json.loads(out).get("status", {}).get("conditions", []):
                if c["type"] in ("Complete", "Failed") and c["status"] == "True":
                    return c["type"]
        time.sleep(5)
    return "Timeout"


def logs(ns, name, tail=40):
    return kubectl("-n", ns, "logs", f"job/{name}", f"--tail={tail}")[1]


RESTRICTED_POD = {"runAsNonRoot": True, "runAsUser": 10001, "seccompProfile": {"type": "RuntimeDefault"}}
RESTRICTED_CTR = {"runAsNonRoot": True, "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}


def tools_job(name, args):
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": "rewards"},
            "spec": {"backoffLimit": 0, "ttlSecondsAfterFinished": 86400, "template": {"spec": {
                "restartPolicy": "Never", "automountServiceAccountToken": False, "nodeSelector": {"ds10/pool": "system"},
                "securityContext": RESTRICTED_POD,
                "containers": [{"name": "tool", "image": PY_IMAGE, "command": ["python", "-u", "/code/tools.py", *args],
                                "env": [{"name": "STORE_URL", "value": "http://store.platform.svc:8080"}, {"name": "HOME", "value": "/tmp"}],
                                "resources": {"requests": {"cpu": "500m", "memory": "512Mi"}, "limits": {"cpu": "2", "memory": "2Gi"}},
                                "securityContext": RESTRICTED_CTR, "volumeMounts": [{"name": "code", "mountPath": "/code"}]}],
                "volumes": [{"name": "code", "configMap": {"name": tf_out("code_configmap")}}]}}}}


def cmd_job(a):
    kubectl("-n", "rewards", "delete", "job", a.name, "--ignore-not-found", "--wait=true")
    create(tools_job(a.name, a.rest))
    state = wait_job("rewards", a.name, 3600)
    print(a.name, state)
    print(logs("rewards", a.name))
    sys.exit(0 if state == "Complete" else 1)


def service_info(vid):
    """The pinned service's own description, read inside one of its pods (the rewards NetworkPolicy keeps the API
    server's proxy out, so this goes through the kubelet)."""
    name = vid.replace("@", "-").replace(".", "-")
    code, out = kubectl("-n", "rewards", "exec", f"deploy/{name}", "--", "python", "-c",
                        "import urllib.request,sys;sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:8080/info',timeout=30).read().decode())")
    if code:
        sys.exit(f"cannot read /info of {vid}: {out}")
    return json.loads(out)


def cmd_register(a):
    info = service_info(a.verifier)
    gq = get(a.gold)
    pq = get(a.policy) if a.policy else None
    fz = (get(a.fuzz) or {}).get("verifiers", {}).get(a.verifier)
    rg = get(a.regress)
    for name, rec in (("gold", gq), ("policy", pq), ("regress", rg)):
        if rec and rec.get("verifier", a.verifier) != a.verifier:
            sys.exit(f"{name} record is for {rec['verifier']}, not {a.verifier}")
    stale = [n for n, rec in (("gold", gq), ("fuzz", fz), ("regress", rg)) if rec and rec.get("code_sha256") not in (None, info["code_sha256"])]
    hq = get(a.human) if a.human else None
    approved, checks = gate(gq["quality"] if gq else None, pq["quality"] if pq else None, fz, rg, hq["quality"] if hq else None)
    if info["purpose"] != "training":
        # Audit and evaluation verifiers are never fuzzed against themselves; only their measured quality is recorded.
        checks = [c for c in checks if c["check"].startswith(("precision", "abstain"))]
        approved = False
        checks.append({"check": "purpose is training", "ok": False, "detail": f"{a.verifier} is registered for {info['purpose']}, owned by {info['owner']}"})
    if stale:
        approved = False
        checks.append({"check": "evidence was measured on the code being registered", "ok": False, "detail": f"stale: {stale}"})
    name, version = a.verifier.split("@")
    entry = {"verifier": a.verifier, "name": name, "version": version, "purpose": info["purpose"], "owner": info["owner"], "contract": info["contract"],
             "code_sha256": info["code_sha256"], "image": info["image"], "status": "approved" if approved else ("rejected" if info["purpose"] == "training" else f"{info['purpose']} only"), "gate": GATE, "checks": checks,
             "evidence": {"gold": a.gold, "policy": a.policy, "human": a.human, "fuzz": a.fuzz, "regress": a.regress}, "registered_at": int(time.time())}
    put(f"registry/{name}/{version}.json", entry)
    print(f"{a.verifier}: {entry['status'].upper()}  code {info['code_sha256'][:16]}  purpose {info['purpose']}")
    for c in checks:
        print(f"  [{'ok' if c['ok'] else 'NO'}] {c['check']}: {c['detail']}")


def cmd_registry(a):
    for k in sorted(keys("registry/")):
        e = get(k)
        print(f"{e['verifier']:30s} {e['status']:9s} {e['purpose']:10s} {e['owner']:13s} code {e['code_sha256'][:12]}")


def rl_job(run, steps):
    setup = ("python3 -m pip freeze 2>/dev/null | grep -i -E '^(torch|transformers|tokenizers|huggingface.hub|numpy|safetensors|accelerate|vllm)==' > /tmp/c.txt; "
             "python3 -m pip install -q --no-cache-dir --disable-pip-version-check -c /tmp/c.txt trl==1.18.2 datasets==5.2.0 > /tmp/pip.log 2>&1 || { cat /tmp/pip.log; exit 1; }; ")
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": f"rl-{run}"[:63], "namespace": "training", "labels": {"ds10/run": run}},
            "spec": {"backoffLimit": 0, "ttlSecondsAfterFinished": 86400, "template": {"metadata": {"labels": {"ds10/run": run}}, "spec": {
                "restartPolicy": "Never", "enableServiceLinks": False, "nodeSelector": {"ds10/pool": "gpu"},
                "tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}],
                "containers": [{"name": "rl", "image": VLLM_IMAGE, "command": ["bash", "-c", setup + f"exec python3 -u /code/rl.py --run {run}"],
                                "env": [{"name": "STORE_URL", "value": "http://store.platform.svc:8080"}, {"name": "HF_HOME", "value": "/work/hf"},
                                        {"name": "PYTHONPATH", "value": "/code"}],
                                "resources": {"requests": {"cpu": "3", "memory": "12Gi", "nvidia.com/gpu": "1"}, "limits": {"memory": "14Gi", "nvidia.com/gpu": "1"}},
                                "volumeMounts": [{"name": "code", "mountPath": "/code"}, {"name": "work", "mountPath": "/work"}, {"name": "shm", "mountPath": "/dev/shm"}]}],
                "volumes": [{"name": "code", "configMap": {"name": tf_out("code_configmap")}}, {"name": "work", "emptyDir": {}},
                            {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "4Gi"}}]}}}}


def pinned(vid, purpose):
    name, version = vid.split("@")
    e = get(f"registry/{name}/{version}.json")
    if e is None:
        print(f"REFUSED: {vid} has no registry entry; measure it and run the gate first")
        sys.exit(3)
    if e["status"] != "approved" and purpose == "training" and not os.environ.get("VQ_RED_TEAM"):
        print(f"REFUSED: {vid} is {e['status']} in the registry: " + "; ".join(c["check"] for c in e["checks"] if not c["ok"]))
        sys.exit(3)
    if e["purpose"] != purpose:
        print(f"REFUSED: {vid} is registered for {e['purpose']}, not {purpose}")
        sys.exit(3)
    info = service_info(vid)
    if info["code_sha256"] != e["code_sha256"]:
        print(f"REFUSED: the {vid} service runs code {info['code_sha256'][:12]}, the registry approved {e['code_sha256'][:12]}")
        sys.exit(3)
    return {"id": vid, "code_sha256": e["code_sha256"], "url": f"http://{vid.replace('@', '-').replace('.', '-')}.rewards.svc:8080", "registry_status": e["status"]}


def cmd_rl(a):
    if a.red_team:
        os.environ["VQ_RED_TEAM"] = "1"
    spec = {"run": a.run, "policy": POLICY, "verifier": pinned(a.verifier, "training"), "audit": pinned(AUDIT, "audit"), "eval": pinned(EVAL, "evaluation"),
            "red_team": bool(a.red_team), "stop_on_hacking": not a.no_stop,
            "hyperparameters": {"seed": 3, "steps": a.steps, "group": 8, "prompts_per_step": 8, "max_tokens": 320, "temperature": 1.0, "lr": a.lr},
            "launched_at": int(time.time())}
    if a.dry_run:
        print(json.dumps({k: spec[k]["id"] for k in ("verifier", "audit", "eval")} | {"pinned": "ok", "dry_run": True}))
        return
    put(f"runs/{a.run}/resolved.json", spec)
    print(json.dumps({k: spec[k]["id"] for k in ("verifier", "audit", "eval")} | {"red_team": spec["red_team"]}))
    create(rl_job(a.run, a.steps))
    if not a.no_wait:
        print(f"rl-{a.run}", wait_job("training", f"rl-{a.run}"[:63], 7200))


def cmd_submit(a):
    """Upload what people returned: one reviewer's labels, or the owner's contract decisions. Written with the
    operator's credentials, like the registry; the cluster only reads them."""
    body = pathlib.Path(a.file).read_bytes()
    key = f"reviews/{a.run}/{'decisions.json' if a.what == 'decisions' else f'labels-{a.what}.jsonl'}"
    s3().put_object(Bucket=bucket(), Key=key, Body=body)
    print(f"uploaded {key} ({len(body.splitlines())} lines)")


def cmd_fuzz_now(a):
    name = f"fuzz-now-{int(time.time()) % 100000}"
    code, out = kubectl("-n", "rewards", "create", "job", name, "--from=cronjob/verifier-fuzz-nightly")
    print(out)
    state = wait_job("rewards", name, 3600)
    print(name, state)
    print(logs("rewards", name))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    j = sub.add_parser("job"); j.add_argument("name"); j.add_argument("rest", nargs=argparse.REMAINDER)
    r = sub.add_parser("register"); r.add_argument("verifier"); r.add_argument("--gold"); r.add_argument("--fuzz"); r.add_argument("--regress"); r.add_argument("--policy"); r.add_argument("--human")
    sub.add_parser("registry")
    rl = sub.add_parser("rl"); rl.add_argument("--run"); rl.add_argument("--verifier"); rl.add_argument("--steps", type=int, default=60)
    rl.add_argument("--lr", type=float, default=2e-6); rl.add_argument("--no-stop", action="store_true"); rl.add_argument("--no-wait", action="store_true")
    rl.add_argument("--red-team", action="store_true", help="allow a rejected training verifier, on purpose, to see what RL finds")
    rl.add_argument("--dry-run", action="store_true", help="check the pins and stop: no run description, no Job")
    sb = sub.add_parser("submit"); sb.add_argument("what", help="a reviewer name, or 'decisions'"); sb.add_argument("--run"); sb.add_argument("--file")
    sub.add_parser("fuzz-now")
    a = ap.parse_args()
    if a.cmd == "job" and a.rest and a.rest[0] == "--":
        a.rest = a.rest[1:]
    {"job": cmd_job, "register": cmd_register, "registry": cmd_registry, "rl": cmd_rl, "fuzz-now": cmd_fuzz_now, "submit": cmd_submit}[a.cmd](a)


if __name__ == "__main__":
    main()
