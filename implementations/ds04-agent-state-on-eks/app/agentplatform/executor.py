"""The sandbox's own small server: run a command, write a file, snapshot the workspace to S3, restore a snapshot.

A snapshot is content-addressed (Design 04, "Working state"): each file is stored once under the hash of its bytes, and the snapshot
itself is a manifest of path to hash. A file that did not change is not uploaded again. Restoring downloads the manifest and the
files it names into an empty workspace.
"""
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import boto3

from .common import BUCKET, Handler
from http.server import ThreadingHTTPServer

WS = Path("/workspace")
s3 = boto3.client("s3")


def safe(path):
    p = (WS / path).resolve()
    if WS.resolve() not in p.parents and p != WS.resolve():
        raise ValueError("path escapes the workspace")
    return p


def run(h, m, body):
    t = time.time()
    cmd = body["cmd"]
    r = subprocess.run(["bash", "-c", cmd], cwd=WS, capture_output=True, text=True, timeout=int(body.get("timeout", 600)))
    return 200, {"exit": r.returncode, "stdout": r.stdout[-2000:], "stderr": r.stderr[-1000:], "seconds": round(time.time() - t, 1)}


def write(h, m, body):
    p = safe(body["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body["content"])
    return 200, {"path": body["path"], "bytes": len(body["content"])}


def listing(h, m, body):
    files = sorted(str(p.relative_to(WS)) for p in WS.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    return 200, {"files": files}


def snapshot(h, m, body):
    session, step = body["session"], body["step"]
    manifest, uploaded, skipped = {}, 0, 0
    for p in sorted(WS.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts:
            continue
        data = p.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        key = f"snapshots/objects/{digest}"
        try:
            s3.head_object(Bucket=BUCKET, Key=key)
            skipped += 1
        except Exception:
            s3.put_object(Bucket=BUCKET, Key=key, Body=data)
            uploaded += 1
        manifest[str(p.relative_to(WS))] = digest
    mbytes = json.dumps(manifest, sort_keys=True).encode()
    sid = hashlib.sha256(mbytes).hexdigest()[:16]
    s3.put_object(Bucket=BUCKET, Key=f"snapshots/manifests/{sid}.json", Body=mbytes)
    return 200, {"snapshot_id": sid, "files": len(manifest), "uploaded": uploaded, "skipped": skipped}


def restore(h, m, body):
    sid = body["snapshot_id"]
    manifest = json.loads(s3.get_object(Bucket=BUCKET, Key=f"snapshots/manifests/{sid}.json")["Body"].read())
    for path, digest in manifest.items():
        p = safe(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(s3.get_object(Bucket=BUCKET, Key=f"snapshots/objects/{digest}")["Body"].read())
    return 200, {"restored_files": len(manifest)}


class H(Handler):
    routes = {
        ("POST", r"/exec"): run,
        ("POST", r"/write"): write,
        ("POST", r"/list"): listing,
        ("POST", r"/snapshot"): snapshot,
        ("POST", r"/restore"): restore,
        ("GET", r"/healthz"): lambda h, m, b: (200, {"ok": True}),
    }


def main():
    WS.mkdir(exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", 8081), H)
    srv.daemon_threads = True
    srv.serve_forever()
