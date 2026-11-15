---
id: d-cgroups
title: "From a YAML limit to a kernel quota"
short: "cgroups"
---
This deep dive follows how `resources.requests` and `resources.limits` turn into cgroup settings that the Linux CFS scheduler enforces.

### Policy maker, translator, enforcer

**Scenario.** A web service and a batch job run on the same node. The web pod asks for 1 CPU with a limit of 2; the batch pod asks for 250m with a limit of 500m. An engineer wants to know what actually happens inside Linux to those four numbers, both when the CPU is busy and when it's idle.

![Policy maker, translator, enforcer](diagrams/sections/d04-1.svg)

**What the picture shows.** Three columns showing how a pod spec becomes kernel behaviour.

- **1. Kubernetes (the policy maker):** two pod specs.
  - **Web service pod (high priority):** requests `cpu: 1000m`, limit `cpu: 2000m`.
  - **Batch job pod (low priority):** requests `cpu: 250m`, limit `cpu: 500m`.

```yaml
# web service pod
resources:
  requests: {cpu: 1000m}
  limits:   {cpu: 2000m}
---
# batch job pod
resources:
  requests: {cpu: 250m}
  limits:   {cpu: 500m}
```

  - **Kubelet (the translator):** reads these specs and writes them into cgroups.
- **2. Linux cgroups (the translator's output):** one cgroup per pod, each with two files.
  - **Web service cgroup:** `cpu.shares = 1024` and `cpu.cfs_quota_us = 200000` per 100 ms period.
  - **Batch job cgroup:** `cpu.shares = 256` and `cpu.cfs_quota_us = 50000`.
  - **The rule:** the request becomes **shares** (a weight); the limit becomes a **quota** (a speed limit).
- **3. Linux kernel CFS (the enforcer):** the CPU cores and the CFS scheduler, shown over one 100 ms period.
  - **A. Contention (CPU busy):** the web service (1024 shares) gets about 80% of the time and the batch job (256 shares) about 20%. Shares keep the split proportional under heavy load.
  - **B. Idle / throttling (CPU free):** the web service runs, then the batch job runs for its 50 ms, then is **throttled** for the remaining 50 ms because it reached its quota, even though the CPU is idle. Limits cap a process even when there's spare capacity.
- **Summary banner:** Kubernetes sets the policy (weights and limits), the kubelet translates it into cgroups, and the kernel enforces it with shares (for contention) and quotas (for capping).

### From a limit to a quota

**Scenario.** A pod has a CPU limit of `2000m`. An engineer looks inside the node and finds `cpu.cfs_quota_us = 200000`, which is 200 ms. But the scheduling window is only 100 ms. How can a pod have 200 ms of CPU inside a 100 ms window?

![From a limit to a quota](diagrams/sections/d04-2.svg)

**What the picture shows.** The arithmetic that turns a CPU limit into a kernel quota.

- **Linux cgroups use two files for a CPU limit:**
  - **`cpu.cfs_period_us`, the scheduling window:** in Kubernetes almost always 100,000 µs, which is 100 ms of real time.
  - **`cpu.cfs_quota_us`, the allowance:** how much CPU time the pod may use within each window.
- **The formula Kubernetes uses:** quota = limit in cores × period.
- **Worked example, a 2000m limit:** 2000m ÷ 1000 = 2.0 cores; 2.0 × 100,000 µs = **200,000 µs** (200 ms).
- **How can 200 ms fit in a 100 ms window?** Quotas add up across all CPU cores. A node with 4 cores has 400 ms of CPU time available in every 100 ms of real time.
- **What the kernel is really saying:** "your pod may do 200 ms of work per window, for example by running at 100% on two cores for the whole 100 ms."
