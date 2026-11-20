#!/usr/bin/env python3
"""A small GPT trained with PyTorch DDP, written the way Design 03 asks training code to behave on the platform.

What the platform expects of every training job, and where it shows up here:
  * Restartable: `--resume-from latest` finds the newest COMPLETE checkpoint and continues from it.
  * Sharded checkpoints: every rank writes its own shard, to local disk first, then to S3.
  * Asynchronous checkpointing (default): training pauses only for a device-to-host copy; the write and the
    upload happen on a background thread. `--ckpt-mode sync` writes inline so the two can be compared.
  * A watchdog: if no step completes for `--hang-timeout` seconds (a stuck collective, for example), the
    process exits so the job controller restarts the job. A hang is not a crash, so nothing else would notice.
  * Metrics: step time, tokens per second and checkpoint pause, as events in S3 and as a Prometheus endpoint.

The model is small on purpose. The mechanisms are the same at any size; only the numbers change.
"""
import argparse
import json
import math
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import boto3
import torch
import torch.distributed as dist
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.parallel import DistributedDataParallel as DDP

p = argparse.ArgumentParser()
p.add_argument("--run-id", required=True)
p.add_argument("--max-steps", type=int, default=600)
p.add_argument("--ckpt-every", type=int, default=100)
p.add_argument("--ckpt-mode", choices=["async", "sync"], default="async")
p.add_argument("--resume-from", choices=["latest", "none"], default="latest")
p.add_argument("--batch", type=int, default=16)
p.add_argument("--block", type=int, default=256)
p.add_argument("--layers", type=int, default=12)
p.add_argument("--dim", type=int, default=768)
p.add_argument("--hang-timeout", type=float, default=90.0)
p.add_argument("--startup-timeout", type=float, default=900.0)
p.add_argument("--step-sleep", type=float, default=0.0, help="slow the steps down so a test can act mid-run")
args = p.parse_args()

RANK = int(os.environ.get("RANK", "0"))
WORLD = int(os.environ.get("WORLD_SIZE", "1"))
LOCAL = int(os.environ.get("LOCAL_RANK", "0"))
BUCKET = os.environ["BUCKET"]
PREFIX = f"runs/{args.run_id}"
ATTEMPT = f"{int(time.time())}-r{RANK}"
LOCAL_DIR = "/tmp/ckpt"
os.makedirs(LOCAL_DIR, exist_ok=True)
s3 = boto3.client("s3")

# ---------------------------------------------------------------------------------------------- events ------
EVENTS = []
T0 = time.time()
STATE = {"step": 0, "loss": 0.0, "step_s": 0.0, "tok_s": 0.0, "ckpt_pause": 0.0, "last_step_at": time.time(), "started": False}


def event(kind, **kw):
    rec = {"t": round(time.time(), 3), "rank": RANK, "ev": kind, **kw}
    EVENTS.append(rec)
    print(json.dumps(rec), flush=True)


def flush_events():
    try:
        s3.put_object(Bucket=BUCKET, Key=f"{PREFIX}/events/{ATTEMPT}.jsonl", Body="\n".join(json.dumps(e) for e in EVENTS).encode())
    except Exception as exc:  # events are evidence, never a reason to stop training
        print(f"events upload failed: {exc}", flush=True)


# ------------------------------------------------------------------------------------------- metrics server ---
class Metrics(BaseHTTPRequestHandler):
    def do_GET(self):
        s = STATE
        body = (
            f'ds03_train_step{{rank="{RANK}",run="{args.run_id}"}} {s["step"]}\n'
            f'ds03_train_loss{{rank="{RANK}",run="{args.run_id}"}} {s["loss"]}\n'
            f'ds03_train_step_seconds{{rank="{RANK}",run="{args.run_id}"}} {s["step_s"]}\n'
            f'ds03_train_tokens_per_second{{rank="{RANK}",run="{args.run_id}"}} {s["tok_s"]}\n'
            f'ds03_train_checkpoint_pause_seconds{{rank="{RANK}",run="{args.run_id}"}} {s["ckpt_pause"]}\n'
            f'ds03_train_seconds_since_last_step{{rank="{RANK}",run="{args.run_id}"}} {time.time() - s["last_step_at"]:.1f}\n'
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


threading.Thread(target=lambda: HTTPServer(("0.0.0.0", 9100), Metrics).serve_forever(), daemon=True).start()


# ------------------------------------------------------------------------------------------------ watchdog ---
def watchdog():
    while True:
        time.sleep(5)
        idle = time.time() - STATE["last_step_at"]
        limit = args.hang_timeout if STATE["started"] else args.startup_timeout
        if idle > limit:
            event("watchdog", idle_seconds=round(idle, 1), limit=limit, step=STATE["step"])
            flush_events()
            os._exit(42)


threading.Thread(target=watchdog, daemon=True).start()

# --------------------------------------------------------------------------------------------- burn-in ------
# A node that cannot do a matrix multiply, or reports a GPU error, must not be given a training job.
dev = torch.device("cuda", LOCAL)
torch.cuda.set_device(dev)
t = time.time()
a = torch.randn(4096, 4096, device=dev, dtype=torch.float16)
for _ in range(20):
    b = a @ a
torch.cuda.synchronize()
event("burnin", ok=bool(torch.isfinite(b).all().item()), seconds=round(time.time() - t, 2), gpu=torch.cuda.get_device_name(dev))

dist.init_process_group("nccl", device_id=dev)

# ----------------------------------------------------------------------------------------------- data -------
s3.download_file(BUCKET, "datasets/tinyshakespeare/input.txt", "/tmp/input.txt")
text = open("/tmp/input.txt").read()
chars = sorted(set(text))
vocab = len(chars)
stoi = {c: i for i, c in enumerate(chars)}
data = torch.tensor([stoi[c] for c in text], dtype=torch.long, device=dev)
n_train = int(0.9 * len(data))
train_data = data[:n_train]


def get_batch(step):
    g = torch.Generator(device="cpu")
    g.manual_seed(1000003 * step + RANK)  # the batch depends only on (step, rank): a resumed run sees the same data
    ix = torch.randint(len(train_data) - args.block - 1, (args.batch,), generator=g).to(dev)
    x = torch.stack([train_data[i:i + args.block] for i in ix])
    y = torch.stack([train_data[i + 1:i + args.block + 1] for i in ix])
    return x, y


# ---------------------------------------------------------------------------------------------- model -------
class Block(nn.Module):
    def __init__(self, d, heads):
        super().__init__()
        self.h = heads
        self.ln1, self.ln2 = nn.LayerNorm(d), nn.LayerNorm(d)
        self.qkv, self.proj = nn.Linear(d, 3 * d), nn.Linear(d, d)
        self.mlp = nn.Sequential(nn.Linear(d, 4 * d), nn.GELU(), nn.Linear(4 * d, d))

    def forward(self, x):
        B, T, C = x.shape
        q, k, v = self.qkv(self.ln1(x)).split(C, dim=2)
        q, k, v = (z.view(B, T, self.h, C // self.h).transpose(1, 2) for z in (q, k, v))
        y = F.scaled_dot_product_attention(q, k, v, is_causal=True).transpose(1, 2).reshape(B, T, C)
        x = x + self.proj(y)
        return x + self.mlp(self.ln2(x))


class GPT(nn.Module):
    def __init__(self):
        super().__init__()
        d = args.dim
        self.tok, self.pos = nn.Embedding(vocab, d), nn.Embedding(args.block, d)
        self.blocks = nn.Sequential(*[Block(d, max(1, d // 64)) for _ in range(args.layers)])
        self.ln, self.head = nn.LayerNorm(d), nn.Linear(d, vocab)

    def forward(self, x, y):
        h = self.tok(x) + self.pos(torch.arange(x.shape[1], device=x.device))
        logits = self.head(self.ln(self.blocks(h)))
        return F.cross_entropy(logits.view(-1, vocab), y.view(-1))


torch.manual_seed(0)
model = GPT().to(dev)
n_params = sum(p_.numel() for p_ in model.parameters())
ddp = DDP(model, device_ids=[LOCAL])
opt = torch.optim.AdamW(ddp.parameters(), lr=3e-4)
scaler = torch.amp.GradScaler()


# ------------------------------------------------------------------------------------------ checkpoints ----
def flat_state():
    """Model and optimizer state as one ordered list of (name, tensor)."""
    items = [(f"model.{k}", v) for k, v in model.state_dict().items()]
    for i, st in enumerate(opt.state_dict()["state"].values()):
        items += [(f"opt.{i}.{k}", v) for k, v in st.items() if torch.is_tensor(v)]
    return items


def snapshot(step):
    """Device-to-host copy of this rank's shard. This is the only part training must wait for."""
    items = flat_state()
    mine = {k: v.detach().to("cpu") for i, (k, v) in enumerate(items) if i % WORLD == RANK}
    return {"step": step, "tensors": mine}


def write_and_upload(snap):
    step = snap["step"]
    name = f"shard-{RANK}-of-{WORLD}.pt"
    path = f"{LOCAL_DIR}/step-{step:06d}-{name}"
    t = time.time()
    torch.save(snap, path)
    local_s = time.time() - t
    t = time.time()
    key = f"{PREFIX}/ckpt/step-{step:06d}/{name}"
    s3.upload_file(path, BUCKET, key)
    s3.put_object(Bucket=BUCKET, Key=f"{PREFIX}/ckpt/step-{step:06d}/done-{RANK}", Body=b"1")  # the marker is written LAST
    size = os.path.getsize(path)
    event("ckpt_written", step=step, local_seconds=round(local_s, 2), upload_seconds=round(time.time() - t, 2), mb=round(size / 1e6, 1))
    os.remove(path)
    flush_events()


UPLOADER = {"thread": None}


def checkpoint(step):
    t = time.time()
    th = UPLOADER["thread"]
    if th is not None and th.is_alive():
        th.join()  # the previous checkpoint must finish first; any wait counts as pause
    snap = snapshot(step)
    if args.ckpt_mode == "sync":
        write_and_upload(snap)
    else:
        UPLOADER["thread"] = threading.Thread(target=write_and_upload, args=(snap,), daemon=True)
        UPLOADER["thread"].start()
    pause = time.time() - t
    STATE["ckpt_pause"] = pause
    event("ckpt", step=step, mode=args.ckpt_mode, pause_seconds=round(pause, 3))


def latest_complete_step():
    steps = {}
    pager = s3.get_paginator("list_objects_v2")
    for page in pager.paginate(Bucket=BUCKET, Prefix=f"{PREFIX}/ckpt/"):
        for o in page.get("Contents", []):
            m = re.match(rf"{PREFIX}/ckpt/step-(\d+)/done-(\d+)$", o["Key"])
            if m:
                steps.setdefault(int(m.group(1)), set()).add(int(m.group(2)))
    full = [s for s, got in steps.items() if len(got) == WORLD]
    return max(full) if full else 0


def restore(step):
    merged = {}
    for r in range(WORLD):
        path = f"{LOCAL_DIR}/restore-{r}.pt"
        s3.download_file(BUCKET, f"{PREFIX}/ckpt/step-{step:06d}/shard-{r}-of-{WORLD}.pt", path)
        merged.update(torch.load(path, map_location="cpu")["tensors"])
        os.remove(path)
    sd = {k[len("model."):]: v for k, v in merged.items() if k.startswith("model.")}
    model.load_state_dict(sd)
    # The optimizer state is rebuilt by taking one tiny step first, then overwriting the tensors.
    x, y = get_batch(0)
    with torch.autocast("cuda", dtype=torch.float16):
        loss = ddp(x, y)
    scaler.scale(loss).backward()
    scaler.step(opt)
    scaler.update()
    opt.zero_grad(set_to_none=True)
    model.load_state_dict(sd)
    states = list(opt.state_dict()["state"].values())
    for i, st in enumerate(states):
        for k, v in st.items():
            if torch.is_tensor(v):
                v.copy_(merged[f"opt.{i}.{k}"].to(v.device))


# ------------------------------------------------------------------------------------------------ run -------
start_step = 0
event("start", run=args.run_id, world=WORLD, params_m=round(n_params / 1e6, 1), mode=args.ckpt_mode, node=os.environ.get("NODE_NAME", ""))
if args.resume_from == "latest":
    cand = torch.tensor([latest_complete_step() if RANK == 0 else 0], device=dev)
    dist.broadcast(cand, src=0)
    start_step = int(cand.item())
    if start_step > 0:
        t = time.time()
        restore(start_step)
        event("resume", step=start_step, seconds=round(time.time() - t, 2))
dist.barrier()
STATE["last_step_at"] = time.time()
STATE["step"] = start_step
step_times = []
for step in range(start_step + 1, args.max_steps + 1):
    t = time.time()
    x, y = get_batch(step)
    with torch.autocast("cuda", dtype=torch.float16):
        loss = ddp(x, y)
    scaler.scale(loss).backward()
    scaler.step(opt)
    scaler.update()
    opt.zero_grad(set_to_none=True)
    torch.cuda.synchronize()
    if args.step_sleep:
        time.sleep(args.step_sleep)
    dt = time.time() - t
    step_times.append(dt)
    STATE.update(step=step, loss=float(loss.item()), step_s=dt, tok_s=args.batch * args.block * WORLD / dt,
                 last_step_at=time.time(), started=True)
    if step == start_step + 1:
        event("first_step", step=step, seconds_since_start=round(time.time() - T0, 1))
    if step % 20 == 0:
        event("step", step=step, loss=round(STATE["loss"], 4), step_seconds=round(dt, 3), tokens_per_second=round(STATE["tok_s"]))
    if step % args.ckpt_every == 0 and step < args.max_steps:
        checkpoint(step)
if UPLOADER["thread"] is not None:
    UPLOADER["thread"].join()
checkpoint(args.max_steps)
if UPLOADER["thread"] is not None:
    UPLOADER["thread"].join()
event("done", steps=args.max_steps, started_at_step=start_step, median_step_seconds=round(sorted(step_times)[len(step_times) // 2], 3) if step_times else None,
      wall_seconds=round(time.time() - T0, 1))
flush_events()
dist.barrier()
dist.destroy_process_group()
