"""RL stage (Design 06, "RL: choosing the objective", "Rewards and verifiers", "Rollout generation"), with TRL's
GRPOTrainer.

In the platform's RL job this process is the TRAINER. Rollouts come from a separate vLLM server (another pod, another
GPU, started by the same JobSet), and rewards come from the verifier pool (another Deployment, CPU only). Every
objective switch of the design is a recipe key mapped one to one onto a GRPOConfig field. The trainer times its two
waits, generation and weight sync, so the cost of each is a measured number in the run's metrics.
"""
import json
import os
import random
import time

from datasets import Dataset
from trl import GRPOConfig, GRPOTrainer

from common import RUN_ID, WORK, Tracker, delete_prefix, finish, hf_callback, load_rows, model_path, register, resolved, \
    text_of, upload_dir, verify

TIMING = {"generate_s": 0.0, "sync_s": 0.0, "reward_s": 0.0, "syncs": 0, "generations": 0, "reward_calls": 0}


def make_rewards(names):
    """Reward functions in TRL's format: one score per completion, or None where the reward does not apply."""

    def timed(fn):
        def wrapper(*a, **kw):
            t0 = time.time()
            out = fn(*a, **kw)
            TIMING["reward_s"] += time.time() - t0
            TIMING["reward_calls"] += 1
            return out
        wrapper.__name__ = fn.__name__
        return wrapper

    @timed
    def math_exact(completions, task, solution, **kw):
        idx = [i for i, t in enumerate(task) if t == "math"]
        out = [None] * len(completions)
        if idx:
            res = verify("math_exact", [text_of(completions[i]) for i in idx], solutions=[solution[i] for i in idx])
            for i, s in zip(idx, res["scores"]):
                out[i] = s
        return out

    @timed
    def unit_tests(completions, task, tests, **kw):
        idx = [i for i, t in enumerate(task) if t == "code"]
        out = [None] * len(completions)
        if idx:
            res = verify("code", [text_of(completions[i]) for i in idx], tests=[tests[i] for i in idx])
            for i, s in zip(idx, res["scores"]):
                out[i] = s
        return out

    @timed
    def format_ok(completions, task, **kw):
        idx = [i for i, t in enumerate(task) if t == "math"]
        out = [None] * len(completions)
        if idx:
            for i, s in zip(idx, verify("format", [text_of(completions[i]) for i in idx])["scores"]):
                out[i] = s
        return out

    table = {"math_exact": math_exact, "unit_tests": unit_tests, "format_ok": format_ok}
    return [table[n] for n in names]


def prompt_set(r, spec, seed):
    """Weighted draw of prompts from each source, after the recipe's pass-rate filter (if the rows carry one)."""
    rng = random.Random(seed)
    want = int(spec["hyperparameters"]["batch_prompts"]) * int(spec["hyperparameters"]["steps"]) * 2
    srcs = spec["data"]["prompts"]
    total = sum(float(s["weight"]) for s in srcs.values())
    flt = spec["data"].get("filter") or {}
    rows, counts = [], {}
    for name, s in srcs.items():
        src_rows = load_rows(r["datasets"][name])
        if flt and "pass_rate" in src_rows[0]:
            src_rows = [x for x in src_rows if flt.get("pass_rate_min", 0) <= x["pass_rate"] <= flt.get("pass_rate_max", 1)]
        n = round(want * float(s["weight"]) / total)
        pick = [rng.choice(src_rows) for _ in range(n)]
        counts[name] = {"available": len(src_rows), "drawn": n}
        for x in pick:
            rows.append({"prompt": x["prompt"], "task": x.get("task", name), "solution": x.get("solution", ""), "tests": x.get("tests", "[]")})
    rng.shuffle(rows)
    return Dataset.from_list(rows), counts


def main():
    r = resolved()
    spec = r["spec"]
    obj, ro, hp = spec["objective"], spec["rollout"], spec["hyperparameters"]
    seed = int(spec.get("seed", 17))
    tracker = Tracker("rl", gpus=int(spec.get("resources", {}).get("gpus", 2)))
    base_dir = model_path(r["base"])
    ds, counts = prompt_set(r, spec, seed)
    tracker.event("prompts", sources=counts, rows=len(ds))

    rewards = [w["name"] for w in spec["rewards"]]
    group = int(ro["group_size"])
    server = ro.get("mode", "server") == "server"
    ckpt_every = int(spec.get("checkpoint_every", 0))
    args = GRPOConfig(
        output_dir=str(WORK / "out"), seed=seed,
        loss_type=obj.get("loss_type", "dapo"), epsilon=float(obj.get("epsilon", 0.2)),
        epsilon_high=float(obj["epsilon_high"]) if obj.get("epsilon_high") is not None else None,
        beta=float(obj.get("beta", 0.0)), importance_sampling_level=obj.get("importance_sampling_level", "token"),
        scale_rewards=obj.get("scale_rewards", "group"), mask_truncated_completions=bool(obj.get("mask_truncated_completions", True)),
        vllm_importance_sampling_correction=bool(obj.get("vllm_importance_sampling_correction", True)),
        reward_weights=[float(w.get("weight", 1.0)) for w in spec["rewards"]],
        num_generations=group, max_completion_length=int(ro["max_response_tokens"]), temperature=float(ro.get("temperature", 1.0)),
        per_device_train_batch_size=group * int(hp["batch_prompts"]) // int(hp.get("grad_accum", 1)),
        gradient_accumulation_steps=int(hp.get("grad_accum", 1)),
        learning_rate=float(hp["lr"]), max_steps=int(hp["steps"]), warmup_steps=int(hp.get("warmup_steps", 0)),
        lr_scheduler_type="constant_with_warmup", max_grad_norm=float(hp.get("max_grad_norm", 1.0)),
        bf16=True, gradient_checkpointing=True, logging_steps=1, report_to="none", log_completions=False,
        save_strategy="steps" if ckpt_every else "no", save_steps=ckpt_every or 500, save_only_model=True, save_total_limit=2,
        use_vllm=True, vllm_mode="server" if server else "colocate",
        vllm_server_host=os.environ.get("ROLLOUT_HOST", "127.0.0.1"), vllm_server_port=8000, vllm_server_timeout=900,
        vllm_gpu_memory_utilization=float(ro.get("colocate_gpu_memory", 0.3)),
        vllm_max_model_length=int(ro.get("max_model_len", 1024)) if not server else None,
    )
    trainer = GRPOTrainer(model=base_dir, reward_funcs=make_rewards(rewards), args=args, train_dataset=ds,
                          callbacks=[hf_callback(tracker)])

    gen = trainer.vllm_generation
    orig_generate, orig_sync = gen.generate, gen.sync_weights

    def generate(*a, **kw):
        t0 = time.time()
        out = orig_generate(*a, **kw)
        TIMING["generate_s"] += time.time() - t0
        TIMING["generations"] += 1
        return out

    def sync_weights(*a, **kw):
        t0 = time.time()
        out = orig_sync(*a, **kw)
        TIMING["sync_s"] += time.time() - t0
        TIMING["syncs"] += 1
        return out

    gen.generate, gen.sync_weights = generate, sync_weights

    # Every logged step also carries the cumulative waits, so the share of a step spent on each is in the metrics.
    from transformers import TrainerCallback

    class Timing(TrainerCallback):
        def on_log(self, a, state, control, logs=None, **kw):
            if logs is not None:
                logs.update({f"timing/{k}": round(v, 3) for k, v in TIMING.items()})

        def on_save(self, a, state, control, **kw):
            # Checkpoint often during RL, keep the last two: a collapse then costs a rollback, not the run.
            step = state.global_step
            n, size = upload_dir(f"{a.output_dir}/checkpoint-{step}", f"runs/{RUN_ID}/ckpt/step-{step:05d}")
            tracker.event("checkpoint", step=step, files=n, mb=round(size / 1e6))
            kept = sorted({c for c in tracker.extra.get("ckpts", [])} | {step})
            for old in kept[:-2]:
                delete_prefix(f"runs/{RUN_ID}/ckpt/step-{old:05d}")
            tracker.extra["ckpts"] = kept[-2:]

    trainer.callback_handler.callbacks.insert(0, Timing())
    t0 = time.time()
    out = trainer.train()
    wall = time.time() - t0
    trainer.save_model(str(WORK / "final"))
    rows = [x for x in tracker.rows if "reward" in x]

    def mean(key, part):
        v = [x[key] for x in part if key in x]
        return round(sum(v) / len(v), 4) if v else None

    head, tail = rows[: max(1, len(rows) // 4)], rows[-max(1, len(rows) // 4):]
    summary = {"steps": out.global_step, "wall_seconds": round(wall, 1), "timing": {k: round(v, 1) for k, v in TIMING.items()},
               "share_of_wall": {k: round(TIMING[k] / wall, 3) for k in ("generate_s", "sync_s", "reward_s")},
               "reward_first_quarter": mean("reward", head), "reward_last_quarter": mean("reward", tail),
               "frac_reward_zero_std_mean": mean("frac_reward_zero_std", rows),
               "entropy_first_quarter": mean("entropy", head), "entropy_last_quarter": mean("entropy", tail),
               "completion_tokens": int(sum(x.get("completions/mean_length", 0) * group * int(hp["batch_prompts"]) for x in rows)),
               "prompts": counts, "mode": "server" if server else "colocate"}
    entry = register(str(WORK / "final"), r, summary)
    finish(tracker, {**summary, "registry_prefix": entry["model_prefix"]})


if __name__ == "__main__":
    main()
