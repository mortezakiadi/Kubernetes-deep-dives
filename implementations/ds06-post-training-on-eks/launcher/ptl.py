#!/usr/bin/env python3
"""ptl: the post-training launcher (Design 06, "The experiment platform on Kubernetes").

    ptl.py hash    <recipe.yaml>                      the recipe's identity: sha256 of its canonical content
    ptl.py diff    <a.yaml> <b.yaml>                  the keys that differ between two recipes
    ptl.py launch  <recipe.yaml> [--set k=v ...] [--dry-run]
    ptl.py status  [<run_id>]                         runs and their state, from the tracking store
    ptl.py compare <run_id> <run_id> ...              the same metrics for several runs, side by side
    ptl.py gate    <run_id>                           apply the recipe's evaluation gates to a registered model
    ptl.py wait    <run_id> [--timeout s]

A launch resolves every reference in the recipe to something immutable (a dataset's content hash, a registry entry's
run, a Hugging Face commit, the image digest, the hash of the code the cluster will run, the Git commit of the recipe),
writes that to the tracking store as runs/<run_id>/resolved.json, and only then creates the Kubernetes object. An
ablation (a recipe with an `ablation:` block) is refused unless its diff from the parent touches exactly the declared
variable, and nothing is created when it is refused.
"""
import argparse
import copy
import hashlib
import json
import os
import pathlib
import secrets
import subprocess
import sys
import time

import boto3
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
KUBECTL = str(ROOT / ".tools" / "kubectl")
ENV = {**os.environ, "KUBECONFIG": str(ROOT / ".kube" / "config"), "AWS_PAGER": ""}
REGION = "us-east-2"
NS = "post-training"
IMAGE = "docker.io/vllm/vllm-openai@sha256:91ee972ab751fb7180bc71c175865e36b091686600dca783d52b2511399810b3"  # v0.33.0, amd64
STAGES = {  # stage -> (script and arguments, service account, GPUs)
    "sft": (["sft.py"], "trainer", 1),
    "rl": (["grpo.py"], "trainer", 2),
    "distill": (["distill.py"], "trainer", 1),
    "filter": (["generate.py", "filter"], "datagen", 1),
    "traces": (["generate.py", "traces"], "datagen", 1),
    "eval": (["generate.py", "eval"], "evaluator", 1),
}
GPU_POD = {"cpu": "6", "memory": "22Gi"}


# ---- helpers ----------------------------------------------------------------------------------------------------------
def die(msg, code=2):
    print(msg, file=sys.stderr)
    sys.exit(code)


_S3 = None


def s3():
    global _S3
    if _S3 is None:
        _S3 = boto3.Session(profile_name=os.environ.get("AWS_PROFILE"), region_name=REGION).client("s3")
    return _S3


def bucket():
    b = os.environ.get("DS06_BUCKET")
    if not b:
        p = subprocess.run(["terraform", "output", "-raw", "bucket"], cwd=ROOT / "terraform" / "20-platform", capture_output=True, text=True, env=ENV)
        b = p.stdout.strip()
        os.environ["DS06_BUCKET"] = b
    return b


def get_json(key):
    try:
        return json.loads(s3().get_object(Bucket=bucket(), Key=key)["Body"].read())
    except s3().exceptions.NoSuchKey:
        return None


def put_json(key, obj):
    s3().put_object(Bucket=bucket(), Key=key, Body=json.dumps(obj, indent=1, sort_keys=True).encode(), ContentType="application/json")


def list_keys(prefix):
    out = []
    for page in s3().get_paginator("list_objects_v2").paginate(Bucket=bucket(), Prefix=prefix):
        out += [o["Key"] for o in page.get("Contents", [])]
    return out


def kubectl(*args, stdin=None):
    p = subprocess.run([KUBECTL, *args], capture_output=True, text=True, env=ENV, input=stdin)
    return p.returncode, (p.stdout + p.stderr).strip()


# ---- recipes ----------------------------------------------------------------------------------------------------------
def load_recipe(path):
    spec = yaml.safe_load(pathlib.Path(path).read_text())
    if not isinstance(spec, dict) or "recipe" not in spec or "stage" not in spec:
        die(f"{path}: a recipe needs at least 'recipe' and 'stage'")
    if spec["stage"] not in STAGES:
        die(f"{path}: unknown stage {spec['stage']!r}; one of {', '.join(STAGES)}")
    return spec


def identity(spec):
    """The part of a recipe that decides what a run does. The ablation block only says where it came from."""
    return {k: v for k, v in spec.items() if k != "ablation"}


def recipe_hash(spec):
    canon = json.dumps(identity(spec), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


def flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict) and v:
            out.update(flatten(v, key))
        else:
            out[key] = v
    return out


def diff(a, b):
    fa, fb = flatten(identity(a)), flatten(identity(b))
    return {k: (fa.get(k, "<absent>"), fb.get(k, "<absent>")) for k in sorted(set(fa) | set(fb)) if fa.get(k, "<absent>") != fb.get(k, "<absent>")}


def set_key(spec, dotted, value):
    node = spec
    parts = dotted.split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = yaml.safe_load(value)


def git_info(path):
    rel = os.path.relpath(path, ROOT)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--", rel], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    return commit[:12], bool(dirty)


def code_hash():
    """Hash of the workload code as the cluster has it (the ConfigMap), checked against this checkout."""
    local = hashlib.sha256()
    for p in sorted((ROOT / "workloads").glob("*")):
        if p.is_file() and p.suffix in (".py", ".sh"):
            local.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
    code, out = kubectl("-n", NS, "get", "configmap", "workload-code", "-o", "json")
    if code != 0:
        die("cannot read the workload-code ConfigMap: " + out)
    live = hashlib.sha256()
    for name, body in sorted(json.loads(out)["data"].items()):
        live.update(name.encode() + b"\0" + body.encode() + b"\0")
    if live.hexdigest() != local.hexdigest():
        die("the code in the cluster differs from this checkout; run terraform apply in terraform/20-platform first")
    return live.hexdigest()


# ---- resolving references ---------------------------------------------------------------------------------------------
def resolve_dataset(ref):
    if not ref.startswith("datasets://") or "@" not in ref:
        die(f"bad dataset reference {ref!r}: expected datasets://<name>@<version>")
    name, version = ref[len("datasets://"):].split("@", 1)
    index = get_json("datasets/index.json") or {}
    if f"{name}@{version}" in index:
        d = index[f"{name}@{version}"]
        return {"ref": ref, "key": d["key"], "sha256": d["sha256"], "rows": d["rows"]}
    meta = get_json(f"datasets/generated/{name}/{version}/meta.json")
    if meta:
        return {"ref": ref, "key": meta["key"], "sha256": meta["sha256"], "rows": meta["rows"], "made_by": meta["made_by"]}
    die(f"dataset {ref} does not exist (not in the index, not generated)")


def registry_entries(recipe, stage):
    out = []
    for k in list_keys(f"registry/{recipe}/{stage}/"):
        if k.endswith("/entry.json"):
            out.append(get_json(k))
    return sorted(out, key=lambda e: e["registered"])


def resolve_model(ref):
    if ref.startswith("hf://"):
        repo, rev = ref[len("hf://"):].split("@", 1)
        if len(rev) != 40:
            die(f"{ref}: a Hugging Face model must be pinned to a full commit hash")
        return {"ref": ref, "kind": "hf", "repo": repo, "revision": rev}
    if ref.startswith("registry://"):
        path, sel = ref[len("registry://"):].split("@", 1)
        recipe, stage = path.split("/", 1)
        entries = registry_entries(recipe, stage)
        if sel == "promoted":
            entries = [e for e in entries if e.get("status") == "promoted"]
        elif sel != "latest":
            entries = [e for e in entries if e["run_id"] == sel]
        if not entries:
            die(f"{ref}: no matching registry entry (a stage's output must be registered, and for @promoted, have passed its gates)")
        e = entries[-1]
        return {"ref": ref, "kind": "registry", "run_id": e["run_id"], "prefix": e["model_prefix"], "recipe_hash": e["recipe_hash"],
                "status": e.get("status")}
    die(f"bad model reference {ref!r}")


# ---- Kubernetes objects -----------------------------------------------------------------------------------------------
def pod_spec(sa, command, env, gpus=1):
    return {
        "serviceAccountName": sa,
        "restartPolicy": "Never",
        "terminationGracePeriodSeconds": 30,
        "containers": [{
            "name": "main", "image": IMAGE, "command": ["bash", "/code/entry.sh", *command],
            "env": [{"name": k, "value": v} for k, v in env.items()] + [{"name": "HF_HOME", "value": "/work/hf"},
                    {"name": "NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}}],
            "envFrom": [{"configMapRef": {"name": "platform-env"}}],
            "resources": {"requests": {**GPU_POD, "nvidia.com/gpu": str(gpus)}, "limits": {"memory": "28Gi", "nvidia.com/gpu": str(gpus)}},
            "volumeMounts": [{"name": "code", "mountPath": "/code"}, {"name": "work", "mountPath": "/work"}, {"name": "shm", "mountPath": "/dev/shm"}],
        }],
        "volumes": [{"name": "code", "configMap": {"name": "workload-code"}}, {"name": "work", "emptyDir": {}},
                    {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "8Gi"}}],
    }


def labels(run_id, spec, h, queue, priority):
    return {"kueue.x-k8s.io/queue-name": queue, "kueue.x-k8s.io/priority-class": priority, "ds06/run-id": run_id,
            "ds06/recipe": spec["recipe"], "ds06/stage": spec["stage"], "ds06/recipe-hash": h[:12]}


def render(run_id, spec, h, queue, priority):
    script, sa, _ = STAGES[spec["stage"]]
    env = {"RUN_ID": run_id}
    lab = labels(run_id, spec, h, queue, priority)
    job = lambda pod: {"backoffLimit": 0, "ttlSecondsAfterFinished": 21600,
                       "template": {"metadata": {"labels": {"ds06/run-id": run_id}}, "spec": pod}}
    if spec["stage"] == "rl" and spec["rollout"].get("mode", "server") == "server":
        name = run_id
        host = f"{name}-rollout-0-0.{name}"
        rollout = pod_spec(sa, ["serve.py"], {**env, "ROLLOUT_PORT": "8000"})
        trainer = pod_spec(sa, script, {**env, "ROLLOUT_HOST": host})
        return {"apiVersion": "jobset.x-k8s.io/v1alpha2", "kind": "JobSet",
                "metadata": {"name": name, "namespace": NS, "labels": lab},
                "spec": {"network": {"enableDNSHostnames": True},
                         "successPolicy": {"operator": "All", "targetReplicatedJobs": ["trainer"]},
                         "failurePolicy": {"maxRestarts": 0},
                         "replicatedJobs": [{"name": "rollout", "replicas": 1, "template": {"spec": job(rollout)}},
                                            {"name": "trainer", "replicas": 1, "template": {"spec": job(trainer)}}]}}
    gpus = 1
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": run_id, "namespace": NS, "labels": lab},
            "spec": job(pod_spec(sa, script, env, gpus))}


# ---- commands ---------------------------------------------------------------------------------------------------------
def cmd_hash(a):
    spec = load_recipe(a.recipe)
    print(recipe_hash(spec))


def cmd_diff(a):
    d = diff(load_recipe(a.a), load_recipe(a.b))
    for k, (x, y) in d.items():
        print(f"{k}: {x!r} -> {y!r}")
    print(f"{len(d)} key(s) differ")


def cmd_launch(a):
    spec = load_recipe(a.recipe)
    for kv in a.set or []:
        k, v = kv.split("=", 1)
        set_key(spec, k, v)
    h = recipe_hash(spec)
    queue, priority = spec.get("queue", "production"), spec.get("priority", "production")
    ablation = None
    if "ablation" in spec:
        ab = spec["ablation"]
        parent_path = (pathlib.Path(a.recipe).parent / ab["parent"]).resolve()
        parent = load_recipe(parent_path)
        changed = diff(parent, spec)
        print(f"ablation of {os.path.relpath(parent_path, ROOT)} (hash {recipe_hash(parent)[:12]}), declared variable: {ab['variable']}")
        for k, (x, y) in changed.items():
            print(f"  changed  {k}: {x!r} -> {y!r}")
        if set(changed) != {ab["variable"]}:
            extra = sorted(set(changed) - {ab["variable"]})
            die(f"REFUSED: an ablation may change only its declared variable ({ab['variable']}); this one also changes {', '.join(extra) or '(nothing declared changed)'}."
                " Nothing was submitted.", 3)
        queue, priority = "ablation", "ablation"  # platform rule: ablations only borrow idle GPUs, at low priority
        ablation = {"parent": os.path.relpath(parent_path, ROOT), "parent_hash": recipe_hash(parent), "variable": ab["variable"],
                    "from": changed[ab["variable"]][0], "to": changed[ab["variable"]][1]}

    stage = spec["stage"]
    commit, dirty = git_info(a.recipe)
    datasets = {}
    if stage == "sft":
        datasets = {n: resolve_dataset(s["source"]) for n, s in spec["data"]["sources"].items()}
    elif stage in ("rl", "distill", "filter", "traces"):
        datasets = {n: resolve_dataset(s["source"]) for n, s in spec["data"]["prompts"].items()}
    elif stage == "eval":
        datasets = {n: resolve_dataset(ref) for n, ref in spec["eval"]["suites"].items()}
    base = resolve_model(spec["target"] if stage == "eval" else spec["base_checkpoint"])
    teacher = resolve_model(spec["teacher_checkpoint"]) if spec.get("teacher_checkpoint") else None
    run_id = a.run_id or f"{stage}-{h[:6]}-{secrets.token_hex(2)}"
    resolved = {"run_id": run_id, "recipe": spec["recipe"], "stage": stage, "recipe_hash": h, "recipe_file": os.path.relpath(a.recipe, ROOT),
                "git_commit": commit, "recipe_uncommitted": dirty, "overrides": a.set or [], "image": IMAGE,
                "code_sha256": code_hash() if not a.dry_run else "(dry run)", "queue": queue, "priority": priority,
                "base": base, "teacher": teacher, "datasets": datasets, "ablation": ablation, "spec": identity(spec),
                "submitted": time.time()}
    obj = render(run_id, spec, h, queue, priority)
    if a.dry_run:
        print(json.dumps({"resolved": resolved, "object": obj}, indent=1))
        return
    if get_json(f"runs/{run_id}/resolved.json"):
        die(f"run {run_id} already exists")
    put_json(f"runs/{run_id}/resolved.json", resolved)
    code, out = kubectl("create", "-f", "-", stdin=json.dumps(obj))
    if code != 0:
        die("kubectl create failed: " + out)
    print(f"launched {run_id}  recipe {spec['recipe']}/{stage}  hash {h[:12]}  queue {queue}/{priority}  base {base['ref']}"
          f"{' -> ' + base.get('run_id', '') if base['kind'] == 'registry' else ''}")
    print(out)


def run_rows(run_ids=None):
    ids = run_ids or sorted({k.split("/")[1] for k in list_keys("runs/") if k.endswith("/resolved.json")})
    rows = []
    for r in ids:
        res, st = get_json(f"runs/{r}/resolved.json") or {}, get_json(f"runs/{r}/status.json") or {}
        rows.append((r, res, st))
    return rows


def cmd_status(a):
    print(f"{'RUN':22s} {'STAGE':8s} {'HASH':12s} {'QUEUE':11s} {'STATE':10s} {'STEP':>5s} {'GPU-S':>7s}")
    for r, res, st in run_rows([a.run_id] if a.run_id else None):
        print(f"{r:22s} {res.get('stage', '?'):8s} {res.get('recipe_hash', '?')[:12]:12s} {res.get('queue', '?'):11s} "
              f"{st.get('state', 'queued'):10s} {st.get('last_step', 0):5d} {st.get('gpu_seconds', 0):7.0f}")


def cmd_compare(a):
    data = []
    for r, res, st in run_rows(a.run_ids):
        summ = get_json(f"runs/{r}/summary.json") or {}
        ev = get_json(f"evals/by-model/{r}.json") or {}
        data.append((r, res, summ, ev))
    keys = [("stage", lambda r, s, e: r.get("stage")), ("recipe hash", lambda r, s, e: r.get("recipe_hash", "")[:12]),
            ("ablation variable", lambda r, s, e: (r.get("ablation") or {}).get("variable", "-")),
            ("ablation value", lambda r, s, e: json.dumps((r.get("ablation") or {}).get("to", "-"))),
            ("GPU seconds", lambda r, s, e: s.get("gpu_seconds")), ("steps", lambda r, s, e: s.get("steps"))]
    flat = lambda d, p="": {**{f"{p}{k}": v for k, v in d.items() if not isinstance(v, dict)},
                            **{kk: vv for k, v in d.items() if isinstance(v, dict) for kk, vv in flat(v, f"{p}{k}.").items()}}
    wanted = a.metric or ["reward_first_quarter", "reward_last_quarter", "frac_reward_zero_std_mean", "entropy_first_quarter",
                          "entropy_last_quarter", "share_of_wall.generate_s", "share_of_wall.sync_s", "share_of_wall.reward_s",
                          "loss_first", "loss_last"]
    eval_keys = ["math.accuracy", "code.mean_pass_rate", "casual.house_style_rate", "casual.step_leak_rate",
                 "casual.step_leak_rate_conversions", "casual.mean_words", "harmful.refusal_rate", "safe-scary.over_refusal_rate"]
    width = max(14, max(len(r) for r, *_ in data) + 1)
    print(f"{'':34s}" + "".join(f"{r:>{width}s}" for r, *_ in data))
    for label, fn in keys:
        print(f"{label:34s}" + "".join(f"{str(fn(res, s, e)):>{width}s}" for _, res, s, e in data))
    for k in wanted:
        vals = [flat(s).get(k) for _, _, s, _ in data]
        if any(v is not None for v in vals):
            print(f"{k:34s}" + "".join(f"{str(v if v is not None else '-'):>{width}s}" for v in vals))
    for k in eval_keys:
        vals = [flat(e.get("suites", {})).get(k) for *_, e in data]
        if any(v is not None for v in vals):
            print(f"{'eval ' + k:34s}" + "".join(f"{str(v if v is not None else '-'):>{width}s}" for v in vals))


def cmd_gate(a):
    res = get_json(f"runs/{a.run_id}/resolved.json")
    if not res:
        die(f"no run {a.run_id}")
    key = f"registry/{res['recipe']}/{res['stage']}/{a.run_id}/entry.json"
    entry, ev = get_json(key), get_json(f"evals/by-model/{a.run_id}.json")
    if not entry:
        die(f"{a.run_id} has no registry entry")
    if not ev:
        die(f"{a.run_id} has no evaluation yet: a model is never promoted without its eval results attached. Nothing changed.", 4)
    suites, results = ev["suites"], []
    for g in res["spec"].get("gates", []):
        if "metric" in g:
            v = suites.get(g["suite"], {}).get(g["metric"])
            ok = v is not None and ("min" not in g or v >= g["min"]) and ("max" not in g or v <= g["max"])
            bound = f">= {g['min']}" if "min" in g else f"<= {g['max']}"
            results.append((ok, f"{g['suite']}.{g['metric']} = {v} (must be {bound})"))
        if "regression_vs" in g:
            ref = resolve_model(g["regression_vs"])
            ref_ev = get_json(f"evals/by-model/{ref.get('run_id') or ref.get('revision')}.json")
            if not ref_ev:
                results.append((False, f"no evaluation of the reference {g['regression_vs']} to compare against"))
                continue
            for suite, metric in g["metrics"].items():
                v, rv = suites.get(suite, {}).get(metric), ref_ev["suites"].get(suite, {}).get(metric)
                ok = v is not None and rv is not None and v >= rv - g["margin"]
                results.append((ok, f"{suite}.{metric} = {v} vs reference {rv} (may drop at most {g['margin']})"))
    verdict = "promoted" if results and all(ok for ok, _ in results) else "blocked"
    for ok, text in results:
        print(f"  {'pass' if ok else 'FAIL'}  {text}")
    entry.update(status=verdict, gate={"checked": time.time(), "eval_run": ev["eval_run"], "results": [{"ok": ok, "check": t} for ok, t in results]})
    put_json(key, entry)
    print(f"{a.run_id}: {verdict.upper()}")


def cmd_wait(a):
    end = time.time() + a.timeout
    while time.time() < end:
        st = get_json(f"runs/{a.run_id}/status.json") or {}
        if st.get("state") in ("succeeded", "failed"):
            print(f"{a.run_id}: {st['state']} after {st.get('gpu_seconds')} GPU seconds")
            return
        time.sleep(15)
    die(f"{a.run_id}: still not finished after {a.timeout}s", 5)


def main():
    p = argparse.ArgumentParser(prog="ptl")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hash").add_argument("recipe")
    d = sub.add_parser("diff")
    d.add_argument("a")
    d.add_argument("b")
    l = sub.add_parser("launch")
    l.add_argument("recipe")
    l.add_argument("--set", action="append")
    l.add_argument("--run-id")
    l.add_argument("--dry-run", action="store_true")
    sub.add_parser("status").add_argument("run_id", nargs="?")
    c = sub.add_parser("compare")
    c.add_argument("run_ids", nargs="+")
    c.add_argument("--metric", action="append")
    sub.add_parser("gate").add_argument("run_id")
    w = sub.add_parser("wait")
    w.add_argument("run_id")
    w.add_argument("--timeout", type=int, default=3600)
    a = p.parse_args()
    {"hash": cmd_hash, "diff": cmd_diff, "launch": cmd_launch, "status": cmd_status, "compare": cmd_compare, "gate": cmd_gate, "wait": cmd_wait}[a.cmd](a)


if __name__ == "__main__":
    main()
