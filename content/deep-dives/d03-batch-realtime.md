---
id: d-batch-realtime
title: "Batch and real-time on one cluster"
short: "EKS Batch and Realtime"
---
One cluster runs a latency-critical web API next to cost-driven batch jobs. This deep dive follows the isolation stack from node tiers down to CPU, memory, network and disk.

### Workloads and node tiers

**Scenario.** Every evening the batch wave arrives and the API's p99 latency jumps above 100 ms.

![Workloads and node tiers](diagrams/sections/d03-1.svg)

**What the picture shows.** Web services are latency-sensitive and need stability, while batch is retry-capable and opportunistic. Each gets its own tier: on-demand, tainted nodes for web (with placeholder pods for headroom), diversified Spot capacity for batch, and a shared, untainted tier for utilization.

**Going deeper.** For LLMs, the latency-critical tier is the **model server**: vLLM, TGI, SGLang or NVIDIA NIM, each exposing an OpenAI-compatible API behind a Service. Large models need whole, dedicated GPUs and minutes to load, so I run them as plain Deployments (KServe's *Standard* mode) on tainted GPU nodes with warm capacity, rather than relying on scale-to-zero.

AI applications follow the same split:

- **Chat-style apps** are synchronous: the user waits, so the orchestrator and model server stay warm and close together.
- **Event-driven AI backends** (for example, scoring every new order for fraud) run asynchronously off a message bus. They can scale from zero with KEDA or Knative, and can live in the cheaper tier.

### Priority, preemption and QoS

**Scenario.** The cluster is full when a traffic spike hits. Who gets evicted, and who decides?

![Priority, preemption and QoS](diagrams/sections/d03-2.svg)

**What the picture shows.**

- Two different mechanisms.
- **PriorityClasses** are the scheduler's tool, applied cluster-wide (`web-critical` preempts, `batch-low` never does).
- **QoS** (Guaranteed > Burstable > BestEffort) is the kubelet's tool, applied on one node under resource pressure.

**Going deeper.**

- Kueue adds a job-level layer on top of these two.
- WorkloadPriorityClasses decide admission order, and preemption can reclaim quota that another team borrowed.
- Which hardware a job gets and who goes first are separate decisions.

### Placement, autoscaling and cost

**Scenario.** Batch costs are climbing even though most nodes look half empty.

![Placement, autoscaling and cost](diagrams/sections/d03-3.svg)

**What the picture shows.**

- Affinity for web, anti-affinity for batch, and requests and limits per workload type.
- Autoscaling uses HPA for web (fast up, slow down) and Karpenter for batch (diversified Spot, consolidation).
- Spot interruptions are handled by checkpointing, and the key alerts are pending pods, queue depth and p99.

**Going deeper.**

- Packing pairs with the **descheduler**, which periodically evicts pods so they can be placed again on a tighter set of nodes.
- Its strategy must match the scheduler's: `HighNodeUtilization` goes with MostAllocated, and `LowNodeUtilization` goes with LeastAllocated.
- Mix them and you get an eviction loop.
- For training, every eviction costs a checkpoint and restart, so PodDisruptionBudgets, maintenance windows and excluded namespaces matter.
- Model-parallel inference scales in *whole groups* (a 4-GPU replica can't become 3), so autoscale on requests per second or latency with HPA or KEDA, and keep replicas warm because model loading is slow.

- LLM servers break the usual autoscaling signals.
- The default HPA watches CPU and memory, but the work happens on the GPU.
- Request-based scaling (Knative's KPA) treats a 10-token answer and a 10,000-token answer as the same.
- What works is **KEDA** on the model server's own metrics, such as `vllm:num_requests_waiting` or `vllm:num_requests_running`, or latency targets (TTFT, time per output token).
- Scale-to-zero rarely fits: a new replica can take minutes to pull and load the model, so keep a warm minimum and alert on token latency and queue depth rather than CPU.

### CPU: shares and throttling

**Scenario.** The API's average latency looks fine, but p99 spikes every few seconds.

![CPU: shares and throttling](diagrams/sections/d03-4.svg)

**What the picture shows.**

- Requests become CPU shares, which split the CPU under contention.
- Limits become a quota per 100 ms window.
- A multi-threaded pod can use up its quota early in the window and then freeze until the next one, which is where the p99 jitter comes from.

### Kernel vs. Kubernetes

**Scenario.** "Why do requests matter if the kernel shares CPU fairly anyway?"

![Kernel vs. Kubernetes](diagrams/sections/d03-5.svg)

**What the picture shows.**

- Kubernetes sets the policy, the kubelet translates it into cgroups, and the kernel enforces it.
- The one thing the kernel can't do is stop a node from being overpacked.
- Only the scheduler's request accounting does that.

### Memory: who gets killed

**Scenario.** A node runs out of memory. Which pod dies first, and why that one?

![Memory: who gets killed](diagrams/sections/d03-6.svg)

**What the picture shows.**

- Memory can't be compressed: the OOM killer runs at container or node level.
- QoS sets `oom_score_adj` (BestEffort 1000, Guaranteed -997, Burstable in between, depending on its memory request).
- Among Burstable pods, the kubelet evicts the one furthest over its request first.

### Network: the unmanaged resource

**Scenario.** A backup pod starts a 10 Gbps transfer and the API on the same node starts dropping connections, yet CPU and memory look perfect.

![Network: the unmanaged resource](diagrams/sections/d03-7.svg)

**What the picture shows.** Kubernetes has no network requests or limits. The fixes are Linux `tc` shaping, CNI bandwidth annotations, and instance types with separate bandwidth for EBS.

### Storage I/O: the other unmanaged resource

**Scenario.** A log archiver saturates a volume, and the database pod sharing it goes from 1 ms to 200 ms latency.

![Storage I/O: the other unmanaged resource](diagrams/sections/d03-8.svg)

**What the picture shows.**

- EBS throttles per volume, not per pod, and Kubernetes doesn't expose the `blkio` controller.
- The symptoms are I/O wait, processes stuck in the D state, and failing liveness probes.
- The fixes are dedicated volumes per critical pod, EBS-optimized instances, and node-level guardrails.