#!/usr/bin/env python3
"""A resumable batch-inference stand-in: it does GPU work in numbered items and records its progress in S3.

Design 03 says only work that tolerates eviction may borrow serving GPUs, and that such work must resume from where it
stopped. This job does exactly that: when Kueue takes its GPU back and the job is later admitted again, it reads
its progress and continues with the next item instead of starting over.
"""
import json
import os
import time

import boto3
import torch

BUCKET, JOB, ITEMS = os.environ["BUCKET"], os.environ["JOB_NAME"], int(os.environ.get("ITEMS", "60"))
KEY = f"batch/{JOB}/progress.json"
s3 = boto3.client("s3")
try:
    done = json.loads(s3.get_object(Bucket=BUCKET, Key=KEY)["Body"].read())["done"]
except s3.exceptions.NoSuchKey:
    done = 0
print(json.dumps({"ev": "start", "resumed_from_item": done, "total": ITEMS, "gpu": torch.cuda.get_device_name(0), "t": time.time()}), flush=True)
x = torch.randn(4096, 4096, device="cuda", dtype=torch.float16)
for i in range(done, ITEMS):
    t = time.time()
    while time.time() - t < 5:  # one item is about five seconds of real GPU work
        (x @ x).sum().item()
    s3.put_object(Bucket=BUCKET, Key=KEY, Body=json.dumps({"done": i + 1}).encode())
    print(json.dumps({"ev": "item", "item": i + 1}), flush=True)
print(json.dumps({"ev": "finished", "items": ITEMS, "t": time.time()}), flush=True)
