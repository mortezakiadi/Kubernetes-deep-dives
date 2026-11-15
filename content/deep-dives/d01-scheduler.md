---
id: d-scheduler
title: "How kube-scheduler picks a node"
short: "EKS Scheduler"
---
Every pod goes through the same decision: filter out the nodes that can't run it, score the ones that can, and bind it to the best one. This deep dive follows that decision step by step.

### Phase 1: filtering

**Scenario.** A pod stays `Pending` with `0/12 nodes are available`. The developer says there's "plenty of room".

![Phase 1: filtering](diagrams/sections/d01-1.svg)

**What the picture shows.** Filtering drops any node that fails a hard rule: requested CPU or memory doesn't fit, nodeSelector doesn't match, an untolerated taint, required affinity or anti-affinity, or a hostPort that's already taken. "Plenty of room" usually means *free usage*, but the scheduler only counts *requests*. The event message lists which rule removed how many nodes.

**Going deeper.** For GPUs, these same filters do the steering. `nvidia.com/gpu: 1` is purely resource-based, so every GPU counts the same: a 16 GB T4 and an 80 GB A100 are both "1". A `nodeSelector` on GPU labels pins an exact model, node affinity can say "at least 40 GB, prefer Hopper", and a taint on GPU nodes keeps everything else off them. Dynamic Resource Allocation (stable since Kubernetes 1.34) lets a pod describe the device it needs, and the driver picks a match at scheduling time. Filtering also runs one pod at a time: a distributed job's workers are judged separately, which is the root of the [gang problem](#d02-2).

### Filtering outcomes at a glance

**Scenario.** On-call needs a quick way to map an event message to a cause.

![Filtering outcomes](diagrams/sections/d01-2.svg)

**What the picture shows.** A cheat sheet linking each kind of constraint (resources, taints, affinity, selector) to its consequence, and a funnel from all nodes to the feasible set that goes on to scoring.

### Phase 2: scoring plugins

**Scenario.** Three nodes pass filtering. Why does the pod always land on the same one?

![Phase 2: scoring plugins](diagrams/sections/d01-3.svg)

**What the picture shows.** Each plugin gives every node a 0–100 score: LeastAllocated (the spreader), BalancedAllocation (keeps CPU and memory even), ImageLocality (image already cached) and preferred affinity. The node with the highest weighted total wins. Preferred affinity is the only one you set directly in the pod spec.

**Going deeper.** For GPU training I switch NodeResourcesFit to **MostAllocated** (bin-packing) and give `nvidia.com/gpu` a higher weight than CPU and memory. Packing leaves whole nodes empty, so the autoscaler can remove them. That matters when a GPU node costs tens of dollars an hour. The catch is a bigger blast radius per node, so I offer it as a separate scheduler profile and let critical services keep spreading.

### Score × weight

**Scenario.** You need to explain on a whiteboard how "points" become a decision.

![Score × weight](diagrams/sections/d01-4.svg)

**What the picture shows.** `final score = Σ (plugin score × plugin weight)`. The score measures *quality* ("how empty is this node?") and the weight measures *importance*. Worked examples: LeastAllocated gives 80 for a node that's 80% free, and ImageLocality rises with how much of the image is cached.

### When affinity loses to ImageLocality

**Scenario.** A pod has a soft affinity to run near its cache service, but keeps landing on a node far away that happens to have its big image.

![When affinity loses to ImageLocality](diagrams/sections/d01-5.svg)

**What the picture shows.** A weak affinity match times weight 2 can still lose to a strong image-cache score. There are two ways to fix it: make the rule *required*, which moves it into filtering, or raise the plugin weight cluster-wide in `KubeSchedulerConfiguration`.

### A worked example

**Scenario.** Two candidate nodes and three plugins. Which one wins?

![A worked example](diagrams/sections/d01-6.svg)

**What the picture shows.** Node 2 wins on the total even though Node 1 wins one plugin. The picture uses equal weights to keep the arithmetic simple. In the real default profile, affinity has weight 2, which only makes Node 2's lead bigger.

### The weight field in your YAML

**Scenario.** A teammate asks what `weight: 80` in a pod spec actually does.

![The weight field in your YAML](diagrams/sections/d01-7.svg)

**What the picture shows.** `weight` (1–100) only exists on *preferred* rules and tunes scoring. *Required* rules have no weight because they're filters. A high weight means "a strong suggestion", and a low one means "nice to have".

### Scheduler score vs. OOM score

**Scenario.** "The scheduler gave this node a low score, so why did my important pod get killed there?"

![Scheduler score vs. OOM score](diagrams/sections/d01-8.svg)

**What the picture shows.** The scheduler's score only matters at placement time. Under memory pressure, the kubelet evicts pods by how far they're over their memory request (then by priority), and the kernel's OOM killer uses `oom_score`. These are separate systems that use separate numbers (see [Memory: who gets killed](#d03-6)).
