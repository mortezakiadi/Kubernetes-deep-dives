"""The state bucket: a run's metrics and events are copied there while it runs, so they outlive the pod and the node.
Checkpoints stay on the node (they are only rollback targets inside one run); the files that tests read are small."""
import pathlib
import threading
import traceback

import os


def s3():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def sync_loop(work: pathlib.Path, bucket: str, prefix: str, stop: threading.Event, every=15):
    """Copy new and changed small files of the run (metrics, events, the controller's settings record) to the bucket."""
    if not bucket:
        return
    client, seen = s3(), {}

    def once():
        for p in sorted(work.rglob("*")):
            if not p.is_file() or "checkpoint" in str(p.relative_to(work)) or "/hf/" in str(p) or p.stat().st_size > 50_000_000:
                continue
            rel = str(p.relative_to(work))
            st = (p.stat().st_size, int(p.stat().st_mtime))
            if seen.get(rel) == st:
                continue
            client.upload_file(str(p), bucket, prefix + rel)
            seen[rel] = st

    while not stop.is_set():
        try:
            once()
        except Exception:
            traceback.print_exc()
        stop.wait(every)
    try:
        once()
    except Exception:
        traceback.print_exc()
