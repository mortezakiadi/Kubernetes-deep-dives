"""Weight-sync benchmark: how long does it take to push a model of a given size from the trainer's GPU into the rollout fleet?

The rollout fleet is started with dummy weights (so nothing is downloaded), the trainer builds the same architecture with random
weights, and the same NCCL weight-transfer path the real runs use sends it, several times. What is measured is the transfer, not learning.
Prints one JSON line.
"""
import json
import os
import statistics
import time
import urllib.request

import torch
from transformers import AutoConfig, AutoModelForCausalLM
from vllm.distributed.weight_transfer import HTTPVLLMWeightSyncClient, ModuleSource, WeightTransferTrainerFactory
from vllm.distributed.weight_transfer.nccl_engine import NCCLTrainerInitInfo
from vllm.utils.network_utils import get_ip, get_open_port

MODEL = os.environ["MODEL"]
URL = os.environ.get("VLLM_URL", "http://rollout-api.rl-scale.svc:8000")
ITERS = int(os.environ.get("ITERS", "5"))
PACKED = os.environ.get("PACKED", "1") == "1"
BUFFER_MB = int(os.environ.get("BUFFER_MB", "1024"))

world = json.loads(urllib.request.urlopen(URL + "/get_world_size", timeout=30).read())["world_size"]
cfg = AutoConfig.from_pretrained(MODEL)
torch.manual_seed(0)
with torch.device("cuda"):
    model = AutoModelForCausalLM.from_config(cfg, dtype=torch.bfloat16)
n_params = sum(p.numel() for p in model.parameters())
n_bytes = sum(p.numel() * p.element_size() for p in model.parameters())
print(f"model {MODEL}: {n_params / 1e9:.2f}B parameters, {n_bytes / 1e9:.2f} GB in bf16; inference workers: {world}", flush=True)

t0 = time.time()
engine = WeightTransferTrainerFactory.trainer_init(
    init_info=NCCLTrainerInitInfo(master_address=get_ip(), master_port=get_open_port(), world_size=world + 1, rank=0, packed=PACKED,
                                  packed_buffer_size_bytes=BUFFER_MB << 20),
    client=HTTPVLLMWeightSyncClient(URL),
    source=ModuleSource(model),
)
init_s = time.time() - t0
times = []
for i in range(ITERS):
    torch.cuda.synchronize()
    t = time.time()
    engine.send_weights()
    torch.cuda.synchronize()
    times.append(time.time() - t)
    print(f"send {i}: {times[-1]:.2f}s", flush=True)
first, rest = times[0], times[1:] or times
med = statistics.median(rest)
print("RESULT " + json.dumps({"model": MODEL, "params_b": n_params / 1e9, "gb": n_bytes / 1e9, "workers": world, "packed": PACKED, "buffer_mb": BUFFER_MB,
                              "init_s": init_s, "first_s": first, "median_s": med, "min_s": min(rest), "max_s": max(rest),
                              "gbps_per_worker": n_bytes / 1e9 / med, "gbit_total": n_bytes * 8 * world / 1e9 / med}), flush=True)
