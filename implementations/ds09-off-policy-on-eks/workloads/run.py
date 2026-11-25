"""One experiment of Design 09: a GRPO run whose data is off-policy on purpose, with the corrections, the metrics and the controller of the design.

Everything a run is comes from environment variables (see `Recipe`), so a sweep cell is one Job with different values. The run writes one JSON
line per optimizer step (kind "step"), one per held-out evaluation (kind "eval"), one per filter report (kind "filter") and one per controller
action (kind "event") to metrics.jsonl, and copies the file to the state bucket while it runs.

TRL's GRPOTrainer does the generation (a colocated vLLM engine), the reward and the advantages. This file replaces its loss with the design's
(`offpolicy.policy_loss`), takes over its batching (a staleness limit, replay and external pools) and adds the diagnostics and the controller.
"""
import json
import os
import pathlib
import random
import re
import shutil
import sys
import threading
import time
import traceback
from collections import defaultdict
from dataclasses import dataclass

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


if os.environ.get("GRID"):   # a sweep: this pod is cell number JOB_COMPLETION_INDEX of the grid
    os.environ.update({k: str(v) for k, v in json.loads(os.environ["GRID"])[int(os.environ["JOB_COMPLETION_INDEX"])].items()})


def _e(name, default, cast=str):
    v = os.environ.get(name)
    return cast(v) if v not in (None, "") else default


def _bool(v):
    return str(v).lower() in ("1", "true", "yes")


@dataclass
class Recipe:
    run_id: str = _e("RUN_ID", "dev")
    model: str = _e("MODEL", "Qwen/Qwen3-0.6B")
    steps: int = _e("STEPS", 60, int)
    lr: float = _e("LR", 3e-6, float)
    seed: int = _e("SEED", 1, int)
    rows: int = _e("ROWS", 32, int)                       # rollouts per micro-batch
    accum: int = _e("ACCUM", 2, int)                      # micro-batches per optimizer step
    group: int = _e("GROUP", 8, int)                      # answers per prompt
    max_completion: int = _e("MAX_COMPLETION", 256, int)
    window: int = _e("WINDOW", 1, int)                    # K: optimizer steps one generation feeds (the staleness window)
    max_age: int = _e("MAX_AGE", -1, int)                 # oldest age (in versions) a fresh sample may have when trained on; -1 means K-1
    correction: str = _e("CORRECTION", "decoupled")       # naive | decoupled
    truncate_at: float = _e("TRUNCATE_AT", 3.0, float)
    mask_above: float = _e("MASK_ABOVE", 10.0, float)
    eps: float = _e("EPS", 0.2, float)
    filter: bool = _bool(_e("FILTER", "0"))               # dynamic sampling: drop groups whose answers all score the same
    mix_replay: float = _e("MIX_REPLAY", 0.0, float)      # share of micro-batches that replay own older data
    mix_external: float = _e("MIX_EXTERNAL", 0.0, float)  # share of micro-batches of external (human-written) solutions
    sft_weight: float = _e("SFT_WEIGHT", 0.2, float)      # weight of the separate loss of the external pool
    replay_max_age: int = _e("REPLAY_MAX_AGE", 64, int)
    replay_max_kl: float = _e("REPLAY_MAX_KL", 0.05, float)
    controller: str = _e("CONTROLLER", "")                # path to a rules file; empty means no controller
    inject: str = _e("INJECT", "")                        # "lr_spike@20x10": at step 20 the learning rate is multiplied by 10
    eval_every: int = _e("EVAL_EVERY", 20, int)
    eval_problems: int = _e("EVAL_PROBLEMS", 128, int)
    eval_samples: int = _e("EVAL_SAMPLES", 4, int)
    ckpt_every: int = _e("CKPT_EVERY", 10, int)
    sched_admission: bool = _bool(_e("SCHED_ADMISSION", "1"))  # the scheduler generates only what the staleness limit will accept (otherwise the buffer throws the rest away)
    mismatch_noise: float = _e("MISMATCH_NOISE", 0.0, float)  # relative noise put on the engine's weights after every sync: a stand-in for an engine that rounds differently (fp8)
    dump_tokens: int = _e("DUMP_TOKENS", 0, int)           # write the per-token log-probabilities of one sample at each of the first N steps
    gpu_util: float = _e("GPU_UTIL", 0.0, float)          # 0: the engine gets a fixed 8 GiB whatever the GPU, so every cell is laid out alike
    grad_ckpt: bool = _bool(_e("GRAD_CKPT", "1"))
    gpus: int = 1


R = Recipe()
BUCKET = _e("STATE_BUCKET", "")
WORK = pathlib.Path(_e("WORK_DIR", "/work")) / R.run_id
WORK.mkdir(parents=True, exist_ok=True)
METRICS = WORK / "metrics.jsonl"
T0 = time.time()
INSTRUCTION = " Think step by step, then put the final answer in \\boxed{}."


def log_line(rec):
    rec = {"t": time.time(), **rec}
    with METRICS.open("a") as f:
        f.write(json.dumps(rec) + "\n")


# ------------------------------------------------------------------------------------------------------------
# Data
# ------------------------------------------------------------------------------------------------------------
def task_type(answer: str) -> str:
    """GSM8K marks each calculation of the reference solution with <<...>>: the count is a fair measure of how long the problem is."""
    n = answer.count("<<")
    return "short" if n <= 2 else ("medium" if n <= 4 else "long")


def gold_completion(answer: str) -> str:
    """A human-written solution in the answer format of the run: the reasoning without the calculator marks, then the boxed result."""
    body, final = answer.split("####")
    body = re.sub(r"<<.*?>>", "", body).strip()
    return f"{body}\nThe final answer is \\boxed{{{final.strip()}}}."


def load_data():
    from datasets import load_dataset
    train = load_dataset("openai/gsm8k", "main", split="train")
    test = load_dataset("openai/gsm8k", "main", split="test")

    def fmt(s):
        return {"prompt": [{"role": "user", "content": s["question"] + INSTRUCTION}], "solution": s["answer"].split("####")[-1].strip(),
                "task_type": task_type(s["answer"]), "gold": gold_completion(s["answer"])}

    train = train.map(fmt, remove_columns=train.column_names).shuffle(seed=R.seed)
    held = [fmt(test[i]) for i in range(R.eval_problems)]
    return train, held


class Gsm8kReward:
    """accuracy_reward, plus a side channel: the rewards of the latest generation with each answer's task type, for the filter report."""
    __name__ = "gsm8k_reward"

    def __init__(self):
        self.last = {"rewards": [], "types": []}

    def __call__(self, completions, solution, task_type, **kwargs):
        from trl.rewards import accuracy_reward
        rs = accuracy_reward(completions, solution, **kwargs)
        rs = [float(r) if r is not None else 0.0 for r in rs]
        self.last = {"rewards": rs, "types": list(task_type)}
        return rs


# ------------------------------------------------------------------------------------------------------------
def build_trainer_class():
    import torch
    from trl import GRPOTrainer
    from trl.trainer.utils import shuffle_sequence_dict, split_tensor_dict

    import offpolicy as op
    from controller import Controller
    from mixing import Chunk, MixedBuffer

    class OffPolicyTrainer(GRPOTrainer):
        def setup_offpolicy(self, reward, held, external_rows, tokenizer):
            self.reward_obj, self.held, self.tok = reward, held, tokenizer
            self.accum = self.args.gradient_accumulation_steps
            self.max_age = R.max_age if R.max_age >= 0 else R.window - 1
            mix = {"fresh": 1.0 - R.mix_replay - R.mix_external, "replay": R.mix_replay, "external": R.mix_external}
            self.buf = MixedBuffer(mix, R.replay_max_age, R.replay_max_kl, seed=R.seed)
            self.sft_weight = R.sft_weight
            self.fresh, self.fresh_ptr, self.fresh_gen_step = None, 0, 0
            self.wasted_chunks = 0
            self.external = self._make_external(external_rows) if R.mix_external > 0 else []
            self.ext_ptr = 0
            spec = yaml.safe_load(pathlib.Path(R.controller).read_text()) if R.controller else {}
            self.live = {"async.max_staleness": self.max_age, "buffer.mix.replay": R.mix_replay, "optim.lr": R.lr}
            self.ctl = Controller(spec, self.live)
            self.window_steps = int(spec.get("window_steps", 10)) if spec else 10
            self.hist, self.micro = [], []
            self.timers = defaultdict(float)
            self.ckpts = []                       # (step, path, healthy)
            self.rollbacks = 0
            self.skips = 0
            self.injected = False
            self.eval_done = set()
            self.step_t0 = time.time()
            # timers around what the design says costs extra: generation, the weight push, the proximal pass
            gen = self.vllm_generation
            for name in ("generate", "sync_weights"):
                orig = getattr(gen, name)

                def timed(*a, _o=orig, _n=name, **k):
                    t = time.time()
                    try:
                        return _o(*a, **k)
                    finally:
                        if _n == "sync_weights" and R.mismatch_noise > 0:
                            self._perturb_engine()
                        self.timers[_n] += time.time() - t
                setattr(gen, name, timed)
            orig_lp = self._get_per_token_logps_and_entropies

            def timed_lp(*a, **k):
                t = time.time()
                try:
                    return orig_lp(*a, **k)
                finally:
                    self.timers["logps_no_grad" if not k.get("compute_entropy") else "logps_train"] += time.time() - t
            self._get_per_token_logps_and_entropies = timed_lp

        def _perturb_engine(self):
            """Engine mismatch on demand: every weight of the sampling engine is multiplied by (1 + sigma * noise), the noise drawn fresh for each version.
            The trainer's weights are untouched, so the gap between the two is exactly what an engine with different rounding would add."""
            model = self.vllm_generation.llm.llm_engine.model_executor.driver_worker.model_runner.model
            g = torch.Generator(device=self.accelerator.device).manual_seed(int(self.state.global_step) * 7919 + R.seed)
            with torch.no_grad():
                for prm in model.parameters():
                    prm.mul_(1 + R.mismatch_noise * torch.randn(prm.shape, generator=g, device=prm.device, dtype=torch.float32).to(prm.dtype))

        # ---------------------------------------------------------------------------------------------------------
        # Generation: tag every sample with the version that produced it, report what the filters would remove
        # ---------------------------------------------------------------------------------------------------------
        def _generate_and_score_completions(self, inputs):
            t = time.time()
            out = super()._generate_and_score_completions(inputs)
            self.timers["generate_and_score"] += time.time() - t
            B = out["completion_ids"].size(0)
            dev = out["completion_ids"].device
            out["gen_step"] = torch.full((B,), int(self.state.global_step), device=dev, dtype=torch.long)
            rs, types = self.reward_obj.last["rewards"], self.reward_obj.last["types"]
            keep = torch.ones(B, device=dev)
            if len(rs) == B:
                G = R.group
                seen, dropped = defaultdict(int), defaultdict(int)
                for g in range(B // G):
                    block = rs[g * G:(g + 1) * G]
                    ty = types[g * G]
                    seen[ty] += 1
                    if len(set(block)) == 1:       # zero variance: no learning signal
                        dropped[ty] += 1
                        if R.filter:
                            keep[g * G:(g + 1) * G] = 0.0
                log_line({"kind": "filter", "step": int(self.state.global_step), "filter_on": R.filter, "groups": dict(seen), "dropped": dict(dropped),
                          "kept_fraction": float(keep.mean().item()), "mean_reward": sum(rs) / len(rs)})
            out["row_keep"] = keep
            return out

        # ---------------------------------------------------------------------------------------------------------
        # Batching: the next micro-batch comes from the fresh buffer, the replay pool or the external pool
        # ---------------------------------------------------------------------------------------------------------
        def _prepare_inputs(self, generation_batch):
            if not self.model.training:
                return super()._prepare_inputs(generation_batch)
            self._maybe_eval()
            self._maybe_inject()
            src = self.buf.pick()
            if src == "replay":
                c = self.buf.take_replay(int(self.state.global_step), self._chunk_behavior_kl)
                if c is not None:
                    return {**c.data, "source": "replay"}
                src = "fresh"
            if src == "external" and self.external:
                chunk = self.external[self.ext_ptr % len(self.external)]
                self.ext_ptr += 1
                return chunk
            return self._next_fresh(generation_batch)

        def _next_fresh(self, generation_batch):
            gs = int(self.state.global_step)
            age_next = gs - self.fresh_gen_step
            if self.fresh is None or self.fresh_ptr >= len(self.fresh) or age_next > self.max_age:
                if self.fresh is not None:
                    self.wasted_chunks += len(self.fresh) - self.fresh_ptr   # samples generated and thrown away: the price of a tighter limit
                n_chunks = self.args.steps_per_generation
                if R.sched_admission:        # admission at the scheduler: do not sample what the limit would reject
                    n_chunks = min(n_chunks, (self.max_age + 1) * self.accum)
                    generation_batch = generation_batch[: n_chunks * R.rows]     # the batch holds rollouts (each prompt already repeated per answer), not prompts
                gb = self._generate_and_score_completions(generation_batch)
                gb = shuffle_sequence_dict(gb)
                self.fresh = split_tensor_dict(gb, n_chunks)
                self.fresh_ptr, self.fresh_gen_step = 0, gs
            chunk = self.fresh[self.fresh_ptr]
            self.fresh_ptr += 1
            chunk["source"] = "fresh"
            if R.mix_replay > 0:
                adv = chunk["advantages"].abs().mean().item()
                self.buf.add_replay(Chunk({k: (v.clone() if hasattr(v, "clone") else v) for k, v in chunk.items()}, int(chunk["gen_step"][0].item()), "replay", adv))
            return chunk

        def _chunk_behavior_kl(self, data):
            """KL of the behavior policy (the engine that wrote the chunk) against the current policy, for the replay admission rule."""
            with torch.no_grad():
                ids = torch.cat([data["prompt_ids"], data["completion_ids"]], 1)
                am = torch.cat([data["prompt_mask"], data["completion_mask"]], 1)
                lp, _, _ = self._get_per_token_logps_and_entropies(self.model, ids, am, data["completion_ids"].size(1), batch_size=16)
                beh = torch.where(torch.isnan(data["sampling_per_token_logps"]), lp, data["sampling_per_token_logps"])
                m = (data["completion_mask"] * data.get("row_keep", torch.ones_like(data["completion_mask"][:, 0])).unsqueeze(1)).bool()
                lr = (lp - beh)[m].clamp(-20, 20)
                return ((lr.exp() - 1) - lr).mean().item() if m.any() else 0.0

        def _make_external(self, rows):
            """Chunks of human-written solutions: prompt and completion tokens only, no log-probabilities, no advantages."""
            tok, dev = self.tok, self.accelerator.device
            pad = tok.pad_token_id if tok.pad_token_id is not None else tok.eos_token_id
            rng = random.Random(R.seed)
            chunks = []
            for _ in range(24):
                batch = rng.sample(rows, R.rows)
                ps = [tok.apply_chat_template(r["prompt"], tokenize=True, add_generation_prompt=True, enable_thinking=False) for r in batch]
                ps = [p["input_ids"] if isinstance(p, dict) or hasattr(p, "keys") else p for p in ps]
                cs = [tok(r["gold"] + tok.eos_token, add_special_tokens=False)["input_ids"][:R.max_completion] for r in batch]
                lp_, lc_ = max(len(p) for p in ps), max(len(c) for c in cs)
                pi = torch.tensor([[pad] * (lp_ - len(p)) + p for p in ps], device=dev)
                pm = torch.tensor([[0] * (lp_ - len(p)) + [1] * len(p) for p in ps], device=dev)
                ci = torch.tensor([c + [pad] * (lc_ - len(c)) for c in cs], device=dev)
                cm = torch.tensor([[1] * len(c) + [0] * (lc_ - len(c)) for c in cs], device=dev)
                chunks.append({"prompt_ids": pi, "prompt_mask": pm, "completion_ids": ci, "completion_mask": cm, "source": "external"})
            return chunks

        # ---------------------------------------------------------------------------------------------------------
        # The loss
        # ---------------------------------------------------------------------------------------------------------
        def _compute_loss(self, model, inputs):
            src = inputs.get("source", "fresh")
            ids = torch.cat([inputs["prompt_ids"], inputs["completion_ids"]], 1)
            am = torch.cat([inputs["prompt_mask"], inputs["completion_mask"]], 1)
            lp, ent, _ = self._get_per_token_logps_and_entropies(model, ids, am, inputs["completion_ids"].size(1), compute_entropy=True)
            mask = inputs["completion_mask"].to(lp.dtype)
            if "row_keep" in inputs:
                mask = mask * inputs["row_keep"].unsqueeze(1).to(lp.dtype)
            rec = {"src": src, "tokens": float(mask.sum().item())}
            if mask.sum() == 0:
                self.micro.append(rec)
                return lp.sum() * 0.0
            rec["entropy"] = ((ent.detach() * mask).sum() / mask.sum()).item()
            if src == "external":
                loss = op.sft_loss(lp, mask) * self.sft_weight / self.accum
                rec["sft_nll"] = op.sft_loss(lp.detach(), mask).item()
                self.micro.append(rec)
                return loss
            adv = inputs["advantages"].unsqueeze(1).to(lp.dtype)
            prox, beh = inputs["old_per_token_logps"], inputs["sampling_per_token_logps"]
            per_tok, diag = op.policy_loss(lp, prox, beh, adv, mask, R.correction, R.eps, R.eps, R.truncate_at, R.mask_above)
            ages = (int(self.state.global_step) - inputs["gen_step"]).clamp(min=0)
            rec["age"] = ages.float().mean().item()
            rec.update({k: float(v.item()) for k, v in diag.items()})
            rec.update(op.offpolicy_metrics(lp, prox, beh, mask, ages))
            self.micro.append(rec)
            if R.dump_tokens and int(self.state.global_step) < R.dump_tokens and len(self.micro) == 1:
                self._dump_tokens(inputs, lp.detach(), prox, beh, ages)
            return op.reduce_loss(per_tok, mask, self.accum)

        def _dump_tokens(self, inputs, lp, prox, beh, ages, n=10):
            """One row's first tokens with the three log-probabilities: the evidence that every token carries its behavior policy."""
            i = int(torch.argmax(ages).item())          # the oldest row of the micro-batch
            L = int(inputs["completion_mask"][i].sum().item())
            toks = [self.tok.decode([t]) for t in inputs["completion_ids"][i, :min(n, L)].tolist()]
            rows = [{"token": t, "logp_behavior": round(float(beh[i, j]), 4), "logp_proximal": round(float(prox[i, j]), 4), "logp_target": round(float(lp[i, j]), 4)}
                    for j, t in enumerate(toks)]
            log_line({"kind": "tokens", "step": int(self.state.global_step), "age": int(ages[i].item()), "gen_step": int(inputs["gen_step"][i].item()), "tokens": rows})

        # ---------------------------------------------------------------------------------------------------------
        # One optimizer step: record it, let the controller look at it, apply what it decides
        # ---------------------------------------------------------------------------------------------------------
        def training_step(self, model, inputs, num_items_in_batch):
            out = super().training_step(model, inputs, num_items_in_batch)
            if len(self.micro) >= self.accum:
                self._finish_step()
            return out

        def _finish_step(self):
            micro, self.micro = self.micro[:self.accum], self.micro[self.accum:]
            step = int(self.state.global_step) + 1       # the step that this batch is about to complete
            rec = {"kind": "step", "step": step, "sources": [m["src"] for m in micro]}
            nums = defaultdict(list)
            for m in micro:
                for k, v in m.items():
                    if k != "src" and isinstance(v, (int, float)):
                        nums[k].append(v)
            for k, v in nums.items():
                rec[k] = sum(v) / len(v)
            rec["age_max"] = max([m.get("age", 0) for m in micro]) if micro else 0
            rec["micro"] = [{k: (round(v, 5) if isinstance(v, float) else v) for k, v in m.items() if k in ("src", "age", "tokens", "entropy", "sft_nll", "offpolicy/behavior_kl", "offpolicy/ess_frac", "offpolicy/ratio_gt3_frac", "clipped_frac")} for m in micro]
            rec["step_seconds"] = time.time() - self.step_t0
            self.step_t0 = time.time()
            rec["settings"] = dict(self.ctl.settings)
            rec["mix"] = {k: round(v, 4) for k, v in self.buf.mix.items()}
            rec["wasted_chunks"] = self.wasted_chunks
            rec["timers"] = {k: round(v, 2) for k, v in self.timers.items()}
            rec["gpu_mem_gb"] = round(torch.cuda.max_memory_allocated() / 2 ** 30, 2)
            if "entropy" in rec:
                ent_series = [h["entropy"] for h in self.hist[-(self.window_steps - 1):] if "entropy" in h] + [rec["entropy"]]
                rec["health/entropy_slope"] = op.entropy_slope(ent_series)
            self.hist.append(rec)
            log_line(rec)
            good = [c for c in self.ckpts if c[2]]
            if good:
                self.ctl.healthy_settings = json.loads((good[-1][1] / "meta.json").read_text())["settings"]
            actions = self.ctl.step(step, self.hist) if R.controller else []
            for a in actions:
                log_line({"kind": "event", **a})
                self._apply(a, step)
            self._sync_settings()
            if R.ckpt_every and R.controller and step % R.ckpt_every == 0:
                self._save_ckpt(step - 1)     # the weights now are the ones after step-1 updates

        def _apply(self, ev, step):
            act = ev["action"]
            if act.get("action") == "skip_batch":
                for p in self.model.parameters():
                    p.grad = None      # no gradient, so the optimizer leaves the weights and its moments alone this step
                self.skips += 1
            elif act.get("action") == "rollback":
                self._rollback(step)
            elif act.get("action") == "page":
                print(f"PAGE {act.get('team')}: {ev['rule']} at step {step} {ev['metrics']}", flush=True)

        def _sync_settings(self):
            s = self.ctl.settings
            self.max_age = int(s["async.max_staleness"])
            if abs(self.buf.mix["replay"] - s["buffer.mix.replay"]) > 1e-9:
                self.buf.set_share("replay", s["buffer.mix.replay"])
            self._set_lr(float(s["optim.lr"]))

        def _set_lr(self, lr):
            for g in self.optimizer.param_groups:
                g["lr"] = lr
            sch = getattr(self.lr_scheduler, "scheduler", self.lr_scheduler)
            if hasattr(sch, "base_lrs"):
                sch.base_lrs = [lr] * len(sch.base_lrs)

        def _maybe_inject(self):
            """Faults on purpose, to test the controller: "lr_spike@30x10" multiplies the learning rate by 10 at step 30 (a bad configuration push);
            "max_age@30=15" lets the staleness limit jump to 15 at step 30 (a loosened limit)."""
            if not R.inject or self.injected:
                return
            gs = int(self.state.global_step)
            m = re.fullmatch(r"lr_spike@(\d+)x([\d.]+)", R.inject)
            if m and gs >= int(m.group(1)):
                new = self.ctl.settings["optim.lr"] * float(m.group(2))
                key, text = "optim.lr", f"optim.lr set to {new}"
            else:
                m = re.fullmatch(r"max_age@(\d+)=(\d+)", R.inject)
                if not (m and gs >= int(m.group(1))):
                    return
                new, key, text = int(m.group(2)), "async.max_staleness", f"async.max_staleness set to {m.group(2)}"
            self.injected = True
            self.ctl.settings[key] = new
            log_line({"kind": "event", "step": gs, "rule": "INJECTED", "action": {"inject": R.inject}, "metrics": {}, "text": text})
            self._sync_settings()

        # ---------------------------------------------------------------------------------------------------------
        # Healthy checkpoints and rollback
        # ---------------------------------------------------------------------------------------------------------
        def _healthy(self):
            """The last window was inside the limits: no ratio tail, the entropy did not fall, the reward is not worse than before."""
            w = self.hist[-self.window_steps:]
            if len(w) < self.window_steps:
                return False
            tail = max(h.get("offpolicy/ratio_gt3_frac", 0) for h in w)
            ess = min(h.get("offpolicy/ess_frac", 1) for h in w)
            return tail < float(self.ctl.spec.get("healthy", {}).get("max_ratio_gt3_frac", 0.01)) and ess > float(self.ctl.spec.get("healthy", {}).get("min_ess_frac", 0.6))

        def _save_ckpt(self, step):
            from safetensors.torch import save_file
            path = WORK / f"checkpoint-{step}"
            path.mkdir(exist_ok=True)
            sd = {k: v.detach().cpu().contiguous() for k, v in self.model.state_dict().items()}
            # tied weights share storage; safetensors wants each name to own its data
            seen, out = {}, {}
            for k, v in sd.items():
                out[k] = v.clone() if v.data_ptr() in seen else v
                seen[v.data_ptr()] = k
            save_file(out, str(path / "model.safetensors"))
            healthy = self._healthy()
            (path / "meta.json").write_text(json.dumps({"step": step, "healthy": healthy, "settings": self.ctl.settings}))
            self.ckpts.append((step, path, healthy))
            log_line({"kind": "checkpoint", "step": step, "healthy": healthy})
            for old in self.ckpts[:-4]:
                shutil.rmtree(old[1], ignore_errors=True)
            self.ckpts = self.ckpts[-4:]

        def _rollback(self, step):
            from safetensors.torch import load_file
            good = [c for c in self.ckpts if c[2]]
            if not good:
                log_line({"kind": "event", "step": step, "rule": "rollback", "action": {"action": "rollback"}, "metrics": {}, "text": "no healthy checkpoint to return to"})
                return
            cstep, path, _ = good[-1]
            self.model.load_state_dict(load_file(str(path / "model.safetensors")), strict=False)
            self.optimizer.state.clear()                       # the moments belong to the weights that were discarded
            for p in self.model.parameters():
                p.grad = None
            self.fresh, self.fresh_ptr = None, 0              # samples from the bad policy are not trained on
            self.buf.replay.clear()
            self._last_loaded_step = -1                       # the engine must take the restored weights
            self.rollbacks += 1
            log_line({"kind": "event", "step": step, "rule": "rollback", "action": {"action": "rollback_done"}, "metrics": {}, "text": f"weights of checkpoint-{cstep} restored, settings restored, buffers cleared"})

        # ---------------------------------------------------------------------------------------------------------
        # Held-out evaluation through the engine that sampled the training data
        # ---------------------------------------------------------------------------------------------------------
        def _maybe_eval(self, force=False):
            gs = int(self.state.global_step)
            if gs in self.eval_done or not R.eval_every:
                return
            if not force and gs % R.eval_every != 0:
                return
            if len(self.micro) % self.accum != 0:
                return
            self.eval_done.add(gs)
            try:
                log_line({"kind": "eval", "step": gs, **self.evaluate()})
            except Exception:
                traceback.print_exc()
            self.step_t0 = time.time()

        def evaluate(self):
            from trl.rewards.accuracy_rewards import _grade
            from vllm import SamplingParams
            t0 = time.time()
            if self._last_loaded_step != int(self.state.global_step):
                self.vllm_generation.sync_weights()
                self._last_loaded_step = int(self.state.global_step)
            prompts = [self.tok.apply_chat_template(r["prompt"], tokenize=False, add_generation_prompt=True, enable_thinking=False) for r in self.held]
            sp = SamplingParams(n=R.eval_samples, temperature=1.0, top_p=1.0, max_tokens=R.max_completion, seed=0)
            outs = self.vllm_generation.llm.generate(prompts, sp, use_tqdm=False)
            scores, lens, by_type = [], [], defaultdict(list)
            for row, o in zip(self.held, outs):
                s = [float(_grade(row["solution"], c.text)[0] or 0.0) for c in o.outputs]
                scores.append(sum(s) / len(s))
                by_type[row["task_type"]].append(scores[-1])
                lens += [len(c.token_ids) for c in o.outputs]
            return {"pass_rate": sum(scores) / len(scores), "mean_tokens": sum(lens) / len(lens), "seconds": time.time() - t0,
                    "by_type": {k: sum(v) / len(v) for k, v in by_type.items()}}

    return OffPolicyTrainer


def main():
    from transformers import AutoTokenizer, TrainerCallback
    from trl import GRPOConfig

    stop = threading.Event()
    if BUCKET:
        from store import sync_loop
        threading.Thread(target=sync_loop, args=(WORK, BUCKET, f"runs/{R.run_id}/", stop), daemon=True).start()
    train, held = load_data()
    reward = Gsm8kReward()
    hard = [r for r in train.select(range(min(len(train), 3000))) if r["task_type"] == "long"]
    cfg = GRPOConfig(
        output_dir=str(WORK / "hf"), per_device_train_batch_size=R.rows, gradient_accumulation_steps=R.accum,
        steps_per_generation=R.window * R.accum, num_generations=R.group, max_completion_length=R.max_completion, temperature=1.0, top_p=1.0, top_k=0,
        learning_rate=R.lr, lr_scheduler_type="constant", max_steps=R.steps, bf16=True, seed=R.seed, report_to="none", logging_steps=1,
        save_strategy="no", use_vllm=True, vllm_mode="colocate", vllm_gpu_memory_utilization=R.gpu_util or round(8 * 2 ** 30 / __import__("torch").cuda.get_device_properties(0).total_memory, 3), vllm_max_model_length=1024,
        vllm_importance_sampling_correction=True, loss_type="dapo", beta=0.0, epsilon=R.eps, epsilon_high=R.eps,
        chat_template_kwargs={"enable_thinking": False}, gradient_checkpointing=R.grad_ckpt, log_completions=False, max_grad_norm=1.0,
    )
    Trainer = build_trainer_class()
    tok = AutoTokenizer.from_pretrained(R.model)

    class RunLog(TrainerCallback):
        def on_log(self, args, state, control, logs=None, **kw):
            if logs:
                log_line({"kind": "train", "step": state.global_step, **{k: v for k, v in logs.items() if isinstance(v, (int, float))}})

        def on_train_end(self, args, state, control, **kw):
            trainer._maybe_eval(force=True)

    trainer = Trainer(model=R.model, reward_funcs=reward, args=cfg, train_dataset=train, processing_class=tok, callbacks=[RunLog()])
    trainer.setup_offpolicy(reward, held, hard, tok)
    import torch, transformers, trl, vllm
    props = torch.cuda.get_device_properties(0)
    log_line({"kind": "start", "recipe": dict(R.__dict__), "gpu": props.name, "gpu_gb": round(props.total_memory / 2 ** 30, 1),
              "versions": {"vllm": vllm.__version__, "trl": trl.__version__, "transformers": transformers.__version__, "torch": torch.__version__},
              "engine_gb": round(trainer.vllm_generation.gpu_memory_utilization * props.total_memory / 2 ** 30, 1)})
    print(f"run {R.run_id}: starting", flush=True)
    try:
        trainer.train()
        log_line({"kind": "done", "step": int(trainer.state.global_step), "seconds": time.time() - T0, "rollbacks": trainer.rollbacks, "skips": trainer.skips,
                  "wasted_chunks": trainer.wasted_chunks, "replay_retired": trainer.buf.retired})
    finally:
        stop.set()
        time.sleep(2)


if __name__ == "__main__":
    main()
