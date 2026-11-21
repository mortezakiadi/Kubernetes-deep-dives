---
id: design-llm-serving
title: "Design 21: Deploying and tuning high-throughput LLM serving"
short: "High-throughput LLM serving"
---
This design answers one question: *how do we deploy LLMs on Kubernetes so they serve as many tokens per GPU as possible while still meeting latency targets?* The levers are well known: tensor, data and expert parallelism; splitting prefill from decode; offloading KV cache to host memory; and FP8 or FP4 quantization. The hard part is knowing which lever helps which workload, applying them together without breaking each other, and proving the result with benchmarks that reflect real traffic. vLLM is the engine here. SGLang and TensorRT-LLM offer the same levers under different names.

The short version: *start from the workload and the SLO, not from the features*. Measure input and output lengths and how much prompt prefix requests share. Define throughput as *goodput*: tokens per second per GPU from requests that met their time-to-first-token and inter-token-latency targets. Pick parallelism by model shape: tensor parallelism within a node for dense models, data-parallel attention with expert parallelism for large mixture-of-experts models. Quantize with an accuracy gate. Add KV offload when prefix reuse is high, and prefill/decode disaggregation when long prompts hurt decode latency. Route requests with a cache-aware endpoint picker. Every change is a benchmarked experiment against the same traffic replay.

### The use case and requirements

**Scenario.** The platform serves two models. A 70B dense model handles chat and a coding assistant on 64 H100s. A large mixture-of-experts model (about 670B total parameters, about 37B active per token) serves agent workloads on 32 B200s. Traffic doubled in a quarter. Coding requests carry 20,000-token prompts that share most of their prefix, and during spikes their prefills make chat's token streaming stutter. Finance asks for 40% more requests per GPU before any new hardware.

![40% more per GPU](diagrams/designs/concepts/ds21-c1.svg)

**What the platform must deliver.**

- **Latency SLOs per product:** for chat, p95 time to first token (TTFT) under 1 s and p95 inter-token latency (ITL) under 50 ms. For the coding assistant, p95 TTFT under 3 s with 20k-token prompts.
- **Goodput as the throughput metric:** tokens per second per GPU from requests that met their SLO.
- **Model-aware parallelism:** a layout per model and GPU type, checked before deployment.
- **Quantization with an accuracy gate:** no quantized model ships unless it passes the evaluation suite within agreed deltas.
- **KV cache that lasts:** shared prefixes stay cached beyond GPU memory.
- **Isolation of prefill from decode** where long prompts hurt streaming latency.
- **Repeatable tuning:** every configuration change benchmarked against replayed production traffic.

**Out of scope:** model loading speed ([Design 20](#design-distributed-training)), training-side model export, and multi-cluster routing ([Design 02](#design-multicluster)).

### Characterize the workload and the SLO

**Scenario.** The team tuned the 70B deployment with a benchmark of 512-token prompts and 128-token answers. Production chat had a median prompt of 3,000 tokens and answers of 600, so the tuned settings were wrong for real traffic.

![Workload first](diagrams/designs/concepts/ds21-c2.svg)

![Characterize the workload and the SLO](diagrams/designs/ds21-1.svg)

**What the picture shows.** The measurements that decide which levers matter.

- **Input and output length distributions per product:** long inputs make prefill dominant. Long outputs make decode and KV memory dominant.
- **Prefix sharing:** the fraction of each prompt's tokens that already appeared in a recent request (system prompts, documents, repository context). High sharing makes prefix caching and KV offload valuable.
- **Arrival pattern:** bursts determine queueing, and so the headroom needed to meet TTFT.
- **KV bytes per token:** from the model's architecture. This sets how many tokens fit on a GPU and so the batch size decode can sustain.
- **SLO per product:** TTFT and ITL percentiles. Goodput counts only requests that meet both.

**The code.** KV-cache size per token and the token capacity of a deployment (Python).

```python
def kv_bytes_per_token(layers: int, kv_heads: int, head_dim: int, dtype_bytes: float) -> float:
    return 2 * layers * kv_heads * head_dim * dtype_bytes          # K and V for every layer

def kv_capacity_tokens(gpus: int, hbm_gb: float, weights_gb: float, kv_per_token: float,
                       util: float = 0.90, activation_gb_per_gpu: float = 6.0) -> int:
    free = gpus * (hbm_gb * util - activation_gb_per_gpu) - weights_gb
    return int(free * 1e9 / kv_per_token)

# 70B dense model: 80 layers, 8 KV heads (GQA), head_dim 128
bf16 = kv_bytes_per_token(80, 8, 128, 2)       # 327,680 B ≈ 320 KiB per token
fp8 = kv_bytes_per_token(80, 8, 128, 1)        # half that #!
print(kv_capacity_tokens(4, 80, 140, bf16))    # BF16 weights + BF16 KV on 4 × H100
print(kv_capacity_tokens(4, 80, 70, fp8))      # FP8 weights + FP8 KV: about 4× the tokens
```

**Going deeper.**

- **Measure from logs, not guesses:** gateway logs with token counts and prefix hashes give the distributions. Replay them for every benchmark ([Design 15](#design-eval-infra)).
- **Goodput keeps tuning honest:** raw tokens per second always improves with bigger batches until latency collapses. Goodput peaks where the SLO starts to break.

### Parallelism for dense models

**Scenario.** The 70B model ran with tensor parallelism of 8 on every node, because "more GPUs per replica is faster". At production load, eight TP=2 replicas would have served 50% more requests per node within the SLO.

![Dense parallelism](diagrams/designs/concepts/ds21-c3.svg)

![Parallelism for dense models](diagrams/designs/ds21-2.svg)

**What the picture shows.** How tensor, pipeline and data parallelism trade latency for throughput.

- **Tensor parallelism (TP):** splits each layer across GPUs in a node, with an all-reduce per layer over NVLink. Higher TP lowers per-token latency and frees memory for KV cache, but adds communication and cuts the number of replicas.
- **Data parallelism (DP), meaning more replicas:** each replica serves its own requests. This scales throughput linearly with no communication between replicas.
- **Pipeline parallelism (PP):** only when a model doesn't fit in one node. It adds latency per stage and needs enough concurrent requests to keep stages busy.
- **The rule of thumb:** use the smallest TP that fits the weights plus enough KV cache for the target batch, then add replicas. Increase TP only when ITL misses the SLO.
- **Quantization changes the answer:** FP8 halves weight memory, so a 70B model fits on fewer GPUs with more KV room.

**The code.** Choosing the smallest TP that leaves enough KV cache for the target concurrency (Python).

```python
def choose_tp(weights_gb, kv_per_token, target_concurrency, mean_context,
              hbm_gb=80, options=(1, 2, 4, 8)):
    need = target_concurrency * mean_context                     # tokens resident at the target load
    for tp in options:
        cap = kv_capacity_tokens(tp, hbm_gb, weights_gb, kv_per_token)
        if cap >= need * 1.2:                                     # 20% headroom for bursts #!
            return {"tp": tp, "kv_tokens": cap, "replicas_per_node": 8 // tp}
    return {"tp": None, "reason": "use PP across nodes or quantize"}

print(choose_tp(70, fp8, target_concurrency=64, mean_context=3600))   # FP8 70B, chat traffic
```

**Going deeper.**

- **Benchmark the two closest layouts:** the sizing gives a candidate. A replayed-traffic benchmark of TP=2 and TP=4 decides.
- **Pin the layout to the GPU type:** the same model may use TP=4 on H100 and TP=2 on B200, so the deployment templates are keyed by both.

### Expert parallelism for mixture-of-experts models

**Scenario.** The MoE model ran with TP=8 per node across 4 nodes, splitting every expert across 32 GPUs. Expert layers spent most of their time in all-reduce, and the batch per GPU was tiny.

![Wide expert parallelism](diagrams/designs/concepts/ds21-c4.svg)

![Expert parallelism for mixture-of-experts models](diagrams/designs/ds21-3.svg)

**What the picture shows.** Data-parallel attention with expert parallelism (wide EP).

- **Attention layers:** replicated in data-parallel ranks (TP=1 or a small TP), each rank handling its own requests and KV cache. Latent-attention models have small KV caches, which favors this.
- **Expert layers:** each GPU holds a subset of experts. Tokens are sent to the GPUs holding their chosen experts and back with all-to-all (dispatch and combine). The EP size is TP × DP.
- **Communication kernels:** DeepEP high-throughput kernels suit prefill, and low-latency kernels with CUDA-graph support suit decode. Multi-node NVLink systems have their own all-to-all backends.
- **Expert load balancing (EPLB):** popular experts are replicated and placement is rebalanced from observed load, so one hot GPU doesn't set the step time.
- **On Kubernetes:** a LeaderWorkerSet runs each serving group (leader plus workers across nodes) as a unit that's created, restarted and scaled together, with topology constraints so the group stays on one fabric block.

**The YAML.** A 32-GPU wide-EP serving group as a LeaderWorkerSet (vLLM, 4 nodes × 8 GPUs).

```yaml
apiVersion: leaderworkerset.x-k8s.io/v1
kind: LeaderWorkerSet
metadata: { name: moe-decode, namespace: serving }
spec:
  replicas: 1
  leaderWorkerTemplate:
    size: 4                                                # 1 leader + 3 workers = 32 GPUs
    restartPolicy: RecreateGroupOnPodRestart               # one failed pod restarts the whole group
    leaderTemplate:
      metadata: { labels: { role: leader, app: moe-decode } }
      spec:
        containers:
        - name: vllm
          image: vllm/vllm-openai:v0.32.0
          command: ["vllm", "serve"]
          args: ["deepseek-ai/DeepSeek-V3-0324",
                 "--tensor-parallel-size=1", "--data-parallel-size=32", "--data-parallel-size-local=8",
                 "--data-parallel-address=$(LWS_LEADER_ADDRESS)", "--data-parallel-rpc-port=13345",
                 "--enable-expert-parallel", "--all2all-backend=deepep_low_latency",   # EP = 32 #!
                 "--enable-eplb", "--kv-cache-dtype=fp8"]
          resources: { limits: { nvidia.com/gpu: 8 } }
    workerTemplate:
      spec:
        containers:
        - name: vllm
          image: vllm/vllm-openai:v0.32.0
          command: ["sh", "-c"]
          args: ["vllm serve deepseek-ai/DeepSeek-V3-0324 --headless --tensor-parallel-size=1
                  --data-parallel-size=32 --data-parallel-size-local=8
                  --data-parallel-start-rank=$((LWS_WORKER_INDEX * 8))
                  --data-parallel-address=$LWS_LEADER_ADDRESS --data-parallel-rpc-port=13345
                  --enable-expert-parallel --all2all-backend=deepep_low_latency --enable-eplb --kv-cache-dtype=fp8"]
          resources: { limits: { nvidia.com/gpu: 8 } }
```

**Going deeper.**

- **Prefill and decode want different EP setups:** prefill uses large batches and high-throughput kernels, decode uses small per-step batches and low-latency kernels. This is one of the strongest reasons to disaggregate MoE serving.
- **The fabric matters as much as the GPUs:** all-to-all over RDMA needs GPUDirect RDMA and a non-blocking fabric. Check the topology placement ([Design 17](#design-gpu-scheduling)) before tuning kernels.

### Engine tuning: batching, scheduling and caching

**Scenario.** The engine ran with defaults. With 20k-token prompts arriving, each prefill took over a whole engine step, and all the decoding requests in the batch waited for it.

![Engine knobs](diagrams/designs/concepts/ds21-c5.svg)

![Engine tuning: batching, scheduling and caching](diagrams/designs/ds21-4.svg)

**What the picture shows.** The engine settings that matter most, and what each trades.

- **Chunked prefill and the token budget:** `--max-num-batched-tokens` caps the tokens per step. Long prompts are split into chunks mixed with decode tokens. A smaller budget protects ITL, and a larger one improves prefill throughput and TTFT.
- **Concurrency cap:** `--max-num-seqs` limits running requests. Too high and KV pressure causes preemptions. Too low and the GPU is underused.
- **Memory split:** `--gpu-memory-utilization` sets how much HBM goes to weights, activations and KV cache.
- **Prefix caching:** reuses KV blocks for shared prefixes. It's nearly free when there's no sharing and large when there is.
- **CUDA graphs, compilation and async scheduling:** reduce CPU overhead per step. This matters most for small decode batches and fast GPUs.
- **Speculative decoding:** a draft model or multi-token-prediction head proposes tokens that the main model verifies. It lowers ITL at low load, but its benefit shrinks at high batch sizes.

**The YAML.** Engine configuration for the 70B chat deployment (vLLM config file, mounted from a ConfigMap).

```yaml
# /etc/vllm/chat-70b-h100.yaml
model: nvidia/Llama-3.3-70B-Instruct-FP8
tensor-parallel-size: 2
gpu-memory-utilization: 0.92
kv-cache-dtype: fp8
max-model-len: 32768
max-num-seqs: 192
max-num-batched-tokens: 4096          # protects ITL during long prefills; tuned by sweep #!
enable-prefix-caching: true
async-scheduling: true
speculative-config: '{"method": "eagle3", "model": "registry.example.com/drafts/llama-70b-eagle3", "num_speculative_tokens": 3}'
```

**Going deeper.**

- **Tune one setting at a time against replayed traffic:** the token budget, max sequences and speculative decoding interact. Changing all three at once makes the result impossible to explain.
- **Watch preemptions:** vLLM reports preempted requests when KV cache runs out. A rising count means the concurrency cap is too high for the memory.

### Prefill/decode disaggregation

**Scenario.** Even with chunked prefill, chat ITL spiked during coding-assistant bursts. The two phases shared every GPU, and a 4096-token budget couldn't satisfy both a 3-second TTFT for 20k-token prompts and a 50 ms ITL for chat.

![P/D disaggregation](diagrams/designs/concepts/ds21-c6.svg)

![Prefill/decode disaggregation](diagrams/designs/ds21-5.svg)

**What the picture shows.** Separate pools for prefill and decode, with KV cache transferred between them.

- **Prefill pool:** processes prompts with large token budgets and the parallelism best for compute-heavy work. It produces the KV cache and the first token.
- **KV transfer:** the decode worker pulls the prompt's KV blocks from the prefill worker over RDMA (NIXL with UCX), asynchronously, without staging through CPUs.
- **Decode pool:** generates tokens with large batches and settings that minimize ITL, never interrupted by prefills.
- **Independent tuning and scaling:** TTFT is tuned on the prefill pool and ITL on the decode pool. The prefill-to-decode ratio (for example, 1:2 or 2:3) follows the input/output mix.
- **When it doesn't pay:** short prompts, small models or slow networks. Then the transfer costs more than the interference it removes. The vLLM docs note that disaggregation targets latency control, not raw throughput.

**The YAML.** The prefill and decode engine arguments (vLLM with NixlConnector; deployed as two Deployments or LeaderWorkerSets).

```yaml
# prefill pool
command: ["vllm", "serve"]
args: ["nvidia/Llama-3.3-70B-Instruct-FP8", "--tensor-parallel-size=4",
       "--max-num-batched-tokens=32768", "--enable-prefix-caching", "--kv-cache-dtype=fp8",
       "--kv-transfer-config", '{"kv_connector":"NixlConnector","kv_role":"kv_both"}']
---
# decode pool
command: ["vllm", "serve"]
args: ["nvidia/Llama-3.3-70B-Instruct-FP8", "--tensor-parallel-size=2",
       "--max-num-seqs=256", "--max-num-batched-tokens=2048", "--kv-cache-dtype=fp8",
       "--kv-transfer-config", '{"kv_connector":"NixlConnector","kv_role":"kv_both"}']   # same connector #!
env:
- { name: UCX_TLS, value: "rc,cuda_copy,cuda_ipc" }     # RDMA + GPU memory transports
```

**Going deeper.**

- **Different TP sizes on each side work:** the connector handles layout differences, which allows prefill at TP=4 for TTFT and decode at TP=2 for replica count.
- **Measure transfer time:** KV size per request (prompt tokens × KV bytes per token) divided by network bandwidth is the minimum added to TTFT. A 20k-token prompt in FP8 is about 3.3 GB for the 70B model.

### Routing: cache-aware and phase-aware

**Scenario.** A round-robin Service spread requests evenly, so each coding session's 20k-token prefix was recomputed on whichever replica it landed on. The prefix-cache hit rate was 9% even though 80% of tokens were shared.

![Cache-aware routing](diagrams/designs/concepts/ds21-c7.svg)

![Routing: cache-aware and phase-aware](diagrams/designs/ds21-6.svg)

**What the picture shows.** An inference-aware gateway that picks the endpoint per request.

- **InferencePool:** a Gateway API inference-extension resource grouping the model-server pods, with an endpoint picker (EPP) that chooses one per request.
- **Signals the picker uses:** which pod likely holds the request's prefix in its KV cache, queue depth, KV-cache usage and, with disaggregation, which prefill and decode pods to pair.
- **Session affinity by prefix, not by user:** the same document or repository prefix lands on the same pods, keeping hit rates high without pinning users.
- **Priority and fairness:** critical traffic (interactive chat) is admitted ahead of sheddable traffic (batch evaluation) when the pool is saturated.
- **llm-d:** packages this pattern (vLLM, the inference gateway's endpoint picker, P/D-aware scheduling and KV-cache indexing) as a Kubernetes-native stack.

**The YAML.** An InferencePool with an endpoint picker, behind an HTTPRoute (Gateway API Inference Extension).

```yaml
apiVersion: inference.networking.k8s.io/v1
kind: InferencePool
metadata: { name: chat-70b, namespace: serving }
spec:
  selector: { matchLabels: { app: chat-70b } }
  targetPorts: [{ number: 8000 }]
  endpointPickerRef:                                   # prefix-cache, load and P/D-aware picking #!
    name: chat-70b-epp
    port: { number: 9002 }
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata: { name: chat-70b, namespace: serving }
spec:
  parentRefs: [{ name: inference-gateway }]
  rules:
  - matches: [{ path: { type: PathPrefix, value: /v1 } }]
    backendRefs: [{ group: inference.networking.k8s.io, kind: InferencePool, name: chat-70b }]
```

**Going deeper.**

- **Prefix-aware routing and load balancing pull in opposite directions:** sending everything with the same prefix to one pod overloads it. The picker scores both and spills over when queues grow.
- **Hash prefixes the way the engine does:** routing by block hashes that match the engine's prefix-cache blocks gives accurate hit predictions. Routing by raw text approximations gives looser ones.

### KV cache offload

**Scenario.** The coding assistant's sessions came back every few minutes with the same repository context. By then the prefix had been evicted from GPU memory, so every return meant recomputing 20,000 tokens of prefill.

![KV offload](diagrams/designs/concepts/ds21-c8.svg)

![KV cache offload](diagrams/designs/ds21-7.svg)

**What the picture shows.** KV cache in tiers beyond GPU memory.

- **GPU HBM:** the working set for running requests.
- **Host memory (CPU DRAM):** evicted blocks are copied to pinned host memory instead of being discarded. A later hit reloads them over PCIe or NVLink-C2C, much faster than recomputing a long prefill.
- **Shared or local storage:** a larger, slower tier for long-lived prefixes across replicas (LMCache, shared filesystems or a KV store).
- **When it pays:** long shared prefixes that return after eviction, such as agent sessions, coding context and RAG documents. When nothing is reused, it's pure overhead.
- **Routing must know about the tiers:** a picker that only sees GPU blocks undervalues pods with the prefix in host memory.

**The YAML.** Native CPU offload in vLLM for the coding deployment.

```yaml
command: ["vllm", "serve"]
args: ["nvidia/Llama-3.3-70B-Instruct-FP8", "--tensor-parallel-size=2",
       "--enable-prefix-caching",                       # offload builds on prefix caching
       "--kv-offloading-backend=native",
       "--kv-offloading-size=400"]                      # GiB of host memory for KV, across TP ranks #!
resources:
  limits: { nvidia.com/gpu: 2, memory: 560Gi }          # budget pinned host memory in the pod limit
```

**Going deeper.**

- **Size host memory per pod:** with FP8 KV at about 160 KiB per token, 400 GiB holds about 2.6 million tokens, or about 130 cached 20k-token prefixes per replica.
- **Mind PCIe bandwidth:** reloading many blocks per step competes with other traffic. Watch offload store and reload rates against throughput.

### FP8 and FP4 quantization with an accuracy gate

**Scenario.** An FP4 version of the 70B model doubled throughput on B200s in a benchmark. A week after release, the coding team found it failed noticeably more unit tests on long functions. Nobody had evaluated it on code.

![Quantization gate](diagrams/designs/concepts/ds21-c9.svg)

![FP8 and FP4 quantization with an accuracy gate](diagrams/designs/ds21-8.svg)

**What the picture shows.** The quantization options and how a quantized model earns its way to production.

- **FP8 (W8A8) on Hopper and Blackwell:** weights and activations in FP8 with per-tensor or per-channel scales. Typically near-lossless for large models. Halves weight memory and uses FP8 tensor cores.
- **FP4 (NVFP4, W4A4) on Blackwell:** 4-bit values with fine-grained block scales. About a quarter of BF16 weight memory, with the largest throughput gains. Accuracy needs checking per task, and some layers are often kept at higher precision.
- **FP8 KV cache:** halves KV memory, doubling the tokens that fit. Its accuracy effect is separate from weight quantization and is tested on its own.
- **Producing checkpoints:** with calibration data that looks like production traffic (NVIDIA ModelOpt, LLM Compressor), recorded in the registry with the recipe used.
- **The gate:** the full evaluation suite, per product and per task family, against the BF16 reference, with agreed maximum deltas. A model that fails any family doesn't ship for that product.

**The code.** An accuracy gate comparing a quantized model with its reference, per task family (Python).

```python
GATES = {"chat_quality": -0.5, "math": -1.0, "code_pass@1": -1.0, "long_context_recall": -1.5, "tool_use": -1.0}

def quantization_gate(reference: dict, candidate: dict, ci: dict) -> dict:
    """reference/candidate: score per task family (points); ci: 95% half-width of the difference."""
    results = {}
    for family, max_drop in GATES.items():
        delta = candidate[family] - reference[family]
        worst = delta - ci[family]                         # pessimistic bound #!
        results[family] = {"delta": round(delta, 2), "worst_case": round(worst, 2), "pass": worst >= max_drop}
    results["ship"] = all(r["pass"] for k, r in results.items() if k in GATES)
    return results
```

**Going deeper.**

- **Calibrate on your traffic:** calibration sets of generic web text under-represent code and long contexts. Sample them from production prompts with privacy filters applied.
- **Gate per product, not globally:** an FP4 model can ship for chat and stay FP8 for the coding assistant. The registry and routing support different variants per product.

### Benchmarking and the tuning loop

**Scenario.** Three teams each reported a different "best" configuration for the same model. Each had used a different benchmark tool, prompt set and concurrency, and none had measured latency percentiles.

![Goodput sweep](diagrams/designs/concepts/ds21-c10.svg)

![Benchmarking and the tuning loop](diagrams/designs/ds21-9.svg)

**What the picture shows.** One benchmark procedure for every serving change.

- **Traffic replay:** sampled production request shapes (lengths, prefix sharing, arrival bursts) per product, versioned like a dataset.
- **Sweep:** request rate or concurrency from low to past saturation, recording TTFT and ITL percentiles and throughput at each point.
- **Goodput curve:** tokens per second per GPU from requests within SLO. The configuration's score is the peak of this curve.
- **Comparison:** the candidate against the current production configuration on the same hardware and replay, repeated to estimate noise.
- **Tools:** an inference-benchmark harness (for example, Inference Perf or vLLM's serving benchmark) driven by the replay ([Design 15](#design-eval-infra)).

**The code.** Finding the goodput peak from a load sweep (Python).

```python
import numpy as np

def goodput(results, ttft_slo_s, itl_slo_ms, gpus):
    """results: list of dicts per load level, each with per-request ttft_s, itl_ms_p95, output_tokens, duration_s."""
    curve = []
    for level in results:
        ok = [r for r in level["requests"] if r["ttft_s"] <= ttft_slo_s and r["itl_ms_p95"] <= itl_slo_ms]
        tokens = sum(r["output_tokens"] for r in ok)
        curve.append({"rate": level["rate"], "goodput_tok_s_gpu": tokens / level["duration_s"] / gpus,
                      "slo_attainment": len(ok) / len(level["requests"])})
    best = max(curve, key=lambda c: c["goodput_tok_s_gpu"] if c["slo_attainment"] >= 0.95 else 0)   #!
    return best, curve
```

**Going deeper.**

- **Report the curve, not one number:** a configuration that peaks higher but falls off faster past its peak is risky under bursty traffic.
- **Benchmark on the target GPUs and network:** disaggregation and wide EP results depend on the fabric. Numbers from a different cluster don't transfer.

### Autoscaling and operations

**Scenario.** The HPA scaled on GPU utilization, which stays near 100% on a busy LLM server at any load. Replicas never scaled down overnight, and during the morning spike new replicas took 9 minutes to become ready.

![Autoscaling](diagrams/designs/concepts/ds21-c11.svg)

![Autoscaling and operations](diagrams/designs/ds21-10.svg)

**What the picture shows.** Scaling on signals that track latency, with pools scaled separately.

- **Scaling signals:** queue depth (waiting requests), KV-cache usage and observed TTFT. GPU utilization doesn't track load for LLM servers.
- **Scale pools independently:** the prefill pool on prompt-token arrival rate and TTFT, the decode pool on running requests and KV usage.
- **Fast readiness:** streamed or cached model loading and pre-pulled images keep scale-up within minutes ([Design 20](#design-distributed-training)).
- **Rollouts:** a new configuration or quantized variant gets a slice of traffic through the gateway, with SLO and quality canaries before it takes all traffic.
- **Dashboards:** TTFT and ITL percentiles per product, goodput, prefix-cache hit rate by tier, preemptions, KV transfer time and expert load balance.

**Going deeper.**

- **Keep warm capacity for bursts:** scale-up is never instant for a 70B model. A small buffer of ready replicas, sized from the arrival pattern, protects TTFT.
- **Scale-down must drain:** in-flight streams finish before a pod stops, and for disaggregated pools the decode side drains last.
- **How KServe does this today (checked November 2026):** its LLMInferenceService scales either through the Workload Variant Autoscaler, which reads KV-cache use and queue depth, or through KEDA triggers you write yourself. Since KServe 0.21, KEDA can take it to zero replicas on either path; the HPA option stops at one ([autoscaling guide](https://kserve.github.io/website/docs/model-serving/generative-inference/llmisvc/autoscaling/llmisvc-autoscaling), [0.21 release](https://kserve.github.io/website/blog/kserve-0.21-release)). Zero replicas frees the pods, not the machines. The GPU node goes away only if the node autoscaler removes it, and the next request then pays a full cold start.
- **A canary with KServe:** run the old and the new version as two LLMInferenceServices in the same routing group, each with a weight. Weights are proportions, so 9 and 1 give a 90/10 split, and you change only the version you're moving. KServe's own guide starts the new version at weight 1 rather than 0, because raising a weight from 0 later may cause brief connection failures. Each version also has a direct path for test traffic, and stopping the old version promotes the new one while keeping the old one for rollback ([canary guide](https://kserve.github.io/website/docs/model-serving/generative-inference/llmisvc/canary-rollout)). KServe moves the traffic; the quality and latency checks that decide each step are still yours.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds21-c12.svg)

- **Phase 1, measure (weeks 1–3):** request-shape logging, replay datasets per product, goodput definition and the benchmark harness. Baseline every deployment.
- **Phase 2, quick wins (weeks 3–6):** FP8 weights and KV cache with the accuracy gate, right-sized TP and more replicas, engine settings tuned by sweep, and cache-aware routing.
- **Phase 3, MoE (weeks 6–10):** data-parallel attention with wide EP and EPLB on LeaderWorkerSets, with topology-aware placement.
- **Phase 4, structural (weeks 8–14):** prefill/decode disaggregation for the coding assistant and MoE, KV offload for long-context products, and NVFP4 variants where the gate passes.

**Trade-offs we accepted.**

- **More moving parts:** disaggregation, offload and an endpoint picker each add components to operate and debug.
- **Per-product variants:** different quantizations and layouts per product cost registry, testing and capacity planning effort.
- **Host memory for KV offload** makes serving nodes more expensive to configure.
- **Benchmarks before every change** slow down tuning, and make it explainable.

**Open questions.**

- **Dynamic P/D ratios:** can the prefill-to-decode split adjust automatically as the traffic mix shifts during the day?
- **Cross-replica KV sharing:** when does a shared KV store across replicas beat cache-aware routing with per-pod offload?
- **Lower precision KV cache:** how far can FP4 KV go before long-context recall degrades?
- **Speculative decoding with disaggregation:** where should the draft model run, and how does it change the P/D ratio?
