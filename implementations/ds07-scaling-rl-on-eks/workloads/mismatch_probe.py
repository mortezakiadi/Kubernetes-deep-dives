"""Train-versus-inference mismatch: score the same sampled answers with the rollout engine and with the trainer's own forward pass.

The rollout fleet samples answers and returns the probability it gave each sampled token. The trainer, which will compute the loss,
recomputes the probability of the same tokens with its own kernels. If the two engines computed the same function the log-ratio is
zero; the spread of the ratio is the mismatch. Prints one JSON line.
"""
import json
import math
import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

MODEL = os.environ.get("MODEL", "Qwen/Qwen3-0.6B")
URL = os.environ.get("VLLM_URL", "http://rollout-api.rl-scale.svc:8000")
TAG = os.environ.get("TAG", "bf16")
N_PROMPTS = int(os.environ.get("N_PROMPTS", "64"))
N_SAMPLES = int(os.environ.get("N_SAMPLES", "4"))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "512"))
TRAIN_DTYPE = os.environ.get("TRAIN_DTYPE", "bfloat16")
CAP = float(os.environ.get("TIS_CAP", "3.0"))


def post(path, body):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=900).read())


rows = [json.loads(line) for line in urllib.request.urlopen(
    "https://raw.githubusercontent.com/openai/grade-school-math/master/grade_school_math/data/test.jsonl", timeout=60).read().decode().splitlines()[:N_PROMPTS]]
tok = AutoTokenizer.from_pretrained(MODEL)
texts = [tok.apply_chat_template([{"role": "user", "content": r["question"] + " Think step by step, then put the final answer in \\boxed{}."}],
                                 add_generation_prompt=True, tokenize=False, enable_thinking=False) for r in rows]
prompts = [tok(t, add_special_tokens=False)["input_ids"] for t in texts]
served = json.loads(urllib.request.urlopen(URL + "/v1/models", timeout=30).read())["data"][0]["id"]


def sample(ids):
    r = post("/v1/completions", {"model": served, "prompt": ids, "n": N_SAMPLES, "temperature": 1.0, "max_tokens": MAX_TOKENS, "logprobs": 0,
                                 "return_tokens_as_token_ids": True})
    out = []
    for ch in r["choices"]:
        toks = [int(t.split(":")[1]) for t in ch["logprobs"]["tokens"]]
        out.append((toks, ch["logprobs"]["token_logprobs"]))
    return out


with ThreadPoolExecutor(8) as ex:
    sampled = list(ex.map(sample, prompts))

model = AutoModelForCausalLM.from_pretrained(MODEL, dtype=getattr(torch, TRAIN_DTYPE)).cuda().eval()
log_ratios = []
for ids, group in zip(prompts, sampled):
    for toks, server_lp in group:
        if not toks:
            continue
        full = torch.tensor([ids + toks], device="cuda")
        with torch.no_grad():
            logits = model(full).logits[0, len(ids) - 1:-1].float()
        lp = torch.log_softmax(logits, dim=-1).gather(1, torch.tensor(toks, device="cuda")[:, None])[:, 0].cpu()
        log_ratios += (lp - torch.tensor(server_lp)).tolist()

lr = torch.tensor(log_ratios)
ratio = lr.exp()
n = len(lr)
abs_sorted = lr.abs().sort(descending=True).values
top1 = abs_sorted[: max(1, n // 100)].sum() / abs_sorted.sum()
w = ratio.clamp(max=CAP)
result = {
    "tag": TAG, "tokens": n, "train_dtype": TRAIN_DTYPE,
    "mean_abs_logratio": lr.abs().mean().item(),
    "kl_k3": ((ratio - 1) - lr).mean().item(),
    "frac_ratio_gt_2": (ratio > 2).float().mean().item(),
    "frac_ratio_lt_half": (ratio < 0.5).float().mean().item(),
    "frac_ratio_gt_cap": (ratio > CAP).float().mean().item(),
    "max_ratio": ratio.max().item(),
    "p99_abs_logratio": lr.abs().quantile(0.99).item(),
    "top1pct_share_of_mismatch": top1.item(),
    "tis_weight_mean": w.mean().item(),
}
print("RESULT " + json.dumps(result), flush=True)
