#!/usr/bin/env python3
"""lh: the operator's command line for the long-horizon platform.

    lh.py rollout   --run R --tasks t1-00,t2-03 --seeds 0,1,2,3 [--parallel 8] [--max-turns 16]
    lh.py scripted  --run R --task T --script replies.json
    lh.py credit    --run R --trajectories A,B --k 4 --max-points 3
    lh.py trainer   logprobs --trajectories A,B --out KEY  |  update --trajectory A --credit KEY --out KEY
    lh.py stats     --run R [--k 4]
    lh.py curriculum --run R --spec curriculum.json --from-run R0

Every command submits Kubernetes Jobs (environments through Kueue; the credit worker and the trainer outside it) and
reads results from the trajectory store's bucket. Nothing here computes a reward or a credit: it only submits and reads.
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
import jobs  # noqa: E402
import metrics  # noqa: E402

KUBECTL = str(ROOT / ".tools" / "kubectl")
ENV = {**os.environ, "KUBECONFIG": str(ROOT / ".kube" / "config"), "AWS_PAGER": ""}
REGION = "us-east-2"
VLLM_IMAGE = "docker.io/vllm/vllm-openai@sha256:91ee972ab751fb7180bc71c175865e36b091686600dca783d52b2511399810b3"
POLICY_REVISION = "a09a35458c702b33eeacc393d103063234e8bc28"
_S3, _B = None, None


def s3():
    global _S3
    if _S3 is None:
        _S3 = boto3.Session(profile_name=os.environ.get("AWS_PROFILE"), region_name=REGION).client("s3")
    return _S3


def bucket():
    global _B
    if _B is None:
        _B = subprocess.run(["terraform", "output", "-raw", "bucket"], cwd=ROOT / "terraform" / "20-platform", capture_output=True, text=True, env=ENV).stdout.strip()
    return _B


def get(key):
    try:
        return json.loads(s3().get_object(Bucket=bucket(), Key=key)["Body"].read())
    except s3().exceptions.NoSuchKey:
        return None


def keys(prefix):
    out = []
    for page in s3().get_paginator("list_objects_v2").paginate(Bucket=bucket(), Prefix=prefix):
        out += [o["Key"] for o in page.get("Contents", [])]
    return out


def kubectl(*args, stdin=None):
    p = subprocess.run([KUBECTL, *args], capture_output=True, text=True, env=ENV, input=stdin)
    return p.returncode, (p.stdout + p.stderr).strip()


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
        time.sleep(10)
    return "Timeout"


def platform_job(name, image, command, sa=None, gpu=False):
    spec = {"restartPolicy": "Never", "nodeSelector": {"ds08/pool": "gpu" if gpu else "system"},
            "containers": [{"name": "main", "image": image, "command": command,
                            "env": [{"name": "POLICY_REVISION", "value": POLICY_REVISION}, {"name": "POLICY_REPO", "value": "Qwen/Qwen2.5-7B-Instruct"}, {"name": "ENV_IMAGE", "value": jobs.IMAGE},
                                    {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}, {"name": "HF_HOME", "value": "/work/hf"}],
                            "resources": {"requests": {"cpu": "3", "memory": "12Gi", "nvidia.com/gpu": "1"}, "limits": {"memory": "14Gi", "nvidia.com/gpu": "1"}} if gpu
                            else {"requests": {"cpu": "200m", "memory": "256Mi"}},
                            "volumeMounts": [{"name": "code", "mountPath": "/code"}, {"name": "work", "mountPath": "/work"}]}],
            "volumes": [{"name": "code", "configMap": {"name": "platform-code"}}, {"name": "work", "emptyDir": {}}]}
    if gpu:
        spec["tolerations"] = [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]
    if sa:
        spec["serviceAccountName"] = sa
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": "platform", "labels": {"ds08/job": name}},
            "spec": {"backoffLimit": 0, "ttlSecondsAfterFinished": 7200, "template": {"metadata": {"labels": {"ds08/job": name}}, "spec": spec}}}


# ---- commands -----------------------------------------------------------------------------------------------------
def cmd_rollout(a):
    tasks, seeds = a.tasks.split(","), [int(s) for s in a.seeds.split(",")]
    name = f"ro-{a.run}"[:63]
    create(jobs.rollout_job(name, a.run, tasks, seeds, min(a.parallel, len(tasks) * len(seeds)), a.max_turns))
    if not a.no_wait:
        print(name, wait_job(jobs.NS, name))


def cmd_scripted(a):
    key = f"curriculum/scripts/{a.run}.json"
    s3().put_object(Bucket=bucket(), Key=key, Body=pathlib.Path(a.script).read_bytes())
    name = f"sc-{a.run}"[:63]
    obj = jobs.env_job(name, ["scripted", "--run", a.run, "--tasks", a.task, "--script", key], 1, 1, "rl-rollouts", "primary", {"ds08/run": a.run})
    create(obj)
    print(name, wait_job(jobs.NS, name, 900))


def cmd_credit(a):
    name = f"cr-{a.run}"[:63]
    create(platform_job(name, jobs.IMAGE, ["python", "-u", "/code/credit.py", "--run", a.run, "--trajectories", a.trajectories,
                                           "--k", str(a.k), "--max-points", str(a.max_points)], sa="credit-worker"))
    if not a.no_wait:
        print(name, wait_job("platform", name))


def cmd_trainer(a):
    name = f"tr-{a.name}"[:63]
    # peft is installed with the image's own torch and transformers held fixed, so the trainer runs the stack vLLM runs.
    setup = ("python3 -m pip freeze 2>/dev/null | grep -i -E '^(torch|transformers|tokenizers|huggingface.hub|numpy|safetensors|accelerate)==' > /tmp/c.txt; "
             "python3 -m pip install -q --no-cache-dir --disable-pip-version-check -c /tmp/c.txt peft==0.22.0 > /tmp/pip.log 2>&1 || { cat /tmp/pip.log; exit 1; }; ")
    args = ["bash", "-c", setup + "exec python3 -u /code/trainer.py \"$@\"", "trainer", a.mode, "--out", a.out]
    if a.mode == "logprobs":
        args += ["--trajectories", a.trajectories]
    else:
        args += ["--trajectory", a.trajectory, "--credit", a.credit]
    create(platform_job(name, VLLM_IMAGE, args, gpu=True))
    print(name, wait_job("platform", name, 3600))


def cmd_replay(a):
    name = f"rp-{a.run}"[:63]
    create(jobs.env_job(name, ["replay", "--run", a.run, "--from", a.trajectory, "--turn", str(a.turn)], 1, 1, "rl-rollouts", "primary", {"ds08/run": a.run}))
    print(name, wait_job(jobs.NS, name, 900))


def cmd_curriculum(a):
    """Reverse curriculum (Design 08, "Exploration"): start episodes from snapshots late in successful trajectories,
    where little of the task remains, and move the start earlier when the success rate there is high enough."""
    spec = json.loads(pathlib.Path(a.spec).read_text())
    src = [get(k) for k in keys(f"trajectories/{a.from_run}-") if metrics.is_root(k)]
    wins = [t for t in src if t and t["outcome"] == 1.0 and t["bugs"] == spec["bugs"]][: spec["max_start_points"]]
    record = {"spec": spec, "source_run": a.from_run, "source_successes": [t["trajectory_id"] for t in wins], "stages": []}
    for n, stage in enumerate(spec["stages"]):
        rmax = stage["remaining_turns_max"]
        starts = []
        for t in wins:
            last = len(t["turns"]) - 1
            cands = [x["index"] for x in t["turns"] if x.get("snapshot_id") and x["action"]["tool"] != "submit" and (rmax is None or last - x["index"] <= rmax)]
            if rmax is None:
                starts.append((t["trajectory_id"], None, None))
            elif cands:
                starts.append((t["trajectory_id"], min(cands), last - min(cands)))
        run = f"{a.run}-st{n}"
        names = []
        for i, (tid, turn, _) in enumerate(starts):
            name = f"cu-{run}-{i}"[:63]
            if turn is None:   # the full task, from the beginning
                task_id = get(f"trajectories/{tid}.json")["task_id"]
                create(jobs.rollout_job(name, run, [task_id], list(range(spec["k"])), spec["k"]))
            else:
                obj = jobs.branch_job(name, run, tid, turn, spec["k"])
                obj["metadata"]["labels"].update({"kueue.x-k8s.io/queue-name": "rl-rollouts", "kueue.x-k8s.io/priority-class": "primary"})
                create(obj)
            names.append(name)
        for name in names:
            wait_job(jobs.NS, name)
        outs = [get(k)["outcome"] for k in keys("trajectories/") if f"-{run}-" in k or k.startswith(f"trajectories/{run}-")]
        rate = round(sum(outs) / len(outs), 3) if outs else None
        need = (stage.get("promote_when") or {}).get("success_rate_min")
        res = {"stage": n, "remaining_turns_max": rmax, "start_points": [{"trajectory": s[0], "from_turn": s[1], "remaining_turns": s[2]} for s in starts],
               "episodes": len(outs), "success_rate": rate, "promote_when": need, "promoted": bool(need is not None and rate is not None and rate >= need)}
        record["stages"].append(res)
        print(json.dumps(res), flush=True)
    s3().put_object(Bucket=bucket(), Key=f"curriculum/{a.run}.json", Body=json.dumps(record, indent=1).encode())


def cmd_stats(a):
    trajs = [get(k) for k in keys(f"trajectories/{a.run}-") if metrics.is_root(k)]
    print(json.dumps(metrics.summary([t for t in trajs if t], k=a.k), indent=1))


def main():
    p = argparse.ArgumentParser(prog="lh")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("rollout")
    r.add_argument("--run", required=True)
    r.add_argument("--tasks", required=True)
    r.add_argument("--seeds", default="0")
    r.add_argument("--parallel", type=int, default=8)
    r.add_argument("--max-turns", type=int, default=16)
    r.add_argument("--no-wait", action="store_true")
    s = sub.add_parser("scripted")
    s.add_argument("--run", required=True)
    s.add_argument("--task", required=True)
    s.add_argument("--script", required=True)
    c = sub.add_parser("credit")
    c.add_argument("--run", required=True)
    c.add_argument("--trajectories", required=True)
    c.add_argument("--k", type=int, default=4)
    c.add_argument("--max-points", type=int, default=3)
    c.add_argument("--no-wait", action="store_true")
    t = sub.add_parser("trainer")
    t.add_argument("mode", choices=["logprobs", "update"])
    t.add_argument("--name", required=True)
    t.add_argument("--trajectories")
    t.add_argument("--trajectory")
    t.add_argument("--credit")
    t.add_argument("--out", required=True)
    st = sub.add_parser("stats")
    st.add_argument("--run", required=True)
    st.add_argument("--k", type=int, default=4)
    rp = sub.add_parser("replay")
    rp.add_argument("--run", required=True)
    rp.add_argument("--trajectory", required=True)
    rp.add_argument("--turn", type=int, required=True)
    cu = sub.add_parser("curriculum")
    cu.add_argument("--run", required=True)
    cu.add_argument("--spec", required=True)
    cu.add_argument("--from-run", required=True)
    a = p.parse_args()
    {"replay": cmd_replay, "curriculum": cmd_curriculum, "rollout": cmd_rollout, "scripted": cmd_scripted, "credit": cmd_credit, "trainer": cmd_trainer, "stats": cmd_stats}[a.cmd](a)


if __name__ == "__main__":
    main()
