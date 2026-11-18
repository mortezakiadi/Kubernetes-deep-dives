---
id: design-scaling-rl
title: "Design 07: Scaling RL beyond the small-run regime"
short: "Scaling RL"
---
This design answers one question: *how do we take a post-training recipe that works on a small run (an 8B model, 4K-token answers, a few hundred steps, 32 GPUs) and make it work with more tokens, longer trajectories, larger models, more steps and a much larger compute budget, and how do we find the bottlenecks that only appear once we leave the small-run regime?* It builds directly on [Design 06](#design-post-training) (recipes, objectives, health signals), [Design 02](#design-multicluster) (capacity and queues), [Design 03](#design-training-inference) (training jobs and checkpoints), [Design 04](#design-agent-state) (long-running state and recovery) and [Design 05](#design-networking) (the fabric that moves weights).

The short version: *scaling RL is two problems at once*. The **algorithm** problem: a recipe that looks best at small scale may hit a lower ceiling at large scale, so we predict with fitted compute–performance curves before committing a big budget. The **systems** problem: at scale, the bottleneck moves from the trainer to rollout long tails, weight synchronization, numerical mismatch between engines, reward throughput, memory for long contexts, and failures over multi-week runs. The platform runs a **scaling ladder** (each rung roughly 4× bigger than the last), measures where time and stability go at every rung, and only climbs when the previous rung's curve and bottleneck profile are understood.

### The use case and requirements

**Scenario.** The recipe from Design 06 works: on an 8B model with 4K-token answers it improves math and code steadily for 500 steps on 32 GPUs. The team now wants a 235B mixture-of-experts model, answers up to 64K tokens, multi-turn tool use, 5,000+ steps and a budget of about 200,000 GPU hours. The first attempt at 8× scale ran at 11% GPU utilization, spent 40% of each step waiting for weight sync, and collapsed on day nine with no obvious cause.

![Small run to big run](diagrams/designs/concepts/ds07-c1.svg)

**What the platform must do.**

- **Predict before spending:** estimate where a recipe will plateau and how fast it gets there, from runs costing a few percent of the full budget.
- **Find the bottleneck at each scale:** break every step's time into rollout, reward, training, weight sync and waiting, and show which one grew as scale grew.
- **Keep GPUs busy despite long tails:** answers whose lengths vary 50× must not leave most GPUs idle.
- **Move weights fast:** pushing a hundreds-of-billions-parameter policy to the rollout fleet every step without stalling.
- **Stay stable for thousands of steps:** detect slow drifts (entropy, mismatch, length) and recover from instability without losing days.
- **Survive failures:** a multi-week run on thousands of GPUs will see node failures; resume in minutes, not hours.

**Out of scope:** the choice of objective and reward itself (Design 06), and the GPU fleet (Design 02).

### What changes when you scale each axis

**Scenario.** The team scales everything at once and can't tell which change caused which problem. They need a map of what each scaling axis breaks, so they can scale one axis at a time.

![Five axes](diagrams/designs/concepts/ds07-c2.svg)

![What changes when you scale each axis](diagrams/designs/ds07-1.svg)

**What the picture shows.** The five scaling axes and the bottleneck each one tends to expose first.

- **More tokens per step (bigger batches, larger groups):** rollout throughput and reward throughput become the limit; the trainer waits. Larger batches also change the optimization (fewer, larger updates).
- **Longer trajectories (16K → 64K+ tokens, multi-turn):** the long tail dominates step time, the KV cache limits how many sequences fit on a rollout GPU, and training activations for long sequences need context parallelism. Multi-turn tool use adds environment latency inside the rollout.
- **Larger models (dense 8B → MoE with hundreds of billions of parameters):** training needs tensor, pipeline, expert and context parallelism; rollout engines need their own, different layout; weight sync moves hundreds of gigabytes per step; MoE routing differs between engines.
- **More steps (hundreds → thousands):** slow drifts (entropy collapse, length growth, mismatch accumulation) have time to matter, the prompt set gets used up, and hardware failures become certain.
- **Larger compute budgets:** the cost of a wrong recipe choice grows, so prediction from small runs becomes the most valuable tool, and run-level operations (checkpointing, preemption, quotas) start to matter as much as algorithms.

**Going deeper.**

- **Scale one axis per rung.** A rung that changes model size *and* trajectory length *and* step count can't tell you which one caused a regression.
- **Some bottlenecks are invisible below a threshold.** Weight sync is negligible for 8B over NVLink and dominant for a large MoE over the network; the long tail barely matters at 4K tokens and dominates at 64K. The ladder exists to find those thresholds.

### Predict before you spend: compute–performance curves

**Scenario.** Two recipes are compared at 2,000 GPU hours. Recipe A is clearly ahead. At 30,000 hours, recipe B passes it and keeps rising while A flattens. Choosing from the small comparison would have wasted the budget on the recipe with the lower ceiling.

![Ceiling vs early lead](diagrams/designs/concepts/ds07-c3.svg)

![Predict before you spend: compute–performance curves](diagrams/designs/ds07-2.svg)

**What the picture shows.** Fitting a saturating (sigmoid) curve of held-out performance against compute, and what its parameters mean.

- **Ceiling (A):** the performance the recipe approaches with unlimited compute. Choices that change the ceiling matter most at scale.
- **Efficiency (B) and midpoint (C_mid):** how quickly the recipe gets to the ceiling, and how much compute it takes to get halfway. Choices that only change efficiency are worth less at large budgets, because every good recipe gets there eventually.
- **Fit early, check late:** fit on the first part of a run (excluding the noisy start) and check the extrapolation against later checkpoints; the gap between predicted and observed is the trust you can place in the curve.
- **Held-out metric, not training reward:** fit on a fixed validation set (for example, mean pass rate over 16 samples per problem), since training reward is measured on a moving, filtered subset.

The published study behind this approach fitted such curves across more than 400,000 GPU hours of experiments and found that some design choices mainly shift the ceiling while others (loss aggregation, normalization, curriculum, the off-policy algorithm) mainly change efficiency. Its extrapolation from the first half of a 100,000-GPU-hour run closely matched the second half.

**The code.** Fit and compare recipes from their eval history (Python, SciPy).

```python
import numpy as np
from scipy.optimize import curve_fit

def sigmoid(C, A, B, C_mid, R0):
    """Held-out pass rate as a function of compute C (GPU hours)."""
    return R0 + (A - R0) / (1.0 + (C_mid / C) ** B)

def fit_recipe(gpu_hours: np.ndarray, pass_rate: np.ndarray, skip_hours: float = 1500):
    keep = gpu_hours > skip_hours                     # drop the noisy start #!
    R0 = pass_rate[0]
    f = lambda C, A, B, C_mid: sigmoid(C, A, B, C_mid, R0)
    (A, B, C_mid), cov = curve_fit(f, gpu_hours[keep], pass_rate[keep],
                                   p0=[0.6, 1.0, 5000], bounds=([R0, 0.1, 100], [1.0, 5.0, 1e6]))
    return {"A": A, "B": B, "C_mid": C_mid, "A_std": float(np.sqrt(cov[0, 0]))}

for name, hist in runs.items():                       # runs: recipe -> (hours, pass_rate) arrays
    p = fit_recipe(*hist)
    print(f"{name:<14} ceiling A={p['A']:.3f}±{p['A_std']:.3f}  B={p['B']:.2f}  C_mid={p['C_mid']:.0f} h")
# Commit the big budget to the recipe with the highest ceiling whose fit is tight,  #!
# not the one that is ahead at the budget you happened to measure.
```

**Going deeper.**

- **Uncertainty matters.** A ceiling estimated from a run that hasn't reached its midpoint is weak; the platform shows the confidence interval and refuses to "rank" recipes whose intervals overlap.
- **Curves per axis:** fit separate curves for larger models and longer generation lengths; both can raise the ceiling while making early progress look slower.

### The scaling ladder

**Scenario.** Leadership asks "when can we start the big run?" The honest answer is "after the rung below it is understood," and the platform needs to make that concrete.

![Scaling ladder](diagrams/designs/concepts/ds07-c4.svg)

![The scaling ladder](diagrams/designs/ds07-3.svg)

**What the picture shows.** A sequence of runs, each roughly 4× the previous, with explicit exit criteria.

- **Rung 1, recipe search (~1–2% of budget):** small model, short answers; many ablations; fit curves; pick candidates.
- **Rung 2, length:** same model, much longer answers; measures the long tail, KV cache pressure and length dynamics.
- **Rung 3, model size:** the target architecture (for example, the MoE) on fewer steps; exposes parallelism, weight sync and routing mismatch.
- **Rung 4, duration:** the target model and length for many steps; exposes drift, prompt exhaustion and failure handling.
- **The production run:** the full budget, with the curve from rung 4 as the forecast it's checked against.
- **Exit criteria at every rung:** the curve fit is stable, the step-time breakdown is understood (no unexplained waiting), the health signals are flat or explained, and a restart from checkpoint has been tested.

**The YAML.** A rung is just a recipe overlay: the base recipe plus the axis being scaled (applied by the launcher from [Design 06](#design-post-training)).

```yaml
# recipes/assistant-v13/ladder/rung3-model-size.yaml
extends: recipes/assistant-v13/rl-base.yaml
ladder:
  rung: 3
  axis: model_size                       # exactly one axis changes per rung #!
  previous: runs/assistant-v13-rung2-8b-64k
  budget_gpu_hours: 12000
  exit_criteria:
    curve_fit_ceiling_ci: 0.03           # ±0.03 on the ceiling estimate
    unexplained_wait_fraction_max: 0.10  # of step time
    restart_from_checkpoint_tested: true
model:
  path: registry://base-235b-a22b-moe@v1
  trainer_parallelism: { tp: 4, pp: 4, ep: 8, cp: 2 }   # training layout
  rollout_parallelism: { tp: 4, ep: 8, dp: 16 }         # inference layout, different #!
rollout:
  max_response_tokens: 65536             # unchanged from rung 2
```

**Going deeper.**

- **Budget the ladder explicitly:** roughly 10–15% of the total compute for rungs 1–4 is a cheap insurance policy against spending the other 85% on the wrong recipe or a run that collapses.
- **Keep a "reference rung"** (small, cheap, fixed) that re-runs nightly on the current code; it catches framework regressions before they reach a big run.

### Where the time goes: the step-time breakdown

**Scenario.** At rung 3, a step takes 14 minutes. Nobody knows whether to buy more rollout GPUs, optimize the trainer or fix the network, because the 14 minutes have never been broken down.

![Step-time breakdown](diagrams/designs/concepts/ds07-c5.svg)

![Where the time goes: the step-time breakdown](diagrams/designs/ds07-4.svg)

**What the picture shows.** How one RL step's time splits as scale grows, and which part to attack.

- **Rollout generation:** at small scale, roughly half the step; with long trajectories, commonly most of it. The long tail (the slowest few percent of answers) often decides the whole step.
- **Reward and environment time:** small for exact-match math; large for sandboxed tests, judges and multi-turn tools.
- **Reference and old-policy log-probabilities:** an extra forward pass over every token; grows with tokens per step.
- **Training (forward + backward + optimizer):** grows with model size and tokens, but usually isn't the largest share in RL.
- **Weight sync:** negligible for a small model on one node; tens of seconds to minutes for a large model across nodes, every step.
- **Idle (bubbles):** GPUs waiting for another stage. The single most useful number to drive down.

**The code.** Every run emits a span per stage per step; the platform turns them into this breakdown and the "bubble" fraction (OpenTelemetry spans, exported like the traces in [Observability](#post-observability)).

```python
import time
from contextlib import contextmanager
from opentelemetry import trace, metrics

tracer = trace.get_tracer("rl.step")
stage_seconds = metrics.get_meter("rl").create_histogram("rl_stage_seconds", unit="s")

@contextmanager
def stage(name: str, step: int, run_id: str, gpus: int):
    t0 = time.perf_counter()
    with tracer.start_as_current_span(f"rl.{name}", attributes={"step": step, "run": run_id}):
        yield
    dt = time.perf_counter() - t0
    stage_seconds.record(dt, {"stage": name, "run": run_id, "gpus": str(gpus)})

# in the training loop
with stage("rollout", step, run_id, rollout_gpus):     batch = rollout_pool.collect(prompts)
with stage("reward", step, run_id, 0):                 rewards = reward_pool.score(batch)
with stage("logprobs", step, run_id, train_gpus):      old_lp = trainer.logprobs(batch)
with stage("train", step, run_id, train_gpus):         trainer.update(batch, rewards, old_lp)
with stage("weight_sync", step, run_id, rollout_gpus): sync.push(trainer.state_dict_shards())  #!
```

**Going deeper.**

- **Measure GPU-seconds, not seconds:** a 60-second stage on 16 rollout GPUs and a 60-second stage on 128 training GPUs cost very different amounts. The breakdown is weighted by the GPUs each stage holds, including GPUs held while idle.
- **Profile the slowest step, not the average:** the long tail is in the tail.

### Long trajectories and the long tail

**Scenario.** At 64K-token answers, the median answer finishes in 4 minutes and the slowest 1% take 25. In synchronous RL, every step waits 25 minutes, and 90% of rollout GPUs sit idle for most of it.

![Long tail](diagrams/designs/concepts/ds07-c6.svg)

![Long trajectories and the long tail](diagrams/designs/ds07-5.svg)

**What the picture shows.** The long-tail problem and the techniques that tame it.

- **Over-provision and cut off (active partial rollouts):** start more answers than the batch needs; when enough have finished, stop the rest and keep their partial output; resume them in the next step under the newer policy.
- **Asynchrony with a staleness bound:** separate rollout and training pools, so the trainer consumes whatever has finished; samples are at most *k* policy versions old, with importance-sampling corrections.
- **Length-aware scheduling:** route long-running requests to engines with free KV cache; rebalance during generation; speculative decoding for long answers.
- **KV cache capacity:** long contexts cap the number of concurrent sequences per rollout GPU; FP8 KV cache and prefix caching (group samples share the prompt) raise it.
- **Training long sequences:** context parallelism splits one sequence across GPUs; sequence packing and dynamic batch sizes by token count keep training balanced.
- **Length dynamics:** answer length usually grows during reasoning RL; the length cap, the overlong penalty and truncation masking (Design 06) decide how that interacts with the budget.

Production systems report that rollout can take 50–80% of step time and more, and that partial-rollout and asynchronous designs recover much of it, with the caveat that stale samples must be corrected or repaired.

**The YAML.** The rollout pool as a vLLM deployment sized for long contexts, separate from training (one group per engine with LeaderWorkerSet for multi-node engines).

```yaml
apiVersion: leaderworkerset.x-k8s.io/v1
kind: LeaderWorkerSet
metadata:
  name: rollout-235b
  namespace: rl-scale
  labels: { kueue.x-k8s.io/queue-name: rl-rollout }
spec:
  replicas: 16                                    # 16 engines, 2 nodes each
  leaderWorkerTemplate:
    size: 2
    restartPolicy: RecreateGroupOnPodRestart      # an engine is all-or-nothing #!
    leaderTemplate:
      spec:
        containers:
        - name: vllm
          image: registry.example.com/rl/vllm:2026.11-rl
          args:
          - --model=/weights/current
          - --tensor-parallel-size=8
          - --pipeline-parallel-size=2
          - --enable-expert-parallel
          - --max-model-len=81920
          - --kv-cache-dtype=fp8                  # more concurrent long sequences #!
          - --enable-prefix-caching               # group samples share the prompt
          - --max-num-seqs=256
          resources: { limits: { nvidia.com/gpu: 8 } }
    workerTemplate:
      spec:
        containers:
        - name: vllm-worker
          image: registry.example.com/rl/vllm:2026.11-rl
          resources: { limits: { nvidia.com/gpu: 8 } }
```

**Going deeper.**

- **Partial rollouts change the data:** one answer may contain tokens from several policy versions. Record the version per token span so the importance-sampling correction uses the right probabilities.
- **The tail grows with training:** as the model learns to think longer, a step-time budget that worked at step 500 fails at step 3,000. Re-check the breakdown regularly.

### Larger models: two layouts and the weight sync between them

**Scenario.** The 235B MoE trains well with one parallel layout but needs a completely different layout to generate efficiently. Every step, the new weights have to get from one layout to the other, across 64 nodes, and that transfer alone takes four minutes.

![Two layouts](diagrams/designs/concepts/ds07-c7.svg)

![Larger models: two layouts and the weight sync between them](diagrams/designs/ds07-6.svg)

**What the picture shows.** The training and rollout layouts, and the paths for moving weights between them.

- **Training layout:** tensor, pipeline, expert, context and data parallelism (Megatron or FSDP), optimized for memory and backward passes.
- **Rollout layout:** tensor and expert parallelism with many data-parallel engines, optimized for decoding throughput and KV cache.
- **Resharding:** each rollout engine needs the full model in its own layout; the trainer's shards must be gathered and re-split. Done naively, this is a full gather through one place.
- **Colocated vs disaggregated:** colocated (same GPUs, time-shared) reshards in GPU memory, no network; disaggregated (separate pools) must cross the network, but lets each pool scale and keeps both busy.
- **Faster sync:** bucketed broadcasts over NCCL on the fast fabric, overlapping sync with the next rollouts, sending only changed parameters or compressed deltas, and pipelining layer by layer so engines start before the whole model arrives.

**The code.** A bucketed, layer-pipelined push from trainer ranks to rollout engines, with the policy version recorded (sketch).

```python
import torch, torch.distributed as dist

BUCKET_BYTES = 1 << 30                                       # 1 GiB buckets

def push_weights(named_shards, group, version: int, resharder):
    """Gather each layer to rollout layout and broadcast in buckets over NCCL."""
    bucket, size = [], 0
    for name, shard in named_shards:                         # iterate layer by layer #!
        full = resharder.to_rollout_layout(name, shard)      # TP/EP re-split for vLLM
        bucket.append((name, full)); size += full.numel() * full.element_size()
        if size >= BUCKET_BYTES:
            _flush(bucket, group); bucket, size = [], 0
    if bucket:
        _flush(bucket, group)
    dist.broadcast(torch.tensor([version], device="cuda"), src=0, group=group)  # engines tag tokens #!

def _flush(bucket, group):
    flat = torch.cat([t.flatten() for _, t in bucket])
    dist.broadcast(flat, src=0, group=group)                 # rollout engines load in place
```

**Going deeper.**

- **Size the sync path like a serving path:** a 235B model in BF16 is about 470 GB. At an effective 50 GB/s per receiving engine that's around 10 seconds; through one bottleneck link it's minutes. Use the GPU fabric ([Design 05](#design-networking)), not the pod network.
- **Measure sync as its own stage** and set a budget (for example, under 5% of step time); above it, overlap sync with rollout or move to asynchronous training.

### Numerics and MoE: the mismatch that grows with scale

**Scenario.** At 8B dense, the inference engine's token probabilities and the trainer's match closely. At 235B MoE with FP8 rollouts, a few percent of tokens have probability ratios above 10, and those tokens dominate the gradient. The run looks fine for a week, then collapses.

![Mismatch grows with scale](diagrams/designs/concepts/ds07-c8.svg)

![Numerics and MoE: the mismatch that grows with scale](diagrams/designs/ds07-7.svg)

**What the picture shows.** Sources of train–inference mismatch, and how each is reduced.

- **Different kernels and precision:** the inference engine and trainer compute the same function in different ways; low-precision rollouts (FP8) widen the gap. Corrections: truncated or masked importance sampling at the token level, and keeping the final projection to logits in FP32 on both sides.
- **MoE routing differences:** a tiny numerical difference can send a token to different experts in the two engines, so the probabilities differ a lot. **Routing replay** records the experts chosen during rollout and reuses them in training.
- **Staleness:** asynchronous rollouts are from older policies; their ratios are expected to differ, and are corrected the same way.
- **Watch the ratio distribution:** track the fraction of tokens with ratio beyond a threshold, and the KL between rollout and trainer probabilities, per step. A widening tail is the earliest warning.
- **Bitwise-consistent inference:** where the engine offers kernels that exactly match the trainer, the mismatch disappears at some throughput cost; worth testing at each rung.

**The code.** Mismatch metrics and a truncated importance weight, computed from probabilities both engines already return.

```python
import torch

def mismatch_metrics(train_logp, rollout_logp, mask, cap: float = 3.0):
    """Per-token log ratio between trainer and rollout engine, and a truncated IS weight."""
    log_ratio = (train_logp - rollout_logp) * mask
    ratio = log_ratio.exp()
    n = mask.sum()
    stats = {
        "mismatch/kl": ((ratio - 1) - log_ratio).sum().item() / n.item(),   # k3 estimator
        "mismatch/frac_ratio_gt_2": ((ratio > 2) & mask.bool()).sum().item() / n.item(),  #!
        "mismatch/max_ratio": ratio[mask.bool()].max().item(),
    }
    is_weight = ratio.clamp(max=cap).detach()               # truncated IS (TIS)
    return stats, is_weight
```

**Going deeper.**

- **Precision is a ceiling choice, not just a speed choice.** Published scaling work found that FP32 for the logit computation shifted the asymptotic performance, not only stability.
- **Test mismatch before the run:** score the same rollouts with both engines on the target model and inspect the ratio histogram; it's a cheap pre-flight check at every rung.

### Rewards and environments at scale

**Scenario.** Moving from math to multi-turn coding agents, each rollout calls tools 20–50 times. The sandbox pool that handled 2,000 test runs per step now gets 150,000 tool calls per step, and rollout GPUs sit idle waiting for sandboxes.

![Environment tier](diagrams/designs/concepts/ds07-c9.svg)

![Rewards and environments at scale](diagrams/designs/ds07-8.svg)

**What the picture shows.** The reward and environment tier as its own autoscaled service.

- **Stateless verifiers:** exact match, unit tests, schema checks; scale horizontally on CPU, batched per step.
- **Judges and reward models:** served on their own GPU pool with vLLM; sized for the step's token count, not for interactive latency.
- **Stateful environments for agentic RL:** sandboxes, browsers and terminals that live for a whole trajectory; pre-warmed pools and snapshots so starting one takes milliseconds ([Design 01](#design-sandboxing)).
- **Environment stragglers:** one slow tool call blocks a trajectory; time out, retry or mark the turn failed, and count failures by environment.
- **Back-pressure:** rollout engines pause new trajectories when the environment queue is full, instead of piling up idle KV cache.

**The YAML.** The sandbox pool scales on queue depth, not CPU, so it grows before rollouts stall (KEDA).

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: rl-sandbox-pool
  namespace: rl-scale
spec:
  scaleTargetRef: { name: sandbox-workers }
  minReplicaCount: 200                           # pre-warmed for the step start #!
  maxReplicaCount: 6000
  cooldownPeriod: 300
  triggers:
  - type: prometheus
    metadata:
      serverAddress: http://prometheus.monitoring:9090
      query: sum(rl_env_queue_depth{run="assistant-v13-prod"})
      threshold: "25"                            # pending tool calls per worker
```

**Going deeper.**

- **Environments are deterministic by contract:** the same seed and actions give the same observations, so trajectories can be replayed for debugging and for importance-sampling checks.
- **Count environment cost per trajectory:** at scale, CPU and sandbox minutes can rival GPU cost for agentic tasks.

### Stability over thousands of steps

**Scenario.** The production run improves for 2,400 steps, then held-out scores start sliding. Entropy fell slowly the whole time, response length doubled since step 1,500, and 30% of prompts now have all-correct groups. Each signal was within its alert threshold; together they predicted the slide.

![Slow drifts](diagrams/designs/concepts/ds07-c10.svg)

![Stability over thousands of steps](diagrams/designs/ds07-9.svg)

**What the picture shows.** Slow drifts that only matter on long runs, and the controls for each.

- **Entropy decay:** exploration fades over thousands of steps. Controls: clip-higher, temperature, entropy-targeted token selection, and resetting the reference point periodically if KL is used.
- **Length growth:** answers get longer as the model learns to think more; step time and memory grow with them. Controls: a length curriculum (raise the cap in stages), overlong penalties, and re-planning rollout capacity.
- **Prompt exhaustion:** the model solves more prompts and the informative set shrinks. Controls: refresh pass-rate estimates, add harder prompts, sample by difficulty, and remove mastered prompts.
- **Mismatch accumulation:** small ratio drift becomes large. Controls: IS corrections, routing replay, precision checks at intervals.
- **Composite alerts:** alert on combinations and trends (entropy slope × length slope × informative-prompt fraction), not single thresholds.
- **Rollback with change:** on a collapse, roll back to the last healthy checkpoint *and* change something (lower LR, tighter staleness, refreshed prompts); a plain restart usually collapses again at the same point.

**Going deeper.**

- **Keep checkpoints dense enough to roll back cheaply** (every 25–50 steps for the latest, every few hundred kept long term), and record the health signals with each so "the last healthy checkpoint" is a query, not a guess.
- **Schedules over a long run:** learning-rate decay, length-cap increases and data refreshes are planned in the recipe, so the curve fitted at rung 4 still applies.

### Failures and recovery on multi-week runs

**Scenario.** A 2,048-GPU run on its eleventh day: one GPU throws an uncorrectable memory error. The whole job crashes; restarting takes 70 minutes (rescheduling, loading a 3 TB checkpoint, warming 64 rollout engines). It happens twice a week.

![Cheap failures](diagrams/designs/concepts/ds07-c11.svg)

![Failures and recovery on multi-week runs](diagrams/designs/ds07-10.svg)

**What the picture shows.** Making failures cheap rather than rare.

- **Separate the blast radius:** a failed rollout engine is replaced while the run continues (rollouts are stateless and retryable); only trainer failures require a training restart.
- **Fast checkpoints:** asynchronous, sharded checkpoints to local NVMe first and object storage in the background, so saving takes seconds of training time ([Design 04](#design-agent-state)).
- **Fast restarts:** hot spare nodes reserved by the queue, pre-pulled images, checkpoints loaded in parallel from the nearest copy, rollout engines warm-started from the new weights.
- **Health checks before admission:** nodes run GPU, NVLink and network tests before joining; repeat offenders are drained automatically.
- **Save the rollout buffer and data cursor** with the checkpoint, so a restart neither repeats nor skips prompts.

**The YAML.** The trainer as a JobSet with a failure policy that restarts the trainer group while rollout engines (a separate LeaderWorkerSet) keep running.

```yaml
apiVersion: jobset.x-k8s.io/v1alpha2
kind: JobSet
metadata:
  name: assistant-v13-prod-trainer
  namespace: rl-scale
  labels: { kueue.x-k8s.io/queue-name: rl-production }   # reserved capacity, not preemptible
  annotations:
    alpha.jobset.sigs.k8s.io/exclusive-topology: network.example.com/fabric-block   # one fabric block #!
spec:
  failurePolicy:
    maxRestarts: 50                                   # weeks-long runs will restart #!
    restartStrategy: Recreate
  replicatedJobs:
  - name: trainer
    replicas: 1
    template:
      spec:
        parallelism: 128
        completions: 128
        backoffLimit: 0
        template:
          spec:
            containers:
            - name: megatron
              image: registry.example.com/rl/trainer:2026.11
              command: ["python3", "-m", "rl.train", "--resume=latest", "--recipe=/recipes/prod.yaml"]
              resources: { limits: { nvidia.com/gpu: 8 } }
              volumeMounts: [ { name: nvme, mountPath: /local-ckpt } ]
            volumes:
            - name: nvme
              hostPath: { path: /mnt/nvme/ckpt }
```

**Going deeper.**

- **Track goodput:** the fraction of reserved GPU-hours that produced training progress. Failures, restarts, sync and bubbles all subtract from it; it's the run-level metric leadership should see.
- **Rehearse a failure at rung 4:** kill a trainer node and a rollout engine on purpose and time the recovery.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds07-c12.svg)

- **Phase 1, measurement:** step-time breakdown, goodput and mismatch metrics on every run, plus the curve-fitting service on eval history.
- **Phase 2, the ladder:** rung recipes, exit criteria and the nightly reference rung.
- **Phase 3, systems for long trajectories and large models:** disaggregated rollout pools, partial rollouts, bucketed weight sync, routing replay, autoscaled environment tier.
- **Phase 4, run operations:** fast checkpoints, hot spares, composite alerts, rollback with change, failure rehearsals.

**Trade-offs we accepted.**

- **Spending 10–15% of the budget on the ladder,** instead of starting the big run immediately.
- **Disaggregated rollout and training:** better utilization and independent scaling, at the cost of network-bound weight sync and more moving parts.
- **Asynchrony and partial rollouts:** large throughput gains, at the cost of off-policy samples that must be corrected and monitored.
- **Lower-precision rollouts:** faster generation, at the cost of more mismatch; only adopted after the ratio histogram at the target scale is acceptable.

**Open questions.**

- **How far do curves extrapolate across model sizes?** We fit per model size today; a joint fit would let rung 1 predict more.
- **Agentic RL at scale:** environments with long, stateful, slow steps may need their own asynchronous design, decoupled from token generation.
- **Weight sync with sparse or delta updates:** if only a small fraction of parameters change meaningfully per step, can we send much less?
- **Multi-cluster RL:** rollout in one region and training in another, with weights crossing a WAN; viable only with large staleness tolerance.
