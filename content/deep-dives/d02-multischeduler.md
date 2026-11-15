---
id: d-multischeduler
title: "When one scheduler isn't enough"
short: "Kubernetes Multi-Scheduler"
---
The default scheduler places pods one at a time and knows nothing about jobs, teams or GPU topology. This deep dive covers the problems that pushed the ecosystem toward Kueue, Volcano, KAI and YuniKorn, and how to choose between them.

### How a Pod finds its scheduler

**Scenario.** A team runs one EKS cluster where some pods run on EC2 nodes and others on Fargate. Nobody on the team ever sets `schedulerName` by hand, yet the staging pods always end up on Fargate and everything else on EC2. A new engineer asks how Kubernetes knows which scheduler should handle each pod.

![How a Pod finds its scheduler](diagrams/sections/d02-1.svg)

**What the picture shows.** How a pod is routed to the right scheduler, using Fargate as the example.

- **Workflow, step 1 (user submission and Fargate profile):** you create a Fargate profile in AWS that says "any pod in the `staging` namespace with the label `type: serverless` runs on Fargate". Then you submit an ordinary pod YAML to the API server.

```yaml
# eksctl ClusterConfig
fargateProfiles:
- name: staging-serverless
  selectors:
  - namespace: staging
    labels:
      type: serverless
```

- **Step 2 (EKS control plane, mutating webhook):** the EKS mutating webhook sees the pod, checks it against your profiles, and on a match injects `schedulerName: fargate-scheduler` into the pod spec, before scheduling starts. Pods that don't match keep the standard spec.

```yaml
# what the webhook adds to a matching pod
spec:
  schedulerName: fargate-scheduler
```

- **Step 3 (scheduling phase, the result):** the modified pod is picked up by the AWS Fargate scheduler.
- **How pods reach the right scheduler:** the API server doesn't route pods; it only stores them. Every scheduler watches for unscheduled pods (no `nodeName` yet) and takes only the ones whose `schedulerName` matches its own name, like separate buckets.
  - **Bucket A (`default-scheduler`):** the standard kube-scheduler on the control plane places these on EC2 nodes.
  - **Bucket B (`fargate-scheduler`):** the Fargate scheduler takes only these, ignores bucket A, and starts a dedicated micro-VM for each pod.

**Going deeper.**

- Ray (through KubeRay) is another two-scheduler setup.
- Kubernetes places and restarts the Ray head and worker pods, and Ray's own scheduler places tasks and actors inside them.
- That suits Python teams well, but capacity accounting and debugging now span two layers.

### Gang scheduling

**Scenario.** A research team submits a distributed PyTorch job with 8 GPU workers. The cluster only has room for 6, so the default scheduler starts 6 workers and leaves 2 `Pending`. The 6 running workers sit idle for hours, burning expensive GPU time while they wait for teammates that may never arrive. How do you stop that from happening?

![Gang scheduling](diagrams/sections/d02-2.svg)

**What the picture shows.** Gang scheduling (all-or-nothing) and why the default scheduler can't do it.

- **The problem:** standard Kubernetes schedules pods one at a time. In distributed training, all 8 workers must start together to run collective operations such as all-reduce. If only 6 fit, those 6 start and wait, wasting GPU time.
- **The custom solution:** a gang scheduler won't start any of the job's pods until the cluster has room for all of them.
- **Can the standard scheduler do it?** No, it has no all-or-nothing start.
  - **The workaround:** you calculate cluster capacity yourself and make sure at least 8 GPU slots are free before you run `kubectl apply`.
  - **The failure point:** in a shared cluster, a coworker's 1-GPU pod submitted one second before yours can take a slot. Your job starts with 7 workers, hangs, and wastes money. Standard Kubernetes can't say "hold these 7 slots until the 8th is free."
- **Best at this:** Volcano and KAI are built specifically for this kind of atomic scheduling.

**Going deeper.** The reason is the **rendezvous barrier**: distributed PyTorch workers wait until every rank has joined, so 7 of 8 workers will sit on their GPUs forever. The options sit at different layers:

- the **coscheduling plugin**: a PodGroup with `minMember` and a timeout, inside kube-scheduler;
- **Kueue**: decides at the job level whether a job may start;
- **KAI** or **Volcano**: replace the scheduler.

Upstream is also designing a native Workload API for this. Model-parallel *inference* has the same property: if one shard's pod is missing or dies, the whole replica is down, so it needs all-or-nothing scheduling and group restarts too.

### Topology bin-packing

**Scenario.** A training job's two pods talk to each other constantly. The default scheduler puts them on two different GPU nodes because both nodes look "least allocated". Now every gradient exchange crosses the network instead of NVLink, and the training run takes several times longer than expected.

![Topology bin-packing](diagrams/sections/d02-3.svg)

**What the picture shows.** Bin-packing for GPU topology: keeping chatty pods on the same fast interconnect.

- **The problem:** the default LeastAllocated scoring spreads pods across nodes. In ML that's bad for performance, because pod-to-pod traffic then goes over the much slower network.
- **The goal:** a scheduler that uses MostAllocated (bin-packing) to put the pods on the same physical GPU node.
- **The benefit:** pods can talk over NVLink, which can be 10–50 times faster than standard Ethernet, cutting training time sharply.
- **Topology-aware bin-packing:** goes further and looks at how the GPUs are wired. On an NVIDIA HGX system, it places pods that talk to each other on GPUs linked by NVLink (fast) rather than only PCIe (slow).
- **Can the standard scheduler do it?** Partly.
  - **The workaround:** a required `podAffinity` with `matchLabels: job: my-training-run` and `topologyKey: kubernetes.io/hostname`, which forces pod 2 onto the node where pod 1 landed.

```yaml
affinity:
  podAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
    - labelSelector:
        matchLabels:
          job: my-training-run
      topologyKey: kubernetes.io/hostname
```

  - **The failure point:** it only works if pod 1 finds a home first, and writing these rules for 100 pods is a management nightmare. A custom scheduler does it automatically for the whole batch.
- **Best at this:** the KAI Scheduler, which uses NVIDIA's GPU Operator to see the exact NVLink fabric.

**Going deeper.** Placement depends on how the model is split:

- **Tensor parallelism** splits every layer and exchanges results constantly. Keep it inside one node on NVLink/NVSwitch.
- **Pipeline parallelism** splits by layers and only hands activations across once per stage, so it tolerates the slower links between nodes.
- **Hybrid:** tensor parallelism inside nodes and pipeline parallelism between them is the common setup.

Even inside one server, not every GPU pair may share the same NVLink group. `nvidia-smi topo -m` shows it, and a job may rightly wait in the queue rather than start on badly connected GPUs.

### Queueing and fair sharing

**Scenario.** In a shared research cluster, one user submits 100 training jobs on Monday morning and every GPU is taken. Other teams' jobs fail immediately with quota errors instead of waiting their turn, and over the weekend the GPUs reserved for one team sit idle while another team is desperate for capacity.

![Queueing and fair sharing](diagrams/sections/d02-4.svg)

**What the picture shows.** Queueing and fair sharing, and where namespace quotas fall short.

- **The problem:** one greedy user can lock everyone else out of a shared cluster.
- **Priority queues:** custom schedulers such as Kueue let you define resource flavors and workload priorities, so jobs wait in line instead of failing.
- **Preemption:** a high-priority inference pod serving a live model can evict a low-priority research training pod when GPUs run short.
- **Fair sharing:** when the cluster is idle one team can use all of it, but as soon as another team submits work, the first team's over-quota pods are preempted to give the second team its fair share.
- **Can the standard scheduler do it?** Only with static quotas.
  - **The workaround:** a `ResourceQuota` per namespace, for example 10 GPUs for the marketing team.

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: gpu-quota
  namespace: marketing
spec:
  hard:
    requests.nvidia.com/gpu: "10"   # static: idle GPUs can't be borrowed
```

  - **The failure point:** these quotas are static. If marketing isn't using its 10 GPUs, no one else can borrow them. And a team that hits its limit gets an immediate error; the pod doesn't queue and start later when space opens up.
- **Best at this:** Apache YuniKorn and Run:ai (via KAI).

**Going deeper.** In Kueue terms:

- **ClusterQueues** hold each team's nominal quota and borrowing limit.
- Teams submit to **LocalQueues**.
- A **cohort** lends idle quota between teams and takes it back by preemption when its owner needs it.

WorkloadPriorityClasses decide admission order, and fair sharing favors teams that have used less recently. Hard namespace ResourceQuotas can't do any of this: idle GPUs in one namespace never help another team's queue.

### Hardware-aware scheduling

**Scenario.** A company buys a new rack of Blackwell GPUs connected by InfiniBand. Its inference team needs FP4 support, and its training team needs all workers on the same InfiniBand switch. The platform team wonders whether node labels are enough, or whether they need a custom scheduler.

![Hardware-aware scheduling](diagrams/sections/d02-5.svg)

**What the picture shows.** Hardware-aware scheduling for new GPU generations.

- **What custom schedulers can look for:** hardware features Kubernetes doesn't track well on its own.
  - **RDMA / InfiniBand:** placing pods on nodes that share the same high-speed InfiniBand switch.
  - **Low-precision support:** sending a workload only to GPUs that support the number formats it needs, such as FP8 (Hopper and newer) or FP4 (Blackwell).
- **Can the standard scheduler do it?** Yes, to a point.
  - **The workaround:** label nodes by hand (`gpu-gen=blackwell`, `network=infiniband`) and add matching `nodeSelector`s to the AI pods, together with taints and tolerations.

```yaml
spec:
  nodeSelector:
    gpu-gen: blackwell
    network: infiniband
  tolerations:
  - key: nvidia.com/gpu
    operator: Exists
    effect: NoSchedule
```

  - **The failure point:** this actually works well, and it's how most teams start. A custom scheduler only becomes necessary with thousands of nodes, when hand-managing labels for every hardware variation (FP8, HBM3, NVLink v4 and so on) becomes impractical.
- **Leading example:** NVIDIA's KAI Scheduler, which is being extended to manage rack-scale Vera Rubin NVL72 systems, where the whole rack behaves like one giant GPU.

**Going deeper.**

- NVIDIA's GPU Feature Discovery labels every node with the GPU product (e.g. `A100-SXM4-40GB`), memory, architecture family, driver and CUDA versions, MIG capability, and the number of shared replicas.
- Kueue turns these labels into **ResourceFlavors**: the premium flavor (NVLink + InfiniBand) is listed first, with a fallback flavor and `TryNextFlavor`.
- Which hardware a job gets (flavor) and who goes first (priority) stay separate decisions.

### Volcano vs. Kueue

**Scenario.** The platform team must pick a batch scheduler for a shared GPU cluster. One group runs PyTorch jobs with a master pod and 100 workers; another group is a set of departments that share 128 GPUs and want to borrow each other's idle capacity. Should they choose Volcano or Kueue?

![Volcano vs. Kueue](diagrams/sections/d02-6.svg)

**What the picture shows.** A side-by-side of the two most common choices, with a scenario for each.

- **Volcano, the batch specialist:** a full standalone batch scheduling system on Kubernetes, donated by Huawei and now a CNCF incubating project. Best for long training jobs, big data (Spark, Flink) and HPC. Its key feature is **gang scheduling**: it treats a group of pods as one job and starts none until there's room for all. You submit a `VolcanoJob` custom resource instead of individual pods.

```yaml
apiVersion: batch.volcano.sh/v1alpha1
kind: Job
metadata:
  name: pytorch-train
spec:
  schedulerName: volcano
  minAvailable: 9            # gang: start nothing until all 9 fit
  tasks:
  - name: master
    replicas: 1
    template: {}             # pod template omitted
  - name: worker
    replicas: 8
    template: {}
```

- **Kueue, the resource quota manager:** a newer, cloud-native job queueing controller. Instead of replacing the scheduler, it is a gatekeeper in front of the default one. Best for shared clusters with many teams, multi-cloud and cloud bursting. It manages a global pool (say 100 A100s), holds jobs in a waitlist (a `ClusterQueue`), and **admits** a job when its quota allows; the standard scheduler then places the pods.
- **The shared scenario:** teams A and B each need 4 GPUs and the cluster has 4. The default scheduler might give each team 2, and both jobs wait forever. Volcano or Kueue gives all 4 to team A, which finishes, and then team B gets the cluster. Utilization goes up.
- **Volcano's strength, task dependencies:** in a PyTorch job with one master and many workers, if the master fails filtering (for example a port conflict), the workers would sit idle forever. Volcano can say "start the 100 workers only if the master is placed." Kueue relies on the default scheduler, which doesn't understand relationships between pod types in a job.
- **Kueue's strength, fair sharing and borrowing:** marketing has a 10-GPU quota, data science has 50. On Friday night marketing is idle and data science needs 60. Kueue sees that marketing's cohort is empty and lends its 10 GPUs. If someone in marketing submits a job on Saturday morning, Kueue preempts the borrowed work and returns the guaranteed GPUs. Volcano focuses on the performance of each job, not on sharing a budget between departments.

```yaml
apiVersion: kueue.x-k8s.io/v1beta1
kind: ClusterQueue
metadata:
  name: data-science
spec:
  cohort: gpu-pool           # queues in a cohort can lend to each other
  namespaceSelector: {}
  resourceGroups:
  - coveredResources: ["nvidia.com/gpu"]
    flavors:
    - name: a100
      resources:
      - name: nvidia.com/gpu
        nominalQuota: 50
        borrowingLimit: 10
  preemption:
    reclaimWithinCohort: Any # lender can take its GPUs back
---
# a job joins the queue with one label
metadata:
  labels:
    kueue.x-k8s.io/queue-name: data-science
```

**Going deeper.**

- The production answer is usually "both layers": Kueue for admission and quota on top of a gang-aware or topology-aware scheduler (kube-scheduler with plugins, KAI or Volcano), with a workload API that groups the pods (PyTorchJob/TrainJob, JobSet, LeaderWorkerSet).
- Kueue stays vendor-neutral and keeps policy separate from mechanism.
- A replacement scheduler is worth it when GPU-specific features (fractional GPUs, deep topology awareness) pay for the extra moving part.

- For fine-tuning, **Kubeflow Trainer** shows the same layering.
- The platform team publishes a `TrainingRuntime` (framework, image, defaults), and data scientists submit a `TrainJob`, often straight from a Python SDK.
- The controller turns the pair into a **JobSet**, a group of Jobs that Kueue can then admit as one gang.

### The landscape

**Scenario.** A manager asks for a one-page overview of the scheduler landscape before approving a proposal. They want to know which tools exist, who maintains them, and which one fits which kind of organization.

![The landscape](diagrams/sections/d02-7.svg)

**What the picture shows.** Two comparison tables and four short "best for" notes.

- **Custom schedulers comparison (top):** NVIDIA KAI Scheduler, Volcano and Apache YuniKorn compared on five features.
  - **Gang scheduling:** KAI native (hierarchical PodGroups), Volcano native (PodGroups), YuniKorn native.
  - **GPU bin-packing:** KAI topology-aware (NVLink/PCIe), Volcano basic bin-packing, YuniKorn hierarchical bin-packing.
  - **Fair sharing:** KAI advanced (fair share and dynamic quotas), Volcano queue-based weights, YuniKorn hierarchical queues.
  - **Hardware awareness:** KAI full (Blackwell / Vera Rubin), Volcano limited (generic GPU), YuniKorn generic.
  - **Primary use case:** KAI for massive AI clusters on NVIDIA hardware, Volcano for general batch and high performance, YuniKorn for multi-tenant enterprise and Spark.
- **Market matrix (bottom):** five products and who maintains them: Volcano (CNCF incubating), Kueue (Kubernetes SIG), Armada (CNCF sandbox), NVIDIA Run:ai (commercial) and YuniKorn (Apache project). All five now cover gang scheduling, bin-packing, fair sharing and some hardware awareness, so the choice comes down to architecture and fit.
- **The four notes on the left:**
  - **Volcano:** a full replacement for the default scheduler that understands jobs, not just pods. Best when you want gang scheduling and NVLink topology awareness in one tool.
  - **Kueue:** doesn't replace the scheduler; it manages quotas and fair sharing *before* pods are created. Best for multi-tenant clusters where teams borrow from each other.
  - **Armada:** a meta-scheduler that looks across several clusters and picks the one with the best bin-packing opportunity. Best when a single cluster is too small.
  - **Run:ai:** fractional GPUs (one A100 split across small jobs) plus a central fair-share dashboard for managers. Best for large enterprises that need a polished UI and strict cost control on H100 / Blackwell clusters.

### What every scheduler relies on

**Scenario.** Whichever scheduler the team picks, someone asks: "How does the scheduler even know a node has 8 GPUs, or that two nodes share an InfiniBand switch?" Without that information, no scheduler can make a smart decision.

![What every scheduler relies on](diagrams/sections/d02-8.svg)

**What the picture shows.** The plumbing every scheduler depends on, plus a note on YuniKorn.

- **YuniKorn:** originally built to bring the scheduling power of Hadoop YARN to Kubernetes. It runs as a replacement (or shim) for the default scheduler.
- **The common foundation:** whatever scheduler you choose, you'll almost certainly run the NVIDIA GPU Operator or the NVIDIA device plugin. These components tell the scheduler what hardware is in each node.
  - **NVIDIA device plugin:** advertises `nvidia.com/gpu` to the API server, so GPUs become a countable resource.
  - **Network topology operator:** advertises InfiniBand or RDMA status, so the scheduler can group pods on the same high-speed switch.

**Going deeper.** On NVIDIA nodes, one **GPU Operator** (configured through a `ClusterPolicy`) installs and maintains everything a scheduler depends on:

- the driver container;
- the container toolkit, which injects `/dev/nvidia*` into containers;
- the device plugin;
- GPU Feature Discovery labels;
- the MIG manager;
- the DCGM exporter, which sends GPU utilization, memory, ECC errors, temperature and power to Prometheus.

If the device plugin isn't running, GPU pods simply stay Pending.
