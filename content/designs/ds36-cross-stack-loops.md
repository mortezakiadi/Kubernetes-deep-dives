---
id: design-cross-stack
title: "Design 36: Closing loops across the stack: finding the highest-leverage improvements and optimizing across layers"
short: "Cross-stack optimization"
---
This design answers one question: *how do we find the improvements that matter most anywhere in the stack, from distributed pre-training, post-training and serving to agent harnesses, runtimes and research environments, and then design and optimize several layers together when no single layer can fix the problem alone?* Every earlier design optimizes one layer well: scheduling ([Design 17](#design-gpu-scheduling)), distributed training and checkpoints ([Design 20](#design-distributed-training)), serving ([Design 21](#design-llm-serving)), off-policy training ([Design 09](#design-off-policy)), agent state ([Design 04](#design-agent-state)) and sandboxed worlds ([Design 30](#design-sandbox-worlds)). This design is about the seams between them, where most of the remaining waste sits and where no team is the obvious owner.

The short version: *measure one end-to-end metric, trace real workloads through every layer, and rank work by how much it moves the end-to-end number*. Each workload family gets a metric tree whose root is what the organization actually wants, such as verified RL training tokens per GPU-hour, and whose leaves are the numbers each team already owns. Workloads are traced end to end with one trace ID across trainer, inference, harness, sandbox and environment, so the critical path is measured rather than argued about. Opportunities are ranked by leverage: the share of the critical path they touch, the speedup they can achieve there, and the effort they need. The biggest wins come from changing two layers at once: tuning serving for RL step time rather than throughput, moving weights from trainer to inference without a checkpoint round trip, laying out harness prompts so the inference cache can reuse them, and choosing model shapes during pre-training with serving cost in view. Every change is accepted on the end-to-end metric, and every loop has an owner.

### The use case and requirements

**Scenario.** An agentic RL run on 1,024 GPUs takes 412 seconds per step. Over one quarter, every team improved its own number. Serving raised decode throughput by 2.9 times on its benchmark. The trainer team raised model FLOPs utilization from 38% to 46%. The sandbox team cut container start time in half. The step time went from 412 seconds to 405, and runs that picked up the new serving settings got slower. Nobody was wrong about their own layer; nobody owned the step.

![Closing loops across the stack](diagrams/designs/concepts/ds36-c1.svg)

**What the system must deliver.**

- **One number per workload family:** an agreed end-to-end metric for pre-training, RL post-training, serving and agent rollouts, computed the same way every week.
- **Measured critical paths:** for each family, the share of end-to-end time and cost spent in each layer, from traces of real runs, refreshed weekly.
- **A ranked list:** the top opportunities across all layers, each with its expected end-to-end gain, effort and owner.
- **Cross-layer changes that land:** changes that need two or more teams have a single owner, a joint design and one end-to-end acceptance test.
- **No silent regressions across seams:** a change that improves its layer's metric while hurting the end-to-end metric is caught before rollout.
- **Results reported end to end:** the quarter's progress is reported as the end-to-end numbers, with how much each change contributed.

**Out of scope:** the internals of each layer, which the earlier designs cover, and research choices such as which RL algorithm to use ([Design 06](#design-post-training)). This design decides where effort goes and how layers are changed together.

### One end-to-end metric and its tree

**Scenario.** In the quarterly review, the serving team showed tokens per second per GPU, the trainer team showed MFU, and the environments team showed episodes per hour. All three went up. Nobody could say whether the organization got more training done per dollar.

![Metric tree](diagrams/designs/concepts/ds36-c2.svg)

![One end-to-end metric and its tree](diagrams/designs/ds36-1.svg)

**What the picture shows.** A metric tree per workload family, with the end-to-end metric at the root and team metrics as leaves.

- **Root for RL post-training:** verified training tokens per GPU-hour. These are tokens that passed reward and filtering and entered an optimizer step, divided by all GPU-hours the run used, including the inference fleet.
- **Branches:** tokens per step, the share of tokens kept after filtering, step time, and GPUs used. Step time splits into the rollout phase, weight sync and the trainer step.
- **Leaves:** the numbers teams already own: decode throughput and tail latency, tool-call latency, sandbox start time, environment reset time, verifier latency, trainer MFU and weight transfer time.
- **Other roots:** pre-training uses tokens trained per GPU-hour at a fixed loss target; serving uses goodput per GPU at the SLO ([Design 21](#design-llm-serving)); agent rollouts use completed, verified episodes per GPU-hour.
- **Same definitions everywhere:** the tree is code, computed from the same telemetry ([Design 24](#design-gpu-observability)) and the cost warehouse ([Design 25](#design-cost-capacity)), so no team computes its own version of the root.

**The code.** Computing the root of the RL tree from its leaves (Python).

```python
from dataclasses import dataclass

@dataclass
class RLStep:
    tokens_generated: int      # all rollout tokens in one step
    kept_share: float          # share kept after reward and filtering
    rollout_s: float
    weight_sync_s: float
    train_s: float
    trainer_gpus: int
    inference_gpus: int

    def step_s(self) -> float:
        return self.rollout_s + self.weight_sync_s + self.train_s          # synchronous loop

    def verified_tokens_per_gpu_hour(self) -> float:
        gpu_hours = self.step_s() * (self.trainer_gpus + self.inference_gpus) / 3600
        return self.tokens_generated * self.kept_share / gpu_hours                          #!

before = RLStep(1_270_000, 0.62, rollout_s=318, weight_sync_s=46, train_s=48, trainer_gpus=512, inference_gpus=512)
print(f"step {before.step_s():.0f} s, {before.verified_tokens_per_gpu_hour():,.0f} verified tokens per GPU-hour")
for name, share in [("rollout", before.rollout_s), ("weight sync", before.weight_sync_s), ("train", before.train_s)]:
    print(f"  {name:12s} {share / before.step_s():.0%} of the step")
```

**Going deeper.**

- **Count every GPU:** a root that divides by trainer GPUs alone rewards moving work onto the inference fleet. The denominator includes every GPU the run reserved, idle or not.
- **Quality belongs in the root:** a faster loop that produces worse models isn't progress. For RL, the root is paired with the evaluation trajectory per GPU-hour ([Design 15](#design-eval-infra)), and a change must not hurt it.

### Tracing a workload through every layer

**Scenario.** The serving team believed rollouts were slow because of the sandbox. The sandbox team believed they were slow because of tool timeouts. Both had dashboards, and neither dashboard could follow a single episode from the trainer's request to its reward.

![Tracing](diagrams/designs/concepts/ds36-c3.svg)

![Tracing a workload through every layer](diagrams/designs/ds36-2.svg)

**What the picture shows.** One trace per step and per episode, across every layer, and a critical path computed from it.

- **One trace ID:** the trainer starts a trace for each step. The ID is propagated into every rollout request, the harness, model calls, tool calls, sandbox operations, environment resets, verifier calls and the weight sync.
- **Spans with layers:** every span carries its layer (`trainer`, `inference`, `harness`, `sandbox`, `environment`, `verifier`, `weights`) and its owner, so time adds up by team.
- **Sampling that keeps the tail:** all step spans are kept; episode spans are sampled at 5%, plus every episode in the slowest 1% of its step, because the tail decides synchronous step time.
- **Critical path:** for each step, the chain of spans that determined when it ended. Time off the critical path can be slow without costing anything.
- **What it showed:** on the critical path, inference decode took 41% of the step, waiting on the slowest episodes 19%, tool execution in sandboxes 12%, weight sync 11%, the trainer step 12%, and environment resets and verifiers 5%.

**The code.** The critical path and time per layer from a step's spans (Python).

```python
from collections import defaultdict

def critical_path(spans: dict[str, dict]) -> list[str]:
    """spans: id -> {layer, start, end, deps}. Walk back from the last span to finish,
    always through the dependency that finished last."""
    cur = max(spans, key=lambda s: spans[s]["end"])
    path = [cur]
    while spans[cur]["deps"]:
        cur = max(spans[cur]["deps"], key=lambda d: spans[d]["end"])                         #!
        path.append(cur)
    return path[::-1]

def time_by_layer(spans: dict[str, dict], path: list[str]) -> dict[str, float]:
    out, prev_end = defaultdict(float), None
    for s in path:
        start = spans[s]["start"] if prev_end is None else max(spans[s]["start"], prev_end)
        out[spans[s]["layer"]] += spans[s]["end"] - start
        prev_end = spans[s]["end"]
    return dict(out)

spans = {
    "sample":   {"layer": "trainer",   "start": 0,   "end": 2,   "deps": []},
    "ep-fast":  {"layer": "inference", "start": 2,   "end": 120, "deps": ["sample"]},
    "ep-slow":  {"layer": "inference", "start": 2,   "end": 240, "deps": ["sample"]},
    "tool-7":   {"layer": "sandbox",   "start": 240, "end": 290, "deps": ["ep-slow"]},
    "reward":   {"layer": "verifier",  "start": 290, "end": 305, "deps": ["tool-7", "ep-fast"]},
    "train":    {"layer": "trainer",   "start": 305, "end": 353, "deps": ["reward"]},
    "sync":     {"layer": "weights",   "start": 353, "end": 399, "deps": ["train"]},
}
path = critical_path(spans)
print(path)
print(time_by_layer(spans, path))
```

**The YAML.** The OpenTelemetry Collector pipeline that tags spans with the run and keeps the slow tail.

```yaml
receivers:
  otlp:
    protocols:
      grpc: { endpoint: 0.0.0.0:4317 }
processors:
  attributes/run:
    actions:
    - { key: k8s.namespace.name, from_context: metadata.k8s-namespace, action: upsert }
  tail_sampling:
    decision_wait: 30s
    policies:
    - name: keep-steps                                  # every step span
      type: string_attribute
      string_attribute: { key: span.kind.rl, values: [step] }
    - name: keep-slow                                   # every slow episode #!
      type: latency
      latency: { threshold_ms: 120000 }
    - name: sample-rest
      type: probabilistic
      probabilistic: { sampling_percentage: 5 }
  batch: {}
exporters:
  otlp/traces:
    endpoint: traces-store.observability.svc:4317
service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [attributes/run, tail_sampling, batch]
      exporters: [otlp/traces]
```

**Going deeper.**

- **The trace needs a stable schema:** layer names, span names and the attributes that identify a step and an episode are versioned like an API. A renamed span quietly breaks every critical-path report.
- **Fixed thresholds miss tails:** a latency threshold keeps slow episodes in absolute terms. The trainer also marks the slowest 1% of each step's episodes explicitly, because what counts as slow changes with task mix.

### Ranking opportunities by leverage

**Scenario.** The planning meeting had 31 proposals from six teams. They were ranked by who argued best. The serving throughput project had been the top priority for two quarters; it touched 41% of the step and, at a fixed batch size, could make that part 1.2 times faster.

![Leverage](diagrams/designs/concepts/ds36-c4.svg)

![Ranking opportunities by leverage](diagrams/designs/ds36-3.svg)

**What the picture shows.** Each proposal is scored with the same arithmetic, using the measured critical path.

- **Share:** the fraction of the end-to-end critical path (or cost) the change touches, from traces, not from the proposer's estimate.
- **Local speedup:** how much faster that part gets, from a benchmark or a prototype where one exists; otherwise an estimate marked as such.
- **End-to-end gain:** Amdahl's law: a speedup of k on a share s cuts end-to-end time by s × (1 − 1 / k).
- **Effort and risk:** engineer-weeks, the number of teams involved and how reversible the change is.
- **Leverage:** end-to-end gain per engineer-week. The top of the list is reviewed by a small cross-layer group each month.
- **What came out on top:** serving tuned for step time, cache-friendly harness prompts, partial rollouts for the slow tail and direct weight sync. All four needed two layers to change together; the decode kernel project came fifth.

**The code.** Scoring proposals with Amdahl's law (Python).

```python
from dataclasses import dataclass

@dataclass
class Proposal:
    name: str
    share: float          # fraction of the critical path it touches (from traces)
    speedup: float        # local speedup on that part
    weeks: float          # engineer-weeks
    layers: int           # number of layers that must change

    def e2e_gain(self) -> float:
        return self.share * (1 - 1 / self.speedup)          # share of end-to-end time removed #!

    def leverage(self) -> float:
        return self.e2e_gain() / self.weeks

props = [Proposal("decode kernel tuning (serving)", 0.41, 1.20, 10, 1),
         Proposal("rollout batch limit tuned for step time", 0.41, 1.25, 2, 2),
         Proposal("partial rollouts for the slow tail", 0.19, 4.0, 6, 2),
         Proposal("direct weight sync, no checkpoint", 0.11, 6.0, 5, 2),
         Proposal("cache-friendly harness prompts", 0.41, 1.35, 3, 2),
         Proposal("trainer MFU 46% -> 52%", 0.12, 1.13, 8, 1),
         Proposal("faster sandbox start", 0.03, 2.0, 4, 1)]
for p in sorted(props, key=Proposal.leverage, reverse=True):
    print(f"{p.name:40s} e2e {p.e2e_gain():5.1%}  per week {p.leverage():.2%}  layers {p.layers}")
```

**Going deeper.**

- **Shares move after each fix:** once the slow tail is fixed, decode becomes a larger share of a shorter step. Rankings are recomputed from fresh traces after every major change, never carried forward.
- **Cost and time are different roots:** a change that frees idle inference GPUs may not shorten the step but cuts cost per verified token. Proposals are scored on both, and the review picks which root matters this quarter.

### Serving tuned for the training loop, not for throughput

**Scenario.** The serving team raised the maximum batch per replica from 32 to 256 for the rollout fleet. Decode throughput per GPU went up 2.9 times. On a replay of one step's episodes, the rollout phase got slower, from 251 seconds to 690, because every step now waited for its longest episodes to decode at a much lower per-sequence speed.

![Serving for the loop](diagrams/designs/concepts/ds36-c5.svg)

![Serving tuned for the training loop, not for throughput](diagrams/designs/ds36-4.svg)

**What the picture shows.** The rollout fleet is tuned for the time to finish a step's episodes, which depends on the slowest sequences, not on the average throughput.

- **Two serving regimes:** product serving maximizes goodput at a latency SLO ([Design 21](#design-llm-serving)). Rollout serving has one consumer, the trainer, and its SLO is the step: all of a step's episodes, including the longest, finished as early as possible.
- **Batch size trades speed per sequence for throughput:** larger batches raise tokens per second per GPU but slow each sequence. With synchronous steps, the longest episodes set the phase time.
- **Joint tuning:** the batch limit, the number of replicas and the scheduling order (longest expected episodes first) are chosen together on a replay of real episode lengths, against the rollout phase time.
- **Change the loop too:** partial rollouts cap the time an episode can hold up a step; unfinished episodes continue in the next step under off-policy corrections ([Design 09](#design-off-policy)). With the cap, larger batches become useful again.
- **Measured:** batch 16 with longest-first scheduling, plus partial rollouts capped at the 95th-percentile length, cut the rollout phase from 318 seconds to 164.

**The code.** Rollout phase time and throughput as the batch limit changes, on the same episode lengths (Python, NumPy).

```python
import numpy as np

rng = np.random.default_rng(0)
lengths = rng.lognormal(np.log(900), 0.8, 1024).astype(int)        # tokens per episode in one step

def rollout_phase(batch: int, replicas: int = 16, l0: float = 0.018, b0: int = 96) -> tuple[float, float]:
    """Seconds per token per sequence grows with batch; the phase ends when the last slot finishes."""
    per_token = l0 * (1 + batch / b0)
    slots = np.zeros(batch * replicas)
    for n in sorted(lengths, reverse=True):                          # longest first
        slots[slots.argmin()] += n * per_token
    return slots.max(), batch / per_token                            # phase seconds, tokens/s per replica #!

for b in (16, 32, 64, 128, 256):
    t, tput = rollout_phase(b)
    print(f"batch {b:3d}: rollout phase {t:5.0f} s, {tput:6.0f} tokens/s per replica")
```

**Going deeper.**

- **The rollout fleet is its own product:** it has a different SLO, different traffic and one customer. Sharing configuration defaults with product serving is how the batch change happened.
- **Async loops change the answer:** with fully asynchronous training, the trainer never waits for a step's slowest episode, and throughput matters again. Which regime a run uses is a joint decision of the RL and serving owners, and the serving configuration follows from it.

### Moving weights from trainer to inference

**Scenario.** After each optimizer step, the trainer saved a checkpoint to object storage, and 16 inference replicas downloaded it and restarted their engines. It took 46 seconds per step, 11% of the step, and nobody owned it: the trainer team thought of it as inference startup, and the serving team thought of it as a checkpoint.

![Weight sync](diagrams/designs/concepts/ds36-c6.svg)

![Moving weights from trainer to inference](diagrams/designs/ds36-5.svg)

**What the picture shows.** Weights move directly from trainer GPUs to inference GPUs, in the layout the inference engine needs, without a storage round trip or a restart.

- **One transfer group:** at startup, the trainer ranks and the inference replicas join a collective group over the GPU network (EFA). The group is part of the run's spec, not something each team configures.
- **Reshard on the way:** trainer shards (FSDP) are gathered per layer and sent in the inference engine's tensor-parallel layout, so the receiving side copies into its existing buffers.
- **In place, between batches:** inference replicas pause new requests, update weights in place and resume. The engine isn't restarted, and its compiled graphs stay valid.
- **Version stamps:** every rollout token carries the policy version that produced it ([Design 09](#design-off-policy)), so an update that lands mid-episode is recorded rather than hidden.
- **Checkpoints stay separate:** durable checkpoints are still written asynchronously on their own schedule ([Design 20](#design-distributed-training)). The sync path is for speed; the checkpoint path is for recovery.
- **Measured:** 46 seconds became 6, for a 70B model in bf16.

**The code.** Estimating both paths for a 70B model (Python).

```python
def via_storage(params_b: float, bytes_per_param: int = 2, write_gbps: float = 10.0, read_gbps: float = 8.0,
                restart_s: float = 15.0) -> float:
    gb = params_b * bytes_per_param
    return gb / write_gbps + gb / read_gbps + restart_s          # save, every replica reads in parallel, restart

def direct(params_b: float, bytes_per_param: int = 2, senders: int = 64, gbps_per_sender: float = 12.5,
           reshard_overhead: float = 0.5, pause_s: float = 3.0) -> float:
    gb = params_b * bytes_per_param
    return gb / (senders * gbps_per_sender) * (1 + reshard_overhead) + pause_s * 2                          #!

print(f"via storage: {via_storage(70):.0f} s   direct: {direct(70):.0f} s")
```

**Going deeper.**

- **Precision must match on both sides:** if inference runs quantized weights, the trainer sends full precision and the inference side quantizes, or the sender quantizes with the same recipe. A mismatch shows up as a jump in the importance ratios ([Design 09](#design-off-policy)), which is the alarm for it.
- **Failure handling is joint:** a replica that misses an update must refuse to serve the old version silently. It rejoins, receives the current weights and only then accepts requests.

### Harnesses and runtimes: making agent rollouts cheap

**Scenario.** Agent episodes averaged 38 model calls each, and each call resent the whole conversation. The inference cache should have reused the shared prefix, but the hit rate was 9%: the harness put a timestamp and the episode ID at the top of the system prompt.

![Harness and runtime](diagrams/designs/concepts/ds36-c7.svg)

![Harnesses and runtimes: making agent rollouts cheap](diagrams/designs/ds36-6.svg)

**What the picture shows.** The harness, the inference engine and the sandbox runtime are designed together around how agent episodes actually run.

- **Prompt layout for the cache:** stable content first (system prompt, tool definitions, task description), then the growing conversation. Per-episode values move to the end of the first user message. The cache-aware router sends an episode's calls to the replica that holds its prefix ([Design 21](#design-llm-serving)).
- **Measured:** prefix-cache hit rate went from 9% to 83% of prompt tokens, and prefill time per call fell by 71%.
- **Overlap tools with decode:** while one episode waits for a tool, its slot decodes another episode. The harness reports "waiting on tool" so the engine can release the slot rather than hold it.
- **Warm sandboxes:** sandboxes come from warm pools sized from the trace's arrival rate ([Design 30](#design-sandbox-worlds)), so start time is off the critical path.
- **Durable episodes:** episodes run as durable workflows ([Design 04](#design-agent-state)), so a preempted rollout replica loses no finished calls.

**The code.** Prefix reuse under two prompt layouts (Python).

```python
def shared_prefix(a: list[str], b: list[str]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n

def hit_rate(prompts: list[list[str]]) -> float:
    """Share of prompt tokens that match an earlier prompt's prefix (what a prefix cache can reuse)."""
    reused = total = 0
    for i, p in enumerate(prompts):
        best = max((shared_prefix(p, q) for q in prompts[:i]), default=0)
        reused += best; total += len(p)
    return reused / total                                                                     #!

SYSTEM = ["sys"] * 1200; TOOLS = ["tool"] * 2400; TASK = ["task"] * 300
def prompt(layout: str, episode: int, turn: int) -> list[str]:
    convo = [f"e{episode}t{t}" for t in range(turn * 150)]
    meta = [f"ts-{episode}-{turn}", f"id-{episode}"]
    if layout == "meta-first":
        return meta + SYSTEM + TOOLS + TASK + convo
    return SYSTEM + TOOLS + TASK + meta[1:] + convo

for layout in ("meta-first", "stable-first"):                      # toy: 4 episodes x 5 turns
    ps = [prompt(layout, e, t) for e in range(4) for t in range(1, 6)]
    print(f"{layout:12s} prefix reuse {hit_rate(ps):.0%}")
```

**Going deeper.**

- **Context management changes the cache:** a harness that summarizes or drops old turns rewrites the prefix and loses the cache. Compaction is scheduled at points where the cost of losing the cache is known, and its effect on hit rate is a tracked number.
- **The harness is part of the model's environment:** prompt layout changes can change behavior. Layout changes go through the same evaluation gate as any training change ([Design 33](#design-continuous-evals)).

### Research environments: stragglers and resets

**Scenario.** A new set of software-engineering environments made RL steps 30% slower. The environments weren't slow on average; 2% of episodes ran a full test suite that took 9 minutes, and every synchronous step waited for them.

![Environments](diagrams/designs/concepts/ds36-c8.svg)

![Research environments: stragglers and resets](diagrams/designs/ds36-7.svg)

**What the picture shows.** Environments get the same tail-latency treatment as services, because in a synchronous loop the slowest episode sets the step time.

- **Measure each environment's tail:** episode duration percentiles per environment and task come from the traces. A new environment's p99 is a review item before it joins a large run.
- **Time budgets per episode:** every environment declares a time budget. Episodes that exceed it are cut and continued later, or scored as timeouts, a choice the environment's owner makes with the RL owner.
- **Cheaper resets:** environments start from snapshots (file system, database, service state) instead of running setup scripts. Reset time for the software tasks fell from 41 seconds to 3.
- **Faster checks:** test-based verifiers run the tests affected by the change first and the full suite only for the final reward ([Design 10](#design-verification)).
- **Mix-aware batching:** long and short environments are spread evenly across steps, so a step isn't made only of slow tasks.

**The code.** How the slowest of many episodes sets the step, and what a time budget does (Python, NumPy).

```python
import numpy as np

rng = np.random.default_rng(2)
def episode_seconds(n: int) -> np.ndarray:
    base = rng.lognormal(np.log(60), 0.5, n)                 # most episodes: about a minute
    slow = rng.random(n) < 0.02                              # 2% run the full suite
    return np.where(slow, base + 540, base)

steps = np.array([episode_seconds(1024) for _ in range(200)])
mean_ep = steps.mean()
sync_step = np.median(steps.max(axis=1))                     # synchronous step waits for the slowest
budget = np.percentile(steps, 95)
capped = np.median(np.minimum(steps, budget).max(axis=1))
print(f"mean episode {mean_ep:.0f} s, step waits {sync_step:.0f} s; "
      f"with a {budget:.0f} s budget the step waits {capped:.0f} s")                       #!
```

**Going deeper.**

- **Cutting episodes biases the data:** episodes that are cut are the long, hard ones. The share cut per task is tracked, and cut episodes are continued, not dropped, so hard tasks still contribute.
- **Determinism helps everyone:** environments that replay exactly from a seed let the serving team benchmark on real episodes and let debugging reproduce a slow step ([Design 35](#design-ai-for-ml-rnd)).

### Pre-training choices that serving and RL pay for

**Scenario.** A new base model used full multi-head attention with 64 KV heads. It trained well. In RL, each inference GPU could hold fewer than one 32K-token episode's KV cache, so rollouts ran at a fraction of the concurrency of the previous model, and the RL budget for the quarter was spent in half the planned time.

![Model shape](diagrams/designs/concepts/ds36-c9.svg)

![Pre-training choices that serving and RL pay for](diagrams/designs/ds36-8.svg)

**What the picture shows.** Decisions made at pre-training time are reviewed with their cost in post-training and serving, before the run starts.

- **Shape review:** attention layout (KV heads, sliding windows), context length, expert count and size, vocabulary and precision are reviewed by the pre-training, RL and serving owners together.
- **Serving cost model:** for each candidate shape, KV cache bytes per token, achievable concurrency at the target context, decode throughput and cost per million tokens, computed from benchmarks of a small version of the shape.
- **RL cost model:** rollout tokens per GPU-hour at the expected episode lengths, which depends on the same KV cache size and on decode speed.
- **The decision:** the quality difference from the pre-training ablation is weighed against the serving and RL cost difference, over the model's expected lifetime of tokens.
- **The result for the next model:** 8 KV heads (grouped-query attention) instead of 64, at a measured quality cost within noise on the ablation suite, and 8 times more concurrent episodes per GPU.

**The code.** KV cache per token and concurrency at 32K context for two attention layouts (Python).

```python
def kv_bytes_per_token(layers: int, kv_heads: int, head_dim: int, bytes_per: int = 2) -> int:
    return 2 * layers * kv_heads * head_dim * bytes_per          # keys and values

def concurrency(free_gb: float, context: int, per_token: int) -> float:
    return free_gb * 1e9 / (context * per_token)                                                #!

for name, kv_heads in [("full MHA, 64 KV heads", 64), ("GQA, 8 KV heads", 8)]:
    b = kv_bytes_per_token(layers=80, kv_heads=kv_heads, head_dim=128)
    print(f"{name:22s} {b / 1024:6.0f} KiB/token, {concurrency(40, 32_768, b):4.1f} episodes of 32K per GPU")
```

**Going deeper.**

- **Lifetime tokens decide:** a model that will serve billions of tokens and generate billions more in RL rollouts can afford a small quality loss for a large serving saving. A short-lived research model may not need the trade.
- **The review is a meeting with numbers:** the cost model's inputs come from benchmarks, not from opinions, and the outcome is written down with its expected end-to-end effect, so it can be checked later.

### Closing the loop: owners, end-to-end tests and guards

**Scenario.** The serving batch change was rolled out on Friday as a routine tuning change. Its own benchmark passed. Nothing checked the RL step time, and the regression was found eleven days later in a weekly report.

![Closing the loop](diagrams/designs/concepts/ds36-c10.svg)

![Closing the loop: owners, end-to-end tests and guards](diagrams/designs/ds36-9.svg)

**What the picture shows.** Each loop across layers has an owner, an end-to-end test and a guard that runs on changes to any layer it crosses.

- **Loop registry:** each loop (RL step, agent episode, pre-training step, product request) lists the layers it crosses, its end-to-end metric, its owner and its replay benchmark.
- **End-to-end replay benchmarks:** a small, fixed replay of each loop (for RL, 50 steps on 64 GPUs with recorded episodes and environments) runs in about 40 minutes.
- **Guards:** a change to any layer in a loop's list runs that loop's replay before rollout. It is blocked if the end-to-end metric gets worse beyond noise, even if the layer's own benchmark improves.
- **Attribution after the fact:** weekly, the change in each root is split across the changes that shipped, using the replay results and the traces, so every team sees its effect on the end-to-end numbers.
- **Owners with authority:** a loop owner can block a change in any layer of their loop and is accountable for the root metric. They don't own the layers; they own the seams.

**The code.** The end-to-end guard on a layer change (Python).

```python
from statistics import mean, stdev
from math import sqrt

def guard(layer_before: list[float], layer_after: list[float], e2e_before: list[float], e2e_after: list[float],
          higher_is_better_e2e: bool = True) -> str:
    """Block a change whose end-to-end metric gets worse beyond noise, whatever its layer metric does."""
    d = mean(e2e_after) - mean(e2e_before)
    se = sqrt(stdev(e2e_before) ** 2 / len(e2e_before) + stdev(e2e_after) ** 2 / len(e2e_after))
    worse = d < -2 * se if higher_is_better_e2e else d > 2 * se
    layer_gain = mean(layer_after) / mean(layer_before) - 1
    if worse:
        return f"block: layer {layer_gain:+.0%}, end-to-end {d:+.0f} (2 SE = {2 * se:.0f})"   #!
    return f"pass: layer {layer_gain:+.0%}, end-to-end {d:+.0f}"

decode_tput = ([1333, 1340, 1329], [3879, 3866, 3891])           # tokens/s per replica, batch 32 -> 256
rl_tokens_per_gpu_h = ([6712, 6698, 6731], [3105, 3150, 3088])   # verified tokens per GPU-hour, replay
print(guard(*decode_tput, *rl_tokens_per_gpu_h))
```

**Going deeper.**

- **Replays must stay representative:** recorded episodes age as tasks and models change. Each loop's replay set is refreshed monthly from fresh traces, and its results are checked against the live run's numbers.
- **Guards cost time, so keep them small:** a 40-minute replay on 64 GPUs per change is affordable; a full run per change isn't. Guards check for regressions; real gains are confirmed in the next live run.

### The cross-layer review and how work gets done

**Scenario.** The direct weight sync had been proposed twice before. Each time, the trainer team and the serving team agreed it was a good idea and each put it on the other's roadmap.

![Organization](diagrams/designs/concepts/ds36-c11.svg)

![The cross-layer review and how work gets done](diagrams/designs/ds36-10.svg)

**What the picture shows.** A small process that turns the ranked list into shipped cross-layer changes.

- **Monthly review:** the loop owners and one engineer from each layer look at fresh critical paths, the ranked list and last month's attribution. The output is the top three cross-layer projects and who leads each.
- **One lead per project:** the lead writes one joint design covering every layer that changes, with one end-to-end acceptance test. Layer teams commit people for the project's duration.
- **Interfaces as contracts:** the seams found this way become versioned interfaces: the weight-sync protocol, the trace schema, the rollout SLO, the environment time budget and the model-shape review checklist.
- **Results in end-to-end terms:** each project reports its expected and measured effect on the root metric. Misses are reviewed, because they show where the cost model is wrong.
- **The quarter's outcome:** RL step time went from 412 seconds to 218 (rollout phase 318 to 164, weight sync 46 to 6), and verified training tokens per GPU-hour rose by 89%, with the evaluation trajectory per GPU-hour unchanged or better on every family.

**Going deeper.**

- **Keep the group small:** the review is five to eight people. A larger forum becomes a status meeting, and the ranked list stops deciding anything.
- **Rotate engineers through seams:** engineers who have worked on both sides of a seam find the next opportunity there. A rotation of one quarter on another layer's team is part of the plan.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds36-c12.svg)

- **Phase 1, measure (weeks 1–6):** metric trees for the four workload families, trace propagation through trainer, inference, harness, sandbox, environment and verifier, critical-path reports, the first ranked list.
- **Phase 2, the RL loop (weeks 5–14):** rollout serving tuned for step time, partial rollouts, direct weight sync, cache-friendly harness prompts, environment time budgets and snapshot resets.
- **Phase 3, guards and owners (weeks 12–18):** loop registry, end-to-end replay benchmarks, guards in every layer's rollout pipeline, weekly attribution.
- **Phase 4, upstream (weeks 16–24):** the model-shape review for the next pre-training run, serving and RL cost models, the monthly review as the planning input for every layer team.

**Trade-offs we accepted.**

- **End-to-end over local metrics:** some local improvements are blocked because they don't help a loop. Teams lose some autonomy over their own roadmaps.
- **Tracing has a cost:** propagating context and keeping tail spans costs about 1% of rollout throughput and a storage bill. Without traces, the ranked list would be opinion.
- **Coupling between layers:** direct weight sync and cache-aware prompt layouts tie layers together. The coupling is made explicit as versioned contracts, but it makes some changes slower.
- **Partial rollouts add off-policy data:** cutting the slow tail shortens steps but trains on data from older policies. The off-policy controls of [Design 09](#design-off-policy) bound it.

**Open questions.**

- **Automatic opportunity finding:** could the agents of [Design 35](#design-ai-for-ml-rnd) read critical paths and propose the ranked list, with the review checking their arithmetic?
- **Joint search over configurations:** serving batch, replica count, partial-rollout cap and staleness limit interact. Can they be tuned together by search on the replay benchmark rather than one at a time?
- **Quality and speed in one root:** verified tokens per GPU-hour treats all tokens alike. Is there a measurable root closer to "model improvement per dollar" that is still stable enough to steer by?
- **Hardware in the loop:** when the next accelerator generation changes the cost of memory against compute, which of today's cross-layer decisions (attention layout, batch sizes, weight-sync paths) should be revisited first?
