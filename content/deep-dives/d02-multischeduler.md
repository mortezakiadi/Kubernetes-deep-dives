---
id: d-multischeduler
title: "When one scheduler isn't enough"
short: "EKS Multi-Scheduler"
---
The default scheduler places pods one at a time and knows nothing about jobs, teams or GPU topology. This deep dive covers the problems that pushed the ecosystem toward Kueue, Volcano, KAI and YuniKorn, and how to choose between them.

### How a Pod finds its scheduler

**Scenario.** On EKS, some pods run on Fargate and others on EC2 nodes, and nobody set `schedulerName` by hand.

![How a Pod finds its scheduler](diagrams/sections/d02-1.svg)

**What the picture shows.** Every scheduler watches for unscheduled pods whose `schedulerName` matches its own. For Fargate, a mutating webhook sets `fargate-scheduler` on pods that match a Fargate profile. The table compares the default scheduler with an ML-oriented one.

**Going deeper.** Ray (through KubeRay) is another two-scheduler setup. Kubernetes places and restarts the Ray head and worker pods, and Ray's own scheduler places tasks and actors inside them. That suits Python teams well, but capacity accounting and debugging now span two layers.

### Gang scheduling

**Scenario.** An 8-worker PyTorch job only fits 6 workers. Six GPUs sit allocated, and nothing trains.

![Gang scheduling](diagrams/sections/d02-2.svg)

**What the picture shows.** Pod-by-pod placement vs. all-or-nothing placement. A gang scheduler keeps every pod pending until the whole group fits.

**Going deeper.** The reason is the **rendezvous barrier**: distributed PyTorch workers wait until every rank has joined, so 7 of 8 workers will sit on their GPUs forever. The options sit at different layers:

- the **coscheduling plugin**: a PodGroup with `minMember` and a timeout, inside kube-scheduler;
- **Kueue**: decides at the job level whether a job may start;
- **KAI** or **Volcano**: replace the scheduler.

Upstream is also designing a native Workload API for this. Model-parallel *inference* has the same property: if one shard's pod is missing or dies, the whole replica is down, so it needs all-or-nothing scheduling and group restarts too.

### Topology bin-packing

**Scenario.** Two pods that exchange tensors constantly end up on different nodes, and step time doubles.

![Topology bin-packing](diagrams/sections/d02-3.svg)

**What the picture shows.** Spreading vs. packing onto one NVLink-connected node. For tightly coupled training, the default spreader is the wrong instinct.

**Going deeper.** Placement depends on how the model is split:

- **Tensor parallelism** splits every layer and exchanges results constantly. Keep it inside one node on NVLink/NVSwitch.
- **Pipeline parallelism** splits by layers and only hands activations across once per stage, so it tolerates the slower links between nodes.
- **Hybrid:** tensor parallelism inside nodes and pipeline parallelism between them is the common setup.

Even inside one server, not every GPU pair may share the same NVLink group. `nvidia-smi topo -m` shows it, and a job may rightly wait in the queue rather than start on badly connected GPUs.

### Queueing and fair sharing

**Scenario.** One researcher submits 100 jobs on Friday night, and nobody else gets a GPU all weekend.

![Queueing and fair sharing](diagrams/sections/d02-4.svg)

**What the picture shows.** A greedy FIFO vs. queues with priorities and fair share, and preemption of low-priority research work when inference needs capacity.

**Going deeper.** In Kueue terms:

- **ClusterQueues** hold each team's nominal quota and borrowing limit.
- Teams submit to **LocalQueues**.
- A **cohort** lends idle quota between teams and takes it back by preemption when its owner needs it.

WorkloadPriorityClasses decide admission order, and fair sharing favors teams that have used less recently. Hard namespace ResourceQuotas can't do any of this: idle GPUs in one namespace never help another team's queue.

### Hardware-aware scheduling

**Scenario.** An FP8 training job lands on older GPUs and runs at a fraction of the expected speed.

![Hardware-aware scheduling](diagrams/sections/d02-5.svg)

**What the picture shows.** A scheduler that matches workloads to hardware features (RDMA/InfiniBand switch, newer GPU generation) instead of only counting GPUs. Labels and selectors work to start with, and become painful across many hardware generations.

**Going deeper.** NVIDIA's GPU Feature Discovery labels every node with the GPU product (e.g. `A100-SXM4-40GB`), memory, architecture family, driver and CUDA versions, MIG capability, and the number of shared replicas. Kueue turns these labels into **ResourceFlavors**: the premium flavor (NVLink + InfiniBand) is listed first, with a fallback flavor and `TryNextFlavor`. Which hardware a job gets (flavor) and who goes first (priority) stay separate decisions.

### Volcano vs. Kueue

**Scenario.** The team has to pick one, and the debate keeps going in circles.

![Volcano vs. Kueue](diagrams/sections/d02-6.svg)

**What the picture shows.** Volcano replaces the scheduler and understands dependencies between task types (start workers only once the master is placed). Kueue sits in front of the default scheduler and manages quota, borrowing and fairness between teams.

**Going deeper.** The production answer is usually "both layers": Kueue for admission and quota on top of a gang-aware or topology-aware scheduler (kube-scheduler with plugins, KAI or Volcano), with a workload API that groups the pods (PyTorchJob/TrainJob, JobSet, LeaderWorkerSet). Kueue stays vendor-neutral and keeps policy separate from mechanism. A replacement scheduler is worth it when GPU-specific features (fractional GPUs, deep topology awareness) pay for the extra moving part.

For fine-tuning, **Kubeflow Trainer** shows the same layering. The platform team publishes a `TrainingRuntime` (framework, image, defaults), and data scientists submit a `TrainJob`, often straight from a Python SDK. The controller turns the pair into a **JobSet**, a group of Jobs that Kueue can then admit as one gang.

### The landscape

**Scenario.** Leadership asks for one page that compares the options.

![The landscape](diagrams/sections/d02-7.svg)

**What the picture shows.** KAI, Volcano and YuniKorn compared on gang scheduling, GPU bin-packing, fair sharing and hardware awareness, plus a market matrix that includes Kueue and Armada.

### What every scheduler relies on

**Scenario.** Whichever scheduler we choose, how does it know what hardware a node has?

![What every scheduler relies on](diagrams/sections/d02-8.svg)

**What the picture shows.** The NVIDIA device plugin advertises `nvidia.com/gpu`, and topology operators label nodes with InfiniBand/RDMA details. Every scheduler reads these labels and resources from the API server.

**Going deeper.** On NVIDIA nodes, one **GPU Operator** (configured through a `ClusterPolicy`) installs and maintains everything a scheduler depends on:

- the driver container;
- the container toolkit, which injects `/dev/nvidia*` into containers;
- the device plugin;
- GPU Feature Discovery labels;
- the MIG manager;
- the DCGM exporter, which sends GPU utilization, memory, ECC errors, temperature and power to Prometheus.

If the device plugin isn't running, GPU pods simply stay Pending.
