---
id: post-observability
title: "Observing LLMs"
short: "Observing LLMs"
---
Kubernetes heals crashed pods, but it can't tell you that answers got slow, or wrong. LLM observability starts with the usual logs, metrics and traces, then switches to tokens instead of requests, and finally adds a layer most services never need: checking that the answers are any good.

### 1. The usual stack still applies

- **Logs** go to stdout and need a log store such as Loki. A vLLM startup log already tells you a lot: version and arguments, the model config, how much GPU memory the weights took and how much is left for the KV cache, and the routes it serves.
- **Metrics** are scraped in Prometheus/OpenMetrics format. KServe has annotations that turn scraping on for a model, plus an aggregator for multi-container pods.
- **Traces:** vLLM emits OpenTelemetry spans (send them to Jaeger, for example), and OpenTelemetry is defining semantic conventions specifically for generative AI.

### 2. Measure tokens, not requests

The GPU does the work, so CPU usage says little. The two phases also behave differently: prefill is compute-heavy and decode is memory-heavy. The core metrics:

- **Time to first token (TTFT):** what a chat user feels as "waiting". It reflects the prefill phase.
- **Time per output token (TPOT), or inter-token latency:** reading speed. About 4–5 tokens per second keeps up with a human reader.
- **Throughput:** prompt and generated tokens per second.
- **End-to-end latency** per request.
- **Queue:** `num_requests_waiting` and `num_requests_running`, the earliest sign of overload.

Turn them into SLIs and SLOs (for example "TPOT under 0.3 s for 99.9% of requests"), alert on them with PrometheusRules, and link a runbook. GPU health comes from vendor exporters (NVIDIA DCGM, AMD, Intel) alongside these.
→ See [What every scheduler relies on](#d02-8).

### 3. Fast isn't enough: watch quality

A model rarely crashes on bad input. It answers confidently and wrongly, which is a **hallucination**. Benchmarks before deployment help, but in production I'd sample 1–10% of responses into an asynchronous **LLM-as-a-judge** pipeline. Its quality scores are exported as Prometheus metrics, so they can be trended and alerted on like latency. For narrow tasks a cheap metric can work instead, such as ROUGE overlap for summaries. Judges work best with specific questions ("Does this reply include personal data?") rather than "Is this answer good?"

### 4. Responsible AI

Explanations are expensive to compute, so capture requests and responses with an inference logger and explain only the disputed cases later. **Bias** can appear at runtime through data drift even if training was clean, so monitor fairness metrics on protected attributes continuously (KServe with TrustyAI can do this). Input and output **guardrails** are covered in [Identity and model endpoints](#d13-6).
