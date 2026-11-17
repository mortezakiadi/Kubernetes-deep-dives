---
id: post-observability
title: "Observing LLMs"
short: "Observing LLMs"
---
Kubernetes heals crashed pods, but it can't tell you that answers got slow, or wrong. LLM observability starts with the usual logs, metrics and traces, then switches to tokens instead of requests, turns those into SLOs and alerts, and finally adds a layer most services never need: checking that the answers are any good.

### The usual stack still applies

**Scenario.** A model server is deployed with no observability beyond `kubectl logs`. When users report slow answers on Monday morning, the team can't tell whether the model, the GPU, the queue or the gateway was slow, or when it started.

![The usual stack still applies](diagrams/posts/p04-1.svg)

**What the picture shows.** The three signals from a model server pod, and where each one goes.

- **Logs** go to stdout and into a log store such as Loki. A vLLM startup log already tells you a lot: the version and arguments, the model configuration, how much GPU memory the weights took and how much is left for the KV cache, and the routes it serves.
- **Metrics** are scraped in Prometheus/OpenMetrics format from `/metrics` on the server port. Every vLLM metric has the `vllm:` prefix. KServe has annotations that turn scraping on for a model, plus an aggregator for multi-container pods.
- **Traces:** vLLM emits OpenTelemetry spans when given an OTLP endpoint. An OpenTelemetry Collector forwards them to a tracing backend (Jaeger or Tempo), so one request can be followed from the gateway through the orchestrator to the model. OpenTelemetry is defining semantic conventions specifically for generative AI.
- **GPU metrics** come from the vendor's exporter (NVIDIA DCGM, AMD, Intel), alongside the server's own metrics. See [What every scheduler relies on](#d02-8).

**The YAML.** Scrape every model server in the namespace, and send traces to the collector.

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: vllm
  namespace: models
spec:
  selector:
    matchLabels: { serving.example.com/server: vllm }   # label on every model pod
  podMetricsEndpoints:
  - port: http                     # vLLM serves /metrics on its API port #!
    path: /metrics
    interval: 15s
---
# In the model server's container:
args:
- --model=Qwen/Qwen2.5-7B-Instruct
- --otlp-traces-endpoint=http://otel-collector.monitoring.svc:4317   # spans per request #!
```

**Going deeper.**

- **Label everything with the model and its version,** so a dashboard can compare v7 and v8 of the same model side by side during a rollout.
- **Log prompts and answers carefully:** they can contain personal data. Keep them out of the general log store, and capture them only through a deliberate path with its own access rules (see "Responsible AI" below).

### Measure tokens, not requests

**Scenario.** The dashboard shows average request latency of 8 seconds, and the team doesn't know whether that's good. A request that generates 2,000 tokens should take longer than one that generates 20. For LLMs, "request latency" mixes two different things, and the user feels them differently.

![Measure tokens, not requests](diagrams/posts/p04-2.svg)

**What the picture shows.** One request's life in the server, and the metric for each part.

- **Queue time:** waiting for a slot in the batch. The earliest sign of overload: `vllm:num_requests_waiting` grows before latency does.
- **Prefill (time to first token, TTFT):** processing the whole prompt and producing the first token. Compute-heavy. It's what a chat user feels as "waiting", and it grows with prompt length.
- **Decode (time per output token, TPOT, or inter-token latency):** generating each following token. Memory-heavy. It's reading speed; about 4–5 tokens per second keeps up with a human reader.
- **End-to-end latency:** the whole request, which depends mostly on how many tokens were generated.
- **Throughput:** prompt and generated tokens per second, per replica and per GPU.
- **CPU usage says little:** the GPU does the work, so CPU stays flat while the queue grows.

**The queries.** PromQL for the core LLM metrics (vLLM's current metric names).

```text
# p95 time to first token, per model
histogram_quantile(0.95, sum by (le, model_name) (rate(vllm:time_to_first_token_seconds_bucket[5m])))

# p95 inter-token latency (time per output token)
histogram_quantile(0.95, sum by (le, model_name) (rate(vllm:inter_token_latency_seconds_bucket[5m])))

# generated tokens per second, per model
sum by (model_name) (rate(vllm:generation_tokens_total[5m]))

# queue and KV-cache pressure, per pod
vllm:num_requests_waiting
vllm:kv_cache_usage_perc
```

**Going deeper.**

- **Prefill and decode behave differently under load.** Long prompts hurt TTFT for everyone in the batch; many concurrent decodes hurt TPOT. That's why disaggregated serving puts them in separate pools at large scale.
- **Prefix-cache hit rate** (`vllm:prefix_cache_hits` over `vllm:prefix_cache_queries`) shows whether cache-aware routing is working. A low hit rate on agent traffic usually means requests are being spread instead of routed.

### SLOs and alerts

**Scenario.** The on-call engineer is paged at 3 a.m. because "latency is high", for a batch job's traffic that nobody is waiting for. The next week, nobody is paged when chat users wait 9 seconds for a first token. Alerts need to describe what users feel, per kind of traffic.

![SLOs and alerts](diagrams/posts/p04-3.svg)

**What the picture shows.** From raw metrics to an alert someone can act on.

- **SLI (what you measure):** the share of requests with TTFT under 2 seconds, or TPOT under 0.3 seconds, from the histograms above.
- **SLO (the target):** for example, "TPOT under 0.3 s for 99.9% of interactive requests over 30 days".
- **Error budget:** the 0.1% of requests allowed to miss. Alerts fire when the budget is being spent too fast (burn-rate alerts), not on every slow minute.
- **Early-warning alerts:** queue length and KV-cache usage, which predict a latency breach minutes before it happens.
- **A runbook link in every alert:** what to check (queue, KV cache, GPU errors, recent rollout) and what to do (scale out, roll back, shed batch traffic).

**The YAML.** Alerts on the user-facing SLI and on the early warnings.

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: llm-slos
  namespace: models
spec:
  groups:
  - name: llm-latency
    rules:
    - alert: LLMTimeToFirstTokenHigh
      expr: |
        histogram_quantile(0.99,
          sum by (le, model_name) (rate(vllm:time_to_first_token_seconds_bucket[10m]))) > 2   #!
      for: 10m
      labels: { severity: page }
      annotations:
        summary: "p99 TTFT above 2s for {{ $labels.model_name }}"
        runbook_url: https://runbooks.example.com/llm/ttft
    - alert: LLMQueueBuilding
      expr: avg by (model_name) (vllm:num_requests_waiting) > 10     # predicts the next alert #!
      for: 5m
      labels: { severity: ticket }
      annotations:
        summary: "Requests queueing for {{ $labels.model_name }}: scale out or shed load"
    - alert: LLMKVCacheFull
      expr: max by (model_name, pod) (vllm:kv_cache_usage_perc) > 0.95
      for: 10m
      labels: { severity: ticket }
```

**Going deeper.**

- **Separate SLOs per traffic class:** interactive chat, agents and batch have different targets. Label requests at the gateway so the metrics can be split.
- **Use the benchmark results** from [Running LLMs in production](#post-production) to set thresholds the hardware can actually meet.

### Fast isn't enough: watch quality

**Scenario.** After a model update, every latency dashboard is green, and support tickets about wrong answers double. A model rarely crashes on bad input. It answers confidently and wrongly, which is a **hallucination**, and no latency metric will catch it.

![Fast isn't enough: watch quality](diagrams/posts/p04-4.svg)

**What the picture shows.** A quality pipeline that runs alongside production traffic, without slowing it.

- **Sample 1–10% of responses** at the gateway or orchestrator, asynchronously, into a queue. Users never wait for the quality check.
- **An LLM-as-a-judge worker** reads each sample and asks a judge model specific questions about it ("Is the answer supported by the retrieved context?", "Does this reply include personal data?").
- **Specific questions work best.** "Is this answer good?" produces noisy scores; narrow, checkable questions produce useful ones.
- **Scores become Prometheus metrics,** so quality can be trended, compared between model versions and alerted on, like latency.
- **Cheap metrics for narrow tasks:** for summaries, an overlap score such as ROUGE against a reference can work without a judge model.

**The code.** A judge worker that scores sampled answers and exports the results as metrics.

```python
import json
from openai import OpenAI
from prometheus_client import Counter, Histogram, start_http_server

judge = OpenAI(base_url="http://judge-model.models.svc:8000/v1", api_key="unused")
FAITHFUL = Histogram("llm_judge_faithfulness", "Judge score 0-1: answer supported by context",
                     ["model", "version"], buckets=[0.2, 0.4, 0.6, 0.8, 0.9, 1.0])
PII = Counter("llm_judge_pii_total", "Sampled answers that contain personal data", ["model", "version"])

QUESTION = """Context:\n{context}\n\nAnswer:\n{answer}\n
Reply in JSON: {{"supported": <0 to 1, how much of the answer the context supports>,
"contains_personal_data": <true or false>}}"""

def score(sample: dict) -> None:
    reply = judge.chat.completions.create(
        model="judge",
        messages=[{"role": "user", "content": QUESTION.format(**sample)}],
        response_format={"type": "json_object"},
        temperature=0,
    )
    verdict = json.loads(reply.choices[0].message.content)
    labels = (sample["model"], sample["version"])
    FAITHFUL.labels(*labels).observe(float(verdict["supported"]))
    if verdict["contains_personal_data"]:
        PII.labels(*labels).inc()

if __name__ == "__main__":
    start_http_server(9100)                       # scraped by Prometheus like any service
    for sample in consume_samples():              # your queue: Kafka, Redis Streams, SQS...
        score(sample)
```

**Going deeper.**

- **The judge is a model too:** use a different (ideally stronger) model than the one being judged, pin its version, and spot-check its verdicts by hand.
- **Run the judge as a Deployment scaled by queue length,** at low priority, so quality checks never take GPUs away from users.

### Responsible AI

**Scenario.** A customer disputes a loan decision that an AI assistant helped make, and asks why. Three months later, a fairness review finds that answers have drifted for one group of users. Both questions need data captured *before* anyone asks them.

![Responsible AI](diagrams/posts/p04-5.svg)

**What the picture shows.** Three practices around the serving path.

- **Capture requests and responses with an inference logger** to a protected store. Explanations are expensive to compute, so explain only the disputed cases later, from the captured data.
- **Monitor bias continuously:** fairness metrics on protected attributes can drift at runtime through data drift, even if training was clean. KServe with TrustyAI can compute them as metrics.
- **Guardrails on input and output:** checks before the model (prompt injection, disallowed topics) and after (personal data, harmful content). Covered in [Identity and model endpoints](#d13-6).

**The YAML.** KServe's built-in logger sends every request and response, as CloudEvents, to a collector you control.

```yaml
apiVersion: serving.kserve.io/v1beta1
kind: InferenceService
metadata:
  name: lending-assistant
  namespace: models
spec:
  predictor:
    logger:
      mode: all                                         # request and response #!
      url: http://inference-log-collector.audit.svc     # protected store, own access rules #!
    model:
      modelFormat: { name: huggingface }
      storageUri: hf://Qwen/Qwen2.5-7B-Instruct
      resources: { limits: { nvidia.com/gpu: "1" } }
```

**Going deeper.**

- **The captured log is sensitive data.** Encrypt it, restrict access to the audit team, and set a retention period that matches the regulation you're under.
- **Version everything that shaped an answer:** model version, system prompt version, retrieved documents. An explanation is only possible if you can reproduce the inputs.
