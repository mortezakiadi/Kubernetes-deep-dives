---
id: d-cgroups
title: "From a YAML limit to a kernel quota"
short: "cgroups"
---
This deep dive follows how `resources.requests` and `resources.limits` turn into cgroup settings that the Linux CFS scheduler enforces.

### Policy maker, translator, enforcer

**Scenario.** A web pod (request 1 core, limit 2) and a batch pod (request 250m, limit 500m) share a busy node. How much CPU does each get?

![Policy maker, translator, enforcer](diagrams/sections/d04-1.svg)

**What the picture shows.**

- The kubelet writes shares (1024 vs. 256) and quotas into each pod's cgroup.
- Under contention, CFS splits the CPU 80/20 by shares.
- When the batch pod hits its quota, it is throttled even if the CPU is otherwise idle.

### From a limit to a quota

**Scenario.** "How can a pod use 200 ms of CPU inside a 100 ms window?"

![From a limit to a quota](diagrams/sections/d04-2.svg)

**What the picture shows.**

- `quota = limit in cores × 100,000 µs`.
- A 2-core limit gives 200 ms per window because quota is counted across all cores, so two threads can run flat out for the whole window.
- On cgroup v2 (recent EKS AMIs) the files are `cpu.weight` and `cpu.max`, but the model is the same.