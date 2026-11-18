---
id: design-eval-infra
title: "Design 15: Evaluation and benchmarking infrastructure"
short: "Evaluation and benchmarking infrastructure"
---
This design answers one question: *how do we build infrastructure that measures three things continuously and consistently: how much the models are improving (progress), how fast training and inference run (throughput), and how reliably training and serving stay up (uptime)?* [Design 12](#design-experiment-analysis) covers the statistics for comparing runs, [Design 10](#design-verification) covers verifier quality, and [Design 14](#design-reliable-rl-infra) covers what makes training reliable. This design is the measurement platform underneath them: suite definitions, eval execution at scale, the results store, performance benchmarks, uptime SLOs and regression detection.

The short version: *one definition, one runner, one store per kind of measurement*. Every eval suite and benchmark is a versioned definition (data, prompts, sampling, scorer, hardware); one scheduler runs them on shared capacity with caching; every result lands in one store with full lineage, per question or per request. Model progress, training throughput, inference throughput and uptime each have a small set of agreed metrics, measured the same way every time, and **benchmark CI** catches regressions from model changes and from software or hardware upgrades alike.

### The use case and requirements

**Scenario.** On Monday, three dashboards disagree: the research tracker shows the new checkpoint 2 points better on reasoning, the release dashboard shows it 1 point worse (different prompt template), and the eval took three days because it waited behind training jobs. The serving team reports 2,300 tokens per second per GPU from a benchmark nobody else can reproduce; a driver upgrade quietly cost 12% of training throughput two weeks ago; and the platform's 99.9% uptime report disagrees with users' experience of two outages.

![Monday's disagreements](diagrams/designs/concepts/ds15-c1.svg)

**What the platform must do.**

- **One definition per measurement:** a suite or benchmark means exactly one thing, versioned, wherever it's run.
- **Fast, cheap evals:** results for a new checkpoint within hours, not days, at predictable cost.
- **Full lineage:** every number traceable to the model, data, code, harness and hardware that produced it.
- **Performance as a first-class measurement:** training and inference throughput benchmarked the same way across models, versions and hardware.
- **Honest uptime:** availability measured from the user's side for serving and as goodput for training.
- **Catch regressions automatically:** from model changes, eval changes and infrastructure changes.

**Out of scope:** statistical methods (Design 12), verifier design (Design 10), production traffic monitoring beyond uptime SLIs.

### Three questions, one platform

**Scenario.** Research, infrastructure and serving teams each built their own measurement tooling. The tools overlap in function and disagree in results.

![Three questions](diagrams/designs/concepts/ds15-c2.svg)

![Three questions, one platform](diagrams/designs/ds15-1.svg)

**What the picture shows.** The three kinds of measurement, what each one measures and who uses it.

- **Model progress:** capability, behavior and safety eval suites run on checkpoints and release candidates. Used by researchers, release gates and leadership.
- **Throughput:** training performance (tokens per second per GPU, model FLOPs utilization, step time) and inference performance (time to first token, inter-token latency, throughput at a latency target). Used by infrastructure, serving and capacity planning.
- **Uptime:** serving availability measured as SLIs from the user's side, and training availability measured as goodput. Used by on-call, platform owners and customers.
- **Shared foundation:** a definition registry, a scheduler on shared GPU capacity, a results store with lineage, and benchmark CI. Built once, used by all three.

**Going deeper.**

- **Separate the question from the tool:** "is the new checkpoint better?" and "did the driver upgrade slow us down?" share the same machinery (definitions, runs, comparisons against a baseline), even though the metrics differ.
- **Each metric has an owner:** who defines it, who approves changes to its definition, and who is paged when it regresses.

### Suite definitions and the harness

**Scenario.** The reasoning score differed by 3 points between two dashboards because one used a chat template with a system prompt and the other didn't. Both were labeled "math-hard".

![Suite definition](diagrams/designs/concepts/ds15-c3.svg)

![Suite definitions and the harness](diagrams/designs/ds15-2.svg)

**What the picture shows.** Everything that determines an eval result, captured in one versioned definition.

- **Data:** the exact dataset version and split, with a content hash.
- **Prompting:** template, system prompt, few-shot examples, chat formatting, tool definitions.
- **Sampling:** temperature, top-p, number of samples per question, maximum tokens, stop sequences, seeds.
- **Scoring:** the verifier or judge and its version ([Design 10](#design-verification)), the metric (pass@1, pass@k, accuracy) and the aggregation (per-question mean, clusters).
- **Harness version:** the eval code itself, pinned by image digest.
- **Any change makes a new version:** "math-hard@v5" always means the same thing; dashboards say which version they show.

**The YAML.** A suite definition in the registry.

```yaml
# suites/math-hard/v5.yaml
suite: math-hard
version: 5
owner: team-reasoning
data:
  source: datasets://math-hard-heldout@v3
  sha256: 9b1e4c...
  split: test
  questions: 1200
  clusters: problem_family              # for clustered standard errors (Design 12)
prompt:
  template: templates/math-cot@v2        # includes the final-answer contract
  system: null
  few_shot: 0
sampling: { temperature: 0.6, top_p: 0.95, samples: 8, max_tokens: 16384, seed: 1234 }   #!
scoring:
  verifier: math-final-answer@3.2.0
  metric: pass@1                         # mean over 8 samples
  report: [pass@1, pass@8, abstain_rate]
harness: registry.example.com/evals/harness@sha256:41aa...
cost_estimate: { gpu_hours_per_70b: 3.1 }
```

**Going deeper.**

- **Templates belong to the model family:** the same suite runs with each model's own chat template; the definition references "the model's template", resolved and recorded at run time.
- **Deprecate, don't edit:** when a suite saturates or has label errors, a new version replaces it; the old version stays runnable so historical results remain comparable.

### Running evals at scale

**Scenario.** A release candidate needs 40 suites, each with several samples per question: about 2 million generations. Run naively, one suite at a time on one node, it takes three days.

![Sharded evals](diagrams/designs/concepts/ds15-c4.svg)

![Running evals at scale](diagrams/designs/ds15-3.svg)

**What the picture shows.** How the scheduler turns "evaluate this checkpoint" into fast, cheap work.

- **Fan-out:** each (checkpoint, suite) is split into shards of questions; shards run in parallel.
- **Shared inference servers:** shards send requests to vLLM servers for the checkpoint, sized to the work, instead of each shard loading the model itself.
- **Generation cache:** generations are cached by a key of (model weights hash, prompt, sampling settings, seed); re-scoring with a new verifier or re-running a dashboard doesn't regenerate.
- **Priorities:** release-candidate evals preempt routine checkpoint evals; both borrow idle GPUs from training queues at lower priority ([Design 02](#design-multicluster)).
- **Retries and partial results:** failed shards are retried; results stream in, so partial scores are visible early with their uncertainty.

**The code.** A shard worker: cached generation against a shared server, then scoring (Python).

```python
import hashlib, json, asyncio, httpx

def cache_key(weights_sha: str, prompt: str, sampling: dict, sample_idx: int) -> str:
    raw = json.dumps([weights_sha, prompt, sampling, sample_idx], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()                     # same inputs → same key #!

async def run_shard(questions, suite, model, cache, scorer, server_url, concurrency=64):
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=900) as client:
        async def one(q, i):
            key = cache_key(model["weights_sha"], q["prompt"], suite["sampling"], i)
            text = cache.get(key)
            if text is None:
                async with sem:
                    r = await client.post(f"{server_url}/v1/completions", json={
                        "model": model["name"], "prompt": q["prompt"], "seed": suite["sampling"]["seed"] + i,
                        **{k: v for k, v in suite["sampling"].items() if k not in ("samples", "seed")}})
                    r.raise_for_status()
                    text = r.json()["choices"][0]["text"]
                cache.put(key, text)
            res = scorer.verify(q, text)
            return {"qid": q["id"], "sample": i, "score": res.score, "verdict": res.verdict, "cache_key": key}
        jobs = [one(q, i) for q in questions for i in range(suite["sampling"]["samples"])]
        return await asyncio.gather(*jobs)
```

**Going deeper.**

- **Determinism isn't guaranteed:** batched inference can change outputs slightly even with fixed seeds. Store generations; recomputing is not the same as re-reading.
- **Cost per suite is tracked:** GPU hours per suite per model size, so suites that cost more than they inform can be trimmed.

### The results store and lineage

**Scenario.** A leadership slide quotes "71.4" for a model. Nobody can find which checkpoint, suite version or harness produced it.

![Results lineage](diagrams/designs/concepts/ds15-c5.svg)

![The results store and lineage](diagrams/designs/ds15-4.svg)

**What the picture shows.** One store for all measurements, with the lineage that makes every number traceable.

- **Granularity:** evals per question and sample; inference benchmarks per request; training benchmarks per step; uptime per probe or per minute of SLI data.
- **Lineage on every row:** model or checkpoint ID and weights hash, suite or benchmark definition version, harness image digest, hardware type and count, software versions (driver, CUDA, framework), run ID.
- **Columnar format:** stored as partitioned Parquet (or a table format on top of it), so per-question analysis and dashboard aggregation read the same data.
- **Derived tables:** aggregates (scores with intervals, throughput curves, SLO compliance) computed from the raw rows by versioned jobs; dashboards read only derived tables.
- **Immutability:** rows are never edited; corrections are new rows with a new definition or scorer version.

**The code.** Writing eval rows with lineage as partitioned Parquet (Python, PyArrow).

```python
import pyarrow as pa, pyarrow.parquet as pq

SCHEMA = pa.schema([
    ("run_id", pa.string()), ("checkpoint", pa.string()), ("weights_sha", pa.string()),
    ("suite", pa.string()), ("suite_version", pa.int32()), ("harness_digest", pa.string()),
    ("verifier", pa.string()), ("qid", pa.string()), ("cluster", pa.string()),
    ("sample", pa.int16()), ("score", pa.float32()), ("verdict", pa.string()),
    ("gen_cache_key", pa.string()), ("hardware", pa.string()), ("ts", pa.timestamp("s")),
])

def write_results(rows: list[dict], root="s3://evals/results"):
    table = pa.Table.from_pylist(rows, schema=SCHEMA)
    pq.write_to_dataset(table, root_path=root,
                        partition_cols=["suite", "suite_version", "checkpoint"])   # fast per-suite reads #!
```

**Going deeper.**

- **Every published number has a link:** dashboards and reports render numbers with a link to the query that produced them.
- **Keep generations separately:** generation text lives in object storage keyed by cache key; rows reference it, so the store stays small enough to query quickly.

### Measuring model progress

**Scenario.** The team's flagship benchmark rose from 55% to 89% in a year. Half the remaining errors turn out to be label mistakes, and the top three models are within noise of each other. The benchmark no longer measures progress.

![Can the eval still measure progress?](diagrams/designs/concepts/ds15-c6.svg)

![Measuring model progress](diagrams/designs/ds15-5.svg)

**What the picture shows.** Tracking progress over time, and checking that the evals can still measure it.

- **Progress views:** each family's score over checkpoints and releases, with intervals ([Design 12](#design-experiment-analysis)); a capability index that combines families with stated weights, alongside the per-family scores.
- **Held-out and public:** private held-out suites (never trained on, rarely shown) next to public benchmarks; a gap that grows between them suggests contamination or overfitting to public benchmarks.
- **Eval health:** saturation (scores near the ceiling), discriminative power (can the suite separate models that differ?), label error rate (sampled human review of disagreements between strong models and labels), and contamination checks against training data.
- **Suite lifecycle:** new suites enter as experimental, become official once their health is measured, and retire when saturated.

**The code.** Eval health checks from the results store: saturation and how well a suite separates recent checkpoints (Python).

```python
import numpy as np

def suite_health(scores_by_model: dict[str, np.ndarray], ceiling=1.0):
    """scores_by_model: model -> per-question mean scores on the same questions."""
    models = sorted(scores_by_model, key=lambda m: scores_by_model[m].mean())
    means = np.array([scores_by_model[m].mean() for m in models])
    headroom = ceiling - means.max()                                   # saturation #!
    separable = 0
    for a, b in zip(models, models[1:]):                               # adjacent models
        d = scores_by_model[b] - scores_by_model[a]
        se = d.std(ddof=1) / np.sqrt(len(d))
        separable += d.mean() > 2 * se
    return {"best": round(means.max(), 3), "headroom": round(headroom, 3),
            "adjacent_pairs_separated": f"{separable}/{len(models) - 1}"}   # low → retire or replace
```

**Going deeper.**

- **Saturation hides regressions too:** near the ceiling, a real regression may look like noise; keep harder versions of saturated suites.
- **Label audits are cheap:** reviewing the questions every strong model gets "wrong" finds most label errors with little effort.

### Training throughput benchmarks

**Scenario.** After a routine driver upgrade, training throughput dropped 12%. It was found two weeks later, in a cost report, because the production run's slowdown was blamed on "the new data mix."

![Training throughput](diagrams/designs/concepts/ds15-c7.svg)

![Training throughput benchmarks](diagrams/designs/ds15-6.svg)

**What the picture shows.** Standard training benchmarks that make throughput comparable across time, hardware and software.

- **Standard workloads:** a small set of fixed training jobs (for example, a dense 8B and a MoE model at fixed parallelism and sequence length), run for a few hundred steps after warm-up.
- **Metrics:** tokens per second per GPU, model FLOPs utilization (MFU: achieved model FLOPs over hardware peak), step time and its breakdown (compute, communication, data loading), memory headroom.
- **Matrix:** each benchmark across hardware types and the software stack (driver, CUDA, NCCL, PyTorch, framework versions).
- **Triggers:** every software or image change to the training stack, every new hardware pool, and nightly on a reference pool.
- **Production comparison:** the production run's own throughput is compared with the benchmark on the same hardware and stack; a gap means the run (data, config) is the cause, not the platform.

**The code.** MFU from measured throughput for a dense transformer (Python).

```python
def mfu(tokens_per_s_per_gpu: float, n_params: float, n_layers: int, seq_len: int, d_model: int,
        peak_flops_per_gpu: float) -> float:
    """Model FLOPs utilization: training FLOPs per token ≈ 6N + 12·L·s·d (attention), forward + backward."""
    flops_per_token = 6 * n_params + 12 * n_layers * seq_len * d_model
    return tokens_per_s_per_gpu * flops_per_token / peak_flops_per_gpu          #!

# 8B dense model, 8K sequence, BF16 dense peak ~989 TFLOP/s on H100 SXM
print(f"{mfu(4300, 8.0e9, 32, 8192, 4096, 989e12):.1%}")
```

**Going deeper.**

- **MFU and goodput answer different questions:** MFU measures how efficiently a step runs; goodput ([Design 14](#design-reliable-rl-infra)) measures how much of the reserved time produced steps. Report both.
- **Benchmark the RL loop too:** for RL, also benchmark rollout throughput, weight-sync time and reward latency at fixed settings ([Design 07](#design-scaling-rl)).

### Inference throughput and latency benchmarks

**Scenario.** The serving team reports 2,300 tokens per second per GPU. The capacity team measures 900. Both are right: one measured at maximum batch with no latency limit, the other at the product's latency target with real prompt lengths.

![Throughput at a latency](diagrams/designs/concepts/ds15-c8.svg)

![Inference throughput and latency benchmarks](diagrams/designs/ds15-7.svg)

**What the picture shows.** Measuring inference performance as a curve, not a single number.

- **Metrics:** time to first token (TTFT), inter-token latency or time per output token, end-to-end latency, requests and tokens per second, and their percentiles.
- **Load sweep:** run at increasing request rates; plot throughput against latency. Throughput only means something together with the latency at which it was measured.
- **Throughput at the SLO:** the headline number is the highest throughput at which p95 TTFT and p95 inter-token latency stay within the product's targets.
- **Realistic traffic:** prompt and output length distributions from production (or a replay), shared prefixes, multi-turn conversations; not uniform synthetic lengths.
- **Same tool, same configs:** a standard, model-server-agnostic load generator such as the Kubernetes community's Inference Perf (which supports vLLM, SGLang and other servers, and real or synthetic datasets) run from the same configs by every team.

**The YAML.** An Inference Perf load sweep against a serving deployment, run as a Kubernetes Job.

```yaml
apiVersion: v1
kind: ConfigMap
metadata: { name: perf-chat-70b, namespace: benchmarks }
data:
  config.yml: |
    api: { type: chat, streaming: true }
    data:
      type: shareGPT                   # realistic conversation lengths; or a production replay
    load:
      type: constant
      stages:                          # sweep request rates; 3 minutes each #!
      - { rate: 5,  duration: 180 }
      - { rate: 10, duration: 180 }
      - { rate: 20, duration: 180 }
      - { rate: 40, duration: 180 }
    server:
      type: vllm
      model_name: assistant-70b-rc3
      base_url: http://assistant-70b-rc3.serving.svc:8000
    report:
      request_lifecycle: { summary: true, per_stage: true, per_request: true }
    storage:
      google_cloud_storage: { bucket_name: perf-results, path: chat-70b/rc3 }
---
apiVersion: batch/v1
kind: Job
metadata: { name: perf-chat-70b-rc3, namespace: benchmarks }
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: inference-perf
        image: quay.io/inference-perf/inference-perf:latest   # pin by digest in practice
        args: ["--config_file", "/cfg/config.yml"]
        volumeMounts: [ { name: cfg, mountPath: /cfg } ]
        resources: { limits: { cpu: "8", memory: 16Gi } }
      volumes: [ { name: cfg, configMap: { name: perf-chat-70b } } ]
```

**Going deeper.**

- **The load generator can be the bottleneck:** check its own CPU and network use; at high rates run several generator pods.
- **Benchmark the whole path:** include the gateway and router in some runs ([Design 03](#design-training-inference)); the server alone and the full path give different, both useful, numbers.

### Uptime and availability

**Scenario.** The platform reported 99.9% uptime for the month: every pod was running. Users experienced two outages: one where the gateway returned errors for 25 minutes while pods were healthy, and one where latency was ten times normal for an hour.

![User-side uptime](diagrams/designs/concepts/ds15-c9.svg)

![Uptime and availability](diagrams/designs/ds15-8.svg)

**What the picture shows.** Measuring uptime as users and training runs experience it.

- **Serving SLIs:** the fraction of requests that succeed *and* meet the latency target, measured at the gateway (real traffic) and by synthetic probes from outside the cluster (catches total outages with no traffic).
- **SLOs and error budgets:** for example, 99.5% of requests good over 30 days; the budget is the allowed bad fraction, and burn-rate alerts fire when it's being used up too fast.
- **Training availability:** goodput per run and the failure ledger ([Design 14](#design-reliable-rl-infra)); "the cluster was up" isn't training uptime.
- **Eval and benchmark availability:** the measurement platform has its own SLO (results for a release candidate within N hours), since slow evals delay every decision.
- **Reports from the same data:** monthly uptime reports are computed from the SLI data, not from component health.

**The YAML.** Recording and alerting rules for a serving SLO with multi-window burn-rate alerts (Prometheus Operator).

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata: { name: chat-api-slo, namespace: monitoring }
spec:
  groups:
  - name: chat-api-sli
    rules:
    - record: sli:chat_good_ratio:rate5m                   # good = success AND under latency target
      expr: |
        sum(rate(gateway_requests_total{route="chat",code!~"5.."}[5m])
            - rate(gateway_requests_slow_total{route="chat",threshold="ttft_2s"}[5m]))
        / sum(rate(gateway_requests_total{route="chat"}[5m]))
    - record: sli:chat_good_ratio:rate1h
      expr: |
        sum(rate(gateway_requests_total{route="chat",code!~"5.."}[1h])
            - rate(gateway_requests_slow_total{route="chat",threshold="ttft_2s"}[1h]))
        / sum(rate(gateway_requests_total{route="chat"}[1h]))
  - name: chat-api-burn
    rules:
    - alert: ChatSLOFastBurn                                 # 99.5% SLO → budget 0.005
      expr: |
        (1 - sli:chat_good_ratio:rate5m) > (14.4 * 0.005)
        and (1 - sli:chat_good_ratio:rate1h) > (14.4 * 0.005)   # burning 2% of budget per hour #!
      for: 2m
      labels: { severity: page }
```

**Going deeper.**

- **Synthetic probes catch silent outages:** when the gateway is down, there may be no failed requests to count; an external probe every 30 seconds sees it.
- **Latency is part of availability:** a 10× slower response is an outage for an interactive product; it counts against the SLO.

### Regression detection and benchmark CI

**Scenario.** Every upgrade of vLLM, PyTorch or the GPU driver is "probably fine." Nobody runs the benchmarks before rollout because they take a day to set up.

![Benchmark CI](diagrams/designs/concepts/ds15-c10.svg)

![Regression detection and benchmark CI](diagrams/designs/ds15-9.svg)

**What the picture shows.** Benchmarks wired into change management.

- **Triggers:** a new model checkpoint (evals), a change to the serving or training image (performance benchmarks), a new hardware pool (both), and a nightly schedule (all, on reference hardware).
- **Baselines:** each benchmark compares with the rolling baseline for the same definition and hardware, using its known run-to-run variation.
- **Gates:** image changes can't roll out to production pools if a benchmark regresses beyond its threshold; model releases need the eval gate ([Design 11](#design-multi-task)).
- **Trend detection:** slow drifts (a 1% loss per week) are caught by change-point detection on the nightly series, not by single comparisons.
- **Triage:** a regression opens an issue with the diff between the two runs' lineage (what changed in model, image, driver, hardware).

**The code.** Comparing a new benchmark result with its rolling baseline using a robust threshold (Python).

```python
import numpy as np

def check_regression(history: list[float], new: float, higher_is_better=True,
                     k: float = 4.0, min_rel: float = 0.02):
    """history: recent results for the same benchmark + hardware + definition."""
    h = np.asarray(history[-20:])
    med = np.median(h)
    mad = np.median(np.abs(h - med)) * 1.4826 + 1e-9                     # robust spread #!
    delta = (new - med) if higher_is_better else (med - new)
    regressed = delta < -max(k * mad, min_rel * abs(med))                # beyond noise AND material
    return {"baseline": round(med, 2), "new": round(new, 2), "rel_change": round(delta / med, 4),
            "regressed": bool(regressed)}

print(check_regression([4310, 4295, 4322, 4301, 4318, 4290, 4307], 3790))   # driver upgrade: −12%
```

**Going deeper.**

- **Benchmarks need their own variance estimates:** some are stable to 0.5%, others vary by 5% run to run; one threshold for all produces either noise or blindness.
- **Pin the benchmark itself:** the benchmark harness and its configs are versioned; a change to the benchmark resets the baseline explicitly.

### The platform on Kubernetes and reporting

**Scenario.** Measurements exist, but each audience reads a different spreadsheet. Leadership wants one page per week; on-call wants live SLOs; researchers want per-question drill-down.

![Measurement views](diagrams/designs/concepts/ds15-c11.svg)

![The platform on Kubernetes and reporting](diagrams/designs/ds15-10.svg)

**What the picture shows.** The components and the views built on them.

- **Definition registry:** eval suites, training and inference benchmarks, SLOs, all versioned with owners.
- **Measurement scheduler:** turns triggers into Kueue-managed jobs (sharded evals, benchmark jobs) on borrowed or reserved capacity; manages vLLM servers for evals and the generation cache.
- **Collectors:** Prometheus for SLIs and live metrics, benchmark tools writing raw results, eval shards writing per-question rows.
- **Results store and derived tables:** Parquet with lineage; versioned aggregation jobs.
- **Views:** a research view (per-question drill-down, comparisons with intervals), an infrastructure view (throughput matrices, regression history), an operations view (SLOs, error budgets, goodput) and a weekly report generated from derived tables.

**Going deeper.**

- **Generated reports, not hand-made ones:** the weekly report is a template filled from derived tables, with each number linked to its query; hand-edited numbers are where disagreements start.
- **Measurement has a budget:** evals and benchmarks get a fixed share of GPU capacity, and the platform reports what each suite and benchmark costs, so the budget is spent on what informs decisions.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds15-c12.svg)

- **Phase 1, definitions and store:** suite and benchmark registry, per-question and per-request results with lineage, one dashboard per audience reading derived tables.
- **Phase 2, scale:** the sharded eval scheduler with shared inference servers, generation cache and priorities; release-candidate evals within hours.
- **Phase 3, performance and uptime:** standard training and inference benchmarks with hardware and software matrices; serving SLIs, synthetic probes, SLOs and burn-rate alerts.
- **Phase 4, CI and health:** benchmark gates on image and model changes, change-point detection on nightly series, eval health and suite lifecycle.

**Trade-offs we accepted.**

- **Versioned definitions slow down quick changes:** a new prompt template means a new suite version and a new baseline, in exchange for numbers that mean the same thing everywhere.
- **Benchmark CI delays upgrades** by hours, in exchange for not discovering a 12% slowdown in a cost report.
- **User-side uptime looks worse** than component uptime, and is the number that matches what users experience.
- **Measurement capacity is reserved,** so some GPUs aren't training; the measurement SLO justifies it.

**Open questions.**

- **Cheaper evals:** how small can a suite be (via question selection) while keeping its ranking of models?
- **Evaluating agents:** long, stateful agent evals need environments and much more compute per question ([Design 08](#design-long-horizon)); how should they be scheduled and cached?
- **Comparable performance across generations:** how to compare throughput across very different model architectures and hardware fairly?
- **Production signals as evals:** which production metrics (user ratings, retries, abandonment) are reliable enough to sit beside offline evals in progress views?
