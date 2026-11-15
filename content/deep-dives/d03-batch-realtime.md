---
id: d-batch-realtime
title: "Batch and real-time on one cluster"
short: "EKS Batch and Realtime"
---
One cluster runs a latency-critical web API next to cost-driven batch jobs. This tab follows the isolation stack from node tiers down to CPU, memory, network and disk.

### Workloads and node tiers {#d03-1}

**Scenario.** Every evening the batch wave arrives and the API's p99 latency jumps above 100 ms.

![Workloads and node tiers](diagrams/sections/d03-1.svg)

**What the picture shows.** Web services are latency-sensitive and need stability, while batch is retry-capable and opportunistic. Each gets its own tier: on-demand, tainted nodes for web (with placeholder pods for headroom), diversified Spot capacity for batch, and a shared, untainted tier for utilization.

### Priority, preemption and QoS {#d03-2}

**Scenario.** The cluster is full when a traffic spike hits. Who gets evicted, and who decides?

![Priority, preemption and QoS](diagrams/sections/d03-2.svg)

**What the picture shows.** Two different mechanisms. **PriorityClasses** are the scheduler's tool, applied cluster-wide (`web-critical` preempts, `batch-low` never does). **QoS** (Guaranteed > Burstable > BestEffort) is the kubelet's tool, applied on one node under resource pressure.

> **From the chapter:** Kueue adds a job-level layer on top. WorkloadPriorityClasses decide admission order, and preemption can reclaim borrowed quota. Which hardware a job gets (flavor) and who goes first (priority) are separate decisions.

### Placement, autoscaling and cost {#d03-3}

**Scenario.** Batch costs are climbing even though most nodes look half empty.

![Placement, autoscaling and cost](diagrams/sections/d03-3.svg)

**What the picture shows.** Affinity for web, anti-affinity for batch, and requests and limits per workload type. Autoscaling uses HPA for web (fast up, slow down) and Karpenter for batch (diversified Spot, consolidation). Spot interruptions are handled by checkpointing, and the key alerts are pending pods, queue depth and p99.

> **From the chapter:** bin-packing (MostAllocated) plus a matching descheduler policy (`HighNodeUtilization`) keeps nodes full, so empty ones can be removed. Never combine it with the opposite policy, or the two will fight each other.

### CPU: shares and throttling {#d03-4}

**Scenario.** The API's average latency looks fine, but p99 spikes every few seconds.

![CPU: shares and throttling](diagrams/sections/d03-4.svg)

**What the picture shows.** Requests become CPU shares, which split the CPU under contention. Limits become a quota per 100 ms window. A multi-threaded pod can use up its quota early in the window and then freeze until the next one, which is where the p99 jitter comes from.

### Kernel vs. Kubernetes {#d03-5}

**Scenario.** "Why do requests matter if the kernel shares CPU fairly anyway?"

![Kernel vs. Kubernetes](diagrams/sections/d03-5.svg)

**What the picture shows.** Kubernetes sets the policy, the kubelet translates it into cgroups, and the kernel enforces it. The one thing the kernel can't do is stop a node from being overpacked. Only the scheduler's request accounting does that.

### Memory: who gets killed {#d03-6}

**Scenario.** A node runs out of memory. Which pod dies first, and why that one?

![Memory: who gets killed](diagrams/sections/d03-6.svg)

**What the picture shows.** Memory can't be compressed: the OOM killer runs at container or node level. QoS sets `oom_score_adj` (BestEffort 1000, Guaranteed -997, Burstable in between, depending on its memory request). Among Burstable pods, the kubelet evicts the one furthest over its request first.

### Network: the unmanaged resource {#d03-7}

**Scenario.** A backup pod starts a 10 Gbps transfer and the API on the same node starts dropping connections, yet CPU and memory look perfect.

![Network: the unmanaged resource](diagrams/sections/d03-7.svg)

**What the picture shows.** Kubernetes has no network requests or limits. The fixes are Linux `tc` shaping, CNI bandwidth annotations, and instance types with separate bandwidth for EBS.

### Storage I/O: the other unmanaged resource {#d03-8}

**Scenario.** A log archiver saturates a volume, and the database pod sharing it goes from 1 ms to 200 ms latency.

![Storage I/O: the other unmanaged resource](diagrams/sections/d03-8.svg)

**What the picture shows.** EBS throttles per volume, not per pod, and Kubernetes doesn't expose the `blkio` controller. The symptoms are I/O wait, processes stuck in the D state, and failing liveness probes. The fixes are dedicated volumes per critical pod, EBS-optimized instances, and node-level guardrails.
