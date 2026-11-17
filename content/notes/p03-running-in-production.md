---
id: post-production
title: "Running LLMs in production"
short: "Running LLMs in production"
---
Getting a model to answer once is easy. Keeping it fast, affordable and correct under real traffic is the hard part. These are the steps I'd go through before calling an LLM deployment production-ready: choose the model with evidence, compress it carefully, size the GPU memory, tune the server, benchmark like production, and scale on the right signals.

### Choose the model with numbers, not vibes

**Scenario.** Two 8B models score within a point of each other on a public leaderboard. The team picks the one with the nicer demo. In production it misreads a quarter of the company's support tickets, which the other model handled fine. Leaderboards build a shortlist; your own evaluations make the decision.

![Choose the model with numbers, not vibes](diagrams/posts/p03-1.svg)

**What the picture shows.** An evaluation pipeline that turns a shortlist into a decision.

- **Shortlist from public leaderboards:** MMLU for knowledge, HellaSwag for commonsense, TruthfulQA for truthfulness. Use them only to narrow the field, because published scores can't be verified and may be inflated by training on the test sets.
- **Re-run the standard benchmarks yourself** with EleutherAI's **lm-evaluation-harness**, against the model as you'll serve it (same server, same quantization).
- **Add task-specific evaluations:** your own labelled examples, **Ragas** for RAG quality (faithfulness, context relevance), and **garak** for security probing (prompt injection, data leakage).
- **Run them as jobs, not by hand:** benchmarks take minutes to hours, so they run asynchronously in a pipeline, and the results are stored with the model version.
- **Decide on the combination:** quality on your tasks, safety results, and latency and cost per token from the benchmark step below.

**The YAML.** TrustyAI wraps lm-evaluation-harness as an `LMEvalJob` custom resource; this one evaluates a model already served by vLLM through its OpenAI-compatible endpoint.

```yaml
apiVersion: trustyai.opendatahub.io/v1alpha1
kind: LMEvalJob
metadata:
  name: eval-qwen-7b-fp8
  namespace: models
spec:
  model: local-completions                      # call a running server, not a local copy #!
  modelArgs:
  - { name: model, value: Qwen/Qwen2.5-7B-Instruct-FP8 }
  - { name: base_url, value: "http://qwen-7b.models.svc:8000/v1/completions" }
  - { name: tokenizer, value: Qwen/Qwen2.5-7B-Instruct }
  - { name: num_concurrent, value: "8" }
  taskList:
    taskNames: ["mmlu", "hellaswag", "truthfulqa_mc2"]   #!
  logSamples: true                              # keep answers for review
  batchSize: "8"
```

**Going deeper.**

- **Evaluate the deployment, not the checkpoint.** Quantization, the chat template and server settings all change results. Point the evaluation at the same server image and arguments you'll ship.
- **Keep a small, private "golden set"** of real tasks with expected answers. It's the evaluation that most predicts user satisfaction, and it can't leak into anyone's training data.

### Compress, then evaluate again

**Scenario.** An 8B model at 16 bits uses half of an L40S's memory for weights alone, leaving little room for concurrent users. The team wants to serve twice the traffic on the same GPUs without buying more.

![Compress, then evaluate again](diagrams/posts/p03-2.svg)

**What the picture shows.** What quantization changes on one GPU.

- **Weights shrink:** an 8B model needs about 16 GB at 16 bits, about 8 GB at FP8 or INT8, and about 4 GB at 4 bits.
- **The freed memory becomes KV cache,** so more requests fit at once, and throughput rises. On GPUs with native FP8 support it can roughly double, often keeping 99% or more of the accuracy.
- **Two conditions:** the server must have native kernels for the format (vLLM does for FP8, INT8 and common 4-bit formats), and the result must be **re-evaluated**, because a bad quantization can quietly ruin quality on some tasks while benchmark averages look fine.
- **Low-risk option:** pre-quantized, validated models published by the model's vendor or a trusted source, with their evaluation results.
- **KV cache can be quantized too:** an FP8 KV cache roughly doubles the number of tokens that fit, with a small quality cost to measure.

**The YAML.** The two common ways to serve FP8 with vLLM (container arguments only).

```yaml
# Option A: a pre-quantized model (vLLM detects the format from its config)
args: ["--model", "Qwen/Qwen2.5-7B-Instruct-FP8"]   # validated by its publisher #!

# Option B: quantize the weights at load time, and the KV cache too
args:
- --model=Qwen/Qwen2.5-7B-Instruct
- --quantization=fp8                                # dynamic FP8 on supported GPUs #!
- --kv-cache-dtype=fp8                              # ~2x the tokens in the same memory
```

**Going deeper.**

- **Re-run the evaluation job from the previous section** against the quantized deployment, and compare per task, not just the average.
- **Smaller model or quantized bigger model?** A quantized 14B model often beats a 16-bit 7B model in the same memory. Evaluate both before deciding.

### Do the memory math

**Scenario.** A model deployed on a 24 GB GPU works in testing, then starts rejecting and queueing requests at 30 concurrent users. The logs say the KV cache is full. Nobody had worked out how much memory each user's context needs.

![Do the memory math](diagrams/posts/p03-3.svg)

**What the picture shows.** Where one replica's GPU memory goes.

- **Weights:** parameters × bytes per parameter (16 GB for 8B at 16 bits).
- **Runtime overhead:** about 1 GB for the CUDA context, the server and its buffers.
- **Activations:** temporary memory during a forward pass, growing with the number of tokens processed at once.
- **KV cache (everything left):** the keys and values of every token of every active request. This decides how many users fit.
- **KV cache per token** = 2 (key and value) × layers × KV heads × head size × bytes per value. Models with **grouped-query attention** (Llama 3, Qwen 2.5) share KV heads, so they need several times less than older models.

**The code.** A small calculator for capacity planning. vLLM prints the same breakdown at startup.

```python
def kv_cache_gb(layers, kv_heads, head_dim, tokens, bytes_per_value=2):
    """Memory for the KV cache of `tokens` tokens (all active requests together)."""
    return 2 * layers * kv_heads * head_dim * bytes_per_value * tokens / 1e9

# Llama 3.1 8B: 32 layers, 8 KV heads (grouped-query attention), head size 128
per_token_kb = kv_cache_gb(32, 8, 128, 1) * 1e6
print(f"{per_token_kb:.0f} KB per token")                      # ~131 KB

gpu_gb, weights_gb, overhead_gb = 48, 16, 2                     # L40S, 8B at 16 bits
free_gb = gpu_gb * 0.9 - weights_gb - overhead_gb               # gpu-memory-utilization 0.9
tokens = free_gb / kv_cache_gb(32, 8, 128, 1)
print(f"{tokens:,.0f} tokens of KV cache ≈ {tokens / 8192:.0f} users at 8k context")

# The same model without grouped-query attention (32 KV heads) needs 4x more per token
print(f"{kv_cache_gb(32, 32, 128, 2048):.1f} GB for one 2k-token request")   # ~1.1 GB
```

**Going deeper.**

- **An older 8B model without grouped-query attention**, at 2k context, needs about 17 GB at one request and about 28 GB at ten. The same calculation for a grouped-query model gives several times more headroom.
- **vLLM warns when it has to preempt requests** because the KV cache is full. Treat that warning, and the `vllm:num_preemptions` counter, as a capacity alert.

### Tune the runtime for the workload

**Scenario.** A RAG application sends 12,000-token prompts, and a chat application sends 500-token prompts with long answers. Both run with vLLM's defaults. The RAG service is rejecting long prompts, and the chat service has idle GPU memory.

![Tune the runtime for the workload](diagrams/posts/p03-4.svg)

**What the picture shows.** The settings that matter most, and what each one trades.

- **`--gpu-memory-utilization`:** how much of the GPU vLLM may claim (default 0.9). Higher means more KV cache; leave headroom if anything else shares the GPU.
- **`--max-model-len`:** the longest context a request may use. It caps the KV cache each request can claim. Don't cut it below what your RAG prompts need, or requests fail.
- **`--max-num-seqs`:** how many requests run in one batch. Higher raises throughput until memory runs out, then causes preemptions.
- **`--tensor-parallel-size` / `--pipeline-parallel-size`:** spread the model over more GPUs, which also adds KV-cache room. Tensor parallelism within a node, pipeline across nodes.
- **`--cpu-offload-gb`:** lets a bigger model fit by keeping part of it in CPU memory, but it's too slow for production.
- **Prefix caching:** reuses the KV cache of shared prompt prefixes (system prompts, RAG templates). On by default in current vLLM; it matters a lot for agents and chat.

**The YAML.** Two tuned configurations of the same model for two workloads.

```yaml
# RAG: long prompts, short answers, fewer concurrent requests
args:
- --model=Qwen/Qwen2.5-7B-Instruct
- --max-model-len=32768                # long retrieved contexts must fit #!
- --max-num-seqs=32
- --gpu-memory-utilization=0.92
- --enable-prefix-caching              # the prompt template is shared across requests

# Chat: short prompts, long answers, many users
args:
- --model=Qwen/Qwen2.5-7B-Instruct
- --max-model-len=8192
- --max-num-seqs=256                   # many users per batch #!
- --gpu-memory-utilization=0.92
```

**Going deeper.**

- **Change one setting at a time and re-run the benchmark below.** The interactions (context length, batch size, memory) are hard to predict.
- **vLLM's defaults are good.** Most tuning is about matching `max-model-len` and `max-num-seqs` to your real prompt and answer lengths, which you get from production metrics.

### Benchmark like production

**Scenario.** A load test with `ab` reports 300 requests per second. In production the model handles 20 before latency doubles. The test sent identical, tiny requests, which tell you nothing about an LLM. Benchmarks need realistic prompt and output lengths, and LLM-specific metrics.

![Benchmark like production](diagrams/posts/p03-5.svg)

**What the picture shows.** A benchmark that runs in CI on every change to the model or its settings.

- **Deploy the candidate** on the same GPU type and count as production.
- **Run an LLM-aware load generator:** **GuideLLM**, **inference-perf** (from Kubernetes WG-Serving), MLPerf Inference or vLLM's own benchmark scripts. They report time to first token, inter-token latency and throughput, not just requests per second.
- **Sweep the load:** from one request at a time up to saturation, to find the highest throughput that still meets the latency targets.
- **Use realistic shapes:** prompt and output lengths drawn from production logs, or synthetic data matched to them.
- **Store and compare:** results go to a store, compared against the current production version; a regression fails the pipeline.
- **Feed capacity planning:** the measured "requests per replica at target latency" sets rate limits, autoscaling targets and the number of GPUs to buy.

**The YAML.** A Kubernetes Job that runs a GuideLLM sweep against a candidate deployment.

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: bench-qwen-7b-fp8
  namespace: models
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: guidellm
        image: registry.example.com/tools/guidellm:0.4    # pip install guidellm
        args:
        - benchmark
        - --target=http://qwen-7b-candidate.models.svc:8000
        - --rate-type=sweep                                # from idle to saturation #!
        - --max-seconds=120
        - --data=prompt_tokens=1500,output_tokens=300      # match production shapes #!
        - --output-path=/results/qwen-7b-fp8.json
        volumeMounts: [ { name: results, mountPath: /results } ]
      volumes:
      - name: results
        persistentVolumeClaim: { claimName: bench-results }
```

**Going deeper.**

- **Benchmark on the real hardware.** Results on a different GPU type, or a GPU shared with something else, don't transfer.
- **Report percentiles:** p50 and p99 of time to first token and inter-token latency at each load level. Averages hide the users who wait.

### Scale, start fast, route smartly

**Scenario.** Traffic doubles every morning. The HPA watches CPU, which stays flat while the GPU queue grows, so it never scales. When replicas are finally added by hand, a round-robin load balancer sends long conversations to replicas that don't have their context cached.

![Scale, start fast, route smartly](diagrams/posts/p03-6.svg)

**What the picture shows.** The three parts of running at scale, each covered in depth elsewhere on this site.

- **Scale on token signals, not CPU:** KEDA scales on the number of waiting requests, KV-cache use or time to first token from the model server's metrics. See [Placement, autoscaling and cost](#d03-3).
- **Start fast:** new replicas only help if they're ready in seconds to a few minutes: warm GPU capacity, pre-pulled images and fast weight loading. See [Networking (cost)](#d12-2) and [Getting model weights into pods](#post-model-data).
- **Route smartly:** inference-aware routing sends each request to the replica that has its prefix in the KV cache (or the right LoRA adapter loaded) and the shortest queue. See [kube-proxy: iptables vs. IPVS](#d09-4).
- **Disaggregated serving at the largest scale:** llm-d and NVIDIA Dynamo split prefill (compute-heavy, drives time to first token) from decode (memory-heavy, drives time per token). The two pools scale separately and share a distributed KV cache (LMCache, NIXL) over very fast networks. See [Latency and EFA](#d10-5).

**The YAML.** A KEDA ScaledObject that scales a model server on its queue length.

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: qwen-7b
  namespace: models
spec:
  scaleTargetRef: { name: qwen-7b }
  minReplicaCount: 2                       # warm floor on reserved capacity
  maxReplicaCount: 12
  cooldownPeriod: 600                      # GPUs are slow to come back; don't flap #!
  triggers:
  - type: prometheus
    metadata:
      serverAddress: http://prometheus.monitoring.svc:9090
      query: |
        avg(vllm:num_requests_waiting{namespace="models", service="qwen-7b"})   #!
      threshold: "5"                       # target: about 5 waiting requests per replica
```

**Going deeper.**

- **Pick the threshold from the benchmark:** the queue length at which latency still meets the target, from the sweep above.
- **Scale-down is the risky direction:** removing a replica drops its KV cache, so set a long cooldown and scale down slowly.
