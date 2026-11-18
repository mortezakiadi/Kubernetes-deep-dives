---
id: design-gpu-scheduling
title: "Design 17: Scheduling that keeps accelerators busy"
short: "Scheduling that keeps accelerators busy"
---
This design answers one question: *how do we schedule a shared GPU fleet so that accelerators stay busy: distributed jobs start all at once or not at all (gang scheduling), small jobs don't fragment nodes that big jobs need (bin-packing), and many teams share the fleet fairly (quotas and fair share)?* [Design 02](#design-multicluster) covers the multi-cluster picture (which cluster a job goes to); this design goes inside one cluster, into the mechanics of admission, placement, preemption and measurement. For how kube-scheduler filters and scores nodes, see the [scheduler deep dive](#d-scheduler); this design builds on it.

The short version: *separate "when may this job start" from "where do its pods go", and measure where every GPU-hour went*. Kueue decides admission: a job is admitted only when its whole gang fits its quota and, with topology-aware scheduling, the physical topology. kube-scheduler places pods with a **bin-packing** profile on GPU nodes, so partially used nodes fill up first and whole nodes stay free for big gangs. Teams get **nominal quotas** in cohorts with borrowing, lending limits and **fair-share** preemption; priorities and backfill keep idle quota working. And a GPU-hour accounting breaks idle time into causes (fragmented, reserved-but-idle, waiting for a gang, preempted work) so each one can be attacked.

### The use case and requirements

**Scenario.** A 1,024-GPU cluster (128 nodes × 8 H100) serves six teams. Finance reports 58% of GPU-hours doing useful work. A 256-GPU training job has waited three days; meanwhile 180 GPUs are free, scattered as 1–3 GPUs per node across 90 nodes. Last week two 64-GPU jobs (one GPU per pod) each got 40 of their pods running and then waited for each other for six hours, holding 80 GPUs idle. One team runs notebooks on 96 GPUs that are busy 8% of the time, and another team says the queue is unfair because "the same team always gets the GPUs."

![A busy-looking idle fleet](diagrams/designs/concepts/ds17-c1.svg)

**What the platform must do.**

- **Start distributed jobs atomically:** all pods of a gang start together, or none hold resources.
- **Keep large jobs placeable:** small jobs mustn't scatter across nodes that a large job needs whole.
- **Guarantee each team a share:** a nominal quota that's available when the team needs it.
- **Lend idle capacity:** unused quota works for others, and is reclaimed quickly when its owner returns.
- **Be fair over time:** teams that used less recently get priority for borrowed capacity.
- **Explain idle time:** every idle GPU-hour attributed to a cause.

**Out of scope:** which cluster a job runs in ([Design 02](#design-multicluster)), node health ([Design 14](#design-reliable-rl-infra)), and GPU sharing inside one device beyond its role in bin-packing.

### Where GPU-hours go

**Scenario.** "58% utilization" is the only number anybody has. It doesn't say whether to buy GPUs, fix the scheduler or talk to the notebook team.

![Where GPU-hours go](diagrams/designs/concepts/ds17-c2.svg)

![Where GPU-hours go](diagrams/designs/ds17-1.svg)

**What the picture shows.** A breakdown of all GPU-hours in a period into categories with different fixes.

- **Busy:** allocated and doing useful work (measured by GPU activity, not just allocation).
- **Allocated but idle:** pods holding GPUs while idle: notebooks, jobs stuck in data loading, services sized for peak. Fix: idle culling, right-sizing, sharing smaller slices.
- **Waiting for a gang:** GPUs held by some pods of a job whose other pods can't start. Fix: all-or-nothing admission.
- **Fragmented:** free GPUs that no pending job can use because they're scattered. Fix: bin-packing and defragmentation.
- **Free and unusable by policy:** quota reserved for a team that isn't using it, not lent. Fix: borrowing and lending limits.
- **Lost to preemption:** work computed then discarded when a job was preempted before its next checkpoint. Fix: checkpoint-aware preemption.
- **Truly free:** nothing pending could use it. Fix: more demand, or fewer GPUs.

**The code.** Classifying each GPU-minute from scheduler and DCGM data (Python sketch).

```python
from collections import Counter

def classify(gpu_minute, pending_jobs, quota_state):
    """gpu_minute: dict with allocated, util (0..1, DCGM), job_state, node_free_gpus, owner_queue."""
    if gpu_minute["allocated"]:
        if gpu_minute["job_state"] == "waiting_for_gang":
            return "waiting_for_gang"                          # some pods up, others pending #!
        if gpu_minute["util"] < 0.05:
            return "allocated_idle"
        if gpu_minute.get("discarded_by_preemption"):
            return "lost_to_preemption"
        return "busy"
    fits = [j for j in pending_jobs if j["gpus_per_pod"] <= gpu_minute["node_free_gpus"]]
    if pending_jobs and not fits:
        return "fragmented"                                    # free, but no pending pod fits here
    if fits and not quota_state.can_admit_any(fits):
        return "blocked_by_quota_policy"
    return "free"

def breakdown(minutes, pending_at, quota_at):
    c = Counter(classify(m, pending_at(m["t"]), quota_at(m["t"])) for m in minutes)
    total = sum(c.values())
    return {k: round(v / total, 3) for k, v in c.most_common()}
```

**Going deeper.**

- **Allocation is not utilization:** a GPU can be allocated and idle; DCGM activity metrics (SM activity, tensor activity) say whether it's working.
- **Report the breakdown per team and per queue:** the fix for "allocated but idle" is a conversation with a team; the fix for "fragmented" is a scheduler change.

### Two layers: admission and placement

**Scenario.** When a job is pending, users look at pod events from kube-scheduler and see "0/128 nodes are available." The real reason is that the job's queue is out of quota, which kube-scheduler doesn't know about.

![Two layers](diagrams/designs/concepts/ds17-c3.svg)

![Two layers: admission and placement](diagrams/designs/ds17-2.svg)

**What the picture shows.** Who decides what, from job submission to running pods.

- **Admission (Kueue):** a job is created suspended and becomes a Workload in a LocalQueue. Kueue admits it when its whole request fits the ClusterQueue's quota (possibly by borrowing), in priority and fairness order, and optionally when it fits the physical topology. Admission is all-or-nothing for the whole job.
- **Placement (kube-scheduler):** once admitted, pods are created and the scheduler filters and scores nodes for each pod ([scheduler deep dive](#d-scheduler)). Profiles decide scoring: bin-packing for GPU pools.
- **Devices (device plugin or DRA):** the node assigns specific GPUs (or slices) to the container.
- **One status per layer:** "pending admission: queue over quota" and "admitted, pods unschedulable" are different problems with different owners.

**Going deeper.**

- **Kueue doesn't replace kube-scheduler:** Kueue decides when a workload starts and against which quota; kube-scheduler still decides which node each pod lands on. Topology-aware scheduling is the point where Kueue also looks at nodes, so that what it admits can actually be placed together.
- **Upstream is moving toward workload-aware scheduling:** kube-scheduler is gaining native awareness of pod groups; the admission/placement split in this design stays valid as that matures.

### Gang scheduling: all or nothing

**Scenario.** Two 64-GPU jobs were created at the same time. The scheduler placed pods from both as GPUs freed up: 40 pods of each ran, and neither could start training without all 64. Both held 40 GPUs idle for six hours.

![Gang scheduling](diagrams/designs/concepts/ds17-c4.svg)

![Gang scheduling: all or nothing](diagrams/designs/ds17-3.svg)

**What the picture shows.** Why per-pod scheduling deadlocks distributed jobs, and the two mechanisms that prevent it.

- **The deadlock:** distributed training can't make progress with some of its workers; per-pod scheduling lets two jobs each take part of the free capacity and wait forever.
- **Quota-level all-or-nothing:** Kueue admits a workload only if its full request fits the quota, so two jobs can't both be admitted into capacity that only fits one.
- **Physical fit (topology-aware scheduling):** quota fit doesn't guarantee node fit; TAS checks that the pods can actually be placed (and placed close together) before admitting.
- **Ready-pods timeout (`waitForPodsReady`):** after admission, if all pods aren't ready within a timeout, the workload is evicted, its pods deleted, and it's requeued with backoff. A recovery timeout handles pods that fail later.
- **Workload types that know their gang:** JobSet, LeaderWorkerSet, RayJob and PyTorchJob present all their pods as one workload with PodSets.

**The YAML.** Kueue configuration for ready-pods gang semantics and fair sharing (Configuration v1beta2).

```yaml
apiVersion: config.kueue.x-k8s.io/v1beta2
kind: Configuration
namespace: kueue-system
waitForPodsReady:
  timeout: 10m                         # all pods ready within 10 min of admission, or requeue #!
  recoveryTimeout: 5m                  # a pod failing later must recover within 5 min
  blockAdmission: false                # TAS handles physical fit; don't serialize admissions
  requeuingStrategy:
    timestamp: Eviction
    backoffBaseSeconds: 60
    backoffMaxSeconds: 1800
    backoffLimitCount: 10              # then the workload is deactivated for a human to look at
fairSharing:
  preemptionStrategies: [LessThanOrEqualToFinalShare, LessThanInitialShare]
```

**Going deeper.**

- **Choose the timeout from data:** it must cover image pulls and model loading for large jobs; measure time-to-all-ready per job size and set the timeout above its 99th percentile.
- **`blockAdmission` trades throughput for safety:** serializing admissions avoids deadlocks without TAS, but leaves capacity idle while each job starts; with TAS checking physical fit, it's usually unnecessary.

### Topology-aware gang placement

**Scenario.** A 64-GPU job is admitted and placed across three network blocks; its all-reduce runs 30% slower than the same job in one block. A gang that fits but is spread out wastes accelerators too.

![Topology-aware placement](diagrams/designs/concepts/ds17-c5.svg)

![Topology-aware gang placement](diagrams/designs/ds17-4.svg)

**What the picture shows.** Placing a gang within the smallest network domain that fits it.

- **Topology levels:** zone → block → rack → host, from node labels set by the cloud provider or a topology discovery tool.
- **Required vs preferred:** `podset-required-topology` waits until one domain can hold the whole PodSet; `podset-preferred-topology` tries the domain and falls back to wider ones.
- **Slices:** for very large jobs, split the PodSet into slices (for example, 16 pods each) that must each fit one rack, without requiring the whole job in one block.
- **Groups:** co-locate several PodSets of one workload (leader and workers of an inference server) in the same domain.
- **Interaction with bin-packing:** keeping whole racks and blocks free (next section) is what makes required topology placement possible without long waits.

**The YAML.** A Topology, a ResourceFlavor that uses it, and a JobSet requiring one block with racks of 16 pods.

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: Topology
metadata: { name: gpu-fabric }
spec:
  levels:
  - nodeLabel: network.example.com/block
  - nodeLabel: network.example.com/rack
  - nodeLabel: kubernetes.io/hostname
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata: { name: h100-fabric }
spec:
  nodeLabels: { gpu.example.com/class: h100 }
  topologyName: gpu-fabric                       # enables TAS for this flavor
---
apiVersion: jobset.x-k8s.io/v1alpha2
kind: JobSet
metadata:
  name: pretrain-ft-64
  labels: { kueue.x-k8s.io/queue-name: llm-team }
spec:
  replicatedJobs:
  - name: workers
    replicas: 1
    template:
      spec:
        parallelism: 8
        completions: 8
        completionMode: Indexed
        template:
          metadata:
            annotations:
              kueue.x-k8s.io/podset-required-topology: network.example.com/block      # whole job, one block #!
              kueue.x-k8s.io/podset-slice-required-topology: network.example.com/rack
              kueue.x-k8s.io/podset-slice-size: "4"                                  # 4 pods (32 GPUs) per rack
          spec:
            containers:
            - name: trainer
              image: registry.example.com/train:2026.11
              resources: { limits: { nvidia.com/gpu: 8 } }
```

**Going deeper.**

- **Topology labels must be accurate:** a wrong block label puts a "same block" job across the fabric. Validate labels against measured bandwidth during node burn-in ([Design 14](#design-reliable-rl-infra)).
- **Required topology can wait forever:** set a policy for how long a job may wait for its domain before it's offered a preferred (wider) placement, if its owner allows it.

### Bin-packing to reduce fragmentation

**Scenario.** The default scoring spreads pods to the least-allocated nodes. Over a week, 1-, 2- and 4-GPU jobs land on almost every node, and no node has 8 free GPUs, though 180 GPUs are free in total.

![Bin-packing](diagrams/designs/concepts/ds17-c6.svg)

![Bin-packing to reduce fragmentation](diagrams/designs/ds17-5.svg)

**What the picture shows.** How scoring policy decides whether free GPUs end up usable or scattered.

- **Spreading (least allocated):** good for CPU services' resilience; for GPUs, it scatters small jobs and destroys whole free nodes.
- **Bin-packing (most allocated):** prefer the node that is already most used, so small jobs fill partly used nodes and whole nodes stay free.
- **Weighted by resource:** score GPU allocation most heavily, so the GPU dimension drives placement on GPU nodes.
- **Separate profiles:** a bin-packing scheduler profile for GPU pools, the default spreading profile for CPU services.
- **Fragmentation metric:** the share of free GPUs that sit on nodes with fewer than 8 free, and the largest gang that could start right now.

**The YAML.** A kube-scheduler profile that bin-packs GPUs (KubeSchedulerConfiguration v1).

```yaml
apiVersion: kubescheduler.config.k8s.io/v1
kind: KubeSchedulerConfiguration
profiles:
- schedulerName: default-scheduler                 # CPU services keep spreading
- schedulerName: gpu-binpack                       # GPU workloads set spec.schedulerName: gpu-binpack
  pluginConfig:
  - name: NodeResourcesFit
    args:
      scoringStrategy:
        type: MostAllocated                        # prefer fuller nodes #!
        resources:
        - { name: nvidia.com/gpu, weight: 10 }     # GPUs dominate the score
        - { name: cpu, weight: 1 }
        - { name: memory, weight: 1 }
  plugins:
    score:
      disabled:
      - name: NodeResourcesBalancedAllocation      # balancing fights packing
```

**The code.** A fragmentation report: how much free capacity is usable by gangs of each size (Python).

```python
def fragmentation(free_per_node: list[int], gpus_per_node: int = 8):
    total_free = sum(free_per_node)
    whole = sum(f for f in free_per_node if f == gpus_per_node)
    report = {"free_gpus": total_free, "whole_free_nodes": whole // gpus_per_node,
              "fragmented_share": round(1 - whole / total_free, 3) if total_free else 0.0}   #!
    for pod_gpus in (1, 2, 4, 8):
        report[f"pods_of_{pod_gpus}_that_fit"] = sum(f // pod_gpus for f in free_per_node)
    return report

# 90 nodes with 1–3 free GPUs, 2 whole free nodes
print(fragmentation([2] * 60 + [1] * 15 + [3] * 15 + [8] * 2))
```

**Going deeper.**

- **Bin-packing has costs:** packed nodes concentrate risk (a node failure hits more jobs) and can create noisy neighbours; keep packing for GPU batch work, not latency-critical services.
- **Your scheduler deep dive applies here:** scoring plugins and weights interact ([scheduler deep dive](#d-scheduler)); test a new profile on a replay of real submissions before switching.

### Defragmentation and right-sizing

**Scenario.** Even with bin-packing, long-running 1-GPU notebooks pin GPUs on dozens of nodes. Packing only affects new pods; the cluster needs a way to fix what's already scattered.

![Recover whole nodes](diagrams/designs/concepts/ds17-c7.svg)

![Defragmentation and right-sizing](diagrams/designs/ds17-6.svg)

**What the picture shows.** Ways to recover whole nodes and avoid wasting whole GPUs on small work.

- **Pool by job shape:** a small-job pool (or partitioned GPUs) for notebooks and 1–2 GPU jobs, and a large-job pool kept for gangs of whole nodes.
- **Partition GPUs for small work:** MIG slices, time-slicing or MPS for notebooks and small inference, configured below Kueue with the device plugin or Dynamic Resource Allocation; the quota counts slices, not whole GPUs.
- **Compaction:** periodically migrate preemptible, checkpointable small jobs off sparsely used nodes (evict and requeue them), so their nodes become whole again; only when a pending gang needs whole nodes.
- **Reserve whole nodes for waiting gangs:** when a large job has waited beyond a threshold, stop placing small jobs on nodes that are about to become free, so the gang can start.
- **Idle culling:** notebooks idle beyond a limit are checkpointed and stopped ([Design 03](#design-training-inference)).

**The code.** Choosing nodes to drain to create whole nodes for a waiting gang, by fewest GPUs moved (Python).

```python
def plan_compaction(nodes, needed_whole_nodes: int, gpus_per_node: int = 8):
    """nodes: dicts with name, used_gpus, movable (all pods preemptible + checkpointable)."""
    candidates = [n for n in nodes if 0 < n["used_gpus"] < gpus_per_node and n["movable"]]
    candidates.sort(key=lambda n: n["used_gpus"])                  # cheapest to empty first #!
    plan, moved = [], 0
    for n in candidates:
        if len(plan) == needed_whole_nodes:
            break
        plan.append(n["name"]); moved += n["used_gpus"]
    free_elsewhere = sum(gpus_per_node - n["used_gpus"] for n in nodes if n["name"] not in plan
                         and n["used_gpus"] < gpus_per_node) - moved
    ok = len(plan) == needed_whole_nodes and free_elsewhere >= 0      # evicted pods must fit elsewhere
    return {"drain": plan if ok else [], "gpus_moved": moved if ok else 0, "feasible": ok}
```

**Going deeper.**

- **Compaction costs work:** evicted jobs lose progress since their last checkpoint; only move checkpointable, preemptible jobs, and only when a pending gang will use the freed nodes.
- **Partitioning changes the unit of quota:** a team's quota for MIG slices is separate from its whole-GPU quota; model them as separate resources or flavors.

### Quotas: nominal, borrowing and lending

**Scenario.** Each team had a hard limit on GPUs. The vision team's 96 GPUs sat idle on weekends while the LLM team's jobs waited, and nobody could lend them without an admin changing limits by hand.

![Quota anatomy](diagrams/designs/concepts/ds17-c8.svg)

![Quotas: nominal, borrowing and lending](diagrams/designs/ds17-7.svg)

**What the picture shows.** The quota model: guaranteed shares that can be lent and borrowed.

- **ResourceFlavors:** kinds of capacity (H100 on the fabric, H100 standalone, spot), each mapped to node labels.
- **ClusterQueue per team:** a **nominal quota** per flavor, the team's guaranteed share.
- **Cohort:** ClusterQueues in a cohort lend unused nominal quota to each other.
- **Borrowing limit:** the most a team can take beyond its nominal quota.
- **Lending limit:** the most of a team's unused quota others can borrow; the rest stays reserved for the team (for latency-sensitive work that must start immediately).
- **Reclaim:** when a team needs its nominal quota back, it can preempt borrowers (next sections).

**The YAML.** Two teams' ClusterQueues in one cohort, with borrowing and lending limits (v1beta2).

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata: { name: llm-team }
spec:
  cohortName: gpu-fleet
  namespaceSelector: { matchLabels: { team: llm } }
  fairSharing: { weight: 2 }                     # larger share of borrowable capacity
  resourceGroups:
  - coveredResources: ["nvidia.com/gpu", "cpu", "memory"]
    flavors:
    - name: h100-fabric
      resources:
      - { name: nvidia.com/gpu, nominalQuota: 512, borrowingLimit: 256 }
      - { name: cpu, nominalQuota: 6144 }
      - { name: memory, nominalQuota: 48Ti }
  preemption:
    reclaimWithinCohort: Any                     # get nominal quota back from borrowers #!
    borrowWithinCohort: { policy: LowerPriority, maxPriorityThreshold: 100 }
    withinClusterQueue: LowerPriority
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata: { name: vision-team }
spec:
  cohortName: gpu-fleet
  namespaceSelector: { matchLabels: { team: vision } }
  resourceGroups:
  - coveredResources: ["nvidia.com/gpu", "cpu", "memory"]
    flavors:
    - name: h100-fabric
      resources:
      - { name: nvidia.com/gpu, nominalQuota: 128, lendingLimit: 96 }   # 32 always kept for vision #!
      - { name: cpu, nominalQuota: 1536 }
      - { name: memory, nominalQuota: 12Ti }
  preemption:
    reclaimWithinCohort: Any
    withinClusterQueue: LowerPriority
```

**Going deeper.**

- **Nominal quotas should add up to the fleet:** quotas that sum to more than the hardware promise guarantees that can't all be kept; quotas that sum to less leave capacity owned by nobody.
- **Quota counts requests, not use:** a notebook holding a GPU at 8% use consumes a whole GPU of quota; idle culling and right-sizing protect the quota as much as the hardware.

### Fair share: weighted shares and fairness over time

**Scenario.** Borrowable capacity went to whichever team submitted first after it freed up. One team with an automated sweep generator got most of it every week.

![Two kinds of fairness](diagrams/designs/concepts/ds17-c9.svg)

![Fair share: weighted shares and fairness over time](diagrams/designs/ds17-8.svg)

**What the picture shows.** Two fairness mechanisms that work at different levels.

- **Fair sharing between ClusterQueues (preemption-based):** borrowable capacity in a cohort is divided by weighted share; a queue below its fair share can preempt workloads of queues above theirs, using dominant resource fairness across resources. Weights express how much each team should get of what's shared; a weight of 0 means always last.
- **Admission fair sharing between LocalQueues (usage-based):** within one ClusterQueue, workloads from LocalQueues (for example, projects within a team) that used fewer resources recently are admitted first. It reorders admission and doesn't evict running work.
- **Preemption strategies:** Kueue's fair-sharing strategies only preempt when it moves shares toward fairness (the preemptor's share after admission must not exceed the victim's), avoiding preemption cycles.
- **What fairness can't fix:** if total demand inside nominal quotas exceeds the fleet, fairness only decides who waits.

**The YAML.** Admission fair sharing for a team's ClusterQueue with several project LocalQueues (v1beta2).

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata: { name: research-team }
spec:
  cohortName: gpu-fleet
  namespaceSelector: { matchLabels: { team: research } }
  fairSharing: { weight: 1 }
  admissionScope:
    admissionMode: UsageBasedAdmissionFairSharing   # projects that used less go first #!
  resourceGroups:
  - coveredResources: ["nvidia.com/gpu"]
    flavors:
    - name: h100-fabric
      resources:
      - { name: nvidia.com/gpu, nominalQuota: 128, borrowingLimit: 128 }
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata: { name: sweeps, namespace: research-sweeps }
spec:
  clusterQueue: research-team
  fairSharing: { weight: 1 }                         # an automated sweep queue gets no extra weight
```

**Going deeper.**

- **Weights are a policy decision:** write down why each team has its weight, and review weights quarterly against business priorities.
- **Show teams their share:** a dashboard of each queue's share against its fair share ends most "the queue is unfair" debates with data.

### Priority, preemption and backfill

**Scenario.** A release-candidate evaluation needed 64 GPUs immediately; all were busy with borrowed research jobs. Preempting them would discard four hours of work, because those jobs checkpointed every four hours.

![Priority, preemption, backfill](diagrams/designs/concepts/ds17-c10.svg)

![Priority, preemption and backfill](diagrams/designs/ds17-9.svg)

**What the picture shows.** How urgent work gets capacity without wasting the work it displaces, and how gaps get filled.

- **Workload priority classes:** a small set (for example, production, release, standard, best-effort) set on workloads; Kueue orders and preempts by them, independently of pod priority.
- **Preemption rules:** reclaim nominal quota from borrowers, preempt lower priority within a queue, and borrow by preempting only below a priority threshold.
- **Cheap preemption:** preemptible work must checkpoint often (minutes, [Design 14](#design-reliable-rl-infra)) and handle termination signals by saving state; preemption prefers victims with the most recent checkpoint.
- **Backfill:** while a large gang waits for whole nodes, short jobs that will finish before those nodes are needed can run in the gaps, if they declare a runtime limit (Kueue enforces a maximum execution time set on the workload).
- **Guardrails:** limits on how often a workload can be preempted, so best-effort jobs still finish eventually.

**The YAML.** Workload priority classes and a preemptible job that declares a maximum runtime for backfill (v1beta2).

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: WorkloadPriorityClass
metadata: { name: release }
value: 1000
description: "Release-candidate evals and production runs"
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: WorkloadPriorityClass
metadata: { name: best-effort }
value: 10
description: "Sweeps and exploration; preemptible, checkpoint every 10 minutes"
---
apiVersion: batch/v1
kind: Job
metadata:
  name: sweep-lr-0042
  namespace: research-sweeps
  labels:
    kueue.x-k8s.io/queue-name: sweeps
    kueue.x-k8s.io/priority-class: best-effort
    kueue.x-k8s.io/max-exec-time-seconds: "5400"      # Kueue stops it after 90 min: a known bound makes it safe to backfill #!
spec:
  parallelism: 1
  completions: 1
  template:
    spec:
      schedulerName: gpu-binpack
      terminationGracePeriodSeconds: 120              # time to save a checkpoint on preemption
      restartPolicy: Never
      containers:
      - name: train
        image: registry.example.com/train:2026.11
        resources: { limits: { nvidia.com/gpu: 2 } }
```

**Going deeper.**

- **Preemption cost is measurable:** record the work lost per preemption (time since last checkpoint × GPUs) and include it in the GPU-hour breakdown.
- **Fewer priority classes are better:** every extra class is a negotiation; most fleets need four or five.

### Measuring and tuning the scheduler

**Scenario.** A proposed change to bin-packing weights is debated for weeks. Nobody can say what it would do to queue wait times or fragmentation, because the only way to find out is to deploy it.

![Measuring the scheduler](diagrams/designs/concepts/ds17-c11.svg)

![Measuring and tuning the scheduler](diagrams/designs/ds17-10.svg)

**What the picture shows.** The metrics that describe scheduling quality, and how to test changes safely.

- **Efficiency:** busy share of GPU-hours, the GPU-hour breakdown, fragmentation share and the largest startable gang.
- **Latency:** queue wait per job size class (1, 8, 64, 256+ GPUs) and per priority; time from admission to all pods ready.
- **Fairness:** each queue's usage against its nominal quota and fair share over time; borrowing and reclaim events.
- **Disruption:** preemptions per day, work lost per preemption, evictions by `waitForPodsReady` timeouts.
- **Trace replay:** record submissions (size, runtime, queue, priority) and replay them against a scheduler simulator or a small test cluster with fake nodes, to compare policies before changing production.

**The code.** A fairness report per queue: usage against nominal quota and fair share (Python).

```python
def fairness(usage_gpu_h: dict, nominal: dict, weights: dict, fleet_gpu_h: float):
    """usage_gpu_h: queue -> GPU-hours used; nominal: queue -> guaranteed GPU-hours; weights: queue -> weight."""
    borrowable = fleet_gpu_h - sum(min(usage_gpu_h[q], nominal[q]) for q in nominal)
    wsum = sum(weights.values())
    rows = {}
    for q in nominal:
        fair = nominal[q] + borrowable * weights[q] / wsum          # nominal + weighted part of the rest
        rows[q] = {"used": round(usage_gpu_h[q]), "nominal": nominal[q], "fair_share": round(fair),
                   "ratio_to_fair": round(usage_gpu_h[q] / fair, 2)}   # far below 1 while waiting = unfair #!
    return rows
```

**Going deeper.**

- **Report wait time by size class:** a scheduler that improves average wait by starving 256-GPU jobs looks better on average and worse where it matters.
- **Change one policy at a time:** packing weights, preemption rules and fair-share weights interact; replay each change separately, then together.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds17-c12.svg)

- **Phase 1, measure:** the GPU-hour breakdown per team and queue, fragmentation, queue wait by size class.
- **Phase 2, gangs:** all workloads through Kueue; `waitForPodsReady` with measured timeouts; topology-aware scheduling for jobs over one node.
- **Phase 3, packing:** a bin-packing profile for GPU pools, pools by job shape, GPU partitioning for small work, idle culling.
- **Phase 4, sharing:** cohorts with borrowing and lending limits, fair-share weights, admission fair sharing within teams, workload priority classes, checkpoint-aware preemption, backfill and compaction.

**Trade-offs we accepted.**

- **All-or-nothing admission** can leave capacity idle while a large job waits for its whole gang; topology-aware admission and backfill reduce it.
- **Bin-packing concentrates risk** on fuller nodes, in exchange for whole free nodes for large jobs.
- **Preemption discards work** since the last checkpoint; frequent checkpoints are required for preemptible work.
- **Lending limits leave some capacity idle** on purpose, so teams with urgent work can start immediately.

**Open questions.**

- **Native gang scheduling:** as kube-scheduler gains workload-aware scheduling, how much of the gang logic moves from Kueue into the scheduler?
- **Elastic jobs:** can training jobs grow and shrink with free capacity (Design 14's per-step recovery) so fragments become useful instead of waiting?
- **Runtime prediction:** can predicted job runtimes make backfill safe without users declaring limits?
- **Cost-aware sharing:** should fair share count GPU-hours equally across GPU generations, or by cost?
