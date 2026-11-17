---
id: design-multicluster
title: "Design 02: Operating CPU and GPU clusters as one compute platform"
short: "Multi-cluster CPU and GPU platform"
---
This design answers one question: *how do we run several CPU and GPU clusters, in more than one region, so that teams see one platform with one way to submit work, fair access to scarce GPUs and real isolation from each other?* It builds on [Queueing and fair sharing](#d02-4), [Gang scheduling](#d02-2), [Dynamic Resource Allocation](#d06-4), [Placement, autoscaling and cost](#d03-3) and [Tenant classes, soft and hard isolation](#d07-1). Everything here targets Amazon EKS.

The short version: clusters are *cattle with roles* (CPU, GPU training, GPU inference), created and configured from Git. Batch and training work is submitted to one *hub* cluster, where Kueue's MultiKueue decides which worker cluster runs it. Inside each cluster, Kueue handles quotas and fair sharing, kube-scheduler with DRA places the pods, and Karpenter buys the nodes. Long-running services and inference aren't scheduled across clusters at runtime; GitOps places them, and global routing spreads their traffic.

### The use case and requirements

**Scenario.** The company grew five EKS clusters by accident: two CPU clusters for services and data pipelines, a training cluster with H100 Capacity Blocks in us-east-1, and two inference clusters in us-east-1 and eu-west-1. Each has its own quotas and its own Slack channel for "who is using the GPUs?". Training jobs wait for days in one cluster while reserved H100s idle in another, and two teams have quietly started buying their own capacity. Leadership asks the platform team to make it one platform.

**What the design must do.**

- **One front door for batch work:** a team submits a training or batch job once, without knowing which cluster will run it.
- **Fair, predictable GPU access:** each team has a guaranteed share, can borrow what others aren't using, and gets it back when it needs it.
- **High utilization of expensive capacity:** reserved GPUs should rarely sit idle; anything idle should be lent out.
- **Autoscaling that matches the work:** CPU scales freely; GPUs come from reserved capacity first, then on-demand, then Spot, according to rules, not luck.
- **Isolation between tenants:** teams can't see or starve each other, and regulated tenants get stronger walls.
- **Operable at scale:** adding a cluster, upgrading one or losing one is routine.

**What it must avoid.**

- One giant cluster where one bad upgrade or one control-plane overload stops everything.
- One cluster per team, which multiplies operations work and strands capacity.
- Teams bypassing the queues by creating pods directly on GPU nodes.

**Out of scope for this design:** sandboxing untrusted code (that's [Design 01](#design-sandboxing)), and the internals of model serving engines.

### The architecture at a glance

**Scenario.** The team needs one picture that shows where users submit work, which component decides where it runs, and how the clusters are kept consistent.

![The architecture at a glance](diagrams/designs/ds02-1.svg)

**What the picture shows.** A hub cluster that runs no user workloads, worker clusters grouped by role, and the two paths work takes: queued batch jobs through MultiKueue, long-running services through GitOps.

- **Hub cluster:** a small cluster with no user workloads. It runs the Kueue manager with MultiKueue, Argo CD for the whole fleet, policy and the fleet's dashboards. It's the single place where teams submit batch and training jobs.
- **MultiKueue (in the hub):** when a job is submitted, the hub's Kueue reserves the team's quota, then copies the job's Workload to candidate worker clusters. The first worker whose own Kueue admits it wins; the copies elsewhere are deleted, and the job's status is synced back to the hub.
- **CPU clusters:** services, data pipelines and CI, one per region. Also MultiKueue workers for CPU-only batch jobs.
- **GPU training clusters:** large clusters with Capacity Blocks, EFA networking and topology-aware placement. They receive training and fine-tuning jobs only through MultiKueue.
- **GPU inference clusters:** at least two regions. Model servers are deployed by GitOps, scale with demand inside each cluster, and receive traffic from a global entry point.
- **Fleet GitOps:** Argo CD, with ApplicationSets, installs the same platform add-ons on every cluster according to its role label, and stamps the namespaces, LocalQueues and ClusterQueues that MultiKueue expects on every worker.
- **Shared services:** identity (IAM Identity Center and EKS Pod Identity), container images, model weights and datasets in S3 per region, metrics and cost data aggregated in one place.

**Going deeper.**

- **Why a hub with no workloads:** the hub holds the queue state for the whole company. Keeping it small, boring and separate means a noisy training job can never take down scheduling for everyone, and the hub can be upgraded on its own schedule. MultiKueue also requires that the manager isn't one of its own workers.
- **Only batch work is scheduled across clusters at runtime.** Services and inference need stable endpoints, warm caches and steady capacity. Moving them per request is the job of the traffic layer, not the scheduler.
- **The hub is not in the data path.** If it's down, running jobs keep running in their worker clusters and inference keeps serving. Only new batch submissions wait.

### Why several clusters, and how to cut them

**Scenario.** One architect argues for "one big cluster per region, simpler". Another wants "one cluster per team, safer". The team needs a rule for when a new cluster is justified.

**The rule: split clusters by failure domain and workload role, never by team.** Teams are separated inside clusters with namespaces, queues and policies; clusters are separated when the *infrastructure* needs to be different or fail independently.

| Split by | Why it's worth a separate cluster | Example |
|---|---|---|
| Region | Data residency, latency, regional failure, where GPU capacity actually exists | Inference in us-east-1 and eu-west-1 |
| Workload role | Different node types, add-ons, upgrade cadence and disruption rules | Training clusters never consolidate GPU nodes mid-job; CPU clusters do it constantly |
| Scale limit | A cluster's control plane, IP space and etcd have practical limits | A second training cluster once the first reaches its node or pod ceiling |
| Hard isolation | A regulated tenant that must not share nodes or a control plane | A dedicated cluster for one customer's data |
| Team | Not a reason on its own | Use namespaces, Kueue quotas and policy instead |

**Going deeper.**

- **Every cluster is created from the same definition.** With EKS Capabilities, Argo CD, ACK and kro run as managed services: a `ResourceGraphDefinition` describes "a GPU training cluster" (VPC, EKS cluster, Karpenter NodePools, add-ons), and a new cluster is one short instance of it in Git.
- **Fewer, larger clusters are usually better for GPUs.** Splitting GPUs across many small clusters creates islands that can't lend to each other. MultiKueue reduces that cost, but a gang of 64 GPUs still has to fit inside one cluster.
- **Keep Kubernetes versions within one minor version across the fleet,** so a job that runs in one worker runs in all of them, and the queue configuration doesn't need per-cluster exceptions.

### Four layers of scheduling decisions

**Scenario.** A researcher asks why her 32-GPU job has been "pending" for an hour. Nobody can answer, because nobody can say which component is holding it. The team needs every decision to have one clear owner.

![Four layers of scheduling decisions](diagrams/designs/ds02-2.svg)

**What the picture shows.** Every batch job passes through four decisions, each made by a different component, in order.

- **Layer 1, which cluster (MultiKueue in the hub):** does the team have quota left in the company-wide queue, and which worker clusters should be offered the job? Pending here means the team is over its global share, or no worker has room.
- **Layer 2, whether and where in the cluster (Kueue in the worker):** does the team's ClusterQueue in this cluster have quota, can it borrow from the cohort, and, for topology-aware jobs, is there a block of nodes close enough on the network? Kueue admits the whole job at once or not at all.
- **Layer 3, which node and which device (kube-scheduler with DRA):** places each pod on a node with the right GPUs, using ResourceClaims for whole GPUs or MIG slices, and honours the node assignments Kueue's topology-aware scheduling chose.
- **Layer 4, buy the node (Karpenter):** pods that can't be placed trigger new nodes from the right NodePool: reserved first, then on-demand, then Spot, within each pool's limits.
- **One status, one place:** the job's Workload object carries the state of layers 1 and 2, including the cluster it was sent to. Platform dashboards show pending time per layer, so "why is it pending?" has a one-line answer.

**Going deeper.**

- **Admission comes before placement.** Kueue keeps jobs suspended until it admits them, so pods never exist for work that has no quota. That's what stops a half-started 32-pod job holding 24 GPUs while waiting for the other 8, which is the core problem gang scheduling solves.
- **`waitForPodsReady` is the safety net:** if an admitted job's pods aren't all running within a timeout, Kueue evicts and requeues the whole job rather than letting it sit half-started. It's on by default in recent Kueue releases.
- **Native gang scheduling in kube-scheduler is coming.** Kubernetes 1.35 introduced the Workload API, and 1.36 split it into Workload and PodGroup APIs (still alpha). This design keeps Kueue as the gang owner for now and revisits once those APIs are stable.
- **Kueue and Karpenter don't talk directly.** Kueue's ProvisioningRequest admission check ("get me 8 nodes, then admit") is implemented by Cluster Autoscaler, not by Karpenter. With Karpenter, Kueue admits on quota and Karpenter reacts to the pods. That's why GPU quotas are set close to what reserved capacity can actually deliver.

### Queues, quotas and fair sharing

**Scenario.** Three teams share 256 reserved H100s. The LLM team needs most of them for a two-week run; the vision team needs bursts; research wants anything idle. Static per-team node pools left 40% of the GPUs idle last quarter. The team needs shares that are guaranteed but not wasted.

![Queues, quotas and fair sharing](diagrams/designs/ds02-3.svg)

**What the picture shows.** The quota model: what each team is guaranteed, what it can borrow and what it can be asked to give back.

- **ResourceFlavors:** one per kind of capacity, such as `h100-reserved`, `h100-ondemand`, `l4-spot`, `cpu-ondemand` and `cpu-spot`. A flavor maps to a Karpenter NodePool through node labels, so quota and capacity line up.
- **ClusterQueue per team:** each team's guaranteed share (`nominalQuota`) per flavor, in order of preference. A job tries the first flavor with room, so reserved GPUs are used before anyone pays for on-demand.
- **Cohort:** all team ClusterQueues belong to one cohort, so unused quota in one queue can be borrowed by another.
- **Borrowing and lending limits:** `borrowingLimit` caps how much a team can take beyond its share; `lendingLimit` caps how much of its share it ever lends, so a team keeps a small floor that's always available.
- **Reclaim:** when a team submits work within its own share, Kueue preempts borrowed workloads from other queues to give the share back. Preempted jobs are requeued, so they must checkpoint.
- **LocalQueue per namespace:** the only thing users reference, with the label `kueue.x-k8s.io/queue-name`. Users never see flavors or ClusterQueues.

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: team-llm
spec:
  cohortName: gpu-org                     # borrow from and lend to the other teams #!
  namespaceSelector:
    matchLabels: { platform.example.com/team: llm }
  preemption:
    reclaimWithinCohort: Any              # take back our share when we need it #!
    withinClusterQueue: LowerPriority
  resourceGroups:
  - coveredResources: ["nvidia.com/gpu", "cpu", "memory"]
    flavors:
    - name: h100-reserved                 # tried first: already paid for #!
      resources:
      - { name: "nvidia.com/gpu", nominalQuota: 128, borrowingLimit: 64, lendingLimit: 96 }
      - { name: "cpu", nominalQuota: 2000 }
      - { name: "memory", nominalQuota: 16Ti }
    - name: h100-ondemand                 # only if reserved is full and the team pays
      resources:
      - { name: "nvidia.com/gpu", nominalQuota: 32 }
      - { name: "cpu", nominalQuota: 500 }
      - { name: "memory", nominalQuota: 4Ti }
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  name: training
  namespace: llm-train
spec:
  clusterQueue: team-llm
```

**Going deeper.**

- **The same queues exist twice.** In the hub, each team's ClusterQueue holds its share of the whole fleet; in each worker, a ClusterQueue with the same name holds its share of that cluster. MultiKueue expects the job's namespace and LocalQueue to exist in every worker, and the hub's quota should equal the sum across workers. GitOps generates both from one table of team shares, so they can't drift.
- **Priority is separate from quota.** Three PriorityClasses are enough: `production` (inference, never preempted by batch), `standard` (scheduled training) and `opportunistic` (experiments and borrowed capacity, preempted first).
- **Fair sharing between borrowers:** when several teams want the same idle GPUs, Kueue's fair sharing divides the spare capacity by weight instead of first-come-first-served.
- **Guaranteed shares should match reality.** Kueue doesn't check quotas against actual nodes. If the sum of `h100-reserved` quotas is larger than the Capacity Blocks you own, admitted jobs will wait for nodes that will never come.

### Sending work to the right cluster

**Scenario.** A fine-tuning job with 16 GPUs is submitted to the hub. Two training clusters have room, one in each region, but the dataset lives in an S3 bucket in us-east-1, and copying 4 TB across regions would cost more than the GPU hours. The dispatch has to be smart about more than free GPUs.

![Sending work to the right cluster](diagrams/designs/ds02-4.svg)

**What the picture shows.** One job's path from submission to running in a worker, and back.

- **Step 1, submit:** the user creates a JobSet (or a Kubeflow TrainJob, RayJob or plain Job) in their namespace in the hub, pointing at their LocalQueue. Kueue keeps it suspended.
- **Step 2, reserve quota:** the hub's Kueue checks the team's fleet-wide ClusterQueue. If there's quota, the Workload gets a quota reservation; if not, it waits in line.
- **Step 3, nominate clusters:** the MultiKueue admission check picks candidate workers from the queue's `MultiKueueConfig`. With the **Incremental** dispatcher, it offers the job to a few clusters at a time, in the order listed, so cheaper or closer clusters are tried first.

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: AdmissionCheck
metadata:
  name: dispatch-us-east-1
spec:
  controllerName: kueue.x-k8s.io/multikueue
  parameters:
    apiGroup: kueue.x-k8s.io
    kind: MultiKueueConfig
    name: us-east-1-gpu
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: MultiKueueConfig
metadata:
  name: us-east-1-gpu
spec:
  clusters:                     # with the Incremental dispatcher, tried in this order #!
  - train-use1-a                # Capacity Blocks: already paid for
  - train-use1-b                # on-demand and Spot
```

- **Step 4, a worker admits:** each candidate's own Kueue decides with local quota and topology. The first to admit wins; the hub deletes the copies in the other clusters and creates the job in the winner.
- **Step 5, run and report:** the job runs entirely inside the worker. The hub mirrors its status, so the user watches it from one place, and cleans up the remote objects when it finishes.

**Going deeper.**

- **Data locality through queues, not magic.** Each region gets its own `MultiKueueConfig`, and each team's ClusterQueue in the hub points at the regional admission check that matches where its data lives. A team with data in both regions gets one queue per region and chooses at submit time.
- **AllAtOnce vs. Incremental:** AllAtOnce offers the job to every worker immediately, giving the fastest start. Incremental offers it to a few at a time and waits between rounds, which keeps expensive or remote clusters as overflow. Use Incremental for GPUs, AllAtOnce for cheap CPU batch.
- **Topology-aware jobs work across clusters too:** workers define their topology levels, and the hub delays the topology decision until a worker has been chosen.
- **Cluster credentials:** the hub needs to create and watch Workloads and Jobs in each worker. Use the ClusterProfile API or a Secret per worker, with a narrowly scoped identity, never a cluster-admin kubeconfig.
- **MultiKueue is beta** (enabled by default in Kueue). It's the right tool here, but pin the Kueue version across the fleet and test upgrades on a staging hub and worker pair first.

### Inside a GPU cluster: placement, devices and topology

**Scenario.** A 64-GPU training job is admitted, but its step time is 30% slower than last week. The pods landed on nodes in different network blocks, and two of them share a host with a noisy inference pod. Getting the cluster is only half the job; getting the right GPUs inside it is the other half.

**The design.**

- **Topology-aware scheduling for training:** training nodes carry labels for their place in the network (block, then node), written by a topology labeller such as Topograph from the EC2 instance topology API. A Kueue `Topology` lists those levels, and jobs ask for `kueue.x-k8s.io/podset-required-topology` (all pods in one block) or `podset-preferred-topology` (as close as possible).
- **Capacity Blocks for big runs:** Capacity Blocks reserve GPU instances for a fixed window, and instances in a block are placed close together in EC2 UltraClusters. They're the default flavor for multi-node training.
- **DRA for devices:** the NVIDIA DRA driver publishes each node's GPUs as ResourceSlices with typed attributes (model, memory, MIG profile). Workloads ask for "one H100 with 80 GB" through a ResourceClaim instead of a bare count.
- **Training nodes run training only:** GPU training NodePools are tainted, and only Kueue-admitted training jobs tolerate them. Inference never lands next to a training job.
- **Unhealthy GPUs are found before jobs are:** DCGM health checks mark a bad node, Kueue's topology-aware scheduling can replace a failed node in a running job's assignment, and the job resumes from its last checkpoint.

**Going deeper.**

- **DRA has been GA since Kubernetes 1.34.** The NVIDIA DRA driver allocates full GPUs and MIG devices; dynamic MIG creation and shared-capacity features are still alpha in the driver, so this design uses pre-configured MIG layouts for now.
- **Keep the device plugin where DRA isn't needed yet.** Full-GPU training pods can keep using `nvidia.com/gpu` counts during the migration; DRA matters most for mixed GPU types and MIG.
- **EFA and NCCL settings belong in the platform's job templates,** not in every team's YAML, so a team can't accidentally run a 64-GPU job over the slow network path.

### Autoscaling: where capacity comes from

**Scenario.** The CFO sees three bills: Capacity Blocks that idled overnight, on-demand H100s bought on a Saturday by an experiment, and a Spot-based inference fleet that dropped 30% of its replicas during an interruption wave. The team needs rules that decide where each workload's capacity comes from.

![Autoscaling: where capacity comes from](diagrams/designs/ds02-5.svg)

**What the picture shows.** Karpenter NodePools ordered by cost and reliability, and which workloads may use each.

- **Reserved GPUs (Capacity Blocks and capacity reservations):** the highest Karpenter weight, so they fill first. Nodes launched into a reservation carry `karpenter.sh/capacity-type: reserved`. Used by scheduled training and the inference baseline.
- **On-demand GPUs:** used only when reserved is full, and only by queues whose ClusterQueue lists the on-demand flavor, with a NodePool `limits` cap per cluster. Weekend experiments can't reach it because their queue doesn't include it.
- **Spot GPUs:** for checkpointing training, batch inference and evaluation. Interruptions requeue the job, which resumes from its checkpoint.
- **CPU on-demand and Spot:** services keep a floor on on-demand; batch, CI and data pipelines prefer Spot. Consolidation runs continuously on CPU pools.
- **Inference scaling:** model servers scale with HPA or KEDA on serving signals (queue depth, KV-cache use, time to first token), not CPU. Replicas above the reserved baseline land on on-demand, never Spot, for latency-critical models.

**Going deeper.**

- **Capacity Blocks end on a fixed date.** EKS Auto Mode and Karpenter start draining nodes in a Capacity Block 10 minutes before EC2 begins terminating them, so jobs on a block must checkpoint, and the Kueue quota for that flavor should be lowered before the block expires.
- **Never consolidate a running training job.** Training pods carry `karpenter.sh/do-not-disrupt: "true"`; GPU NodePools use `consolidateAfter` with a long delay and disruption budgets that only allow empty nodes to be removed.
- **Scale inference out across regions before up within one.** When one region's GPUs are exhausted, the global router sends more traffic to the other region, which is faster than waiting for new GPU nodes.
- **Cold starts are dominated by images and weights.** Keep model weights in a regional cache (see [Getting model weights into pods](#post-model-data)) and pre-pull serving images on GPU nodes, or autoscaling will be too slow to help.

### Multi-tenant isolation

**Scenario.** A bank-owned subsidiary joins the platform with a rule: its training data must never be on a node shared with another tenant. At the same time, a dozen small internal teams just want a few GPUs each without waiting for a whole node. One isolation model doesn't fit both.

![Multi-tenant isolation](diagrams/designs/ds02-6.svg)

**What the picture shows.** Three tenant classes, each with stronger walls and a higher cost, and three ways to share a GPU.

- **Shared (default):** a namespace per team per cluster, RBAC, Kueue quotas through a LocalQueue, default-deny NetworkPolicy, Pod Security "restricted" and per-team cost reporting. Nodes are shared.
- **Dedicated nodes:** the tenant's own Karpenter NodePools, tainted for that tenant only, plus its own ClusterQueue and flavor. The control plane is shared, the nodes aren't.
- **Dedicated cluster:** a separate worker cluster, still joined to the hub through MultiKueue and to the fleet through GitOps, so the tenant gets the same platform with no shared nodes or control plane.
- **Whole GPU:** the default for training and large models. No sharing, no interference.
- **MIG slice:** hardware partitions with their own memory and compute, requested through DRA. Safe between tenants, good for small models and development.
- **Time-slicing:** several pods take turns on one GPU, with no memory isolation. Only within one team, for notebooks and development, never across tenants.

**Going deeper.**

- **Close the side doors to GPUs.** A policy (Kyverno or a ValidatingAdmissionPolicy) rejects any pod that requests a GPU without a Kueue queue label, and GPU nodes are tainted so only admitted workloads tolerate them. Otherwise one `kubectl run` bypasses every quota.
- **Namespaces are created by the platform, not by teams.** A team's namespace, LocalQueue, network policy, quotas and cost labels are one Git entry, applied to every cluster where the team has a share.
- **Identity per team:** each team's workloads use EKS Pod Identity roles scoped to their own S3 prefixes and model registry paths. A training job in one namespace can't read another team's dataset.
- **Running untrusted code** (evaluation harnesses, agent tools) goes to the sandbox platform from [Design 01](#design-sandboxing), not to the shared GPU clusters.

### Observability, cost and chargeback

**Scenario.** At the quarterly review, every team claims it's GPU-starved, and finance says GPU utilization is 45%. Both can be true, and the platform needs the data to prove which.

**What's measured.**

- **Queue health (Kueue metrics):** pending workloads and wait time per ClusterQueue, quota used vs. nominal, borrowed and lent capacity, and preemptions. This is the "who is starved" view.
- **Dispatch health (MultiKueue):** which worker each job went to, time from submission to admission in a worker, and how often jobs spill over to on-demand clusters.
- **GPU health and use (DCGM exporter):** GPU utilization, memory, power, XID errors and NVLink errors per node, per pod and per team. Low utilization on admitted jobs is a code problem, not a capacity problem, and should go back to the team.
- **Capacity:** reserved vs. used per Capacity Block, nodes per NodePool, Spot interruptions.
- **Cost:** OpenCost or Kubecost per namespace and team, with reserved capacity charged to the team that owns the share and borrowed capacity charged to the borrower.

**Going deeper.**

- Aggregate metrics from every cluster into one store (Amazon Managed Service for Prometheus or Thanos), labelled with cluster, region and role, so fleet-wide dashboards are one query.
- Publish a weekly "idle reserved GPU hours" number per team. It changes behaviour faster than any quota.

### Failure modes and operations

**Scenario.** Before launch, the on-call team walks through "what happens when each box fails", so the runbook exists before the incident.

| Failure | What users see | What the design does |
|---|---|---|
| Hub cluster down | New batch jobs can't be submitted; status in the hub goes stale | Running jobs keep running in workers; inference unaffected; hub restored from Git; queue objects are declarative |
| Worker cluster lost | Jobs on it stop | After a timeout, MultiKueue treats the worker as lost; jobs are requeued and dispatched elsewhere, resuming from checkpoints |
| Capacity Block expires | Jobs on it are drained | Nodes drain 10 minutes before termination; quota for that flavor lowered beforehand; jobs checkpoint and requeue |
| Spot interruption wave | Spot jobs requeue | Checkpoint-and-requeue; inference never on Spot above its floor |
| Bad GPU or node | One job slows or fails | DCGM health checks cordon the node; topology-aware scheduling replaces it; job resumes from its checkpoint |
| Kueue config drift between hub and workers | Jobs dispatched but never admitted | Both generated from one source in Git; a CI check compares hub and worker queues |
| Region outage | Inference in that region down | Global routing shifts traffic to the other region; training queues for that region wait or use the other region's queue |

**Going deeper.**

- **Upgrades go cluster by cluster:** a staging hub and worker first, then one worker per role, then the rest. Because workers are interchangeable for MultiKueue, a worker can be drained of new work during its upgrade by removing it from the `MultiKueueConfig`.
- **Every long job checkpoints.** Preemption, Spot, Capacity Block expiry and node failure all end the same way: requeue and resume. A job that can't resume should run only on reserved capacity at `standard` priority, and its owner should know that's a risk.

### Rollout plan, trade-offs and open questions

**Scenario.** Leadership asks for a plan that delivers value in weeks, not after a year of migration.

**Rollout.**

- **Phase 1, one cluster done right:** Kueue with cohorts, flavors and team queues in the existing training cluster; GPU admission policy; Kueue and DCGM dashboards. This alone recovers most idle GPUs.
- **Phase 2, the fleet from Git:** Argo CD with ApplicationSets by role, cluster definitions in kro, team onboarding as one Git entry.
- **Phase 3, the hub:** hub cluster with MultiKueue, the two training clusters as workers, Incremental dispatch with reserved clusters first. Teams switch their LocalQueue to the hub.
- **Phase 4, CPU batch and isolation tiers:** CPU clusters as workers for batch, dedicated-node and dedicated-cluster tenants, chargeback.

**Trade-offs we accepted.**

- **Kueue over Volcano or a commercial scheduler:** native Kubernetes APIs, MultiKueue for multi-cluster, and a large community, at the cost of some features (such as advanced GPU sharing) that other schedulers have built in. See [Volcano vs. Kueue](#d02-6).
- **A hub cluster:** one more cluster to run, but one queue for the company and a clear owner for every decision.
- **Quota-based admission with Karpenter** instead of capacity-guaranteed admission with Cluster Autoscaler's ProvisioningRequest: Karpenter's flexibility and speed for the whole fleet, at the cost of occasionally admitted jobs waiting for nodes.
- **Static placement for inference:** simpler and predictable, at the cost of not moving models automatically to wherever GPUs are free.

**Open questions for the next review.**

- **Native gang and topology scheduling** in kube-scheduler (Workload and PodGroup APIs): when they're stable, can Kueue hand placement to them?
- **Cross-cluster inference routing:** model-aware routing across clusters with the Gateway API Inference Extension, instead of plain regional load balancing.
- **Bursting beyond AWS:** whether on-premises or other-cloud GPU clusters should join as MultiKueue workers for overflow.
- **Quota from budgets:** generating each team's GPU quota from its budget and the reserved capacity calendar, instead of by hand.
