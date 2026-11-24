---
id: design-efficiency-programs
title: "Design 38: Efficiency programs: per-config baselines, stranding, unused capacity and job-level utilization across training, inference and eval"
short: "Efficiency programs and per-config baselines"
---
This design answers one question: *how do we run the fleet's efficiency programs, stranding and rightsizing, unused capacity recovery and job-level utilization across training, inference and eval, so that every gap has a number, a baseline it is measured against and a team that owns closing it?* Earlier designs built the pieces. [Design 19](#design-scheduling-impact) defined active utilization and proved the scheduling layer raised it. [Design 24](#design-gpu-observability) collects the signals. [Design 25](#design-cost-capacity) turns GPU-hours into dollars, and [Design 26](#design-cost-programs) books savings against a counterfactual. Design 19 stops at a line it draws on purpose: the scheduler can make GPUs busy, but it can't make a slow data loader faster. This design is about everything below that line, and about the teams who own it.

The short version: *give every workload configuration a baseline, measure each job against its own config, and hand each gap to the team that owns the system causing it*. A config is a fingerprint of what determines efficiency: workload family, model, parallelism, sequence length, hardware and software versions. Each config has a measured "achievable" level, taken from its own best sustained runs. A paid GPU-hour is followed down a waterfall from paid to healthy, allocated, active and productive, and every loss on the way has a named cause and an owning team. Training jobs are measured by goodput and MFU, inference by tokens per GPU-second at the SLO, and eval by GPU-seconds per scored item. Stranded GPUs are classified by why nothing can use them. Held-but-unused capacity is found, reclaimed and lent to a scavenger queue. Gaps go into one register, ranked by dollars per engineer-week, and are worked with the system-owning teams in a monthly review. Regression guards keep closed gaps closed.

### The use case and requirements

**Scenario.** The fleet has grown to 16,000 H100-class GPUs. At a blended $2.40 per GPU-hour it costs about $28 million a month, so one point of utilization is worth about $280,000 a month. The scheduling program ([Design 19](#design-scheduling-impact)) lifted active utilization to 71%, and the scorecards say the fleet is in good shape. Under that number, the same 70B training config reaches 52% MFU in its best runs and averages 38% across all runs. Serving deployments produce 41% of the tokens per GPU-second their own benchmarks reach at the same latency. Eval jobs keep their GPUs busy for 23% of the time they hold them. About 6% of healthy GPUs sit free but can't be used by anything waiting. Nobody owns these numbers, because each one belongs to a different team's system.

![Efficiency below active utilization](diagrams/designs/concepts/ds38-c1.svg)

**What the programs must deliver.**

- **One waterfall:** every paid GPU-hour followed down to productive work, with each loss sized in GPU-hours and dollars and given a cause.
- **Per-config baselines:** a measured achievable level for every workload configuration that runs at scale, refreshed on a schedule.
- **Job-level utilization for all three families:** training, inference and eval each measured in their own unit of useful work, against their config's baseline.
- **Stranding by cause:** free GPUs that no pending work can use, split by the reason, so each reason goes to the team that can fix it.
- **Unused capacity recovery:** capacity that is held and not used, found within hours, returned to its owner or lent to a scavenger queue.
- **Rightsizing by config:** workloads moved to the config that does their work at the lowest cost, chosen from measured baselines.
- **Gaps owned by system teams:** one register, one ranking, one review cadence, and a contract with each owning team about who measures and who fixes.
- **Gains that hold:** regression guards in every release pipeline that can move a config's efficiency.

**Out of scope:** scheduling policy itself ([Design 17](#design-gpu-scheduling)), the observability pipeline ([Design 24](#design-gpu-observability)), cost attribution and the savings ledger ([Design 25](#design-cost-capacity), [Design 26](#design-cost-programs)), and the cross-layer work of [Design 36](#design-cross-stack). This design uses all of them and adds the measurement and the operating model that sit between them and the teams who own the systems.

### Where a paid GPU-hour goes

**Scenario.** The platform team reported 71% active utilization. The training framework team reported 48% MFU on their benchmark. Finance asked how much of the $28 million did useful work. Neither number answered that, and multiplying them gave a figure nobody could defend, because they used different denominators.

![The loss waterfall](diagrams/designs/concepts/ds38-c2.svg)

![Where a paid GPU-hour goes](diagrams/designs/ds38-1.svg)

**What the picture shows.** A waterfall from paid GPU-hours to productive GPU-hours, where each step down is a named loss with an owner.

- **Paid:** every GPU-hour on an invoice or amortized from owned hardware ([Design 25](#design-cost-capacity)). This is the denominator for every other number.
- **Healthy:** minus GPUs down, in repair or failing health checks. Owner: fleet health ([Design 23](#design-health-remediation)).
- **Allocated:** minus free GPUs. Free GPUs split into *stranded* (work is waiting but can't use them) and *idle* (nothing is waiting). Stranded goes to scheduling and to workload owners; idle goes to capacity planning.
- **Active:** minus GPU-hours that were held but not active. Owner: the unused capacity program and the workload's team.
- **Productive:** minus the gap between what the job achieved and what its config achieves at its best. This gap is converted to GPU-hours: a job that ran at 38% MFU on a config whose achievable level is 52% wasted 27% of its active GPU-hours. Owner: the team that owns the system causing the gap, such as the training framework, the data loader, the serving engine or the eval harness.
- **One denominator, additive losses:** every bar is in GPU-hours of the same paid total, so the losses add up to the difference between paid and productive, and each can be priced at the pool's rate.
- **What the scenario's fleet looked like:** 11.7 million paid GPU-hours a month, 4.8 million productive. The largest single loss was not idle capacity. It was the gap between configs' average and achievable efficiency.

**The code.** The monthly waterfall from the hourly GPU fact table and a per-job efficiency table (warehouse SQL).

```sql
-- fact_job_efficiency: one row per workload per hour, written by the job-level pipelines in this design
--   workload_uid, hour_ts, config_id, active_gpu_h, efficiency, achievable   (efficiency in the family's unit)
WITH m AS (SELECT DATE '2026-10-01' AS s, DATE '2026-11-01' AS e),
g AS (
  SELECT SUM(1.0)                                         AS paid,
         SUM(healthy_frac)                                AS healthy,
         SUM(LEAST(allocated_frac, healthy_frac))         AS allocated,
         SUM(LEAST(active_frac, allocated_frac))          AS active
  FROM fact_gpu_hour, m WHERE hour_ts >= s AND hour_ts < e),
st AS (
  SELECT SUM(stranded_gpu_h) AS stranded FROM fact_stranding_hour, m WHERE hour_ts >= s AND hour_ts < e),
p AS (
  SELECT SUM(active_gpu_h * LEAST(efficiency / NULLIF(achievable, 0), 1.0)) AS productive        -- #!
  FROM fact_job_efficiency, m WHERE hour_ts >= s AND hour_ts < e)
SELECT paid,
       paid - healthy                    AS lost_unhealthy,
       stranded                          AS lost_stranded,
       healthy - allocated - stranded    AS lost_idle_no_demand,
       allocated - active                AS lost_held_not_active,
       active - productive               AS lost_below_config_baseline,
       productive
FROM g, st, p;
```

**Going deeper.**

- **Productive is relative to the config, not to the hardware:** a job at its config's achievable level counts as fully productive even at 52% MFU. The distance from the config's achievable level to the hardware peak is a separate question, about choosing better configs and improving the systems, and it is tracked per config in the next section.
- **Jobs without a config baseline are reported, not guessed:** their active hours count as productive, and their share of active hours is printed under the waterfall. A large share means the baseline coverage is the first thing to fix.

### Configs and per-config baselines

**Scenario.** The serving team was told their 8B deployment was "inefficient" because it ran at 30% SM activity. The same deployment on its benchmark reached 31%. It was doing exactly what that config can do. Meanwhile a 70B training job at 61% SM activity, which looked healthy, was 20 points of MFU below runs of the same config a week earlier.

![Per-config baselines](diagrams/designs/concepts/ds38-c3.svg)

![Configs and per-config baselines](diagrams/designs/ds38-2.svg)

**What the picture shows.** A job is compared only with runs of its own config, and each config has a measured achievable level.

- **The config fingerprint:** workload family (pre-training, post-training, SFT, serving, eval), model architecture and size, parallelism (tensor, pipeline, data, expert, micro-batch), sequence length, GPU type and interconnect, and the versions that move efficiency: framework or engine, CUDA, NCCL and driver. The fingerprint is a hash of these fields. Serving reuses the fingerprints of [Design 22](#design-serving-benchmarks).
- **A unit of useful work per family:** training is measured by MFU and goodput, serving by output tokens per GPU-second at the SLO, eval by GPU-seconds per scored item. Generic GPU activity is kept as context, never as the target.
- **Achievable is the config's own best sustained level:** the 90th percentile of daily efficiency over the config's healthy runs in the last 28 days, with at least five runs. One lucky run can't set it, and a config that never ran well still gets a baseline from its own history.
- **A reference above it:** where a benchmark exists ([Design 15](#design-eval-infra) for training, [Design 22](#design-serving-benchmarks) for serving), it is stored next to the achievable level. A config whose achievable level sits far below its benchmark has a system gap even if every job matches its own history.
- **Provisional baselines:** a config with fewer than five runs borrows the achievable level of its nearest neighbour (same family and model, one field different) and is marked provisional. Gaps measured against a provisional baseline are shown, but they are not assigned to a team.
- **Version changes make a new config:** a new NCCL or engine version produces a new fingerprint. Its first runs are compared with the previous version's baseline, which is how the regression guards later in this design work.

**The code.** Computing a config fingerprint and its achievable baseline from daily job records (Python).

```python
import hashlib, json, statistics

FIELDS = ["family", "model", "params_b", "tp", "pp", "dp", "ep", "micro_batch", "seq_len",
          "gpu", "interconnect", "framework", "cuda", "nccl", "driver"]

def config_id(job: dict) -> str:
    key = json.dumps({f: job.get(f) for f in FIELDS}, sort_keys=True)
    return "cfg-" + hashlib.sha256(key.encode()).hexdigest()[:12]

def achievable(daily: list[dict], min_runs: int = 5) -> dict:
    """daily: one row per run per day with 'run' and 'efficiency'; only healthy runs (no node faults that day)."""
    runs = {d["run"] for d in daily}
    if len(runs) < min_runs:
        return {"status": "provisional", "runs": len(runs)}
    vals = sorted(d["efficiency"] for d in daily)
    p90 = vals[min(len(vals) - 1, int(0.9 * len(vals)))]                     # best sustained, not best ever #!
    return {"status": "measured", "runs": len(runs), "achievable": round(p90, 3),
            "median": round(statistics.median(vals), 3)}

job = {"family": "pretrain", "model": "dense-70b", "params_b": 70, "tp": 8, "pp": 4, "dp": 16, "ep": 1,
       "micro_batch": 1, "seq_len": 8192, "gpu": "H100-SXM", "interconnect": "EFA-3200",
       "framework": "megatron-core-0.14", "cuda": "12.9", "nccl": "2.27", "driver": "575"}
daily = [{"run": f"r{i % 7}", "efficiency": e} for i, e in
         enumerate([0.52, 0.31, 0.49, 0.38, 0.51, 0.36, 0.29, 0.50, 0.41, 0.33, 0.53, 0.37, 0.30, 0.47])]
print(config_id(job), achievable(daily))
```

**Going deeper.**

- **Why the config's own best and not the hardware peak:** a team can act on "your runs reach 52% half the time and 31% the other half". It can't act on "you are far from 989 TFLOPS". Both numbers are kept; only the first is assigned as a gap.
- **Fewer, larger configs:** hundreds of one-off configs dilute every baseline. The platform publishes recommended configs per model size and family, and the share of GPU-hours running on recommended configs is itself a tracked number.

### Job-level utilization in training: goodput and MFU

**Scenario.** A 512-GPU pre-training run took 19 days instead of the planned 13. Its MFU while stepping was close to the config's achievable level. The extra six days were spent elsewhere: 40 minutes to start after every restart, a stall of four minutes at each hourly checkpoint, 23 restarts that each threw away up to an hour of steps, and a data loader that stalled the whole job whenever one shard was slow.

![Goodput](diagrams/designs/concepts/ds38-c4.svg)

![Job-level utilization in training: goodput and MFU](diagrams/designs/ds38-3.svg)

**What the picture shows.** A training job's GPU-hours split into productive steps and the named ways time is lost, from events the trainer emits.

- **Step events, not sampling:** a small library owned by the training framework team emits an event at job start, first step, every step (with tokens and step time), each checkpoint start and end, each data wait above a threshold, and each restart with the step it resumed from. The events land in the warehouse next to `fact_gpu_hour`.
- **Goodput:** the share of the job's allocated GPU-hours spent on steps whose results were kept. Steps redone after a restart are not goodput.
- **The losses, each with its owner:** startup from admission to first step (platform: image caching, weight and dataset loading, [Design 20](#design-distributed-training)), checkpoint stalls (checkpoint library), lost work since the last checkpoint and restart time (reliability, [Design 14](#design-reliable-rl-infra) and [Design 23](#design-health-remediation)), data stalls (data loader owners), and stragglers (fleet health and the network team).
- **MFU inside the steps:** model FLOPs per token, times tokens per second, over the GPUs' peak. For a dense transformer the FLOPs per token are about 6N plus the attention term 12·L·H·Q·T, as defined in the PaLM paper's appendix ([Chowdhery et al., 2022](https://arxiv.org/abs/2204.02311)). Peak is the dense BF16 figure of the GPU, 989 TFLOPS for H100 SXM ([NVIDIA H100](https://www.nvidia.com/en-us/data-center/h100/)). MFU compared with the config's achievable MFU gives the step-efficiency gap.
- **Two gaps, two kinds of fix:** goodput losses are fixed by the platform and reliability teams; step-efficiency gaps are fixed by the training framework and the model owners. The job report shows both, so a job isn't sent to the wrong team.
- **What the 19-day run would have shown:** goodput of 64%, MFU while stepping 49% against an achievable 52%. The six lost days were almost all goodput, and most of that was startup and lost work.

**The code.** Goodput and its losses from a job's event stream, plus MFU (Python).

```python
def mfu(tokens_per_s: float, params: float, layers: int, heads: int, head_dim: int, seq: int,
        gpus: int, peak_flops: float = 989e12) -> float:
    flops_per_token = 6 * params + 12 * layers * heads * head_dim * seq          # PaLM appendix B #!
    return tokens_per_s * flops_per_token / (gpus * peak_flops)

def goodput(events: list[dict], gpus: int, end: float) -> dict:
    """events: dicts with t (s), kind in start|first_step|step|ckpt_start|ckpt_end|data_wait|restart,
    and step / dur / resume_step where relevant. Returns GPU-hours by bucket."""
    b = {"productive": 0.0, "startup": 0.0, "checkpoint": 0.0, "data_wait": 0.0, "lost_work": 0.0}
    steps, start, ck = {}, None, None
    for e in events:
        k = e["kind"]
        if k in ("start", "restart"):
            start = e["t"]
            if k == "restart":
                for s in [s for s in steps if s > e["resume_step"]]:              # steps thrown away #!
                    b["lost_work"] += steps.pop(s)
        elif k == "first_step":
            b["startup"] += e["t"] - start
        elif k == "step":
            steps[e["step"]] = e["dur"]
        elif k == "ckpt_start":
            ck = e["t"]
        elif k == "ckpt_end":
            b["checkpoint"] += e["t"] - ck
        elif k == "data_wait":
            b["data_wait"] += e["dur"]
    b["productive"] = sum(steps.values())
    total = end - events[0]["t"]
    b["other"] = total - sum(b.values())
    return {k: round(v * gpus / 3600, 1) for k, v in b.items()} | {"goodput": round(b["productive"] / total, 3)}

ev = [{"t": 0, "kind": "start"}, {"t": 2400, "kind": "first_step"}]
t = 2400
for s in range(1, 301):
    ev.append({"t": t, "kind": "step", "step": s, "dur": 12.0}); t += 12.0
    if s % 100 == 0:
        ev += [{"t": t, "kind": "ckpt_start"}, {"t": t + 240, "kind": "ckpt_end"}]; t += 240
ev += [{"t": t + 60, "kind": "restart", "resume_step": 200}, {"t": t + 2460, "kind": "first_step"}]
print(goodput(ev, gpus=512, end=t + 2460))
print(f"MFU {mfu(5.15e5, 70e9, 80, 64, 128, 8192, gpus=512):.1%}")
```

**Going deeper.**

- **The library is owned by the framework team, not the platform:** step events have to come from inside the training loop. The platform provides the schema and the pipeline; the framework team ships the library in every supported framework image, so a job gets goodput reporting without any change by its owner.
- **MFU and HFU differ on purpose:** hardware FLOPs utilization counts recomputation from activation checkpointing; MFU doesn't. A config that recomputes more shows higher HFU for the same useful work, so baselines use MFU.
- **Post-training is the same with a different unit:** RL jobs ([Design 07](#design-scaling-rl)) spend much of their time in rollouts. Their goodput uses the step events of the trainer and the rollout engine together, and the end-to-end unit of [Design 36](#design-cross-stack), verified training tokens per GPU-hour, is the efficiency number.

### Job-level utilization in inference: tokens per GPU-second at the SLO

**Scenario.** The chat-70b deployment served 41% of the tokens per GPU-second that its benchmark reached at the same p95 TTFT. The serving team blamed traffic: "it's quiet at night". Autoscaling ([Design 26](#design-cost-programs)) had already removed the night-time excess. Most of the remaining gap was at peak traffic, where the deployment ran batches half the size the benchmark ran, because a client sent requests with a 32k-token context that the KV cache couldn't hold alongside others.

![Inference efficiency](diagrams/designs/concepts/ds38-c5.svg)

![Job-level utilization in inference: tokens per GPU-second at the SLO](diagrams/designs/ds38-4.svg)

**What the picture shows.** Production efficiency compared with the config's benchmark curve at the same load, with the gap split into a load part and a config part.

- **The unit:** output tokens per GPU-second, counting only requests that met the deployment's latency SLO. Tokens that arrived too late aren't useful work.
- **The benchmark curve:** for each fingerprint, [Design 22](#design-serving-benchmarks) measures tokens per GPU-second at each concurrency up to the SLO limit. Production is compared with the curve at the concurrency it actually ran, not with the curve's maximum.
- **Load gap:** the difference between the curve at the observed concurrency and the curve's maximum within the SLO. It is caused by running more replicas than the traffic needs, and it belongs to autoscaling and capacity ([Design 26](#design-cost-programs)).
- **Config gap:** the difference between production and the curve at the same concurrency. It is caused by the system: KV cache too small for the real request mix, prefix cache misses, the wrong tensor parallelism for the traffic, or routing that ignores cache locality. It belongs to the serving team.
- **Signals from the engine:** output tokens (`vllm:generation_tokens_total`), running requests (`vllm:num_requests_running`), KV cache use (`vllm:kv_cache_usage_perc`, 0 to 1) and prefix cache hits and queries ([vLLM metrics](https://docs.vllm.ai/en/latest/design/metrics.html)), each labelled with the config ID by the deployment template.
- **What chat-70b showed:** a load gap of 15 points and a config gap of 43 points. Capping context length per client tier and moving long-context requests to a separate pool closed most of the config gap.

**The YAML.** Recording rules that publish tokens per GPU-second, concurrency and KV cache use per config, built on the GPU-level rules of [Design 24](#design-gpu-observability) (Prometheus Operator).

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata: { name: serving-efficiency, namespace: monitoring }
spec:
  groups:
  - name: serving-efficiency
    interval: 1m
    rules:
    - record: pod:config:info                          # the config ID from the pod label set by the template
      expr: |
        max by (cluster, namespace, pod, config_id) (
          label_replace(kube_pod_labels{label_efficiency_example_com_config_id!=""},
                        "config_id", "$1", "label_efficiency_example_com_config_id", "(.*)"))
    - record: config:gpu_allocated:sum
      expr: sum by (cluster, config_id) (gpu:allocated:bool * on (cluster, namespace, pod) group_left (config_id) pod:config:info)
    - record: config:serving_output_tokens:rate5m
      expr: |
        sum by (cluster, config_id) (
          rate(vllm:generation_tokens_total[5m]) * on (cluster, namespace, pod) group_left (config_id) pod:config:info)
    - record: config:serving_output_tokens_per_gpu_second:rate5m
      expr: config:serving_output_tokens:rate5m / config:gpu_allocated:sum                 #!
    - record: config:serving_running_requests_per_replica:avg5m
      expr: |
        avg by (cluster, config_id) (
          avg_over_time(vllm:num_requests_running[5m]) * on (cluster, namespace, pod) group_left (config_id) pod:config:info)
    - record: config:serving_kv_cache_usage:avg5m
      expr: |
        avg by (cluster, config_id) (
          avg_over_time(vllm:kv_cache_usage_perc[5m]) * on (cluster, namespace, pod) group_left (config_id) pod:config:info)
```

**The code.** Splitting a deployment's gap into its load part and its config part using the benchmark curve (Python).

```python
import bisect

def curve_at(curve: list[tuple[float, float]], conc: float) -> float:
    """curve: (concurrency per replica, tokens per GPU-second) points within the SLO, sorted by concurrency."""
    xs = [c for c, _ in curve]
    i = bisect.bisect_left(xs, conc)
    if i == 0:
        return curve[0][1]
    if i == len(curve):
        return curve[-1][1]
    (x0, y0), (x1, y1) = curve[i - 1], curve[i]
    return y0 + (y1 - y0) * (conc - x0) / (x1 - x0)

def split_gap(curve, observed_conc: float, observed_tps: float) -> dict:
    best = max(y for _, y in curve)
    at_load = curve_at(curve, observed_conc)
    return {"efficiency": round(observed_tps / best, 2),
            "load_gap": round((best - at_load) / best, 2),                    # autoscaling and capacity
            "config_gap": round((at_load - observed_tps) / best, 2)}          # the serving system #!

bench = [(1, 95.0), (4, 310.0), (8, 520.0), (16, 780.0), (24, 905.0), (32, 960.0)]
print(split_gap(bench, observed_conc=18.0, observed_tps=394.0))
```

**Going deeper.**

- **The rules count every token; the daily number counts only tokens within the SLO:** Prometheus can't tell which request each token belonged to. The live series shows all output tokens, and the daily efficiency in `fact_job_efficiency` is computed from the gateway's request logs, keeping only requests that met the SLO.
- **The config label must be exported:** kube-state-metrics adds pod labels to `kube_pod_labels` only for keys on its `--metric-labels-allowlist`, so the config ID key is added there once for all namespaces.
- **SM activity is the wrong target for serving:** a decode-heavy deployment is memory-bound and shows low SM activity at its best. Tokens per GPU-second at the SLO is the only number that compares configs fairly.
- **The request mix is part of the config:** a benchmark replayed with last quarter's traffic shape can make a deployment look worse than it is. Traffic shapes are re-captured monthly ([Design 22](#design-serving-benchmarks)), and a large shift in the mix triggers a new benchmark before gaps are assigned.

### Job-level utilization in eval: GPU-seconds per scored item

**Scenario.** Every checkpoint of the main training run was evaluated on 31 suites. Each suite was its own job: it loaded the 70B checkpoint onto eight GPUs in about 11 minutes, generated answers in small batches, waited on the sandbox service and the judge model, and then held all eight GPUs while the last shard finished. Eval used 9% of the fleet, and its GPUs were busy for 23% of the time they were held.

![Eval efficiency](diagrams/designs/concepts/ds38-c6.svg)

![Job-level utilization in eval: GPU-seconds per scored item](diagrams/designs/ds38-5.svg)

**What the picture shows.** An eval job's GPU time split into load, generation, external waits and tail, and the change that removes most of it.

- **The unit:** GPU-seconds per scored item, per suite config (suite, model size, generation settings, hardware). A suite with long agentic tasks has a higher number than a multiple-choice suite; each is compared only with itself.
- **Load:** time from admission until the model can serve. Paid once per suite per checkpoint when every suite is its own job.
- **Generation:** time spent producing answers. Small batches make it slow: a suite of 500 items can't fill a batch the way serving traffic does.
- **External waits:** GPUs held while the harness waits on sandboxes, tools or a judge model ([Design 15](#design-eval-infra)).
- **Tail:** GPUs held while the slowest shard finishes, after the others are done.
- **The change: evals as clients of a shared endpoint.** For each checkpoint, one inference deployment loads the weights once and serves every suite as a client. Suites send requests concurrently, so batches fill; waits on sandboxes and judges no longer hold GPUs, because the harness runs on CPU nodes; and the endpoint scales down when the queue drains. The eval harness team owns the client side, the serving team owns the endpoint.
- **What the scenario's eval showed:** the projection below promised about 10× for the main run's 31 suites. Measured, GPU-seconds per item fell by between 3× and 7× by suite, because real batches filled less than the projection assumed. Eval's share of the fleet dropped from 9% to 3% for the same number of checkpoints evaluated.

**The code.** Breaking down an eval job's held GPU time and projecting the shared-endpoint version (Python).

```python
def eval_breakdown(job: dict) -> dict:
    """job: gpus, load_s, gen_s, wait_s, tail_s, items. Times are wall-clock with all GPUs held."""
    held = job["load_s"] + job["gen_s"] + job["wait_s"] + job["tail_s"]
    return {"gpu_s_per_item": round(held * job["gpus"] / job["items"], 1),
            "busy_share": round(job["gen_s"] / held, 2),
            **{k: round(job[k] / held, 2) for k in ("load_s", "gen_s", "wait_s", "tail_s")}}

def shared_endpoint(suites: list[dict], batch_gain: float = 2.5) -> dict:
    """One load per checkpoint, generation sped up by fuller batches, no GPU held for waits or tails."""
    before = sum((s["load_s"] + s["gen_s"] + s["wait_s"] + s["tail_s"]) * s["gpus"] for s in suites)
    after = max(s["load_s"] for s in suites) * suites[0]["gpus"] \
        + sum(s["gen_s"] * s["gpus"] / batch_gain for s in suites)                          #!
    return {"gpu_h_before": round(before / 3600, 1), "gpu_h_after": round(after / 3600, 1),
            "reduction": f"{before / after:.1f}x"}

suite = {"gpus": 8, "load_s": 660, "gen_s": 720, "wait_s": 1300, "tail_s": 450, "items": 500}
print(eval_breakdown(suite))
print(shared_endpoint([suite] * 31))
```

**Going deeper.**

- **Determinism must survive the change:** a shared endpoint batches requests from different suites together, which can change sampled outputs. Suites that need bit-exact reproducibility pin greedy decoding and a fixed batch composition, and run on a dedicated replica of the same endpoint.
- **Eval is bursty by nature:** checkpoints arrive in waves. Eval endpoints run in a preemptible queue that borrows idle capacity, so a wave fills gaps in the fleet instead of buying capacity for it.

### Stranding: free GPUs nothing can use

**Scenario.** At 14:00 on a weekday, 960 GPUs were healthy and free while 41 jobs waited. The scheduling team said the free GPUs were "fragmented". That was true of a third of them. Another third were on nodes whose CPUs were fully requested by other pods. The rest were on nodes cordoned for repair two weeks earlier, still counted as healthy because no health check had failed since.

![Stranding by cause](diagrams/designs/concepts/ds38-c7.svg)

![Stranding: free GPUs nothing can use](diagrams/designs/ds38-6.svg)

**What the picture shows.** Every free GPU, every few minutes, tested against what is waiting, and the reason it can't be used recorded.

- **Stranded is relative to demand:** a free GPU is stranded only if some pending workload wants GPUs of that type and can't use this one. A free GPU with nothing waiting for its type is idle, not stranded, and goes to capacity planning.
- **Repair limbo:** the node is cordoned or tainted for maintenance and the repair has passed its target time. Owner: fleet health ([Design 23](#design-health-remediation)).
- **CPU or memory stranding:** the node has free GPUs but not enough free CPU or memory for any pending pod's per-GPU request. Owner: the workloads holding the node's CPU, through rightsizing ([Design 26](#design-cost-programs)) and an admission cap on CPU per GPU.
- **Shape stranding:** the node's free GPUs are fewer than the smallest pending pod needs. Owner: scheduling, through bin-packing and defragmentation ([Design 17](#design-gpu-scheduling)).
- **Topology stranding:** enough free GPUs exist, but not inside one NVLink domain or network block that a pending gang requires. Owner: scheduling, through topology-aware placement and block-level defragmentation.
- **Policy stranding:** the GPUs belong to a quota that isn't lent to the queue that is waiting. Owner: the quota's owner, through lending limits.
- **What the scenario's 960 GPUs were:** 310 shape and topology, 330 CPU, 290 repair limbo and 30 policy. Each category went to a different team with its GPU-hours and dollars attached.

**The code.** Classifying one node's free GPUs against the pending queue (Python).

```python
def classify(node: dict, pending: list[dict], repair_sla_h: float = 48) -> tuple[str, int]:
    """node: gpu, free_gpus, free_cpu, free_mem_gi, block_free_gpus, cordoned_h, quota_lent_to (set of queues).
    pending: pods with gpu, gpus, cpu, mem_gi, queue, block_gpus (GPUs the gang needs inside one block, or 0)."""
    n = node["free_gpus"]
    want = [p for p in pending if p["gpu"] == node["gpu"]]
    if n == 0:
        return "none", 0
    if not want:
        return "idle", n                                                        # no demand for this type
    if node.get("cordoned_h", 0) > repair_sla_h:
        return "repair_limbo", n
    lent = [p for p in want if p["queue"] in node["quota_lent_to"]]
    if not lent:
        return "policy", n
    fits_shape = [p for p in lent if p["gpus"] <= n]
    if not fits_shape:
        return "shape", n
    fits_cpu = [p for p in fits_shape if p["cpu"] <= node["free_cpu"] and p["mem_gi"] <= node["free_mem_gi"]]
    if not fits_cpu:
        return "cpu_memory", n                                                  # GPUs free, CPUs taken #!
    if all(p["block_gpus"] > node["block_free_gpus"] for p in fits_cpu):
        return "topology", n
    return "usable", n

node = {"gpu": "H100", "free_gpus": 4, "free_cpu": 12, "free_mem_gi": 400, "block_free_gpus": 24,
        "cordoned_h": 0, "quota_lent_to": {"research", "eval-scavenger"}}
pending = [{"gpu": "H100", "gpus": 4, "cpu": 48, "mem_gi": 800, "queue": "research", "block_gpus": 0},
           {"gpu": "H100", "gpus": 8, "cpu": 96, "mem_gi": 1600, "queue": "pretrain", "block_gpus": 256}]
print(classify(node, pending))
```

**Going deeper.**

- **Sample often, report hourly:** stranding changes within minutes as jobs start and finish. The classifier runs every five minutes from scheduler state, and the hourly table stores GPU-hours per category per pool.
- **The order of tests matters:** each free GPU gets exactly one reason, tested from the one that is cheapest to confirm to the one most open to argument. Repair limbo comes before shape, because a cordoned node is unusable whatever its shape.
- **Stranding is a moving target:** fixing CPU stranding exposes shape stranding on the same nodes. The register tracks the total stranded share as well as each category, so a fix that only moves GPUs from one category to another doesn't count as closed.

### Rightsizing: choosing the config that does the work cheapest

**Scenario.** A team fine-tuned a 70B model on 64 GPUs with tensor parallelism of 8 and no pipeline, because that was the config in the example they copied. The config's achievable MFU was 34%. A config with tensor parallelism of 4 and two pipeline stages, on the same 64 GPUs, had an achievable MFU of 47% for the same model and sequence length. Nobody had told them, because no tool compared configs for the same work.

![Choosing the config](diagrams/designs/concepts/ds38-c8.svg)

![Rightsizing: choosing the config that does the work cheapest](diagrams/designs/ds38-7.svg)

**What the picture shows.** Candidate configs for the same work ranked by measured cost per unit of work, with the trade-offs each one brings.

- **The same work:** configs are compared within a family for the same model and sequence length (training), the same model and SLO (serving), or the same suite (eval). Only configs with measured baselines are ranked.
- **Cost per unit of work:** the pool's price per GPU-hour divided by the config's achievable work per GPU-hour. For training that is dollars per billion tokens at achievable MFU; for serving, dollars per million output tokens at the SLO; for eval, dollars per thousand scored items.
- **Constraints beside cost:** time to finish (fewer GPUs is cheaper and slower), memory headroom, the availability of the GPU type in the queues the team can use, and the latency SLO for serving.
- **Unmeasured configs get a trial, not a guess:** if the recommender has reason to believe an unmeasured config is better (the nearest measured neighbour is better), it proposes a short trial run, in the scavenger queue, to create the baseline.
- **How it reaches the team:** a recommendation on the job's report and a pull request against the team's job template. The platform doesn't change a running job's config.
- **How this differs from Design 26:** [Design 26](#design-cost-programs) rightsizes a workload from its own usage: fewer GPUs, a smaller GPU type, a lower CPU request. This section changes the config itself, using what other runs of other configs measured.

**The code.** Ranking measured configs for one piece of training work (Python).

```python
def rank(configs: list[dict], tokens_b: float, deadline_h: float) -> list[dict]:
    """configs: name, gpus, gpu, price_h, achievable_mfu, params, peak, measured (bool).
    Training work: tokens_b billion tokens of a dense model at ~6N FLOPs per token."""
    out = []
    for c in configs:
        if not c["measured"]:
            out.append({"config": c["name"], "status": "trial proposed"}); continue
        tps = c["achievable_mfu"] * c["gpus"] * c["peak"] / (6 * c["params"])          # tokens per second
        hours = tokens_b * 1e9 / tps / 3600
        cost = hours * c["gpus"] * c["price_h"]
        out.append({"config": c["name"], "hours": round(hours, 1), "usd": round(cost),
                    "usd_per_b_tokens": round(cost / tokens_b), "meets_deadline": hours <= deadline_h})
    return sorted(out, key=lambda r: (r.get("status") is not None, not r.get("meets_deadline", False),
                                      r.get("usd", 0)))                                       #!

base = {"gpu": "H100", "price_h": 2.40, "params": 70e9, "peak": 989e12, "measured": True}
print(*rank([{**base, "name": "tp8-pp1-64gpu", "gpus": 64, "achievable_mfu": 0.34},
             {**base, "name": "tp4-pp2-64gpu", "gpus": 64, "achievable_mfu": 0.47},
             {**base, "name": "tp4-pp2-32gpu", "gpus": 32, "achievable_mfu": 0.48},
             {**base, "name": "tp2-pp4-64gpu", "gpus": 64, "achievable_mfu": 0.0, "measured": False}],
            tokens_b=20, deadline_h=100), sep="\n")
```

**Going deeper.**

- **Recommended configs are a product:** the platform and the framework team publish one recommended config per model size, sequence length and GPU type, with its baseline. Most rightsizing is moving jobs onto the recommended config, which also makes baselines stronger.
- **Serving rightsizing goes through the benchmark gate:** a cheaper serving config must meet the latency SLO on replayed traffic ([Design 22](#design-serving-benchmarks)) before it is recommended.

### Unused capacity recovery: held, not used

**Scenario.** On a typical day, 9% of allocated GPU-hours were held with no activity. The biggest pieces were not notebooks. They were a training team's reservation of 256 GPUs kept "for the next run" for nine days, finished pods of large jobs still counted against quota while their last workers wrote outputs, eval jobs holding eight GPUs while uploading results, and serving replicas kept warm for a launch that had slipped two weeks.

![Recovering held capacity](diagrams/designs/concepts/ds38-c9.svg)

![Unused capacity recovery: held, not used](diagrams/designs/ds38-8.svg)

**What the picture shows.** Held-but-unused capacity found from the hourly data, returned in the way that fits each kind, and lent to a scavenger queue in the meantime.

- **Detection:** a workload is flagged when its GPUs were allocated and below the activity threshold for a set number of hours: two for batch and eval, twelve for reservations, twenty-four for serving outside its declared quiet hours. The threshold uses the activity definition of [Design 19](#design-scheduling-impact).
- **Release what is finished:** job integrations report pods that no longer need their quota through Kueue's dynamic reclaim, the `reclaimablePods` field of the workload status, so the quota of finished workers returns while the last ones are still running ([Kueue: Workload](https://kueue.sigs.k8s.io/docs/concepts/workload/)). Eval and batch jobs release their GPUs before uploading results, by moving the upload to a CPU sidecar job.
- **Lend what is reserved:** a reservation that isn't running anything lends its quota to the cohort. A scavenger ClusterQueue with no quota of its own borrows it, and its workloads are preempted as soon as the owner submits work.
- **Ask about what is warm:** serving replicas and reservations held for a future date get an owner question with the cost so far. No answer within a week means the capacity is lent; an answer sets an expiry date.
- **Count what is recovered, not what is freed:** freed GPU-hours that nobody then uses are idle capacity, not savings. The program reports recovered GPU-hours that became active work in the scavenger queue or elsewhere. Booking the dollars, cash or avoided cost, is done in the ledger of [Design 26](#design-cost-programs).

**The YAML.** A scavenger queue that only borrows, and a priority class so its work is preempted first (Kueue v1beta2).

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: WorkloadPriorityClass
metadata: { name: scavenger }
value: 10
description: "Runs only on borrowed capacity; preempted first"
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata: { name: scavenger }
spec:
  cohortName: h100-fleet
  namespaceSelector:
    matchLabels: { efficiency.example.com/scavenger: "true" }
  preemption:
    reclaimWithinCohort: Never
    withinClusterQueue: LowerPriority
  resourceGroups:
  - coveredResources: ["cpu", "memory", "nvidia.com/gpu"]
    flavors:
    - name: h100
      resources:
      - { name: "cpu", nominalQuota: 0 }
      - { name: "memory", nominalQuota: 0 }
      - { name: "nvidia.com/gpu", nominalQuota: 0 }        # owns nothing, borrows everything it runs on #!
---
# owners' queues take their capacity back from borrowers
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata: { name: pretrain }
spec:
  cohortName: h100-fleet
  preemption:
    reclaimWithinCohort: Any                                # scavenger work is evicted when the owner returns
    withinClusterQueue: LowerPriority
  resourceGroups:
  - coveredResources: ["cpu", "memory", "nvidia.com/gpu"]
    flavors:
    - name: h100
      resources:
      - { name: "cpu", nominalQuota: 12288 }
      - { name: "memory", nominalQuota: 196608Gi }
      - { name: "nvidia.com/gpu", nominalQuota: 1024 }
```

**Going deeper.**

- **Scavenger work must checkpoint often:** it is preempted without notice when owners return. Eval endpoints, data processing and sweeps fit; long training doesn't.
- **Reservations are a symptom:** teams hold capacity because they don't trust they will get it back. The fix that lasts is queue wait guarantees for owners ([Design 17](#design-gpu-scheduling)), published and met, so holding is no longer worth it.

### Working with the system-owning teams

**Scenario.** The efficiency team found 40 gaps in its first quarter and filed 40 tickets. Eleven were closed. The rest sat in backlogs, because each ticket said "your system is inefficient" without saying how much it cost, how it was measured, or why it should beat the owning team's own roadmap.

![The gap register](diagrams/designs/concepts/ds38-c10.svg)

![Working with the system-owning teams](diagrams/designs/ds38-9.svg)

**What the picture shows.** One register of gaps, ranked by value per effort, worked through a contract with each owning team and a monthly review.

- **A gap is a row:** config or pool, loss category, GPU-hours lost per month, dollars at the pool's rate, owning team, the evidence (the job reports and the query behind the number), a proposed fix and an effort estimate agreed with the owner.
- **Ranked by dollars per engineer-week:** the register is ordered by monthly dollars recoverable divided by engineer-weeks to fix. A $400,000 gap that takes a quarter of a team's time ranks below a $90,000 gap that takes a day.
- **The contract with each owning team:** the efficiency team measures, sizes and verifies; the owning team decides how to fix and ships the fix. Each owning team agrees a target per config it owns, for example "the recommended 70B pre-training config reaches 50% achievable MFU by the end of the quarter".
- **Embedded help for the big ones:** for the top gaps, an engineer from the efficiency team works inside the owning team until the fix ships. That is often what turns a ticket into a closed gap.
- **The monthly review:** the top ten gaps, what moved, what is blocked and why, chaired by the infrastructure lead with every owning team present. Decisions are recorded in the register.
- **Closed means measured:** a gap is closed after four weeks of measured efficiency at the new level, not when the fix merges. The verified GPU-hours go to the ledger of [Design 26](#design-cost-programs).

**The code.** Ranking the register and deciding whether a gap can be closed (Python).

```python
from dataclasses import dataclass, field

@dataclass
class Gap:
    name: str
    owner: str
    category: str
    gpu_h_month: float
    rate: float                      # $ per GPU-hour of the pool
    eng_weeks: float
    weekly_after_fix: list[float] = field(default_factory=list)    # measured efficiency / achievable, per week

    @property
    def usd_month(self) -> float:
        return self.gpu_h_month * self.rate

    def score(self) -> float:
        return self.usd_month / max(self.eng_weeks, 0.2)

    def closed(self, target: float, weeks: int = 4) -> bool:
        w = self.weekly_after_fix[-weeks:]
        return len(w) == weeks and min(w) >= target                    # four weeks at the new level #!

reg = [Gap("70B pretrain: startup after restart", "training-platform", "goodput", 61_000, 2.40, 6),
       Gap("chat-70b: long-context KV pressure", "serving", "config_gap", 38_000, 2.40, 2),
       Gap("eval: one job per suite", "eval-harness", "eval", 96_000, 2.40, 10),
       Gap("H100 pool: CPU stranding", "admission-policy", "stranding", 22_000, 2.40, 0.5)]
for g in sorted(reg, key=Gap.score, reverse=True):
    print(f"{g.name:40s} ${g.usd_month:>9,.0f}/mo  {g.eng_weeks:>4} eng-wk  ${g.score():>9,.0f}/eng-wk  {g.owner}")
reg[1].weekly_after_fix = [0.91, 0.93, 0.92, 0.94]
print("chat-70b closed:", reg[1].closed(target=0.90))
```

**Going deeper.**

- **The number must survive the owner's review:** before a gap is assigned, the owning team sees the query and the evidence and can challenge it. A gap the owner doesn't believe won't be fixed.
- **Credit goes to the owner:** the register and the ledger credit the owning team for the GPU-hours recovered. Teams fix gaps faster when the result shows up on their own scorecard.
- **Some gaps are accepted:** a gap can be closed as "accepted" with a reason, such as a research config that is deliberately unusual. It stays visible, priced, and is reviewed each quarter.

### Holding the gains: regression guards

**Scenario.** A routine image update moved the training images to a new NCCL version. MFU on the 70B pre-training config dropped from 50% to 44%. Nobody noticed for 17 days, because every job still ran and every dashboard was green. The config ran on about 4,000 GPUs, so the 17 days cost about 200,000 GPU-hours of throughput, close to $470,000.

![Regression guards](diagrams/designs/concepts/ds38-c11.svg)

![Holding the gains: regression guards](diagrams/designs/ds38-10.svg)

**What the picture shows.** Two guards per config: one in the release pipelines before a change reaches the fleet, one on the fleet after.

- **Before release:** every pipeline that can move a config's efficiency (framework images, serving engine versions, eval harness, drivers, CUDA, NCCL, node images) runs the benchmark of each recommended config it affects. A result below the config's baseline by more than its noise band blocks the release, and the owning team sees the number.
- **Noise bands from repeated runs:** each benchmark is run several times when its baseline is set, and the band is a multiple of the median absolute deviation of those runs. A gate tighter than the noise blocks good releases; a looser one lets regressions through.
- **After release:** a daily check compares each config's median efficiency with its baseline. A drop beyond the band on two consecutive days opens an alert for the owning team, with the versions that changed that day.
- **Versions are in the fingerprint:** because a new NCCL or engine version makes a new config, the daily check compares the new config with the old one's baseline directly, and the alert names the version change.
- **Decay is a register entry:** a regression that isn't fixed within a week becomes a gap in the register, with its GPU-hours and dollars, like any other.

**The code.** A noise band from repeated benchmark runs, and the release gate that uses it (Python).

```python
import statistics

def band(runs: list[float], k: float = 3.0) -> tuple[float, float]:
    med = statistics.median(runs)
    mad = statistics.median(abs(r - med) for r in runs) or 0.002
    return med, k * 1.4826 * mad                                                   # MAD scaled to a std dev #!

def gate(baseline_runs: list[float], candidate_runs: list[float]) -> str:
    med, width = band(baseline_runs)
    cand = statistics.median(candidate_runs)
    delta = cand - med
    verdict = "block" if delta < -width else "pass"
    return f"{verdict}: baseline {med:.3f} ± {width:.3f}, candidate {cand:.3f} ({delta / med:+.1%})"

baseline = [0.501, 0.497, 0.503, 0.499, 0.502, 0.498]
print(gate(baseline, [0.442, 0.445, 0.440]))          # the NCCL update
print(gate(baseline, [0.499, 0.503, 0.500]))          # a harmless change
```

**Going deeper.**

- **Benchmarks are small versions of real configs:** a 70B pre-training benchmark runs on 64 GPUs for 20 minutes, not on 512 for a day. Some regressions only appear at full scale, which is why the fleet-side guard exists too.
- **Improvements reset the baseline:** when a release raises efficiency beyond the band, the new level becomes the baseline after a week. Gains then can't quietly erode back to the old level.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds38-c12.svg)

- **Phase 1, the waterfall and stranding (weeks 1–6):** the config fingerprint on every job and deployment, `fact_job_efficiency` with GPU activity as a placeholder efficiency, the stranding classifier and its hourly table, and the monthly waterfall. Stranding goes first because it needs no change by any workload team.
- **Phase 2, training goodput and baselines (weeks 4–12):** the step-event library in the framework images, goodput reports for every training job, MFU per job, and achievable baselines for the top 20 training configs by GPU-hours.
- **Phase 3, inference and eval (weeks 8–16):** config labels on deployments, the serving recording rules, gap splits against [Design 22](#design-serving-benchmarks) curves, eval job breakdowns, and the first shared eval endpoint for the main training run's checkpoints.
- **Phase 4, recovery and rightsizing (weeks 10–20):** held-but-unused detection, dynamic reclaim in the job integrations, the scavenger queue, owner questions for reservations and warm replicas, and the config recommender with recommended configs per model size.
- **Phase 5, the operating model (weeks 6–24, growing):** the gap register from the first week of data, the contracts with owning teams, the monthly review, then the release gates and the daily fleet check per config.

**Trade-offs we accepted.**

- **Baselines from history can be low:** a config that has always run badly gets a low achievable level, so its jobs look fine. The benchmark reference beside the baseline exists to catch this, but only where a benchmark exists.
- **Owning teams carry the fixes:** the efficiency team doesn't fix other teams' systems. Gaps close as fast as owners prioritize them, and the embedded engineers are the only way to speed that up.
- **More instrumentation in the training loop:** step events add a small cost to every step and a library every framework image must ship. The events are batched and written asynchronously, and the cost is measured in the framework's own benchmarks.
- **A shared eval endpoint couples suites:** one bad suite can slow others on the same endpoint. Per-suite concurrency limits contain it, at the cost of some batching.
- **Scavenger work is second-class:** it can lose progress at any time. Teams that need reliable progress pay for quota.

**Open questions.**

- **Configs across families:** an RL job is a trainer and a rollout fleet with different configs. Should the efficiency unit be the end-to-end one of [Design 36](#design-cross-stack) only, or should each part keep its own baseline as well?
- **Raising achievable levels:** the baseline measures what a config does at its best today. Who owns moving the best itself closer to the benchmark or the hardware peak, and how is that work sized against closing gaps to today's best?
- **Stranding caused by policy choices:** some topology stranding is the cost of keeping large blocks free for the next big gang. How much stranding is the right price for faster gang starts, and who decides?
- **Efficiency targets in planning:** should capacity plans ([Design 25](#design-cost-capacity)) assume the committed efficiency targets are met, and what happens to the plan when they aren't?
