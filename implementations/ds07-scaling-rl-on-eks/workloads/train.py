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
    ckpt_keep: int = _e("CKPT_KEEP", 2, int)
    watchdog_s: int = _e("WATCHDOG_S", 420, int)           # no new step for this long: the trainer exits so that JobSet restarts it
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


def _digits(x):
    return [int(c) for c in str(abs(x))]


def _collatz(x):
    n = 0
    while x != 1:
        x = x // 2 if x % 2 == 0 else 3 * x + 1
        n += 1
    return n


def _is_prime(x):
    return int(x > 1 and all(x % d for d in range(2, int(x ** 0.5) + 1)))


# Five kinds of small function. Measured on the base model before choosing (eight answers each): it gets 24% to 69% of the tests right, and no group of
# eight answers scores the same for all eight. Kinds the model always gets right, or never, were left out: the trainer drops a group whose answers
# all score the same, and a run made only of such groups never takes a step.
TASKS = [
    ("returns how many decimal digits of x are even", lambda x, k: sum(1 for d in _digits(x) if d % 2 == 0), (10, 99999)),
    ("returns 1 if x is prime and 0 otherwise", lambda x, k: _is_prime(x), (1, 200)),
    ("returns x with its decimal digits reversed", lambda x, k: int(str(x)[::-1]), (1, 99999)),
    ("returns the number of positive divisors of x", lambda x, k: sum(1 for d in range(1, x + 1) if x % d == 0), (1, 100)),
    ("returns the number of steps the Collatz process takes to reach 1 from x (halve if even, otherwise 3x+1)", lambda x, k: _collatz(x), (1, 40)),
]


def pyexec_tasks(n, seed):
    """Small functions of mixed difficulty to write. The reward is the share of hidden tests that pass when the answer runs in the sandbox pool.
    The tasks are hard enough that a 0.6B model gets some right and some wrong: a group whose answers all score the same carries no signal and
    the trainer drops it."""
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        spec, fn, (lo, hi) = rng.choice(TASKS)
        k = rng.randint(2, 9)
        xs = [rng.randint(lo, hi) for _ in range(6)]
        rows.append({"prompt": [{"role": "user", "content": f"Write a Python function f(x) that {spec.format(k=k)}. Reply with only the code in a ```python block."}],
                     "tests": json.dumps([[x, fn(x, k)] for x in xs])})
    return rows


class SandboxReward:
    """Scores each answer by running its code in the sandbox pool: one queue message per call, TOOL_CALLS calls per answer."""
    __name__ = "sandbox_reward"

    def __init__(self, calls=1):
        self.calls, self.client = calls, None

    def _redis(self):
        import redis
        if self.client is None:
            self.client = redis.Redis(host="redis.rl-scale.svc", port=6379, decode_responses=True, socket_timeout=400, socket_connect_timeout=10)
        return self.client

    def _score_one(self, text, t):
        import uuid
        r = self._redis()
        m = re.search(r"```python\n(.*?)```", text, re.S) or re.search(r"```\n(.*?)```", text, re.S)
        code = m.group(1) if m else ""
        ids = []
        for _ in range(self.calls):  # the answer's calls go to the pool together, like the tool calls of one turn of an agent
            jid = uuid.uuid4().hex
            ids.append(jid)
            r.lpush("sandbox:queue", json.dumps({"id": jid, "code": code, "tests": json.loads(t), "ts": time.time()}))
        scores = []
        for jid in ids:
            res = r.blpop(f"sandbox:result:{jid}", timeout=300)
            scores.append(json.loads(res[1])["passed"] if res else 0.0)
        return float(sum(scores) / len(scores))

    def __call__(self, completions, tests, **kwargs):
        texts = [(c[-1]["content"] if isinstance(c, list) else c) for c in completions]
        t0 = time.time()
        with ThreadPoolExecutor(max(len(texts), 1)) as ex:  # the answers of a group are scored side by side
            scores = list(ex.map(self._score_one, texts, tests))
        print(f"[reward] {len(scores)} answers scored in {time.time() - t0:.1f}s: {[round(x, 2) for x in scores]}", flush=True)
        return scores


class LocalReward:
    """The same scoring without the queue: each answer's code runs in a child process of the trainer. Used to tell a problem in the queue from a problem in the task."""
    __name__ = "local_reward"

    def __call__(self, completions, tests, **kwargs):
        import subprocess
        import sys
        harness = "import json,sys\nns={}\nexec(sys.argv[1],ns)\nt=json.loads(sys.argv[2]);ok=0\nfor x,w in t:\n    try: ok+=int(ns['f'](x)==w)\n    except Exception: pass\nprint(ok/len(t))"

        def one(comp, t):
            text = comp[-1]["content"] if isinstance(comp, list) else comp
            m = re.search(r"```python\n(.*?)```", text, re.S)
            if not m:
                return 0.0
            try:
                p = subprocess.run([sys.executable, "-c", harness, m.group(1), t], capture_output=True, text=True, timeout=5)
                return float(p.stdout.strip().splitlines()[-1])
            except Exception:
                return 0.0
        with ThreadPoolExecutor(len(completions)) as ex:
            return list(ex.map(one, completions, tests))


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
    with ThreadPoolExecutor(16) as ex:
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
    model_path = [R.model]
    rollback_weights = [None]
    if BUCKET:
        resume = restore_from_bucket()
        if resume is None and R.resume_from and R.resume_step:
            # A rollback with a change: a new run starts from the weights of another run's checkpoint, with a fresh optimizer and its own settings.
            src = pathlib.Path("/work") / R.resume_from / f"checkpoint-{R.resume_step}"
            restore_from_bucket(prefix=f"runs/{R.resume_from}/checkpoint-{R.resume_step}/", dest=src)
            if (src / "model.safetensors").exists():
                rollback_weights[0] = src / "model.safetensors"
                print(f"rollback: starting from the weights of {R.resume_from}/checkpoint-{R.resume_step}", flush=True)
        threading.Thread(target=sync_loop, args=(stop,), daemon=True).start()
    restarts = int((WORK / "restarts").read_text()) + 1 if (WORK / "restarts").exists() else 0
    (WORK / "restarts").write_text(str(restarts))

    if R.task == "gsm8k":
        train, held = gsm8k()
        rewards = accuracy_reward
    else:  # pyexec and pyexec-local
        from datasets import Dataset
        train, held = Dataset.from_list(pyexec_tasks(4000, R.seed)), []
        rewards = LocalReward() if R.task == "pyexec-local" else SandboxReward(R.tool_calls)

    def log_line(rec):
        with METRICS.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    def watchdog():
        """NCCL has no timeout: a peer that died leaves the collective blocked forever. A process that has stopped making progress is
        worse than one that has failed, because nothing restarts it. This thread turns the first into the second."""
        while True:
            time.sleep(15)
            if "t_train0" in STATE and time.time() - STATE.get("t_progress", STATE["t_train0"]) > R.watchdog_s:
                print(f"WATCHDOG: no training step for {R.watchdog_s} s; exiting so that the restart policy takes over", flush=True)
                os._exit(3)

    threading.Thread(target=watchdog, daemon=True).start()

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
            STATE["t_progress"] = time.time()
            if held and R.eval_every and state.global_step % R.eval_every == 0 and state.global_step > 0:
                try:
                    log_line({**self._stamp(state), "kind": "eval", **evaluate(held)})
                except Exception:
                    traceback.print_exc()

        def on_save(self, args, state, control, **kw):
            log_line({**self._stamp(state), "kind": "checkpoint"})

    cfg = dict(
        output_dir=str(WORK), save_strategy="steps", save_steps=R.ckpt_every, save_total_limit=R.ckpt_keep, max_steps=R.max_steps,
        per_device_train_batch_size=R.batch_rows, gradient_accumulation_steps=R.grad_accum, num_generations=R.num_generations,
        max_completion_length=R.max_completion, chat_template_kwargs={"enable_thinking": R.thinking}, learning_rate=R.learning_rate,
        lr_scheduler_type=R.lr_schedule, report_to="none", vllm_server_base_url=R.vllm_url, logging_steps=1, seed=R.seed, bf16=True,
        max_staleness=R.max_staleness, weight_sync_steps=R.weight_sync_steps, score_centering=R.score_centering,
        max_concurrent_rollouts=R.max_concurrent,
    )
    if R.epsilon_high:
        cfg["epsilon_high"] = R.epsilon_high
    config = AsyncGRPOConfig(**cfg)
    trainer = AsyncGRPOTrainer(model=model_path[0], args=config, train_dataset=train, reward_funcs=rewards, callbacks=[RunLog()])
    if rollback_weights[0] is not None:
        # The engines serve the model under its own name, so the trainer keeps that name and takes the checkpoint's weights into its model; the first
        # weight push of the run then carries them to the fleet.
        from safetensors.torch import load_file
        missing, unexpected = trainer.model.load_state_dict(load_file(str(rollback_weights[0])), strict=False)
        print(f"rollback: loaded the checkpoint into the model ({len(missing)} missing, {len(unexpected)} unexpected keys)", flush=True)
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
