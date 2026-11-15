---
id: d-challenges-1
title: "Failure modes: storage, memory and controller loops"
short: "Challenges 1"
---
These are real incidents written as *problem → why → fix*, one per section.

### Volume attachment limit

**Scenario.** Database pods 29 and 30 stay `Pending` on a big node that has plenty of free CPU.

![Volume attachment limit](diagrams/sections/d05-1.svg)

**What the picture shows.** Nitro instances can attach only about 28 volumes. The fix is to spread stateful pods with anti-affinity or topology spread constraints, so the scheduler stops piling them onto one node.

### The stuck volume

**Scenario.** A node dies, and the database pod crash-loops on its replacement node.

![The stuck volume](diagrams/sections/d05-2.svg)

**What the picture shows.** The EBS volume still appears attached to the dead node, so the new node can't attach it. The quick fix is to force-detach it. The lasting fix is to keep the EBS CSI driver up to date.

### Snapshot restored in the wrong AZ

**Scenario.** A restored database never starts.

![Snapshot restored in the wrong AZ](diagrams/sections/d05-3.svg)

**What the picture shows.** EBS volumes are tied to one AZ. If you restore into the zone your nodes are in, or let `WaitForFirstConsumer` choose the zone, the volume and the pod end up in the same place.

### Force delete and filesystem corruption

**Scenario.** Someone runs `kubectl delete pod --force --grace-period=0` on a database mid-write.

![Force delete and filesystem corruption](diagrams/sections/d05-4.svg)

**What the picture shows.** Never force-delete a database pod unless its node is already gone. To recover, restore the last good snapshot and replay the WAL.

### IOPS throttling cascade

**Scenario.** Latency goes from 1 ms to over 100 ms, the connection pool fills up, and the whole app falls over.

![IOPS throttling cascade](diagrams/sections/d05-5.svg)

**What the picture shows.** gp3 gives a 3,000-IOPS baseline. Provision the IOPS you actually need in the StorageClass, and watch IOPS (or BurstBalance on gp2) before you hit the limit.

### Cross-AZ transfer costs

**Scenario.** The Kafka bill's networking line is bigger than its compute line.

![Cross-AZ transfer costs](diagrams/sections/d05-6.svg)

**What the picture shows.** Every replicated message crosses AZs, and each crossing costs money. The fixes are compression (zstd or snappy) and rack-aware reads from replicas in the same AZ.

### The page-cache memory trap

**Scenario.** An ML job that loops over the same dataset gets OOM-killed, but its own memory use looks small.

![The page-cache memory trap](diagrams/sections/d05-7.svg)

**What the picture shows.** Active page cache counts toward the container's working set. Watch `container_memory_working_set_bytes`, leave headroom between request and limit, and use `posix_fadvise(DONTNEED)` on data you have finished reading.

### Thundering herd

**Scenario.** 100 batch jobs finish at the same moment, all write to the database, and the web service's queries start failing.

![Thundering herd](diagrams/sections/d05-8.svg)

**What the picture shows.** The batch jobs effectively DDoS your own database. Put a queue (SQS or Kafka) in between with controlled consumers, or add random jitter to the batch jobs as a quick fix.

### Scheduler thrashing

**Scenario.** A traffic spike makes HPA add 50 web pods, which preempt 50 batch pods. The Job controller recreates them, and they get preempted again, over and over.

![Scheduler thrashing](diagrams/sections/d05-9.svg)

**What the picture shows.** A feedback loop between controllers. Changing `restartPolicy` alone doesn't stop it. The fix is to queue instead of retry: admit batch jobs through Kueue only when capacity exists, give batch `preemptionPolicy: Never`, and set a Job `podFailurePolicy`.

> **From the chapter:** the same kind of loop happens when a descheduler's strategy disagrees with the scheduler's scoring. A steadily rising eviction count with no consolidation is the signal.

### CPU limit vs. request gap

**Scenario.** Web latency spikes even though node CPU is only at 60%.

![CPU limit vs. request gap](diagrams/sections/d05-10.svg)

**What the picture shows.** A batch pod with a 1-core request and an 8-core limit can burst to 8 cores, but the scheduler only reserved 1 core for it. Neighbours see sudden contention. For CPU-heavy batch, set requests close to limits.
