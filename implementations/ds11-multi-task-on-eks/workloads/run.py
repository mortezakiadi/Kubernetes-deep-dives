"""One experiment of Design 11: GRPO post-training of a small model on a mixture of task families, with per-family measurements.

Everything a run is comes from environment variables (see `Recipe`), so a sweep cell is one pod with different values. MODE selects what the pod does:
    train   GRPO on the mixture (MIX), starting from the base model or from the final weights of another run (INIT_FROM); optionally saves its final weights
    eval    score a model (the base model, or the final weights of INIT_FROM) on every family's held-out set; no training
    verify  the verifier-precision gate and the decontamination check of the domain contract
    probe   per-module gradients of each family's loss on the model, and their cosines (the interference probe)
    merge   combine the final weights of several runs into one model (task arithmetic with trimming and sign agreement), save it as a run's final weights
The run writes one JSON line per optimizer step group (kind "families"), per evaluation (kind "eval") and so on to metrics.jsonl, copied to the state bucket.
"""
import json
import os
import pathlib
import random
import shutil
import sys
import threading
import time
import traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import families as F  # noqa: E402

if os.environ.get("GRID"):   # a sweep: this pod is cell number JOB_COMPLETION_INDEX of the grid
    os.environ.update({k: str(v) for k, v in json.loads(os.environ["GRID"])[int(os.environ["JOB_COMPLETION_INDEX"])].items()})


def _e(name, default, cast=str):
    v = os.environ.get(name)
    return cast(v) if v not in (None, "") else default


def _bool(v):
    return str(v).lower() in ("1", "true", "yes")


EQUAL = {f: 1 / len(F.FAMILIES) for f in F.FAMILIES}   # the seventh family has weight 0 unless a recipe gives it one
MEAN_TOKENS = {"math": 219, "code": 61, "instr": 34, "tools": 35, "multiling": 14, "safety": 190, "sorting": 29}   # measured on the base model (S02); used to balance a batch by tokens


@dataclass
class Recipe:
    run_id: str = _e("RUN_ID", "dev")
    mode: str = _e("MODE", "train")
    model: str = _e("MODEL", "Qwen/Qwen3-0.6B")
    init_from: str = _e("INIT_FROM", "")                  # a run whose final weights this run starts from ("" = the base model)
    steps: int = _e("STEPS", 100, int)
    lr: float = _e("LR", 3e-6, float)
    seed: int = _e("SEED", 1, int)
    rows: int = _e("ROWS", 32, int)                       # rollouts per micro-batch
    accum: int = _e("ACCUM", 2, int)                      # micro-batches per optimizer step
    group: int = _e("GROUP", 4, int)                      # answers per prompt
    max_completion: int = _e("MAX_COMPLETION", 256, int)
    mix: str = _e("MIX", "")                              # JSON {family: weight}; "" = equal weights
    floor: float = _e("FLOOR", 0.0, float)                # no family below this share ("minimum shares")
    balance: bool = _bool(_e("BALANCE", "0"))             # weights are shares of TOKENS, not of prompts
    eval_every: int = _e("EVAL_EVERY", 25, int)
    eval_n: int = _e("EVAL_N", 100, int)                  # held-out prompts per family
    eval_samples: int = _e("EVAL_SAMPLES", 4, int)
    eval_seed: int = _e("EVAL_SEED", 0, int)              # the sampling seed of the evaluation (S02 measures its noise with it)
    save_final: bool = _bool(_e("SAVE_FINAL", "0"))
    probe_prompts: int = _e("PROBE_PROMPTS", 16, int)
    merge_from: str = _e("MERGE_FROM", "")                # JSON {run_id: weight} for MODE=merge
    merge_keep: float = _e("MERGE_KEEP", 0.2, float)      # share of each task vector kept by the trimming step
    gpu_util: float = _e("GPU_UTIL", 0.0, float)
    pool: int = _e("POOL", 3000, int)                     # training prompts generated per family


R = Recipe()
BUCKET = _e("STATE_BUCKET", "")
WORK = pathlib.Path(_e("WORK_DIR", "/work")) / R.run_id
WORK.mkdir(parents=True, exist_ok=True)
METRICS = WORK / "metrics.jsonl"
T0 = time.time()


def log_line(rec):
    with METRICS.open("a") as f:
        f.write(json.dumps({"t": time.time(), **rec}) + "\n")


# ------------------------------------------------------------------------------------------------------------
# State bucket
# ------------------------------------------------------------------------------------------------------------
def s3():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def upload_dir(path, prefix):
    c = s3()
    for p in sorted(pathlib.Path(path).rglob("*")):
        if p.is_file():
            c.upload_file(str(p), BUCKET, prefix + str(p.relative_to(path)))


def fetch_dir(prefix, dest):
    c, got = s3(), 0
    dest = pathlib.Path(dest)
    for page in c.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        for o in page.get("Contents", []):
            rel = o["Key"][len(prefix):]
            if rel and not rel.endswith("/"):
                (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                c.download_file(BUCKET, o["Key"], str(dest / rel))
                got += 1
    if not got:
        raise RuntimeError(f"nothing under s3://{BUCKET}/{prefix}")
    return str(dest)


def model_path(run_id=None):
    """The model to start from: the base model by name, or the final weights of another run, downloaded."""
    run_id = R.init_from if run_id is None else run_id
    return fetch_dir(f"runs/{run_id}/final/", f"/work/models/{run_id}") if run_id else R.model


# ------------------------------------------------------------------------------------------------------------
# Data
# ------------------------------------------------------------------------------------------------------------
def normalize_mix():
    mix = json.loads(R.mix) if R.mix else dict(EQUAL)
    mix = {f: max(float(mix.get(f, 0.0)), 0.0) for f in F.ALL}
    if R.floor:
        mix = {f: (max(v, R.floor) if f in F.FAMILIES else v) for f, v in mix.items()}
    tot = sum(mix.values())
    return {f: v / tot for f, v in mix.items()}


def sample_shares(mix):
    """Weights are shares of the batch's prompts, or (BALANCE) of its tokens: a family of short answers then needs more prompts for the same share."""
    if not R.balance:
        return mix
    w = {f: v / MEAN_TOKENS[f] for f, v in mix.items()}
    tot = sum(w.values())
    return {f: v / tot for f, v in w.items()}


def load_math():
    from datasets import load_dataset
    train = load_dataset("openai/gsm8k", "main", split="train").shuffle(seed=R.seed)
    test = load_dataset("openai/gsm8k", "main", split="test")
    fmt = lambda r: {"prompt": r["question"] + F.BOXED, "solution": r["answer"].split("####")[-1].strip(), "meta": {}}  # noqa: E731
    return [fmt(r) for r in train.select(range(min(len(train), R.pool)))], [fmt(test[i]) for i in range(R.eval_n)]


def row(family, ex):
    return {"prompt": [{"role": "user", "content": ex["prompt"]}], "family": family, "meta": json.dumps(ex["meta"]), "solution": ex.get("solution", "")}


def build_data():
    """(training rows in order, {family: held-out rows}). The training stream is drawn family by family at the mixture's shares."""
    math_train, math_eval = load_math()
    pools = {"math": math_train}
    evals = {"math": math_eval}
    for fam in F.GENERATORS:
        pools[fam] = F.make_examples(fam, R.pool, 1000 + R.seed, "train")
        evals[fam] = F.make_examples(fam, R.eval_n, 9999, "eval")
    shares = sample_shares(normalize_mix())
    rng = random.Random(R.seed)
    n = R.steps * (R.rows * R.accum // R.group) + 64
    fams = rng.choices(list(shares), weights=list(shares.values()), k=n)
    cursor = defaultdict(int)
    train = []
    for fam in fams:
        train.append(row(fam, pools[fam][cursor[fam] % len(pools[fam])]))
        cursor[fam] += 1
    return train, {fam: [row(fam, e) for e in rows] for fam, rows in evals.items()}, {f: fams.count(f) for f in F.ALL}


# ------------------------------------------------------------------------------------------------------------
# Rewards
# ------------------------------------------------------------------------------------------------------------
def score_one(family, text, meta, solution):
    if family == "math":
        from trl.rewards import accuracy_reward
        return float(accuracy_reward([[{"content": text}]], [solution])[0] or 0.0)
    return F.VERIFIERS[family](text, meta)


class MultiReward:
    """One reward function for every family, dispatching on the row's family; keeps what the last generation scored for the per-family report."""
    __name__ = "multi_reward"

    def __init__(self):
        self.last = {"rewards": [], "families": [], "tokens": []}
        self.pool = ThreadPoolExecutor(8)

    def __call__(self, completions, family, meta, solution, completion_ids=None, **kwargs):
        texts = [(c[-1]["content"] if isinstance(c, list) else c) for c in completions]
        metas = [json.loads(m) for m in meta]
        # math-verify's timeouts use signals, which only work in the main thread: the other families are scored in threads (their verifiers run child processes)
        # while the main thread scores the maths answers
        jobs = [(i, a) for i, a in enumerate(zip(family, texts, metas, solution)) if a[0] != "math"]
        futs = [(i, self.pool.submit(score_one, *a)) for i, a in jobs]
        rs = [0.0] * len(texts)
        for i, (fam, text, meta, sol) in enumerate(zip(family, texts, metas, solution)):
            if fam == "math":
                rs[i] = score_one(fam, text, meta, sol)
        for i, f in futs:
            rs[i] = f.result()
        self.last = {"rewards": rs, "families": list(family), "tokens": [len(x) for x in completion_ids] if completion_ids else []}
        return rs


# ------------------------------------------------------------------------------------------------------------
# Evaluation: every family's held-out set, through the engine that sampled the training data
# ------------------------------------------------------------------------------------------------------------
def evaluate(llm, tok, evals):
    from trl.rewards.accuracy_rewards import _grade
    from vllm import SamplingParams
    t0 = time.time()
    sp = SamplingParams(n=R.eval_samples, temperature=1.0, top_p=1.0, max_tokens=R.max_completion, seed=R.eval_seed)
    out, lens = {}, {}
    pool = ThreadPoolExecutor(8)
    for fam, rows in evals.items():
        prompts = [tok.apply_chat_template(r["prompt"], tokenize=False, add_generation_prompt=True, enable_thinking=False) for r in rows]
        gens = llm.generate(prompts, sp, use_tqdm=False)
        scores, n_tok = [], []
        for r, g in zip(rows, gens):
            meta = json.loads(r["meta"])
            texts = [c.text for c in g.outputs]
            n_tok += [len(c.token_ids) for c in g.outputs]
            if fam == "math":
                s = [float(_grade(r["solution"], t)[0] or 0.0) for t in texts]
            else:
                s = list(pool.map(lambda t: F.VERIFIERS[fam](t, meta), texts))
            scores.append(sum(s) / len(s))
        out[fam] = sum(scores) / len(scores)
        lens[fam] = sum(n_tok) / len(n_tok)
    return {"by_family": out, "mean": sum(out.values()) / len(out), "min": min(out.values()), "tokens": lens, "seconds": time.time() - t0}


# ------------------------------------------------------------------------------------------------------------
# MODE=train
# ------------------------------------------------------------------------------------------------------------
def train_mode():
    import torch
    from transformers import AutoTokenizer, TrainerCallback
    from trl import GRPOConfig, GRPOTrainer

    train, evals, counts = build_data()
    from datasets import Dataset
    path = model_path()
    tok = AutoTokenizer.from_pretrained(R.model)
    reward = MultiReward()
    props = torch.cuda.get_device_properties(0)
    cfg = GRPOConfig(
        output_dir=str(WORK / "hf"), per_device_train_batch_size=R.rows, gradient_accumulation_steps=R.accum, num_generations=R.group,
        max_completion_length=R.max_completion, temperature=1.0, top_p=1.0, top_k=0, learning_rate=R.lr, lr_scheduler_type="constant", max_steps=R.steps,
        bf16=True, seed=R.seed, report_to="none", logging_steps=1, save_strategy="no", use_vllm=True, vllm_mode="colocate",
        vllm_gpu_memory_utilization=R.gpu_util or round(8 * 2 ** 30 / props.total_memory, 3), vllm_max_model_length=1024, beta=0.0,
        chat_template_kwargs={"enable_thinking": False}, gradient_checkpointing=True, log_completions=False, max_grad_norm=1.0, shuffle_dataset=False,
    )
    G = R.group

    class Trainer(GRPOTrainer):
        def _generate_and_score_completions(self, inputs):
            out = super()._generate_and_score_completions(inputs)
            rs, fams, toks = reward.last["rewards"], reward.last["families"], reward.last["tokens"]
            rec = {}
            for g in range(len(rs) // G):
                f = fams[g * G]
                block = rs[g * G:(g + 1) * G]
                d = rec.setdefault(f, {"groups": 0, "reward": 0.0, "flat": 0, "tokens": 0.0, "adv": 0.0})
                d["groups"] += 1
                d["reward"] += sum(block) / G
                d["flat"] += len(set(block)) == 1
                d["tokens"] += (sum(toks[g * G:(g + 1) * G]) / G) if toks else 0
                m = sum(block) / G
                sd = (sum((x - m) ** 2 for x in block) / G) ** 0.5
                d["adv"] += sum(abs(x - m) / (sd + 1e-6) for x in block) / G if sd > 0 else 0.0
            log_line({"kind": "families", "step": int(self.state.global_step), "by_family": {f: {"groups": d["groups"], "reward": d["reward"] / d["groups"], "flat_share": d["flat"] / d["groups"],
                                                                                              "mean_tokens": d["tokens"] / d["groups"], "signal": d["adv"] / d["groups"]} for f, d in rec.items()}})
            return out

    class RunLog(TrainerCallback):
        def on_log(self, args, state, control, logs=None, **kw):
            if logs:
                log_line({"kind": "train", "step": state.global_step, **{k: v for k, v in logs.items() if isinstance(v, (int, float))}})

        def on_step_begin(self, args, state, control, **kw):
            if ((R.eval_every and state.global_step % R.eval_every == 0) or state.global_step == 0) and state.global_step not in done_evals:
                run_eval(state.global_step)

        def on_train_end(self, args, state, control, **kw):
            if state.global_step not in done_evals:
                run_eval(state.global_step)

    done_evals = set()

    def run_eval(step):
        done_evals.add(step)
        gen = trainer.vllm_generation
        if trainer._last_loaded_step != step:
            gen.sync_weights()
            trainer._last_loaded_step = step
        log_line({"kind": "eval", "step": step, **evaluate(gen.llm, tok, evals)})

    trainer = Trainer(model=path, reward_funcs=reward, args=cfg, train_dataset=Dataset.from_list(train), processing_class=tok, callbacks=[RunLog()])
    import transformers, trl, vllm
    log_line({"kind": "start", "recipe": dict(R.__dict__), "mix": normalize_mix(), "prompt_counts": counts, "gpu": props.name, "gpu_gb": round(props.total_memory / 2 ** 30, 1),
              "versions": {"vllm": vllm.__version__, "trl": trl.__version__, "transformers": transformers.__version__, "torch": torch.__version__}})
    print(f"run {R.run_id}: starting", flush=True)
    trainer.train()
    if R.save_final and BUCKET:
        out = WORK / "final"
        trainer.model.save_pretrained(out, safe_serialization=True)
        tok.save_pretrained(out)
        upload_dir(out, f"runs/{R.run_id}/final/")
        shutil.rmtree(out, ignore_errors=True)
        log_line({"kind": "saved", "prefix": f"runs/{R.run_id}/final/"})
    log_line({"kind": "done", "step": int(trainer.state.global_step), "seconds": time.time() - T0})


# ------------------------------------------------------------------------------------------------------------
# MODE=eval
# ------------------------------------------------------------------------------------------------------------
def eval_mode():
    from transformers import AutoTokenizer
    from vllm import LLM
    _, evals, _ = build_data()
    path = model_path()
    tok = AutoTokenizer.from_pretrained(R.model)
    llm = LLM(model=path, tokenizer=R.model, max_model_len=1024, gpu_memory_utilization=0.35, seed=0, dtype="bfloat16")
    log_line({"kind": "start", "recipe": dict(R.__dict__)})
    log_line({"kind": "eval", "step": 0, **evaluate(llm, tok, evals)})
    log_line({"kind": "done", "step": 0, "seconds": time.time() - T0})


# ------------------------------------------------------------------------------------------------------------
# MODE=merge
# ------------------------------------------------------------------------------------------------------------
def ties_merge(base, specialists, weights, keep=0.2):
    """Task arithmetic with trimming and sign agreement (TIES, simplified): each specialist's weights minus the base, the smallest changes dropped, the rest
    added to the base where the signs agree with the weighted majority."""
    import torch
    merged = {}
    for name, b in base.items():
        if not b.is_floating_point():
            merged[name] = b
            continue
        d = torch.stack([s[name].float() - b.float() for s in specialists])
        k = max(1, int(keep * d[0].numel()))
        flat = d.abs().flatten(1)
        thr = flat.kthvalue(d[0].numel() - k + 1, dim=1).values
        d = d * (d.abs() >= thr.view(-1, *[1] * (d.dim() - 1)))
        w = torch.tensor(weights).view(-1, *[1] * (d.dim() - 1))
        sign = torch.sign((d * w).sum(0))
        agree = (torch.sign(d) == sign) & (d != 0)
        merged[name] = (b.float() + (d * agree * w).sum(0) / (agree * w).sum(0).clamp(min=1e-8)).to(b.dtype)
    return merged


def merge_mode():
    from safetensors.torch import load_file, save_file
    from huggingface_hub import snapshot_download
    spec = json.loads(R.merge_from)
    base_dir = snapshot_download(R.model, allow_patterns=["*.safetensors", "*.json"])
    base = load_file(str(pathlib.Path(base_dir) / "model.safetensors"))
    specs, weights, stats = [], [], {}
    for rid, w in spec.items():
        d = model_path(rid)
        sd = load_file(str(pathlib.Path(d) / "model.safetensors"))
        specs.append({k: v for k, v in sd.items()})
        weights.append(float(w))
    # the base's tied output weights may be absent from a saved checkpoint: restrict to the names every file has
    names = set(base)
    for s in specs:
        names &= set(s)
    base = {k: base[k].float() for k in names}
    specs = [{k: s[k] for k in names} for s in specs]
    for rid, s in zip(spec, specs):
        num = sum(float(((s[k].float() - base[k]) ** 2).sum()) for k in names if base[k].is_floating_point()) ** 0.5
        stats[rid] = {"task_vector_norm": num}
    merged = ties_merge(base, specs, weights, R.merge_keep)
    out = WORK / "final"
    out.mkdir(exist_ok=True)
    from transformers import AutoTokenizer
    for f in pathlib.Path(base_dir).glob("*.json"):
        shutil.copy(f, out / f.name)
    save_file({k: v.contiguous() for k, v in merged.items()}, str(out / "model.safetensors"), metadata={"format": "pt"})
    AutoTokenizer.from_pretrained(R.model).save_pretrained(out)
    upload_dir(out, f"runs/{R.run_id}/final/")
    log_line({"kind": "merged", "from": spec, "keep": R.merge_keep, "stats": stats, "tensors": len(names)})
    log_line({"kind": "done", "step": 0, "seconds": time.time() - T0})


# ------------------------------------------------------------------------------------------------------------
# MODE=probe: per-module gradients of each family's loss, and their cosines
# ------------------------------------------------------------------------------------------------------------
def probe_mode():
    import torch
    from transformers import AutoTokenizer
    from trl import GRPOConfig, GRPOTrainer
    from datasets import Dataset

    train, evals, _ = build_data()
    path = model_path()
    tok = AutoTokenizer.from_pretrained(R.model)
    reward = MultiReward()
    props = torch.cuda.get_device_properties(0)
    cfg = GRPOConfig(output_dir=str(WORK / "hf"), per_device_train_batch_size=R.rows, gradient_accumulation_steps=1, num_generations=R.group, max_completion_length=R.max_completion,
                     temperature=1.0, top_p=1.0, top_k=0, learning_rate=R.lr, max_steps=1, bf16=True, seed=R.seed, report_to="none", save_strategy="no", use_vllm=True, vllm_mode="colocate",
                     vllm_gpu_memory_utilization=round(8 * 2 ** 30 / props.total_memory, 3), vllm_max_model_length=1024, beta=0.0, chat_template_kwargs={"enable_thinking": False},
                     gradient_checkpointing=True, log_completions=False, shuffle_dataset=False)
    trainer = GRPOTrainer(model=path, reward_funcs=reward, args=cfg, train_dataset=Dataset.from_list(train), processing_class=tok)
    model = trainer.model
    model.gradient_checkpointing_enable()
    model.train()
    trainer.current_gradient_accumulation_steps = 1
    grads, mb = {}, 16        # micro-batches of 16 rollouts: the probe holds the gradients of six families, so activations have to stay small
    for fam in F.FAMILIES:
        rows = []
        src = build_family_rows(fam)
        for r in src[: R.probe_prompts]:
            rows += [r] * R.group
        model.zero_grad(set_to_none=True)
        n_micro = 0
        for i in range(0, len(rows), mb):
            inputs = trainer._generate_and_score_completions(rows[i:i + mb])
            loss = trainer._compute_loss(model, inputs)
            (loss / max(1, len(rows) // mb)).backward()
            n_micro += 1
        grads[fam] = {n: p.grad.detach().flatten().to("cpu", dtype=torch.float16) for n, p in model.named_parameters() if p.grad is not None}
        log_line({"kind": "probe_family", "family": fam, "reward": sum(reward.last["rewards"]) / max(len(reward.last["rewards"]), 1), "micro_batches": n_micro,
                  "grad_norm": float(sum(float((g.float() ** 2).sum()) for g in grads[fam].values()) ** 0.5)})
    names = sorted(next(iter(grads.values())))
    pairs = {}
    for i, a in enumerate(F.FAMILIES):
        for b in F.FAMILIES[i + 1:]:
            per = {}
            dot = na = nb = 0.0
            for n in names:
                x, y = grads[a][n].float().cuda(), grads[b][n].float().cuda()
                d, xa, yb = float(x @ y), float(x @ x), float(y @ y)
                dot, na, nb = dot + d, na + xa, nb + yb
                per[n] = d / ((xa * yb) ** 0.5 + 1e-12)
            pairs[f"{a}|{b}"] = {"whole": dot / ((na * nb) ** 0.5 + 1e-12), "modules": per}
    norms = {fam: {n: round(float(g.float().norm()), 6) for n, g in gs.items()} for fam, gs in grads.items()}
    log_line({"kind": "probe", "pairs": pairs, "modules": len(names), "norms": norms})
    log_line({"kind": "done", "step": 0, "seconds": time.time() - T0})


def verify_mode():
    """The verifier-precision gate and the cross-domain decontamination check of the domain contract, for every family including the seventh."""
    train, evals, _ = build_data()
    log_line({"kind": "start", "recipe": dict(R.__dict__)})
    out = {}
    for fam in F.ALL:
        if fam == "math":
            tr, _ = load_math()
            exs = tr[:200]
            golds = [f"The answer is \\boxed{{{e['solution']}}}" for e in exs]
            bads = [f"The answer is \\boxed{{{int(float(e['solution'].replace(',', ''))) + 1}}}" for e in exs]
            metas = [{}] * len(exs)
            sols = [e["solution"] for e in exs]
        else:
            exs = F.make_examples(fam, 200, 31337, "train")
            pairs = [F.gold_bad(fam, e) for e in exs]
            golds, bads, metas, sols = [p[0] for p in pairs], [p[1] for p in pairs], [e["meta"] for e in exs], [""] * len(exs)
        right = [score_one(fam, t, m, so) for t, m, so in zip(golds, metas, sols)]
        wrong = [score_one(fam, t, m, so) for t, m, so in zip(bads, metas, sols)]
        out[fam] = {"n": len(exs), "recall": sum(r == 1.0 for r in right) / len(right), "false_accept": sum(w == 1.0 for w in wrong) / len(wrong), "mean_wrong": sum(wrong) / len(wrong)}
    key = lambda r: r["prompt"][0]["content"] + "|" + r["meta"]     # a prompt together with its hidden tests or constraints: the five code tasks repeat, their tests do not
    prompts_train = {fam: {key(r) for r in rows} for fam, rows in {f: [row(f, e) for e in F.make_examples(f, 1500, 1001, "train")] for f in F.GENERATORS}.items()}
    prompts_train["math"] = {key(r) for r in [row("math", e) for e in load_math()[0]]}
    eval_prompts = {fam: {key(r) for r in rows} for fam, rows in evals.items()}
    overlaps = {f"{a}->{b}": len(prompts_train[a] & eval_prompts[b]) for a in prompts_train for b in eval_prompts}
    log_line({"kind": "verify", "families": out, "overlaps": overlaps, "versions": F.FAMILY_VERSION})
    log_line({"kind": "done", "step": 0, "seconds": time.time() - T0})


def build_family_rows(fam):
    if fam == "math":
        tr, _ = load_math()
        return [row("math", e) for e in tr]
    return [row(fam, e) for e in F.make_examples(fam, 200, 4242, "train")]


def main():
    stop = threading.Event()
    if BUCKET:
        from store import sync_loop
        threading.Thread(target=sync_loop, args=(WORK, BUCKET, f"runs/{R.run_id}/", stop), daemon=True).start()
    try:
        {"train": train_mode, "eval": eval_mode, "merge": merge_mode, "probe": probe_mode, "verify": verify_mode}[R.mode]()
    except Exception:
        traceback.print_exc()
        log_line({"kind": "error", "text": traceback.format_exc()[-1500:]})
        raise
    finally:
        stop.set()
        time.sleep(2)


if __name__ == "__main__":
    main()
