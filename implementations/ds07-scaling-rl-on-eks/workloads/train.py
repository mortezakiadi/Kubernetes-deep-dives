"""The trainer of Design 07's experiments: an asynchronous GRPO run against the rollout fleet.

One recipe is one set of environment variables (see `Recipe`). The run writes one JSON line per logged step to a
metrics file, evaluates the policy on held-out problems at fixed steps, saves checkpoints, and copies both to the
state bucket in the background. If the pod is replaced, the next one restores the newest checkpoint and goes on.
"""
import json
import os
import pathlib
import random
import re
import threading
import time
import traceback
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass


def _e(name, default, cast=str):
    v = os.environ.get(name)
    return cast(v) if v not in (None, "") else default


def _bool(v):
    return str(v).lower() in ("1", "true", "yes")


@dataclass
class Recipe:
    run_id: str = _e("RUN_ID", "dev")
    model: str = _e("MODEL", "Qwen/Qwen3-0.6B")
    task: str = _e("TASK", "gsm8k")                       # gsm8k | pyexec
    max_steps: int = _e("MAX_STEPS", 100, int)
    learning_rate: float = _e("LEARNING_RATE", 1e-5, float)
    lr_schedule: str = _e("LR_SCHEDULE", "constant")
    batch_rows: int = _e("BATCH_ROWS", 16, int)
    grad_accum: int = _e("GRAD_ACCUM", 2, int)
    num_generations: int = _e("NUM_GENERATIONS", 8, int)
    max_completion: int = _e("MAX_COMPLETION", 1024, int)
    thinking: bool = _bool(_e("THINKING", "0"))
    max_staleness: int = _e("MAX_STALENESS", 4, int)
    weight_sync_steps: int = _e("WEIGHT_SYNC_STEPS", 1, int)
    score_centering: bool = _bool(_e("SCORE_CENTERING", "1"))
    epsilon_high: float = _e("EPSILON_HIGH", 0.0, float)
    max_concurrent: int = _e("MAX_CONCURRENT", 512, int)
    seed: int = _e("SEED", 1, int)
    vllm_url: str = _e("VLLM_URL", "http://rollout-api.rl-scale.svc:8000")
    eval_every: int = _e("EVAL_EVERY", 10, int)
    eval_problems: int = _e("EVAL_PROBLEMS", 200, int)
    eval_samples: int = _e("EVAL_SAMPLES", 4, int)
    ckpt_every: int = _e("CKPT_EVERY", 25, int)
    gpus: int = _e("GPUS_HELD", 3, int)                    # GPUs the run holds: the trainer's and every rollout engine's
    lora: bool = _bool(_e("LORA", "0"))
    tool_calls: int = _e("TOOL_CALLS", 1, int)             # environment calls per answer (pyexec)
    start_step_offset: int = _e("START_STEP_OFFSET", 0, int)
    resume_from: str = _e("RESUME_FROM", "")               # a run id whose checkpoint this run starts from (rollback with a change)
    resume_step: int = _e("RESUME_STEP", 0, int)


R = Recipe()
BUCKET = _e("STATE_BUCKET", "")
WORK = pathlib.Path("/work") / R.run_id
WORK.mkdir(parents=True, exist_ok=True)
METRICS = WORK / "metrics.jsonl"
PREFIX = f"runs/{R.run_id}/"
T0 = time.time()
STATE = {"eval_s": 0.0}

INSTRUCTION = " Think step by step, then put the final answer in \\boxed{}."


# ------------------------------------------------------------------------------------------------------------
# State bucket: the node is the fast copy, the bucket is the copy that survives the node.
# ------------------------------------------------------------------------------------------------------------
def s3():
    import boto3
    return boto3.client("s3", region_name="us-east-1")


def restore_from_bucket(prefix=None, dest=None):
    """Download everything under the run's prefix. Returns the newest checkpoint directory, or None."""
    if not BUCKET:
        return None
    client, prefix, dest = s3(), prefix or PREFIX, dest or WORK
    got = 0
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        for o in page.get("Contents", []):
            rel = o["Key"][len(prefix):]
            if not rel or rel.endswith("/"):
                continue
            p = dest / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(BUCKET, o["Key"], str(p))
            got += 1
    ckpts = sorted((p for p in dest.glob("checkpoint-*") if (p / "COMPLETE").exists()), key=lambda p: int(p.name.split("-")[1]))
    print(f"restored {got} files from s3://{BUCKET}/{prefix}; newest complete checkpoint: {ckpts[-1].name if ckpts else None}", flush=True)
    return ckpts[-1] if ckpts else None


def sync_loop(stop):
    """Copy new and changed files of the run to the bucket. A checkpoint is copied whole and marked COMPLETE last."""
    if not BUCKET:
        return
    client, seen = s3(), {}
    while not stop.is_set():
        try:
            for p in sorted(WORK.rglob("*")):
                if not p.is_file() or "/hf/" in str(p) or p.name == "COMPLETE":
                    continue
                rel = p.relative_to(WORK)
                if str(rel).startswith("tmp-"):
                    continue
                st = (p.stat().st_size, int(p.stat().st_mtime))
                if seen.get(str(rel)) == st:
                    continue
                client.upload_file(str(p), BUCKET, PREFIX + str(rel))
                seen[str(rel)] = st
            for d in WORK.glob("checkpoint-*"):
                marker = d / "COMPLETE"
                key = f"{PREFIX}{d.name}/COMPLETE"
                if not marker.exists() and all(str(f.relative_to(WORK)) in seen for f in d.rglob("*") if f.is_file()):
                    marker.write_text(str(time.time()))
                    client.upload_file(str(marker), BUCKET, key)
                    print(f"checkpoint {d.name} is in the bucket", flush=True)
        except Exception:
            traceback.print_exc()
        stop.wait(10)


# ------------------------------------------------------------------------------------------------------------
# Data and rewards
# ------------------------------------------------------------------------------------------------------------
def gsm8k():
    from datasets import load_dataset
    train = load_dataset("openai/gsm8k", "main", split="train")
    test = load_dataset("openai/gsm8k", "main", split="test")

    def fmt(s):
        return {"prompt": [{"role": "user", "content": s["question"] + INSTRUCTION}], "solution": s["answer"].split("####")[-1].strip()}
    train = train.map(fmt, remove_columns=train.column_names).shuffle(seed=R.seed)
    held = [fmt(test[i]) for i in range(R.eval_problems)]
    return train, held


def pyexec_tasks(n, seed):
    """Small functions to write. The reward is the share of hidden tests that pass when the answer runs in the sandbox pool."""
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        a, b, c = rng.randint(2, 9), rng.randint(1, 20), rng.randint(2, 5)
        kind = rng.choice(["lin", "mod", "poly"])
        if kind == "lin":
            spec, fn = f"returns {a} * x + {b}", (lambda x, a=a, b=b: a * x + b)
        elif kind == "mod":
            spec, fn = f"returns x modulo {a + 1}, plus {c}", (lambda x, a=a, c=c: x % (a + 1) + c)
        else:
            spec, fn = f"returns x squared minus {b}", (lambda x, b=b: x * x - b)
        xs = [rng.randint(0, 30) for _ in range(5)]
        rows.append({"prompt": [{"role": "user", "content": f"Write a Python function f(x) that {spec}. Reply with only the code in a ```python block."}],
                     "tests": json.dumps([[x, fn(x)] for x in xs])})
    return rows


class SandboxReward:
    """Scores each answer by running its code in the sandbox pool: one queue message per call, TOOL_CALLS calls per answer."""
    __name__ = "sandbox_reward"

    def __init__(self, calls=1):
        self.calls, self.client = calls, None

    def _redis(self):
        import redis
        if self.client is None:
            self.client = redis.Redis(host="redis.rl-scale.svc", port=6379, decode_responses=True)
        return self.client

    def __call__(self, completions, tests, **kwargs):
        import uuid
        r, out = self._redis(), []
        for comp, t in zip(completions, tests):
            text = comp[-1]["content"] if isinstance(comp, list) else comp
            m = re.search(r"```python\n(.*?)```", text, re.S) or re.search(r"```\n(.*?)```", text, re.S)
            code = m.group(1) if m else ""
            score, t_env = 0.0, time.time()
            for _ in range(self.calls):
                jid = uuid.uuid4().hex
                r.lpush("sandbox:queue", json.dumps({"id": jid, "code": code, "tests": json.loads(t), "ts": time.time()}))
                res = r.blpop(f"sandbox:result:{jid}", timeout=120)
                score = json.loads(res[1])["passed"] if res else 0.0
            out.append(float(score))
        return out


# ------------------------------------------------------------------------------------------------------------
# Held-out evaluation, through the rollout fleet's own API, so it measures the policy the fleet is serving.
# ------------------------------------------------------------------------------------------------------------
def post(path, body, timeout=600):
    req = urllib.request.Request(R.vllm_url + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def evaluate(held):
    from trl.rewards.accuracy_rewards import _grade
    t0 = time.time()
    model = json.loads(urllib.request.urlopen(R.vllm_url + "/v1/models", timeout=30).read())["data"][0]["id"]

    def one(row):
        r = post("/v1/chat/completions", {"model": model, "messages": row["prompt"], "n": R.eval_samples, "temperature": 1.0,
                                           "max_tokens": R.max_completion, "chat_template_kwargs": {"enable_thinking": R.thinking}})
        return [ch["message"]["content"] or "" for ch in r["choices"]], r["usage"]["completion_tokens"] / max(len(r["choices"]), 1)
    # The requests run in threads; math-verify's timeouts need the main thread, so the grading happens here.
    with ThreadPoolExecutor(32) as ex:
        got = list(ex.map(one, held))
    scores, lengths = [], []
    for row, (texts, n_tok) in zip(held, got):
        s_row = [float(_grade(row["solution"], t)[0] or 0.0) for t in texts]
        scores.append(sum(s_row) / len(s_row))
        lengths.append(n_tok)
    res = list(zip(scores, lengths))
    dt = time.time() - t0
    STATE["eval_s"] += dt
    return {"eval/pass_rate": sum(r[0] for r in res) / len(res), "eval/mean_tokens": sum(r[1] for r in res) / len(res), "eval/seconds": dt}


# ------------------------------------------------------------------------------------------------------------
def main():
    from transformers import TrainerCallback
    from trl import AsyncGRPOConfig, AsyncGRPOTrainer
    from trl.rewards import accuracy_reward

    stop = threading.Event()
    resume = None
    if BUCKET:
        resume = restore_from_bucket()
        if resume is None and R.resume_from:
            # A rollback with a change: start a new run from another run's checkpoint.
            src = pathlib.Path("/work") / R.resume_from
            src.mkdir(parents=True, exist_ok=True)
            all_ck = restore_from_bucket(prefix=f"runs/{R.resume_from}/", dest=src)
            want = src / f"checkpoint-{R.resume_step}" if R.resume_step else all_ck
            if want and want.exists():
                import shutil
                shutil.copytree(want, WORK / want.name, dirs_exist_ok=True)
                (WORK / want.name / "COMPLETE").unlink(missing_ok=True)
                resume = WORK / want.name
                print(f"rollback: starting from {R.resume_from}/{want.name}", flush=True)
        threading.Thread(target=sync_loop, args=(stop,), daemon=True).start()
    restarts = int((WORK / "restarts").read_text()) + 1 if (WORK / "restarts").exists() else 0
    (WORK / "restarts").write_text(str(restarts))

    if R.task == "gsm8k":
        train, held = gsm8k()
        rewards = accuracy_reward
    else:
        from datasets import Dataset
        train, held = Dataset.from_list(pyexec_tasks(4000, R.seed)), []
        rewards = SandboxReward(R.tool_calls)

    def log_line(rec):
        with METRICS.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    class RunLog(TrainerCallback):
        def __init__(self):
            self.last = time.time()

        def _stamp(self, state):
            run_s = time.time() - STATE.get("t_train0", T0) - STATE["eval_s"]
            return {"t": time.time(), "step": state.global_step + R.start_step_offset, "restarts": restarts, "run_seconds": run_s,
                    "gpu_hours": run_s * R.gpus / 3600}

        def on_train_begin(self, args, state, control, **kw):
            STATE["t_train0"] = time.time()
            STATE["need_eval0"] = bool(held) and not resume

        def on_step_begin(self, args, state, control, **kw):
            # The first weight sync of the run happens in the trainer's own train-begin hook, so the starting policy is measured here,
            # when the fleet is serving this run's initial weights and not the last run's final ones.
            if STATE.pop("need_eval0", False):
                try:
                    log_line({**self._stamp(state), "kind": "eval", **evaluate(held)})
                except Exception:
                    traceback.print_exc()

        def on_log(self, args, state, control, logs=None, **kw):
            if logs and "loss" in logs or (logs and "perf/step_s" in logs):
                log_line({**self._stamp(state), "kind": "train", **{k: v for k, v in logs.items()}})

        def on_step_end(self, args, state, control, **kw):
            if held and R.eval_every and state.global_step % R.eval_every == 0 and state.global_step > 0:
                try:
                    log_line({**self._stamp(state), "kind": "eval", **evaluate(held)})
                except Exception:
                    traceback.print_exc()

        def on_save(self, args, state, control, **kw):
            log_line({**self._stamp(state), "kind": "checkpoint"})

    cfg = dict(
        output_dir=str(WORK), save_strategy="steps", save_steps=R.ckpt_every, save_total_limit=2, max_steps=R.max_steps,
        per_device_train_batch_size=R.batch_rows, gradient_accumulation_steps=R.grad_accum, num_generations=R.num_generations,
        max_completion_length=R.max_completion, chat_template_kwargs={"enable_thinking": R.thinking}, learning_rate=R.learning_rate,
        lr_scheduler_type=R.lr_schedule, report_to="none", vllm_server_base_url=R.vllm_url, logging_steps=1, seed=R.seed, bf16=True,
        max_staleness=R.max_staleness, weight_sync_steps=R.weight_sync_steps, score_centering=R.score_centering,
        max_concurrent_rollouts=R.max_concurrent,
    )
    if R.epsilon_high:
        cfg["epsilon_high"] = R.epsilon_high
    config = AsyncGRPOConfig(**cfg)
    trainer = AsyncGRPOTrainer(model=R.model, args=config, train_dataset=train, reward_funcs=rewards, callbacks=[RunLog()])
    print(f"run {R.run_id}: starting (restart {restarts}); resume={resume}", flush=True)
    try:
        trainer.train(resume_from_checkpoint=str(resume) if resume else None)
        log_line({"t": time.time(), "kind": "done", "step": R.max_steps})
    finally:
        try:
            trainer.end()
        except Exception:
            pass
        # one last pass of the copier before the pod goes away
        stop.set()
        time.sleep(1)
        if BUCKET:
            try:
                c = s3()
                for p in WORK.rglob("*"):
                    if p.is_file() and "/hf/" not in str(p) and not str(p.relative_to(WORK)).startswith("tmp-") and p.name != "COMPLETE":
                        c.upload_file(str(p), BUCKET, PREFIX + str(p.relative_to(WORK)))
            except Exception:
                traceback.print_exc()


if __name__ == "__main__":
    main()
