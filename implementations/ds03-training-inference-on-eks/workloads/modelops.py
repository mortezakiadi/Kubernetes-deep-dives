#!/usr/bin/env python3
"""The model registry, as a small command line (Design 03, "From training to serving: registry and promotion").

The registry is a layout in S3:
  registry/models/<name>/<version>/            the weights (safetensors) and manifest.json, immutable once written
  registry/models/<name>/pointers/<stage>      a one-line file naming the version that is in that stage
  registry/audit/<name>/<time>-<stage>.json    one record per promotion: who, what, and the evidence it passed on
  evals/<name>/<version>.json                  evaluation results, written by the evaluation job

Serving never reads anything except the version the `production` pointer names.
Commands:  register | prune | show | promote
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import tempfile

import boto3

BUCKET = os.environ["BUCKET"]
s3 = boto3.client("s3")
ROOT = "registry/models"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def versions(name):
    out = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=f"{ROOT}/{name}/", Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            v = cp["Prefix"].rstrip("/").split("/")[-1]
            if v.startswith("v") and v[1:].isdigit():
                out.add(int(v[1:]))
    return sorted(out)


def next_version(name):
    vs = versions(name)
    return f"v{(vs[-1] + 1) if vs else 1}"


def pointer(name, stage):
    try:
        return s3.get_object(Bucket=BUCKET, Key=f"{ROOT}/{name}/pointers/{stage}")["Body"].read().decode().strip()
    except s3.exceptions.NoSuchKey:
        return None


def upload_version(name, version, folder, meta):
    files = {}
    for fn in sorted(os.listdir(folder)):
        path = os.path.join(folder, fn)
        if not os.path.isfile(path):
            continue
        files[fn] = {"sha256": sha256(path), "bytes": os.path.getsize(path)}
        s3.upload_file(path, BUCKET, f"{ROOT}/{name}/{version}/{fn}")
    manifest = {"name": name, "version": version, "created": dt.datetime.now(dt.timezone.utc).isoformat(), "files": files, **meta}
    s3.put_object(Bucket=BUCKET, Key=f"{ROOT}/{name}/{version}/manifest.json", Body=json.dumps(manifest, indent=1).encode())  # written last
    return manifest


def download_version(name, version, folder):
    os.makedirs(folder, exist_ok=True)
    manifest = json.loads(s3.get_object(Bucket=BUCKET, Key=f"{ROOT}/{name}/{version}/manifest.json")["Body"].read())
    for fn in manifest["files"]:
        s3.download_file(BUCKET, f"{ROOT}/{name}/{version}/{fn}", os.path.join(folder, fn))
    return manifest


def cmd_register(a):
    from huggingface_hub import snapshot_download
    with tempfile.TemporaryDirectory() as tmp:
        snapshot_download(a.source, local_dir=tmp, allow_patterns=["*.json", "*.safetensors", "*.txt", "merges.txt", "vocab.json", "*.model"])
        version = next_version(a.name)
        m = upload_version(a.name, version, tmp, {"source": f"huggingface:{a.source}", "parent": None, "transform": None, "note": a.note})
    print(json.dumps({"registered": f"{a.name}/{version}", "files": len(m["files"]), "mb": round(sum(f["bytes"] for f in m["files"].values()) / 1e6)}))


def cmd_prune(a):
    import torch
    from safetensors.torch import load_file, save_file
    with tempfile.TemporaryDirectory() as tmp:
        m = download_version(a.name, a.parent, tmp)
        total = zeroed = 0
        for fn in m["files"]:
            if not fn.endswith(".safetensors"):
                continue
            tensors = load_file(os.path.join(tmp, fn))
            for k, v in tensors.items():
                # Magnitude pruning of the large linear weights; embeddings and norms are left alone.
                if v.ndim == 2 and "embed" not in k and "lm_head" not in k:
                    cut = torch.quantile(v.abs().float().flatten()[:: max(1, v.numel() // 1_000_000)], a.sparsity)
                    mask = v.abs() < cut.to(v.dtype)
                    tensors[k] = v.masked_fill(mask, 0)
                    zeroed += int(mask.sum())
                    total += v.numel()
            save_file(tensors, os.path.join(tmp, fn), metadata={"format": "pt"})
        version = next_version(a.name)
        meta = {"source": "derived", "parent": f"{a.name}/{a.parent}", "transform": {"magnitude_prune": a.sparsity, "zeroed_fraction": round(zeroed / max(1, total), 4)}, "note": a.note}
        upload_version(a.name, version, tmp, meta)
    print(json.dumps({"registered": f"{a.name}/{version}", "parent": a.parent, "target_sparsity": a.sparsity, "zeroed_fraction": meta["transform"]["zeroed_fraction"]}))


def cmd_show(a):
    out = {"name": a.name, "versions": [f"v{v}" for v in versions(a.name)], "pointers": {s: pointer(a.name, s) for s in ("staging", "production")}}
    evals = {}
    for v in out["versions"]:
        try:
            r = json.loads(s3.get_object(Bucket=BUCKET, Key=f"evals/{a.name}/{v}.json")["Body"].read())
            evals[v] = {"quality": r["quality"], "tokens_per_second": r["tokens_per_second"]}
        except s3.exceptions.NoSuchKey:
            evals[v] = None
    out["evaluations"] = evals
    print(json.dumps(out))


def cmd_promote(a):
    """A promotion is allowed only when every gate passes. The decision and its evidence are recorded either way."""
    try:
        ev = json.loads(s3.get_object(Bucket=BUCKET, Key=f"evals/{a.name}/{a.version}.json")["Body"].read())
    except s3.exceptions.NoSuchKey:
        ev = None
    gates = []
    if ev is None:
        gates.append(("evaluation exists", False, "no evaluation result for this version"))
    else:
        current = pointer(a.name, "production")
        base = None
        if current:
            try:
                base = json.loads(s3.get_object(Bucket=BUCKET, Key=f"evals/{a.name}/{current}.json")["Body"].read())
            except s3.exceptions.NoSuchKey:
                base = None
        floor = max(a.min_quality, (base["quality"] - a.max_regression) if base else 0)
        gates.append(("quality", ev["quality"] >= floor, f"quality {ev['quality']:.2f} vs required {floor:.2f}" + (f" (production {current} scores {base['quality']:.2f})" if base else "")))
        gates.append(("throughput", ev["tokens_per_second"] >= a.min_tps, f"{ev['tokens_per_second']:.0f} tokens/s vs required {a.min_tps:.0f}"))
        # Integrity: the weights in the registry must still match the manifest.
        with tempfile.TemporaryDirectory() as tmp:
            m = json.loads(s3.get_object(Bucket=BUCKET, Key=f"{ROOT}/{a.name}/{a.version}/manifest.json")["Body"].read())
            ok = True
            for fn, meta in m["files"].items():
                if fn.endswith(".safetensors"):
                    s3.download_file(BUCKET, f"{ROOT}/{a.name}/{a.version}/{fn}", os.path.join(tmp, fn))
                    ok = ok and sha256(os.path.join(tmp, fn)) == meta["sha256"]
            gates.append(("integrity", ok, "weights match the manifest checksums" if ok else "checksum mismatch"))
    passed = all(g[1] for g in gates)
    record = {"time": dt.datetime.now(dt.timezone.utc).isoformat(), "model": a.name, "version": a.version, "to": a.to, "approved": passed,
              "gates": [{"gate": g[0], "passed": g[1], "detail": g[2]} for g in gates]}
    s3.put_object(Bucket=BUCKET, Key=f"registry/audit/{a.name}/{record['time']}-{a.to}.json", Body=json.dumps(record, indent=1).encode())
    if passed:
        s3.put_object(Bucket=BUCKET, Key=f"{ROOT}/{a.name}/pointers/{a.to}", Body=a.version.encode())
    print(json.dumps(record))
    sys.exit(0 if passed else 3)


p = argparse.ArgumentParser()
sub = p.add_subparsers(dest="cmd", required=True)
r = sub.add_parser("register"); r.add_argument("--name", required=True); r.add_argument("--source", required=True); r.add_argument("--note", default="")
pr = sub.add_parser("prune"); pr.add_argument("--name", required=True); pr.add_argument("--parent", required=True); pr.add_argument("--sparsity", type=float, required=True); pr.add_argument("--note", default="")
sh = sub.add_parser("show"); sh.add_argument("--name", required=True)
pm = sub.add_parser("promote"); pm.add_argument("--name", required=True); pm.add_argument("--version", required=True); pm.add_argument("--to", default="production")
pm.add_argument("--min-quality", type=float, default=0.5); pm.add_argument("--max-regression", type=float, default=0.10); pm.add_argument("--min-tps", type=float, default=100)
a = p.parse_args()
{"register": cmd_register, "prune": cmd_prune, "show": cmd_show, "promote": cmd_promote}[a.cmd](a)
