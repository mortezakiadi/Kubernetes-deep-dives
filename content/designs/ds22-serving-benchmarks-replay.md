---
id: design-serving-benchmarks
title: "Design 22: Benchmarking and traffic replay for LLM serving, with per-config baselines"
short: "Serving benchmarks and baselines"
---
This design answers one question: *how do we build tools that measure LLM serving latency correctly, replay real traffic against any configuration, and keep a trusted performance baseline for every configuration we run?* [Design 21](#design-llm-serving) relies on a benchmark procedure to choose parallelism, quantization and engine settings. [Design 15](#design-eval-infra) covers evaluation and benchmarking across the whole program. This design is the serving benchmark toolkit itself: metric definitions, traffic capture, open-loop replay, percentile statistics, sweeps, configuration fingerprints, the baseline store and regression gates.

The short version: *measure from the client, replay open-loop, and attach every number to a fingerprint*. TTFT, TPOT, inter-token latency and end-to-end latency are computed per request from streaming timestamps on the client, with exact definitions everyone uses. Production traffic is captured as request *shapes* (arrival times, token counts, prefix structure), not as prompt text, and replayed on its original schedule so a slow server can't hide its queueing. Percentiles come from merged histograms with confidence intervals. Every result is stored under a fingerprint of the model, engine, flags, hardware and replay dataset. A change ships only if its result is within noise of, or better than, the baseline for its fingerprint.

### The use case and requirements

**Scenario.** The serving team runs 14 configurations across 3 models, 2 GPU types and 2 engine versions. When a vLLM upgrade went out, chat p99 TTFT doubled in production, yet the pre-release benchmark had shown "a 12% improvement". It turned out the benchmark used a fixed concurrency of 32. The server's slowdown just meant fewer requests were sent, so the queueing never showed up in the numbers.

![A benchmark that said +12%](diagrams/designs/concepts/ds22-c1.svg)

**What the tools must deliver.**

- **One set of metric definitions:** TTFT, TPOT, ITL and end-to-end latency, computed the same way in every tool and dashboard.
- **Realistic load:** replay of production arrival times and request shapes per product, at any speed multiple.
- **Correct latency under overload:** an open-loop load generator that sends on schedule regardless of how the server responds.
- **Trustworthy percentiles:** p50, p90, p99 with confidence intervals, merged correctly across load-generator pods.
- **Baselines per configuration:** a stored, versioned result for every fingerprint, refreshed on a schedule.
- **Regression gates:** engine upgrades, config changes and new model variants are blocked when they're worse than the baseline beyond noise.
- **Privacy:** no customer prompt text leaves the production boundary.

**Out of scope:** model quality evaluation ([Design 15](#design-eval-infra)), and how to tune configurations ([Design 21](#design-llm-serving)).

### Defining the metrics precisely

**Scenario.** One dashboard computed "TPOT" as total time divided by output tokens, and another as the average gap between tokens after the first. For long prompts they differed by 3×, and two teams argued for a week about whether a change had helped.

![Metric definitions](diagrams/designs/concepts/ds22-c2.svg)

![Defining the metrics precisely](diagrams/designs/ds22-1.svg)

**What the picture shows.** One streamed request on a timeline, and the metrics computed from it.

- **TTFT (time to first token):** from the client sending the request to the first content token arriving. It includes network, gateway queueing, scheduling and prefill.
- **TPOT (time per output token):** (end-to-end latency − TTFT) ÷ (output tokens − 1). One number per request, the average decode pace that user saw.
- **ITL (inter-token latency):** each gap between consecutive tokens. Its percentiles across all gaps show stalls that TPOT averages away.
- **End-to-end latency:** from send to the last token.
- **Token counts from the tokenizer:** streamed chunks can carry several tokens, so output tokens are counted with the model's tokenizer (or the server's usage field), not by counting chunks.
- **Client-side timestamps with a monotonic clock:** measured where the user experiences them. Server-side metrics explain the numbers but don't replace them.

**The code.** Per-request metrics from streaming timestamps (Python).

```python
from dataclasses import dataclass

@dataclass
class StreamRecord:
    sent: float                     # time.perf_counter() when the request was sent
    chunk_times: list[float]        # arrival time of every content chunk
    chunk_tokens: list[int]         # tokens in each chunk (tokenizer or usage deltas)

def request_metrics(r: StreamRecord) -> dict:
    ttft = r.chunk_times[0] - r.sent
    e2e = r.chunk_times[-1] - r.sent
    out = sum(r.chunk_tokens)
    tpot = (e2e - ttft) / (out - 1) if out > 1 else None          # one value per request #!
    itl = []                                                        # per-token gaps
    for prev, t, n in zip(r.chunk_times, r.chunk_times[1:], r.chunk_tokens[1:]):
        itl += [(t - prev) / n] * n                                 # spread a multi-token chunk evenly
    return {"ttft_s": ttft, "tpot_s": tpot, "e2e_s": e2e, "output_tokens": out, "itl_s": itl}
```

**Going deeper.**

- **Publish the definitions as a library:** the benchmark tools, the gateway's metrics and the dashboards import the same functions, so the definitions can't drift apart.
- **Report TPOT and ITL percentiles together:** a good TPOT p99 with a bad ITL p99 means requests stall in the middle, often because of prefill interference.

### Capturing production traffic as shapes

**Scenario.** To build a realistic benchmark, someone asked to copy a day of production prompts into the benchmark cluster. Legal said no, and the team fell back to a public chat dataset whose lengths looked nothing like their coding traffic.

![Capture shapes](diagrams/designs/concepts/ds22-c3.svg)

![Capturing production traffic as shapes](diagrams/designs/ds22-2.svg)

**What the picture shows.** What the gateway records, and how replays are built from it without prompt text.

- **Per request:** arrival time, product, model, input and output token counts, the max-tokens setting, whether it streamed, and a session ID.
- **Prefix structure:** a chain of hashes of fixed-size token blocks (for example, 256 tokens), salted per capture. Matching hashes show which requests shared which prefix, and for how long, without revealing content.
- **Synthesis:** replays generate prompts with the same lengths and the same prefix-sharing structure. Requests whose hash chains match share identical synthetic blocks, so prefix-cache behavior is preserved.
- **Output length control:** the replay sets max tokens to the recorded output length and ignores end-of-sequence tokens, so decode work matches production.
- **Versioned datasets:** each capture window becomes a replay dataset with a version, and its summary statistics are stored next to it.

**The code.** Synthesizing prompts that preserve lengths and prefix sharing from hash chains (Python).

```python
import random

def synthesize(trace: list[dict], vocab_size: int, block: int = 256, seed: int = 0) -> list[dict]:
    """trace rows: arrival_s, input_tokens, output_tokens, block_hashes (list of str)."""
    blocks: dict[str, list[int]] = {}                                 # same hash -> same synthetic tokens
    rng = random.Random(seed)
    out = []
    for row in trace:
        ids = []
        for h in row["block_hashes"]:
            if h not in blocks:
                blocks[h] = [rng.randrange(1000, vocab_size) for _ in range(block)]   #!
            ids += blocks[h]
        tail = row["input_tokens"] - len(ids)                          # unshared remainder
        ids += [rng.randrange(1000, vocab_size) for _ in range(max(tail, 0))]
        out.append({"arrival_s": row["arrival_s"], "prompt_token_ids": ids[: row["input_tokens"]],
                    "max_tokens": row["output_tokens"], "ignore_eos": True})
    return out
```

**Going deeper.**

- **Random tokens aren't natural text:** acceptance rates for speculative decoding and some MoE routing patterns depend on content. For those experiments, use a licensed or internal corpus sliced to the recorded lengths.
- **Salt and rotate the hashes:** a hash chain without a salt can be checked against guessed prompts. Salting per capture keeps the structure while protecting content.

### Open-loop replay and coordinated omission

**Scenario.** The upgrade benchmark used 32 concurrent clients, each sending its next request when the previous one finished. When the new version slowed down, the clients slowed down too. Arrival rate dropped, queues never formed, and TTFT looked fine.

![Open-loop replay](diagrams/designs/concepts/ds22-c4.svg)

![Open-loop replay and coordinated omission](diagrams/designs/ds22-3.svg)

**What the picture shows.** Closed-loop and open-loop load, and why only one reveals queueing.

- **Closed loop (fixed concurrency):** each client waits for a response before sending. A slower server automatically receives less load. This hides queueing, a problem known as *coordinated omission*.
- **Open loop (scheduled arrivals):** requests are sent at their scheduled times (from a trace, a Poisson process or a ramp) whether or not earlier ones have finished. A slower server builds a queue, and TTFT shows it.
- **Lateness tracking:** the generator records how late each send was compared with its schedule. If the generator itself falls behind, the run is invalid.
- **Speed multiples:** a trace can be replayed at 1×, 1.5× or 2× its original rate to probe headroom while keeping its burst structure.
- **When closed loop is right:** measuring the maximum throughput of a server at a fixed batch, as one point in a sweep, but never for latency SLOs.

**The code.** An open-loop replayer that sends on schedule and records lateness (Python, asyncio and httpx).

```python
import asyncio, json, time, httpx

async def send(client, url, req, t0, results):
    sched = t0 + req["arrival_s"]
    await asyncio.sleep(max(0.0, sched - time.perf_counter()))
    sent = time.perf_counter()
    rec = {"lateness_s": sent - sched, "sent": sent, "chunk_times": [], "chunk_tokens": []}
    body = {"model": req["model"], "prompt": req["prompt_token_ids"], "max_tokens": req["max_tokens"],
            "ignore_eos": True, "stream": True, "stream_options": {"include_usage": True}}
    try:
        async with client.stream("POST", url, json=body, timeout=600) as r:
            async for line in r.aiter_lines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    chunk = json.loads(line[6:])
                    if chunk.get("choices") and chunk["choices"][0].get("text"):
                        rec["chunk_times"].append(time.perf_counter()); rec["chunk_tokens"].append(1)
                    if chunk.get("usage"):
                        rec["usage"] = chunk["usage"]           # authoritative token count
    except httpx.HTTPError as e:
        rec["error"] = type(e).__name__
    results.append(rec)

async def replay(url, requests, speed=1.0, max_inflight=4096):
    limits = httpx.Limits(max_connections=max_inflight, max_keepalive_connections=max_inflight)
    results, t0 = [], time.perf_counter() + 2.0                  # 2 s to schedule everything
    async with httpx.AsyncClient(limits=limits, http2=False) as client:
        tasks = [asyncio.create_task(send(client, url, {**r, "arrival_s": r["arrival_s"] / speed}, t0, results))
                 for r in requests]                              # all scheduled up front: open loop #!
        await asyncio.gather(*tasks)
    late = sorted(r["lateness_s"] for r in results)
    assert late[int(0.99 * len(late))] < 0.05, "generator fell behind: run invalid"
    return results
```

**Going deeper.**

- **Errors and timeouts are results:** a request that timed out at 600 s counts as a 600 s (or worse) latency in percentiles and fails goodput. Dropping it flatters the run.
- **Connection limits can reintroduce closed-loop behavior:** if the HTTP client caps connections below the in-flight count, sends wait for free connections. Size limits above the expected peak, and check lateness.

### Load generation at scale on Kubernetes

**Scenario.** A single benchmark pod generating 2,000 requests per second for a 64-GPU deployment ran out of CPU parsing streams. Its timestamps were delayed by its own event loop, and the "server latency" it reported was mostly its own.

![Distributed generators](diagrams/designs/concepts/ds22-c5.svg)

![Load generation at scale on Kubernetes](diagrams/designs/ds22-4.svg)

**What the picture shows.** Distributed load generation that the benchmark itself doesn't distort.

- **Indexed Job of generators:** N pods, each replaying a deterministic shard of the trace (requests where index mod N equals its index), so the combined schedule equals the trace.
- **Placement:** generators run on dedicated CPU nodes in the same cluster and zone as the gateway, never on the GPU nodes under test.
- **Through the real path:** requests go through the same gateway and endpoint picker as production, so routing and cache-aware placement are measured too.
- **Start barrier:** all generators start at the same wall-clock time (synchronized clocks), and each uses a monotonic clock for its own measurements.
- **Self-checks:** each generator reports its CPU usage, event-loop lag and send lateness. A run where any generator was saturated is discarded.
- **Results:** per-request records and histograms written to object storage, then merged by a collector.

**The YAML.** A benchmark run as an Indexed Job. Each pod runs the team's replayer image (the open-loop replayer above, packaged with its shard logic) on one shard of the trace.

```yaml
apiVersion: batch/v1
kind: Job
metadata: { name: bench-chat70b-h100-tp2-r20261119, namespace: bench }
spec:
  completionMode: Indexed
  completions: 8
  parallelism: 8                                         # 8 generators, one shard of the trace each #!
  template:
    spec:
      nodeSelector: { node-pool: bench-loadgen }         # CPU nodes, never the GPUs under test
      restartPolicy: Never
      containers:
      - name: loadgen
        image: registry.example.com/bench/replayer:1.4          # platform image: replayer + metrics library
        args: ["--trace", "s3://bench-replays/chat/2026-11-w46", "--speed", "1.0", "--out", "s3://bench-results/$(JOB_NAME)"]
        env:
        - { name: SHARD, valueFrom: { fieldRef: { fieldPath: "metadata.annotations['batch.kubernetes.io/job-completion-index']" } } }
        - { name: SHARDS, value: "8" }
        - { name: JOB_NAME, value: bench-chat70b-h100-tp2-r20261119 }
        - { name: START_AT, value: "2026-11-19T22:00:00Z" }   # start barrier; clocks synced by chrony
        resources: { requests: { cpu: "8", memory: 16Gi }, limits: { cpu: "8", memory: 16Gi } }
        volumeMounts: [{ name: config, mountPath: /config }]
      volumes: [{ name: config, configMap: { name: bench-chat70b-run } }]
```

**Going deeper.**

- **Measure the generator's ceiling once:** benchmark the generators against a stub server that returns instant streams, and record the maximum rate per pod with acceptable lateness. Plan shards so no pod exceeds half of it.
- **Benchmark the path you care about:** for engine tuning, send directly to a pod. For product SLOs, go through the gateway. Record which one in the result.

### Percentiles done right

**Scenario.** Eight generators each reported their own p99 TTFT, and the summary averaged them into "p99 = 0.84 s". The true p99 across all requests was 1.31 s, because one generator's shard happened to include most of the long coding prompts.

![Percentiles](diagrams/designs/concepts/ds22-c6.svg)

![Percentiles done right](diagrams/designs/ds22-5.svg)

**What the picture shows.** How latency distributions are aggregated and reported.

- **Never average percentiles:** merge the raw measurements (or mergeable histograms such as HDR histograms or DDSketch) and compute percentiles once.
- **Enough samples for the tail:** a p99 from 1,000 requests rests on 10 requests. Each load level runs long enough for at least a few thousand requests per product.
- **Confidence intervals:** bootstrap over sessions (not individual requests, which are correlated within a session) to get an interval for each percentile.
- **Warm-up and cool-down excluded:** the first and last minutes of each stage are dropped, so cold caches and draining queues don't skew results.
- **Per product and per length bucket:** a mixed p99 hides that long prompts behave differently. Report both the mix and each bucket.

**The code.** Merged percentiles with a session-level bootstrap interval (Python).

```python
import numpy as np

def percentile_ci(values_by_session: dict[str, list[float]], q: float, n_boot=2000, seed=0):
    """values_by_session: session id -> TTFT samples from all generators, already merged."""
    sessions = list(values_by_session)
    allv = np.concatenate([values_by_session[s] for s in sessions])
    point = np.percentile(allv, q)
    rng = np.random.default_rng(seed)
    boots = []
    for _ in range(n_boot):
        pick = rng.choice(len(sessions), size=len(sessions), replace=True)   # resample sessions #!
        boots.append(np.percentile(np.concatenate([values_by_session[sessions[i]] for i in pick]), q))
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return {"q": q, "value": float(point), "ci95": (float(lo), float(hi)), "n": int(allv.size)}
```

**Going deeper.**

- **Use log-scaled histograms for latency:** they keep relative precision from milliseconds to minutes in small, mergeable structures, which matters at millions of requests.
- **Report the n:** a percentile without its sample count and interval can't be compared with another one.

### Sweeps, saturation and goodput curves

**Scenario.** Each configuration was benchmarked at one request rate, chosen by whoever ran it. Comparisons were meaningless, because one config was measured below its knee and the other above it.

![Sweeps](diagrams/designs/concepts/ds22-c7.svg)

![Sweeps, saturation and goodput curves](diagrams/designs/ds22-6.svg)

**What the picture shows.** The standard sweep every configuration gets.

- **Find saturation first:** a short ramp finds the rate at which throughput stops increasing.
- **Stages around it:** fixed-duration stages from low load to past saturation, spaced more densely near the knee.
- **Per stage:** TTFT, TPOT, ITL and end-to-end percentiles with intervals, throughput, error rate and goodput against the product's SLO.
- **The headline numbers:** maximum goodput, and the highest rate at which every SLO percentile holds (the *SLO capacity*). These go into the baseline.
- **Replay speed as the load knob:** with trace replay, stages are speed multiples of the trace, which keeps the mix and bursts realistic at every level.

**The YAML.** A saturation sweep with Inference Perf (fragment of its config file). The same stages are applied as speed multiples when replaying a trace.

```yaml
load:
  type: poisson
  interval: 15
  sweep:
    type: geometric          # stages clustered around the detected saturation point #!
    timeout: 60
    num_stages: 8
    stage_duration: 300
api:
  type: completion
  streaming: true
data:
  type: shared_prefix         # synthetic prefix sharing; use the team's replayer for trace replays
```

**Going deeper.**

- **Stop at the knee for SLO capacity:** past saturation, queues grow for the whole stage and latency depends on stage length, which isn't a property of the configuration.
- **Keep the stage duration fixed across configs:** a 5-minute stage and a 15-minute stage past saturation produce different p99s from the same server.

### Server-side metrics next to client metrics

**Scenario.** Client TTFT p99 rose from 0.9 s to 1.6 s after a gateway change, but the engine's own TTFT histogram didn't move. The extra time was spent in a new authentication filter at the gateway.

![Client vs server](diagrams/designs/concepts/ds22-c8.svg)

![Server-side metrics next to client metrics](diagrams/designs/ds22-7.svg)

**What the picture shows.** Breaking client latency into where the time went.

- **Client:** the TTFT and TPOT users experience.
- **Gateway and endpoint picker:** time from receiving a request to forwarding it, queueing and routing decisions.
- **Engine:** vLLM's own histograms for TTFT, inter-token latency, queue time and prefill and decode time (`vllm:time_to_first_token_seconds`, `vllm:request_queue_time_seconds` and related metrics), plus running and waiting requests and KV-cache usage.
- **GPU:** DCGM activity, memory bandwidth and power, to see whether the engine was compute-bound or waiting.
- **The breakdown:** client TTFT minus engine TTFT minus gateway time is network plus unaccounted time. When it grows, the problem is outside the engine.

**The code.** Pulling engine histograms for the run window and comparing them with client percentiles (Python, Prometheus HTTP API).

```python
import requests

PROM = "http://prometheus.monitoring:9090/api/v1/query"

def server_quantile(metric: str, q: float, selector: str, window: str) -> float:
    expr = f'histogram_quantile({q}, sum by (le) (rate({metric}_bucket{{{selector}}}[{window}])))'
    r = requests.get(PROM, params={"query": expr}).json()
    return float(r["data"]["result"][0]["value"][1])

def breakdown(client_ttft_p99: float, selector: str, window: str = "5m") -> dict:
    engine = server_quantile("vllm:time_to_first_token_seconds", 0.99, selector, window)
    queue = server_quantile("vllm:request_queue_time_seconds", 0.99, selector, window)
    return {"client_ttft_p99": client_ttft_p99, "engine_ttft_p99": engine, "engine_queue_p99": queue,
            "outside_engine_s": round(client_ttft_p99 - engine, 3)}      # gateway + network #!
```

**Going deeper.**

- **Server histograms have coarse buckets:** their quantiles are interpolated, so use them to locate time, not to replace client percentiles.
- **Capture engine and GPU metrics into the result:** a stored benchmark with its server-side context can be explained months later.

### Configuration fingerprints and the baseline store

**Scenario.** A dashboard showed "chat-70b: 1,450 tok/s/GPU" from three months ago. Nobody could say which engine version, flags or replay dataset produced it, so it couldn't be compared with anything.

![Fingerprints](diagrams/designs/concepts/ds22-c9.svg)

![Configuration fingerprints and the baseline store](diagrams/designs/ds22-8.svg)

**What the picture shows.** What identifies a configuration, and what a baseline record holds.

- **Fingerprint inputs:** model and revision (weights hash), quantization, engine and version, all engine flags (normalized and sorted), parallelism, GPU type and count, driver and CUDA versions, and node type.
- **Workload identity:** replay dataset and version, speed multiples, SLO definitions and the path benchmarked (pod or gateway).
- **Baseline record:** fingerprint, workload, per-stage percentiles with intervals, SLO capacity, maximum goodput, server-side context and links to raw data.
- **Lineage:** each new baseline references the one it replaced and the change that caused it, so a configuration's history reads as a list of changes and their effects.
- **Storage:** a table in the warehouse for querying, and raw records in object storage for re-analysis.

**The code.** Computing a stable fingerprint from a deployment's effective configuration (Python).

```python
import hashlib, json

IGNORED_FLAGS = {"--port", "--host", "--served-model-name", "--disable-log-requests"}

def fingerprint(model_rev: str, engine: str, engine_version: str, flags: list[str],
                gpu: str, gpus: int, driver: str, cuda: str) -> str:
    norm = sorted(f for f in flags if f.split("=")[0] not in IGNORED_FLAGS)   # order-independent #!
    doc = {"model": model_rev, "engine": f"{engine}=={engine_version}", "flags": norm,
           "hw": {"gpu": gpu, "count": gpus, "driver": driver, "cuda": cuda}}
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()[:16]

fp = fingerprint("llama-3.3-70b-fp8@sha256:9c1e", "vllm", "0.32.0",
                 ["--tensor-parallel-size=2", "--kv-cache-dtype=fp8", "--max-num-batched-tokens=4096"],
                 "H100-SXM-80GB", 2, "580.95", "13.0")
```

**Going deeper.**

- **Read the effective configuration from the running server, not the manifest:** defaults change between engine versions. Record the resolved configuration the engine logs at startup.
- **Fingerprint the replay dataset too:** the same configuration measured on two different replays has two baselines.

### Regression gates in CI

**Scenario.** The vLLM upgrade had passed CI. The CI benchmark ran once, closed-loop, on whatever GPUs were free, and accepted anything within 20% of a number typed into a YAML file a year earlier.

![Regression gate](diagrams/designs/concepts/ds22-c10.svg)

![Regression gates in CI](diagrams/designs/ds22-9.svg)

**What the picture shows.** How a change is compared with its baseline before it ships.

- **Triggers:** engine or image upgrades, engine flag changes, new quantized variants, gateway and endpoint-picker changes, and driver updates on the benchmark pool.
- **Runs:** the candidate and the current baseline configuration both run on the same benchmark pool, back to back, with repeats (for example, 3 each) to measure run-to-run noise.
- **Comparison:** each headline metric (SLO capacity, maximum goodput, TTFT and TPOT p99 at the production rate) is compared with a noise-aware threshold derived from the repeats.
- **Verdicts:** pass, regression (block with a report), or improvement (propose a new baseline for review).
- **Reports:** the goodput curves overlaid, the per-metric deltas with intervals, and the server-side breakdown for any regression.

**The code.** A noise-aware comparison of candidate and baseline repeats (Python).

```python
import numpy as np

def compare(baseline: list[float], candidate: list[float], higher_is_better: bool, min_effect: float = 0.03):
    """Each list: one headline metric from repeated runs (e.g., SLO capacity in req/s)."""
    b, c = np.mean(baseline), np.mean(candidate)
    noise = max(np.std(baseline, ddof=1), np.std(candidate, ddof=1), 1e-9)
    rel = (c - b) / b if higher_is_better else (b - c) / b       # positive = better
    if abs(c - b) < 2 * noise or abs(rel) < min_effect:
        return {"verdict": "within_noise", "rel_change": round(rel, 3)}
    return {"verdict": "improvement" if rel > 0 else "regression", "rel_change": round(rel, 3)}   #!

print(compare([18.2, 18.6, 18.4], [15.1, 15.4, 15.0], higher_is_better=True))   # regression
```

**Going deeper.**

- **Run the baseline again rather than reusing the stored number:** the benchmark pool's hardware, drivers and neighbors drift. A same-day baseline run removes that drift from the comparison.
- **Keep a small and a full gate:** a 20-minute gate on every engine-flag change, and the full sweep for upgrades and new variants.

### Running the benchmark service

**Scenario.** Benchmarks ran on whatever GPUs were idle, often next to a training job saturating the same network switch. Results varied by 15% from run to run, so no change smaller than that could be detected.

![Benchmark service](diagrams/designs/concepts/ds22-c11.svg)

![Running the benchmark service](diagrams/designs/ds22-10.svg)

**What the picture shows.** The benchmark service as a platform component.

- **Dedicated benchmark pool:** a small set of GPU nodes per GPU type, isolated from training traffic, admitted through Kueue with exclusive placement ([Design 17](#design-gpu-scheduling)).
- **Scheduled baselines:** every production fingerprint is re-run weekly, so slow drift (drivers, firmware, kernel) is caught even without a change.
- **Production comparison:** live production percentiles, at similar request rates, are compared with the baseline's predictions. A gap means the replay no longer represents production, so a new capture is due.
- **Self-service:** teams submit a benchmark by naming a fingerprint and a replay dataset. Results land in the store with a report link.
- **Noise budget:** the service tracks its own run-to-run variation per configuration and publishes the smallest change it can detect.

**Going deeper.**

- **Refresh replays on a schedule:** traffic changes as products evolve. A monthly capture, with a diff of its length and sharing distributions against the previous one, shows when baselines need to be rebuilt.
- **Treat the benchmark service like production:** its own versions, change log and on-call. A broken benchmark silently approves regressions.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds22-c12.svg)

- **Phase 1, definitions and capture (weeks 1–3):** the metrics library, gateway capture of request shapes and salted prefix hashes, and the first replay datasets per product.
- **Phase 2, the replayer (weeks 3–6):** open-loop distributed replay on Kubernetes, generator self-checks, merged histograms and bootstrap intervals.
- **Phase 3, baselines (weeks 6–9):** fingerprints, the baseline store, standard sweeps and server-side breakdowns for every production configuration.
- **Phase 4, gates and service (weeks 9–12):** CI regression gates, the dedicated benchmark pool, weekly re-baselining and production-versus-baseline drift alerts.

**Trade-offs we accepted.**

- **Synthetic content:** replays preserve shape but not text, so content-dependent effects (speculative decoding acceptance, expert routing) need separate corpora.
- **Dedicated benchmark GPUs** cost capacity that could serve or train.
- **Slower releases:** full sweeps with repeats take hours for engine upgrades.
- **Strict validity rules discard runs:** generator lateness or saturation invalidates a run instead of producing a doubtful number.

**Open questions.**

- **Agentic traffic:** multi-turn sessions where each request depends on the previous answer need dependency-aware replay, not independent arrivals.
- **Cost per token as a first-class metric:** should baselines carry dollars per million tokens at SLO capacity, using each GPU type's price?
- **Benchmarks across clusters:** can baselines from one cluster predict another with different networking ([Design 18](#design-multicloud-fleet))?
- **Smaller detectable changes:** what isolation (fixed clocks, pinned firmware, reserved switches) would bring run-to-run noise from 3% to 1%?
