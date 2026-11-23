"""The trainer's side of long-horizon credit, on one GPU with the same model the rollout engine serves.

    trainer.py logprobs --trajectories A,B --out credit/R/logprob-check.json
        For every turn, recompute the log-probabilities of the tokens the model generated, (a) on the context the
        model actually saw (stored with the turn, compacted where the episode was compacted) and (b) on the full,
        uncompacted history. (a) must match the rollout engine's numbers; (b) is what a trainer gets wrong.

    trainer.py update --trajectory A --credit credit/R/A.json --out credit/R/update-A.json
        The first-order effect of ONE policy-gradient step (on a LoRA adapter) on each turn's mean token log-probability, for two
        advantage assignments of the same trajectory: trajectory-level (every turn the same) and the Monte Carlo
        turn-level advantages from the credit worker. delta_t = lr * <grad mean_logp_t, grad J>, J = sum_t A_t mean_logp_t.

Runs as a Job on a GPU node; reads and writes only through the trajectory store.
"""
import argparse
import json
import os
import sys

import torch
from huggingface_hub import snapshot_download
from transformers import AutoModelForCausalLM, AutoTokenizer

sys.path.insert(0, "/code")
import agent  # noqa: E402
from common import get, put, task  # noqa: E402

REPO, REV = os.environ.get("POLICY_REPO", "Qwen/Qwen2.5-7B-Instruct"), os.environ["POLICY_REVISION"]


def load():
    path = snapshot_download(REPO, revision=REV, cache_dir="/work/hf")
    tok = AutoTokenizer.from_pretrained(path)
    model = AutoModelForCausalLM.from_pretrained(path, dtype=torch.bfloat16, device_map="cuda")  # straight to the GPU: the node has 16 GiB of RAM
    model.gradient_checkpointing_enable()
    model.config.use_cache = False
    return tok, model


def ids_for(tok, msgs, completion_ids):
    prompt = tok.apply_chat_template(msgs, add_generation_prompt=True, tokenize=True)
    prompt = list(prompt["input_ids"] if hasattr(prompt, "keys") else prompt)
    return prompt, prompt + list(completion_ids)


def token_logps(model, prompt, full):
    x = torch.tensor([full]).cuda()
    logits = model(input_ids=x, use_cache=False).logits[0, len(prompt) - 1:-1].float()
    return torch.log_softmax(logits, -1).gather(1, x[0, len(prompt):].unsqueeze(1)).squeeze(1)


def cmd_logprobs(a, tok, model):
    model.eval()
    rows = []
    for tid in a.trajectories.split(","):
        tr = get(f"trajectories/{tid}.json")
        issue = task(tr["task_id"])["issue"]
        for t in tr["turns"]:
            lp = get(t["logprobs_ref"])
            seen = get(t["context_ref"])
            full_hist = agent.messages(issue, tr["turns"][: t["index"]], 0)
            with torch.no_grad():
                p1, f1 = ids_for(tok, seen, lp["token_ids"])
                a1 = token_logps(model, p1, f1)
                p2, f2 = ids_for(tok, full_hist, lp["token_ids"])
                a2 = token_logps(model, p2, f2)
            ref = torch.tensor(lp["logprobs"]).cuda()
            rows.append({"trajectory": tid, "turn": t["index"], "compacted": t["compacted_upto"] > 0, "tokens": len(lp["token_ids"]),
                         "prompt_tokens_rollout": t["prompt_tokens"], "prompt_tokens_seen": len(p1), "prompt_tokens_full": len(p2),
                         "mean_abs_diff_seen": round(float((a1 - ref).abs().mean()), 4),
                         "mean_abs_diff_full_history": round(float((a2 - ref).abs().mean()), 4)})
            print(json.dumps(rows[-1]), flush=True)
    put(a.out, {"rows": rows, "model": f"{REPO}@{REV}"})


def cmd_update(a, tok, model):  # noqa: C901
    model.train()  # gradient checkpointing is active only in train mode (Qwen2.5 has no dropout)
    tr, cr = get(f"trajectories/{a.trajectory}.json"), get(a.credit)
    # The policy step is taken on a LoRA adapter (rank 16 on the attention projections): a 7B model's full gradients do
    # not fit next to its weights on a 24 GB GPU. The first-order effect per turn is measured on the adapter's parameters.
    from peft import LoraConfig, get_peft_model
    model = get_peft_model(model, LoraConfig(r=16, lora_alpha=32, target_modules=["q_proj", "k_proj", "v_proj", "o_proj"], lora_dropout=0.0))
    model.enable_input_require_grads()
    params = [p for p in model.parameters() if p.requires_grad]
    turns = []
    for t in tr["turns"]:
        lp = get(t["logprobs_ref"])
        turns.append(ids_for(tok, get(t["context_ref"]), lp["token_ids"]))

    def grad_of(weights):
        model.zero_grad(set_to_none=True)
        for (prompt, full), w in zip(turns, weights):
            if w:
                (token_logps(model, prompt, full).mean() * w).backward()
        return [p.grad.detach().clone() if p.grad is not None else torch.zeros_like(p) for p in params]

    out = {"trajectory": a.trajectory, "outcome": tr["outcome"], "lr": a.lr, "trainable": "LoRA r=16 on q,k,v,o", "methods": {}}
    for method in ("turn_advantage_trajectory", "turn_advantage_mc"):
        adv = cr[method]
        gJ = grad_of(adv)                       # gradient of J = sum_t A_t * mean log p_t
        deltas = []
        for i, (prompt, full) in enumerate(turns):
            model.zero_grad(set_to_none=True)
            token_logps(model, prompt, full).mean().backward()   # gradient of this turn's mean log-probability
            dot = sum(float((q.grad.float() * g.float()).sum()) for q, g in zip(params, gJ) if q.grad is not None)
            deltas.append(round(a.lr * dot, 6))
        del gJ
        torch.cuda.empty_cache()
        out["methods"][method] = {"advantages": adv, "delta_mean_logp": deltas}
        print(json.dumps({"method": method, "advantages": adv, "delta": deltas}), flush=True)
    out["actions"] = [t["action"].get("tool") for t in tr["turns"]]
    put(a.out, out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["logprobs", "update"])
    p.add_argument("--trajectories")
    p.add_argument("--trajectory")
    p.add_argument("--credit")
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--out", required=True)
    a = p.parse_args()
    tok, model = load()
    (cmd_logprobs if a.mode == "logprobs" else cmd_update)(a, tok, model)


if __name__ == "__main__":
    main()
