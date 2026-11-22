"""Shared pieces of every post-training job: the resolved recipe, datasets with checked hashes, the tracking store,
the model registry, and the client of the reward service.

The launcher writes everything a run needs to S3 before the job starts (runs/<run_id>/resolved.json), so a job reads
one immutable description and never a mutable pointer. Everything a job learns goes back to S3 under runs/<run_id>/.
"""
import hashlib
import json
import os
import pathlib
import time
import urllib.request

import boto3

BUCKET = os.environ["BUCKET"]
REGION = os.environ.get("AWS_REGION", "us-east-2")
RUN_ID = os.environ.get("RUN_ID", "")
VERIFIER = os.environ.get("VERIFIER_URL", "http://verifier.rewards.svc.cluster.local:8080")
WORK = pathlib.Path(os.environ.get("WORK_DIR", "/work"))
_s3 = boto3.client("s3", region_name=REGION)


# ---- S3 ------------------------------------------------------------------------------------------------------------
def get_bytes(key):
    return _s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()


def get_json(key, default=None):
    try:
        return json.loads(get_bytes(key))
    except _s3.exceptions.NoSuchKey:
        return default


def put_json(key, obj):
    _s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(obj, indent=1, sort_keys=True, default=str).encode(),
                   ContentType="application/json")


def put_text(key, text):
    _s3.put_object(Bucket=BUCKET, Key=key, Body=text.encode())


def upload_dir(local, prefix):
    n = size = 0
    for p in sorted(pathlib.Path(local).rglob("*")):
        if p.is_file():
            _s3.upload_file(str(p), BUCKET, f"{prefix}/{p.relative_to(local)}")
            n, size = n + 1, size + p.stat().st_size
    return n, size


def download_prefix(prefix, local):
    local = pathlib.Path(local)
    n = 0
    for page in _s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix.rstrip("/") + "/"):
        for o in page.get("Contents", []):
            dest = local / o["Key"][len(prefix.rstrip("/")) + 1:]
            dest.parent.mkdir(parents=True, exist_ok=True)
            _s3.download_file(BUCKET, o["Key"], str(dest))
            n += 1
    return n


def delete_prefix(prefix):
    for page in _s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix.rstrip("/") + "/"):
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if keys:
            _s3.delete_objects(Bucket=BUCKET, Delete={"Objects": keys})


# ---- the run's description -------------------------------------------------------------------------------------------
def resolved():
    """The recipe as the launcher resolved it: every dataset, checkpoint and image pinned, plus the recipe hash."""
    return get_json(f"runs/{RUN_ID}/resolved.json")


def load_rows(ds):
    """Read a resolved dataset and refuse to use it if its bytes are not the ones the launcher recorded."""
    data = get_bytes(ds["key"])
    digest = hashlib.sha256(data).hexdigest()
    if digest != ds["sha256"]:
        raise RuntimeError(f"dataset {ds['ref']} changed since launch: sha256 {digest[:12]} != recorded {ds['sha256'][:12]}")
    return [json.loads(l) for l in data.decode().splitlines() if l.strip()]


def model_path(ref):
    """A resolved model: either a pinned Hugging Face revision or a registry entry in S3. Returns a local directory."""
    if ref["kind"] == "hf":
        from huggingface_hub import snapshot_download
        return snapshot_download(ref["repo"], revision=ref["revision"], cache_dir=str(WORK / "hf"))
    local = WORK / "models" / ref["run_id"]
    if not (local / "config.json").exists():
        t0 = time.time()
        n = download_prefix(ref["prefix"], local)
        print(f"downloaded {n} files of {ref['ref']} in {time.time() - t0:.1f}s", flush=True)
    return str(local)


# ---- tracking ----------------------------------------------------------------------------------------------------------
class Tracker:
    """Writes the run's status and every logged metric to runs/<run_id>/ as the run goes, so the health monitor and
    a person can read them before the run ends."""

    def __init__(self, stage, gpus=1):
        self.prefix = f"runs/{RUN_ID}"
        self.stage, self.gpus, self.t0 = stage, gpus, time.time()
        self.rows, self.extra = [], {}
        try:
            import torch
            self.extra.update(gpu=torch.cuda.get_device_name(0), node=os.environ.get("NODE_NAME", ""))
        except Exception:
            pass
        self.status("running")

    def status(self, state, **kw):
        self.extra.update(kw)
        put_json(f"{self.prefix}/status.json", {"run_id": RUN_ID, "stage": self.stage, "state": state, "started": self.t0,
                                                 "updated": time.time(), "gpus": self.gpus,
                                                 "gpu_seconds": round((time.time() - self.t0) * self.gpus, 1),
                                                 "last_step": self.rows[-1]["step"] if self.rows else 0, **self.extra})

    def log(self, step, metrics):
        row = {"step": int(step), "t": round(time.time() - self.t0, 2)}
        row.update({k: (round(v, 6) if isinstance(v, float) else v) for k, v in metrics.items() if isinstance(v, (int, float))})
        self.rows.append(row)
        put_text(f"{self.prefix}/metrics.jsonl", "".join(json.dumps(r) + "\n" for r in self.rows))
        print("METRICS " + json.dumps(row), flush=True)

    def event(self, name, **kw):
        body = {"ev": name, "t": round(time.time() - self.t0, 2), **kw}
        self.n_events = getattr(self, "n_events", 0) + 1
        put_json(f"{self.prefix}/events/{self.n_events:03d}-{name}.json", body)
        print("EVENT " + json.dumps(body, default=str), flush=True)


def hf_callback(tracker):
    from transformers import TrainerCallback

    class _CB(TrainerCallback):
        def on_log(self, args, state, control, logs=None, **kw):
            if logs and state.is_world_process_zero:
                tracker.log(state.global_step, logs)
                tracker.status("running")

    return _CB()


def finish(tracker, summary):
    put_json(f"runs/{RUN_ID}/summary.json", {"run_id": RUN_ID, "stage": tracker.stage,
                                              "wall_seconds": round(time.time() - tracker.t0, 1),
                                              "gpu_seconds": round((time.time() - tracker.t0) * tracker.gpus, 1), **summary})
    tracker.status("succeeded")
    print("SUMMARY " + json.dumps(summary, default=str), flush=True)


# ---- registry --------------------------------------------------------------------------------------------------------
def register(model_dir, r, summary):
    """Upload a finished stage's model and its registry entry: which recipe (by hash), from which base, on which data."""
    prefix = f"registry/{r['recipe']}/{r['stage']}/{RUN_ID}"
    t0 = time.time()
    n, size = upload_dir(model_dir, f"{prefix}/model")
    entry = {"run_id": RUN_ID, "recipe": r["recipe"], "stage": r["stage"], "recipe_hash": r["recipe_hash"],
             "recipe_file": r["recipe_file"], "git_commit": r["git_commit"], "base": r["base"]["ref"],
             "base_resolved": r["base"].get("run_id") or r["base"].get("revision"),
             "data": [{"ref": d["ref"], "sha256": d["sha256"]} for d in r["datasets"].values()],
             "image": r["image"], "code_sha256": r["code_sha256"], "model_prefix": f"{prefix}/model",
             "files": n, "bytes": size, "upload_seconds": round(time.time() - t0, 1), "status": "candidate",
             "summary": summary, "registered": time.time()}
    put_json(f"{prefix}/entry.json", entry)
    print(f"registered {prefix}: {n} files, {size / 1e6:.0f} MB in {entry['upload_seconds']}s", flush=True)
    return entry


# ---- reward service ----------------------------------------------------------------------------------------------------
def verify(kind, completions, **fields):
    body = json.dumps({"kind": kind, "completions": completions, **fields}).encode()
    for attempt in range(5):
        try:
            req = urllib.request.Request(f"{VERIFIER}/score", data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())
        except Exception as e:  # the pool may be scaling; retry, then fail the step loudly
            if attempt == 4:
                raise
            print(f"verifier call failed ({e}); retrying", flush=True)
            time.sleep(2 * (attempt + 1))


def text_of(completion):
    """A TRL completion is either a string or a list of chat messages."""
    if isinstance(completion, str):
        return completion
    return "".join(m.get("content", "") for m in completion if m.get("role") == "assistant") or completion[-1].get("content", "")
