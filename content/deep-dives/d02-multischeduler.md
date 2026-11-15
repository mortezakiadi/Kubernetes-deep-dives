---
id: d-multischeduler
title: "When one scheduler isn't enough"
short: "EKS Multi-Scheduler"
---
The default scheduler places pods one at a time and knows nothing about jobs, teams or GPU topology. This tab covers the problems that pushed the ecosystem toward Kueue, Volcano, KAI and YuniKorn, and how to choose between them.

### How a Pod finds its scheduler

**Scenario.** On EKS, some pods run on Fargate and others on EC2 nodes, and nobody set `schedulerName` by hand.

![How a Pod finds its scheduler](diagrams/sections/d02-1.svg)

**What the picture shows.** Every scheduler watches for unscheduled pods whose `schedulerName` matches its own. For Fargate, a mutating webhook sets `fargate-scheduler` on pods that match a Fargate profile. The table compares the default scheduler with an ML-oriented one.

### Gang scheduling

**Scenario.** An 8-worker PyTorch job only fits 6 workers. Six GPUs sit allocated, and nothing trains.

![Gang scheduling](diagrams/sections/d02-2.svg)

**What the picture shows.** Pod-by-pod placement vs. all-or-nothing placement. A gang scheduler keeps every pod pending until the whole group fits.

> **From the chapter:** the reason is PyTorch's **rendezvous barrier**: no worker starts until all of them have joined. The chapter's options: the coscheduling plugin (a PodGroup with `minMember` and a timeout), Kueue's job-level admission, or a replacement scheduler (KAI, Volcano). Upstream Kubernetes is also designing a native Workload API for this.

### Topology bin-packing

**Scenario.** Two pods that exchange tensors constantly end up on different nodes, and step time doubles.

![Topology bin-packing](diagrams/sections/d02-3.svg)

**What the picture shows.** Spreading vs. packing onto one NVLink-connected node. For tightly coupled training, the default spreader is the wrong instinct.

> **From the chapter:** NVLink inside a node is far faster than InfiniBand across nodes, and Ethernet is slower still. Tensor parallelism and FSDP talk on every forward and backward pass, so placement matters most for them. A job may rightly wait in the queue rather than start on badly connected GPUs.

### Queueing and fair sharing

**Scenario.** One researcher submits 100 jobs on Friday night, and nobody else gets a GPU all weekend.

![Queueing and fair sharing](diagrams/sections/d02-4.svg)

**What the picture shows.** A greedy FIFO vs. queues with priorities and fair share, and preemption of low-priority research work when inference needs capacity.

> **From the chapter:** this is the "GPU as a Service" model. In Kueue, a ClusterQueue holds nominal quota and borrowing limits, teams submit to LocalQueues, and a cohort lends idle quota and takes it back by preemption. Fair sharing favors teams that have used less recently.

### Hardware-aware scheduling

**Scenario.** An FP8 training job lands on older GPUs and runs at a fraction of the expected speed.

![Hardware-aware scheduling](diagrams/sections/d02-5.svg)

**What the picture shows.** A scheduler that matches workloads to hardware features (RDMA/InfiniBand switch, newer GPU generation) instead of only counting GPUs. Labels and selectors work to start with, and become painful across many hardware generations.

> **From the chapter:** Kueue expresses this as **ResourceFlavors** chosen by node labels (interconnect, fabric, GPU type). The premium flavor is listed first, with a fallback flavor and `TryNextFlavor`. KAI reads GPU topology directly from the NVIDIA stack.

### Volcano vs. Kueue

**Scenario.** The team has to pick one, and the debate keeps going in circles.

![Volcano vs. Kueue](diagrams/sections/d02-6.svg)

**What the picture shows.** Volcano replaces the scheduler and understands dependencies between task types (start workers only once the master is placed). Kueue sits in front of the default scheduler and manages quota, borrowing and fairness between teams.

> **From the chapter:** the common production answer is "both layers": Kueue for admission and quota on top of a gang- or topology-aware scheduler, with a workload API (PyTorchJob/TrainJob, JobSet, LeaderWorkerSet) that groups the pods.

### The landscape

**Scenario.** Leadership asks for one page that compares the options.

![The landscape](diagrams/sections/d02-7.svg)

**What the picture shows.** KAI, Volcano and YuniKorn compared on gang scheduling, GPU bin-packing, fair sharing and hardware awareness, plus a market matrix that includes Kueue and Armada.

### What every scheduler relies on

**Scenario.** Whichever scheduler we choose, how does it know what hardware a node has?

![What every scheduler relies on](diagrams/sections/d02-8.svg)

**What the picture shows.** The NVIDIA device plugin advertises `nvidia.com/gpu`, and topology operators label nodes with InfiniBand/RDMA details. Every scheduler reads these labels and resources from the API server.
