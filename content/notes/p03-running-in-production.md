---
id: post-production
title: "Running LLMs in production"
short: "Running LLMs in production"
---
Getting a model to answer once is easy. Keeping it fast, affordable and correct under real traffic is the hard part. These are the steps I'd go through before calling an LLM deployment production-ready.

### 1. Choose the model with numbers, not vibes

Two models of the same size can behave very differently on your task. Use public leaderboards (MMLU for knowledge, HellaSwag for commonsense, TruthfulQA for truthfulness) only to build a shortlist, because published scores can't be verified. Then re-run the evaluations yourself with tools like EleutherAI's **lm-evaluation-harness**. Benchmarks take minutes to hours, so run them as asynchronous jobs in a pipeline. TrustyAI wraps the harness as an `LMEvalJob` custom resource. Add task-specific checks too: **Ragas** for RAG quality and **garak** for security probing.

### 2. Compress, then evaluate again

An 8B model at 16 bits needs about 16 GB for its weights alone. **Quantization** to FP8 or INT8 roughly halves that and can raise throughput substantially on the same GPU (up to about 2× on GPUs with native FP8), often keeping 99% or more of the accuracy. Two conditions apply: the runtime must have native kernels for the quantized format (vLLM does), and the result must be re-evaluated, because a bad quantization can quietly ruin quality. Pre-quantized, validated models are the low-risk option.

### 3. Do the memory math

A rough GPU memory budget for one replica:

- the weights (parameters × bytes per parameter);
- about 1 GB of runtime overhead;
- activations, which grow quickly with sequence length;
- the output tensor;
- everything left goes to the **KV cache**.

Example: an 8B fp16 model without grouped-query attention, with a 2k context and batch size 1, needs about 17 GB; at batch size 10 it needs about 28 GB. Models with grouped-query attention (like Llama 3) need several times less KV cache. vLLM prints this breakdown at startup, and warns when it has to evict KV-cache blocks because it ran out of room.

### 4. Tune the runtime for the workload

vLLM's defaults are good. The settings that matter most are:

- `gpu-memory-utilization`: how much of the GPU it may claim.
- `max-model-len`: caps the context, which sizes the KV cache. Don't cut it below what RAG prompts need.
- `max-num-seqs`: batch size, a trade between throughput and memory.
- `tensor-parallel-size` / `pipeline-parallel-size`: more GPUs, and more KV-cache room.
- `cpu-offload-gb`: lets bigger models fit, but is too slow for production.

### 5. Benchmark like production

Use LLM-aware load generators that report TTFT and inter-token latency: **GuideLLM**, **inference-perf** (from Kubernetes WG-Serving), MLPerf Inference, or vLLM's benchmark scripts. Run them in CI on the same GPU type and count as production, store the results, and use them for capacity planning, rate limits and autoscaling targets.

### 6. Scale, start fast, route smartly

Autoscaling, startup time and routing each have their own section with the details:

- [Placement, autoscaling and cost](#d03-3): scale on token metrics with KEDA.
- [Networking (cost)](#d12-2): from minutes to seconds to ready.
- [kube-proxy: iptables vs. IPVS](#d09-4): KV-cache- and LoRA-aware routing.
- [Latency and EFA](#d10-5): disaggregated serving.

At the largest scale, **disaggregated serving** (llm-d, NVIDIA Dynamo) splits prefill (compute-heavy, drives time to first token) from decode (memory-heavy, drives time per token). The two pools scale separately and share a distributed KV cache (LMCache, NIXL) over very fast networks.
