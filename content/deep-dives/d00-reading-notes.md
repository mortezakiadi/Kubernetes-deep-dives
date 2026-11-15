---
id: reading-notes
title: "Reading notes: job scheduling for AI training on Kubernetes"
short: "Reading notes: job scheduling"
---
These are my notes on the "Job Scheduling Optimization" chapter of *Generative AI on Kubernetes*, in my own words, with links to the sections of my diagrams where each idea shows up. The chapter's starting point is that a distributed training job is not a normal Kubernetes app: it holds scarce GPUs for days, its pods only work as a group, it moves huge amounts of data between nodes, and many teams want the same hardware. Each of those facts needs its own scheduling answer.

### 1. The default scheduler is built for web apps

kube-scheduler decides one pod at a time: filter out nodes that can't run the pod, score the rest, bind to the winner, and hand off to the kubelet. Its default scoring **spreads** pods out for availability. For GPU training, the chapter argues for the opposite: **bin-packing** with the `MostAllocated` strategy of the NodeResourcesFit plugin, with GPUs weighted more heavily than CPU and memory. Packing leaves whole nodes empty, so the autoscaler can remove them. The trade-off is that one node failure hits more jobs, so a common setup offers two scheduler profiles: bin-packing for experiments and spreading for critical work.
→ See [Phase 1: filtering](#d01-1), [Phase 2: scoring](#d01-3) and [Score × weight](#d01-4).

### 2. Placement decays, so something has to re-pack

Good initial placement doesn't last: jobs finish at different times and the cluster fragments. The **descheduler** periodically evicts pods so the scheduler can place them again. Its strategy must match the scheduler's: `HighNodeUtilization` goes with `MostAllocated`, and `LowNodeUtilization` goes with `LeastAllocated`. Mixing them up creates an eviction loop. For training, every eviction costs a checkpoint and restart, so PodDisruptionBudgets, quiet maintenance windows and namespace exclusions matter.
→ See [Scheduler thrashing](#d05-9), which is the same kind of feedback loop between controllers.

### 3. Gang scheduling: all or nothing

PyTorch workers meet at a **rendezvous barrier** before training starts. If 7 of 8 workers are placed and the 8th can't be, the 7 sit on GPUs doing nothing, and that capacity is wasted. Gang scheduling (coscheduling) keeps the whole group pending until all of it fits. The options sit at different layers:

- the **coscheduling plugin** (PodGroup with `minMember` and a timeout) inside kube-scheduler,
- **Kueue**, which decides at the job level whether a job may start,
- **KAI Scheduler** and **Volcano**, which replace the scheduler.

A native Workload API for gang scheduling is also being worked on upstream (KEP-4671).
→ See [Gang scheduling](#d02-2) and [Volcano vs. Kueue](#d02-6).

### 4. Topology: the right GPUs, not just enough GPUs

Where the GPUs sit matters as much as how many there are. NVLink inside a node is far faster than InfiniBand between nodes, which is far faster than plain Ethernet. Tensor parallelism and FSDP talk on every forward and backward pass, so they suffer most from a bad placement. The approaches differ:

- **Kueue** models topology as ResourceFlavors (node labels such as interconnect and fabric) and lists the premium flavor first, with a fallback.
- **KAI** reads GPU topology directly from the NVIDIA stack.
- **Volcano** scores nodes using topology labels.

Inside a node, the kubelet's **Topology Manager** keeps a pod's CPUs and GPUs on the same NUMA node.
→ See [Topology bin-packing](#d02-3), [Hardware-aware scheduling](#d02-5) and [NUMA and CPU pinning](#d08-5).

### 5. Quota as a service, not a wall

Plain ResourceQuotas are hard walls per namespace: one team's idle GPUs can't help another team's queue. The chapter's "GPU as a Service" model uses Kueue in two tiers:

- **ClusterQueues** hold nominal quota and borrowing limits.
- **LocalQueues** are what teams submit to.
- A **cohort** lets teams borrow each other's idle quota, and the owner reclaims it by preemption when it needs it back.

**WorkloadPriorityClasses** decide admission order, and fair sharing favors teams that have used less recently. Flavor order and fungibility (`TryNextFlavor`) decide *which hardware* a job gets. Priority decides *who goes first*. These are separate decisions.
→ See [Queueing and fair sharing](#d02-4) and [Priority, preemption and QoS](#d03-2).

### 6. How the pieces fit

The recommended stack layers the tools by job: Kueue for admission and quota on top, a gang-aware or topology-aware scheduler underneath (kube-scheduler with plugins, KAI or Volcano), and a workload API such as PyTorchJob/TrainJob, JobSet or LeaderWorkerSet that groups the pods. That is also the stack I would start from on EKS.
