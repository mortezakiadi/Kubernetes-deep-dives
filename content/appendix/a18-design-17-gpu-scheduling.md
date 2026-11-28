---
id: appendix-design-17-eks
title: "Appendix R: Design 17 built and tested on Amazon EKS"
short: "R. Design 17 on EKS (built and tested)"
---
This appendix describes a working, scaled-down implementation of [Design 17: Scheduling that keeps accelerators busy](#design-gpu-scheduling). I built it with Terraform on a standard Amazon EKS cluster in one AWS account: Kueue for admission (quotas in cohorts, borrowing and lending, fair sharing, priorities, the pods-ready timeout and topology-aware admission), a second kube-scheduler with a bin-packing and a spreading GPU profile for placement, a GPU-hour accounting service that puts every GPU into one category every 10 seconds, two real GPUs measured with DCGM, and a simulated fleet of 128 GPUs on which the scheduling behaviour is tested.

The order is the one a reader needs: first the context, then the software I implemented, the EKS topology and instances, the architecture, how it is implemented, and then the scenarios I tested, with the commands, the results, why the results can be trusted and why the system produces them. Section 6 says plainly what I did not build. (Appendices M to Q are kept for Designs 12 to 16, which are not built yet.)

**What "scaled down" and "simulated" mean here.** The design is about a 1,024-GPU cluster of 128 nodes with 8 H100s each. This build has two real GPUs and a simulated fleet of 16 nodes with 8 GPUs each, 128 in all, in 2 network blocks of 2 racks of 4 hosts. The simulated nodes are Kubernetes Node objects run by KWOK: no machine stands behind them, their GPUs do not exist, and pods bound to them are never started, only moved through their lifecycle (ready, then complete after the run time set on each pod). That is enough for this design, because everything it is about happens in the control plane: Kueue admits, preempts and orders real Workload objects, the real kube-scheduler binary scores and binds real Pod objects to those nodes, and the accounting service reads the same API. What a simulated GPU cannot show is whether it is busy, so the real GPUs are used for exactly that (S02), and simulated pods declare their activity.

11 of 11 scenarios passed, 35 of 35 checks.

| Scenario | Title | Checks passed |
|---|---|---|
| S01 | The platform at rest: cluster, real and simulated GPUs, Kueue, the second scheduler, accounting | 6 of 6 |
| S02 | Allocation is not utilization: two real GPUs, one working, one held by an idle notebook | 3 of 3 |
| S03 | Gang scheduling: per-pod placement deadlocks two jobs; Kueue admits one whole job | 2 of 2 |
| S04 | Quota fit is not node fit: the pods-ready timeout, and topology-aware admission | 3 of 3 |
| S05 | Topology-aware gang placement: whole jobs inside one block, slices inside one rack | 4 of 4 |
| S06 | Bin-packing against spreading: the same 64 GPUs of small jobs, and a 4-node gang after them | 2 of 2 |
| S07 | Defragmentation: drain the cheapest nodes so a waiting 4-node gang can start | 3 of 3 |
| S08 | Quotas: nominal shares, borrowing, a lending limit, and reclaim | 2 of 2 |
| S09 | Fair share: weighted shares of borrowed capacity, and admission fair sharing inside a team | 2 of 2 |
| S10 | Priority, preemption and backfill: urgent work, the cost of preemption, and bounded small jobs | 5 of 5 |
| S11 | Measuring the scheduler: where every GPU-hour of this run went, and queue wait by job size | 3 of 3 |

### Context: the problem, the actors and who talks to whom

**Scenario.** The design's opening case: finance reports 58% of GPU-hours doing useful work; a 256-GPU job has waited three days while 180 GPUs sit free in fragments; two 64-GPU jobs deadlocked each other for six hours; a team's notebooks hold 96 GPUs at 8% use; and another team says the queue is unfair. This build reproduces each of those situations on purpose, at a smaller size, and shows the mechanism the design proposes for it.

Six terms are used throughout.

- **Gang.** A job whose pods are only useful together, such as distributed training. Here every multi-pod Job is a gang.
- **Admission.** Kueue's decision that a job may start: its whole request fits the quota (and, with topology-aware scheduling, the topology). Until then the Job is suspended and has no pods.
- **Placement.** kube-scheduler's choice of a node for each pod of an admitted job.
- **ClusterQueue and cohort.** A team's quota (nominal, borrowing limit, lending limit); queues in one cohort lend unused quota to each other.
- **Fragmentation.** Free GPUs scattered so that no pending pod fits. Reported as whole free nodes, the fragmented share of free GPUs, and the largest gang of 8-GPU pods that could start now.
- **GPU-hour breakdown.** Every GPU of every GPU node, every 10 seconds, in one category: busy, allocated but idle, waiting for a gang, fragmented, blocked by quota policy, or free; plus work lost to preemption.

![Who uses the platform, what it is made of, and what it talks to](diagrams/appendix/a18-1.svg)

**What the picture shows.** Teams submit Jobs labelled with a queue. Kueue admits them, the second scheduler places their pods on the simulated fleet, and the accounting service reads nodes, pods, workloads and DCGM to explain where the GPU-hours went. Everything goes through the Kubernetes API server.

### 1. The software components I implemented

**Scenario.** A reader needs to know which parts I wrote and which I reused, before reading any result.

| # | Component | What it does | Where it runs | Written or reused |
|---|---|---|---|---|
| 1 | **Standard EKS cluster** | Kubernetes 1.37 with a system node group and a real-GPU node group. | AWS | Written (Terraform) |
| 2 | **Kueue 0.20.8** | Admission: the design's Configuration (waitForPodsReady, fair sharing, admission fair sharing), a three-level Topology, two ResourceFlavors over the simulated fleet (with and without topology), three WorkloadPriorityClasses. Each scenario creates its own ClusterQueues and LocalQueues. | `kueue-system` | Reused, configured |
| 3 | **Second kube-scheduler 1.37.1** | The design's KubeSchedulerConfiguration as two profiles, `gpu-binpack` (MostAllocated) and `gpu-spread` (LeastAllocated), both weighting GPUs 10 to 1 over CPU and memory, with BalancedAllocation off. | `kube-system` | Reused, configured |
| 4 | **KWOK 0.8.0** | Runs the 16 simulated nodes and moves pods bound to them through their lifecycle; a pod completes after the delay in its annotation. One stage written for this build marks a pod being evicted as failed, so its Job replaces it. | `kube-system` | Reused, plus one stage |
| 5 | **Simulated fleet (`make_fleet.py`)** | 16 Node objects, 8 GPUs each, labelled with block, rack and host, tainted so only Kueue-admitted or explicitly tolerating pods land there. | Applied by `up.sh` | Written |
| 6 | **Device plugin 0.20.2 and DCGM exporter 4.9.0** | Advertise the two real GPUs and export their utilization per pod. | GPU nodes | Reused |
| 7 | **Accounting service (`accounting.py`)** | Every 10 s: every GPU into one category from nodes, pods, workloads and DCGM; lost work at each preemption; `/breakdown` and `/now`. | `platform` | Written |
| 8 | **Scenario runner and screenshot renderer** | Creates each scenario's queues and jobs, runs the commands, records output, redacts private values, draws terminal-style screenshots. | `tests/` | Adapted from Appendix K |

The Terraform is about 890 lines; the platform's own code (the accounting service and the fleet generator) about 270 lines of Python; the test code about 1120 lines.

**Going deeper.**

- **Why a second scheduler.** EKS runs kube-scheduler in its managed control plane and does not expose its configuration, so the design's bin-packing profile runs as a second scheduler in the cluster. Jobs choose it with `schedulerName`. Running both profiles in one binary means S06 compares exactly one setting.
- **Why two flavors over the same nodes.** With topology-aware scheduling, Kueue itself chooses the domain and pins pods to it at admission, which would hide what the scheduler profile does. S06 and S07 use the flavor without topology; S04 and S05 use the one with it.
- **Scaled timeouts.** The design's `waitForPodsReady` timeout is 10 minutes, to cover image pulls of real jobs; here it is 60 seconds and the requeue backoff starts at 10 seconds, so a scenario can show an eviction and a requeue.

### 2. EKS topology and instances

**Scenario.** The design needs one cluster whose scheduler can be configured, a GPU fleet with a known topology, and a way to measure whether an allocated GPU works.

![One VPC in us-east-2: a system node group, a small real-GPU node group, and a simulated fleet](diagrams/appendix/a18-2.svg)

**What the picture shows.** The VPC with three private subnets and one NAT gateway, the EKS cluster with its two managed node groups, and the simulated fleet's two blocks of two racks, which exist only as Node objects.

**Why standard EKS with managed node groups, and not Auto Mode.** EKS Auto Mode runs its own node provisioning and does not let you add the components this design depends on in the same way: a second scheduler that owns placement of GPU pods, fake nodes that the provisioner must ignore, and a GPU pool whose driver stays fixed while utilization is measured. A standard cluster with managed node groups keeps every one of those under this build's control, and its instances are ordinary EC2 instances that `verify-clean.sh` can check after teardown.

| Item | What I built | Why |
|---|---|---|
| **Region** | us-east-2, one VPC (10.86.0.0/16), one NAT gateway. | Design 21's build uses us-east-1 in the same account. |
| **System node group** | 2 × `m6i.2xlarge`, on demand. | Kueue, the second scheduler, KWOK, the accounting service, CoreDNS. |
| **GPU node group** | 2 nodes, one GPU each, on demand, pinned AL2023 NVIDIA AMI; types in the order `g6e.xlarge`, `g6.2xlarge`, `g6e.2xlarge`, `g5.xlarge`, `g5.2xlarge`, `g6.xlarge`. | S02 needs one busy and one idle pod on whole GPUs. EC2 chooses the type by capacity; S01 records what the run got. |
| **Simulated fleet** | 16 nodes × 8 `nvidia.com/gpu`, labels `network.ds17.io/block` and `network.ds17.io/rack`, taint `kwok.x-k8s.io/node=fake`. | The design's scheduling problems need a fleet of 8-GPU nodes in a topology; 128 GPUs keep every effect visible at a small cost. |
| **Namespaces** | `team-llm`, `team-vision`, `team-research`, `research-sweeps`, `research-paper`, `nokueue`, `gpu-real`, `platform`, each labelled with its team. | ClusterQueues select namespaces by team label. |

### 3. Architecture

**Scenario.** One map of who decides what, so each scenario can say which layer produced what it shows.

![Two layers: when a job may start, and where its pods go](diagrams/appendix/a18-3.svg)

**What the picture shows.** The path of a job from submission to ready pods, and the two status sources a user reads: Kueue's Workload conditions for admission, and the pod's scheduling status for placement.

![The same 64 GPUs of small jobs, spread and packed (S06)](diagrams/appendix/a18-4.svg)

**What the picture shows.** The two layouts the scheduler profiles produced in the development run for the same trace of small jobs: spread leaves every node partly used, packed leaves 8 nodes whole.

![Quotas in a cohort: nominal, borrowing, lending, reclaim (S08)](diagrams/appendix/a18-5.svg)

**What the picture shows.** The two team queues of S08, and how S09 and S10 use the same objects for fair sharing and priority.

![Every GPU, every 10 seconds, in exactly one category](diagrams/appendix/a18-6.svg)

**What the picture shows.** The accounting service's categories and inputs.

### 4. How I implemented it

**Scenario.** Someone who wants to rebuild or extend this needs to know how the pieces fit, in the order they are built.

1. **Terraform, cluster layer (`terraform/10-cluster`).** The VPC, subnets, NAT gateway and S3 endpoint; the cluster and its roles; the node add-ons; the system and GPU node groups; CoreDNS and metrics-server. The provider has `allowed_account_ids`; the account ID and the administrator's addresses live only in a git-ignored `local.auto.tfvars`. State is local and git-ignored.
2. **Terraform, platform layer (`terraform/20-platform`).** Namespaces; the device plugin and DCGM exporter; KWOK's controller and stages (vendored under `platform/kwok`, image pinned by digest, without its FlowSchema, which EKS refuses); Kueue with the Configuration; the Topology, the two flavors and the priority classes; the second scheduler with its RBAC and configuration; the accounting service with read-only access to nodes, pods and Kueue objects.
3. **The simulated fleet.** `up.sh` applies the 16 Node objects from `platform/make_fleet.py` and waits until KWOK reports all of them Ready.
4. **Jobs.** Every scenario job is an Indexed Job. A job with a queue label is created suspended and admitted by Kueue; a job without one carries its own toleration and node selector and goes straight to the scheduler (S03, and the filler jobs that occupy GPUs). Each pod carries its run time, its declared activity and its checkpoint interval as annotations, and a pod failure policy that ignores eviction, so a drained pod is replaced instead of failing its job.
5. **Queues.** Each scenario creates the ClusterQueues and LocalQueues it needs (quotas, cohorts, weights, limits, preemption settings, admission scope) and deletes them, with its jobs, at the end.
6. **Accounting.** Every 10 seconds the service lists nodes, pods and Workloads, scrapes every DCGM exporter pod, and assigns every GPU one category (section 3). It keeps the samples in memory and answers `/breakdown` for a time window, per team and per pool.
7. **Scenarios and evidence.** `tests/run_scenarios.py` runs every command it shows, records the real output, redacts the account ID, the administrator's addresses and the local user name, computes each check from the output, and draws the screenshot from the same text.

### 5. The scenarios I tested

**Scenario.** Each test below starts from a question the design asks and ends with evidence. Every command was run by the scenario runner against the live platform, in one run from a fresh build, in the order shown, and the output is what the command printed (shortened here; the screenshot and the evidence file have it in full). Each check is computed by the program from that output, not written by hand. An earlier fresh build was run first and failed two checks: on that build EC2 replaced a real GPU node for capacity while S02 was running, and S11's check asked for at least 30 recorded queue waits where the run recorded 28. I made `up.sh` wait until both real GPU nodes stay Ready for a minute, made S11 check the size classes instead of a count, tore the build down, built again and ran all eleven scenarios once. What is shown is that run.

**How to read the screenshots.** They are drawn from the recorded text by the renderer in `tests/render_terminal.py`. Lines starting with `$` are the commands; lines starting with `[PASS]` or `[FAIL]` are the checks. Where a step reads the cluster or the accounting service rather than running one command, the step's title says what it read.


### S01: The platform at rest: cluster, real and simulated GPUs, Kueue, the second scheduler, accounting

**Design reference:** Two layers: admission and placement.

**What this tests.** Check that the platform is what section 2 says before testing behaviour: a standard EKS cluster with a system and a real-GPU node group, 16 simulated 8-GPU nodes in two blocks of two racks, Kueue with the topology and flavors, a second scheduler with two GPU profiles, DCGM on the real GPUs, the accounting service, and no workload.

Before testing behaviour I check that the platform is what section 2 says: the cluster, the two node groups, the 16 simulated nodes and their GPUs, Kueue and its objects, the second scheduler's two profiles, DCGM and the accounting service, and no workload.

![S01: The platform at rest: cluster, real and simulated GPUs, Kueue, the second scheduler, accounting](diagrams/appendix/a18-evidence/S01.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ aws eks describe-cluster --name ds17-sched --query cluster.{status:status,version:version,autoMode:computeConfig.enabled}
{
    "status": "ACTIVE",
    "version": "1.37",
    "autoMode": false
}

$ kubectl get nodes -L ds17/pool,node.kubernetes.io/instance-type,network.ds17.io/block,network.ds17.io/rack --sort-by=.metadata.name
NAME                                         STATUS   ROLES    AGE     VERSION               POOL       INSTANCE-TYPE   BLOCK   RACK
fake-b1-r1-h1                                Ready    <none>   70s     kwok-v0.8.0           fake-gpu                   b1      b1-r1
fake-b1-r1-h2                                Ready    <none>   70s     kwok-v0.8.0           fake-gpu                   b1      b1-r1
fake-b1-r1-h3                                Ready    <none>   70s     kwok-v0.8.0           fake-gpu                   b1      b1-r1
fake-b1-r1-h4                                Ready    <none>   69s     kwok-v0.8.0           fake-gpu                   b1      b1-r1
fake-b1-r2-h1                                Ready    <none>   69s     kwok-v0.8.0           fake-gpu                   b1      b1-r2
fake-b1-r2-h2                                Ready    <none>   69s     kwok-v0.8.0           fake-gpu                   b1      b1-r2
fake-b1-r2-h3                                Ready    <none>   69s     kwok-v0.8.0           fake-gpu                   b1      b1-r2
fake-b1-r2-h4                                Ready    <none>   68s     kwok-v0.8.0           fake-gpu                   b1      b1-r2
fake-b2-r1-h1                                Ready    <none>   68s     kwok-v0.8.0           fake-gpu                   b2      b2-r1
fake-b2-r1-h2                                Ready    <none>   68s     kwok-v0.8.0           fake-gpu                   b2      b2-r1
fake-b2-r1-h3                                Ready    <none>   68s     kwok-v0.8.0           fake-gpu                   b2      b2-r1
fake-b2-r1-h4                                Ready    <none>   68s     kwok-v0.8.0           fake-gpu                   b2      b2-r1
fake-b2-r2-h1                                Ready    <none>   68s     kwok-v0.8.0           fake-gpu                   b2      b2-r2
fake-b2-r2-h2                                Ready    <none>   67s     kwok-v0.8.0           fake-gpu                   b2      b2-r2
fake-b2-r2-h3                                Ready    <none>   67s     kwok-v0.8.0           fake-gpu                   b2      b2-r2
… (5 more lines in the screenshot and in evidence/S01.txt)

$ GPUs each pool advertises (status.allocatable nvidia.com/gpu)
simulated: 16 nodes, 128 GPUs, blocks ['b1', 'b2'], racks ['b1-r1', 'b1-r2', 'b2-r1', 'b2-r2']
real: 2 nodes, 2 GPUs, types ['g6e.2xlarge', 'g6e.xlarge']

$ kubectl -n kueue-system get deploy kueue-controller-manager -o jsonpath='{..image}'
registry.k8s.io/kueue/kueue:v0.20.8

$ kubectl get topology,resourceflavor,workloadpriorityclass -o name
topology.kueue.x-k8s.io/ds17-fabric
resourceflavor.kueue.x-k8s.io/fake-h100
resourceflavor.kueue.x-k8s.io/fake-h100-plain
workloadpriorityclass.kueue.x-k8s.io/best-effort
workloadpriorityclass.kueue.x-k8s.io/release
workloadpriorityclass.kueue.x-k8s.io/standard

$ the second scheduler's profiles (ds17-scheduler-config)
          "weight": 10
          "weight": 1
          "weight": 1
  "schedulerName": "gpu-binpack"
          "weight": 10
          "weight": 1
          "weight": 1
  "schedulerName": "gpu-spread"

$ kubectl -n kube-system get deploy ds17-scheduler -o custom-columns=NAME:.metadata.name,READY:.status.readyReplicas,IMAGE:.spec.template.spec.containers[0].image
NAME             READY   IMAGE
ds17-scheduler   1       registry.k8s.io/kube-scheduler:v1.37.1@sha256:b6e2474a6c20309f0df1e7337364281fda95c6a0722259ba7f032a1e177d3f29

$ kubectl -n gpu-monitoring get pods -o wide
NAME                  READY   STATUS    RESTARTS   AGE    IP             NODE                                         NOMINATED NODE   READINESS GATES
dcgm-exporter-2pddk   1/1     Running   0          4m7s   10.86.61.247   ip-10-86-52-153.us-east-2.compute.internal   <none>           <none>
dcgm-exporter-748jz   1/1     Running   0          4m7s   10.86.25.39    ip-10-86-25-255.us-east-2.compute.internal   <none>           <none>

$ kubectl get jobs,workloads -A

No resources found

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the cluster is ACTIVE on Kubernetes 1.37, standard EKS (Auto Mode off) | Terraform creates a plain cluster with managed node groups and no compute configuration. |
| PASS | 16 simulated nodes with 8 GPUs each (128 GPUs) in 2 blocks of 2 racks, all Ready; 2 real one-GPU nodes; 2 system nodes | KWOK keeps the Node objects up/make_fleet.py created Ready; the device plugin advertises the real GPUs. |
| PASS | Kueue runs with the topology ds17-fabric, two flavors over the simulated fleet and three priority classes | The platform layer creates them after the Kueue chart is ready. |
| PASS | a second kube-scheduler runs with a bin-packing (MostAllocated) and a spreading (LeastAllocated) GPU profile | Both profiles weight GPUs 10 to 1 over CPU and memory and differ only in strategy. |
| PASS | DCGM runs on both real GPU nodes, and the accounting service is sampling | The chart's DaemonSet selects the GPU pool; the accounting service samples every 10 s. |
| PASS | no job and no workload exist yet | A fresh build. |

**Why you can trust this result.**

- Every fact comes from the system that owns it: the cluster from the EKS API, nodes and their allocatable GPUs from the API server, the profiles from the scheduler's own ConfigMap.
- The simulated nodes are listed next to the real ones by the same command; the difference is visible in their labels and instance types.

**Why you get this output.**

- The simulated nodes are Ready because KWOK renews their leases and status, as a kubelet would.
- The real GPU nodes advertise one GPU each because the device plugin runs on the GPU pool.

### S02: Allocation is not utilization: two real GPUs, one working, one held by an idle notebook

**Design reference:** Where GPU-hours go.

**What this tests.** On the two real GPUs, start a pod that runs the CUDA n-body benchmark in a loop and a 'notebook' pod that holds a GPU and does nothing. Both GPUs are allocated; read what DCGM measures on each, and how the accounting service classifies them.

The design's first warning is that allocation is not utilization. On the two real GPUs, one pod runs the CUDA n-body benchmark and one 'notebook' holds a GPU doing nothing. Both GPUs are 100% allocated.

![S02: Allocation is not utilization: two real GPUs, one working, one held by an idle notebook](diagrams/appendix/a18-evidence/S02.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl apply -f real-gpu-pods.yaml   # trainer-busy (n-body loop), notebook-idle (sleep)
pod/trainer-busy created
pod/notebook-idle created

$ kubectl -n gpu-real get pods -o custom-columns=POD:.metadata.name,NODE:.spec.nodeName,PHASE:.status.phase,GPU:.spec.containers[0].resources.limits.nvidia\.com/gpu
POD             NODE                                         PHASE     GPU
notebook-idle   ip-10-86-25-255.us-east-2.compute.internal   Running   1
trainer-busy    ip-10-86-52-153.us-east-2.compute.internal   Running   1

$ DCGM_FI_DEV_GPU_UTIL_RATIO from each DCGM exporter pod (kubectl get --raw .../pods/<exporter>:9400/proxy/metrics)
DCGM_FI_DEV_GPU_UTIL_RATIO{gpu="0",UUID="GPU-8195f9d4-db93-2ae3-790b-4b44d4db51c1",pci_bus_id="00000000:30:00.0",device="nvidia0",modelName="NVIDIA L40S",hostname="ip-10-86-52-153.us-east-2.compute.i…
DCGM_FI_DEV_GPU_UTIL_RATIO{gpu="0",UUID="GPU-1259fda4-38e4-a2d7-e464-6efb65514e1e",pci_bus_id="00000000:30:00.0",device="nvidia0",modelName="NVIDIA L40S",hostname="ip-10-86-25-255.us-east-2.compute.i…

$ accounting: GPU-minutes of the real pool over the last 90 s, by category
{
 "samples": 10,
 "gpu_minutes_by_category": {
  "allocated_idle": 1.7,
  "busy": 1.7
 },
 "share": {
  "allocated_idle": 0.5,
  "busy": 0.5
 }
}

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | both GPUs are allocated, on two different nodes: 100% allocation | Each pod requests one nvidia.com/gpu; the device plugin assigns each a whole device. |
| PASS | DCGM measures the trainer's GPU above 50% busy and the notebook's at 0% | Allocation counts requests; DCGM counts the GPU's own activity. |
| PASS | the accounting service puts half of the allocated GPU-minutes in busy and half in allocated_idle | It matches DCGM's per-pod utilization to the pod that holds each GPU. |

**Why you can trust this result.**

- Utilization is DCGM's own measurement, read from each exporter pod and labelled with the pod that holds the GPU.
- The accounting service gets the same numbers independently and classifies them; its result is compared, not copied.

**Why you get this output.**

- The trainer's GPU is busy because the benchmark keeps its kernels running; the notebook's is at 0% because sleep never touches it.
- The accounting service splits the allocated GPU-minutes in half because one of two allocated GPUs is idle.

### S03: Gang scheduling: per-pod placement deadlocks two jobs; Kueue admits one whole job

**Design reference:** Gang scheduling: all or nothing.

**What this tests.** Leave 16 free GPUs in rack b1-r1 and submit two 12-pod jobs (one GPU per pod) at the same time, first straight to the scheduler, then through a Kueue queue with a 16-GPU quota. Distributed training needs all 12 workers.

Two jobs of 12 one-GPU pods compete for 16 free GPUs in one rack, first straight through the scheduler, then through a Kueue queue with 16 GPUs of quota. Real job controllers create pods in growing batches; to make the race reproducible, the scenario raises both jobs' parallelism in alternating steps of 2.

![S03: Gang scheduling: per-pod placement deadlocks two jobs; Kueue admits one whole job](diagrams/appendix/a18-evidence/S03.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl apply -f filler.yaml   # 2 pods x 8 GPUs in rack b1-r1: 16 of its 32 GPUs remain free
job.batch/filler created

$ kubectl apply -f gang-a.yaml -f gang-b.yaml   # no queue: every pod goes straight to the scheduler; parallelism 2 of 12
job.batch/gang-a created
job.batch/gang-b created

$ kubectl -n nokueue get jobs   # after raising both to parallelism 12 in alternating steps of 2
JOB      PARALLELISM   COMPLETIONS
gang-a   12            12
gang-b   12            12

$ pods of each job after 40 s
gang-a {'running': 8, 'pending': 4, 'gpus_held': 8}
gang-b {'running': 8, 'pending': 4, 'gpus_held': 8}

$ accounting /now: GPUs waiting for a gang
16

$ kubectl apply -f gang-a.yaml -f gang-b.yaml   # labelled kueue.x-k8s.io/queue-name: gangs (ClusterQueue quota 16 GPUs)
job.batch/gang-a created
job.batch/gang-b created

$ kubectl -n team-llm get workloads -o custom-columns=WORKLOAD:.metadata.name,ADMITTED:.status.conditions[?(@.type=='Admitted')].status,PENDING:.status.conditions[?(@.type=='QuotaReserved')].message
WORKLOAD           ADMITTED   PENDING
job-gang-a-1df3d   True       Quota reserved in ClusterQueue cq-gang
job-gang-b-e54e5   False      couldn't assign flavors to pod set main: insufficient unused quota for nvidia.com/gpu in flavor fake-h100-plain, 8 more needed

$ pods of each job after admission
gang-a {'running': 12, 'pending': 0, 'gpus_held': 12}
gang-b {'running': 0, 'pending': 0, 'gpus_held': 0}

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | without admission both jobs start part of their pods and neither has all 12: a deadlock that holds GPUs | The scheduler places each pod alone as GPUs are free; both jobs take part of the 16 and wait for the rest. |
| PASS | with Kueue one job is admitted whole (12 pods running) and the other holds nothing until it fits; no GPU waits for a gang | Kueue admits a workload only if its whole request fits the quota; the second one stays suspended with no pods. |

**Why you can trust this result.**

- The pod counts are the API server's; the GPUs waiting for a gang are the accounting service's count of GPUs held by pods of an incomplete gang.
- The Kueue half uses the same jobs, nodes and free GPUs; only the queue label differs.

**Why you get this output.**

- Without admission each pod is placed on its own as soon as a GPU is free, so both jobs take part of the 16 and neither can start training.
- With Kueue the second job's whole request does not fit the remaining quota, so it stays suspended with no pods and holds nothing.

### S04: Quota fit is not node fit: the pods-ready timeout, and topology-aware admission

**Design reference:** Gang scheduling: all or nothing (waitForPodsReady); Topology-aware gang placement.

**What this tests.** Rack b1-r1 has 8 free GPUs, but the queue has 16 GPUs of quota. Submit a 12-pod job through a queue without topology: Kueue admits it, only 8 pods can run, and waitForPodsReady (60 s here) must evict and requeue it. Then free the rack and watch it start. Finally submit the same job through a topology-aware queue, which checks the rack's real free GPUs before admitting.

Quota fit is not node fit. The queue has 16 GPUs of quota but the rack has only 8 free, because pods Kueue does not manage use the rest.

![S04: Quota fit is not node fit: the pods-ready timeout, and topology-aware admission](diagrams/appendix/a18-evidence/S04.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl apply -f filler.yaml   # 3 pods x 8 GPUs: rack b1-r1 keeps 8 of 32 GPUs free
job.batch/filler created

$ kubectl apply -f train-12.yaml   # 12 pods x 1 GPU, queue plain (flavor without topology)
job.batch/train-12 created

$ pods 15 s after admission
{'running': 8, 'pending': 4, 'gpus_held': 8}

$ kubectl -n team-llm get workload <train-12's> -o jsonpath='{.status.conditions[?(@.type=="Evicted")]} {.status.requeueState}'
{"reason": "PodsReadyTimeout", "message": "Exceeded the PodsReady timeout team-llm/job-train-12-f0d95", "requeueState": {"count": 1, "requeueAt": "2026-11-27T23:35:09Z"}}

$ after the filler is deleted (rack fully free)
{'running': 12, 'pending': 0, 'gpus_held': 12}

$ kubectl apply -f filler.yaml   # the same 3 x 8 GPUs: rack b1-r1 keeps 8 free
job.batch/filler created

$ kubectl apply -f train-12-tas.yaml   # the same job, queue tas (flavor with topology), podset-required-topology: rack
job.batch/train-12 created

$ the workload 30 s later
{"admitted": false, "pods": {"running": 0, "pending": 0, "gpus_held": 0}, "message": "couldn't assign flavors to pod set main: topology \"ds17-fabric\" allows to fit only 8 out of 12 pod(s); all pods…

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | Kueue admits the job on quota, 8 pods run and 4 stay pending, and after the 60 s timeout the workload is evicted (PodsReadyTimeout) and requeued with backoff | waitForPodsReady: all pods of an admitted workload must be ready within the timeout, or it gives its quota back and waits. |
| PASS | once the rack has room, the requeued workload is admitted again and all 12 pods run | The requeue backoff expired and the quota still fitted; this time the nodes did too. |
| PASS | through the topology-aware queue the job is not admitted while the rack lacks 12 free GPUs, and holds none | TAS checks the free GPUs of the topology domain, including those used by pods Kueue does not manage, before it admits. |

**Why you can trust this result.**

- The eviction reason and requeue count are Kueue's own Workload status.
- The topology-aware queue's refusal is Kueue's own message, naming how many of the 12 pods fit in the rack.

**Why you get this output.**

- Kueue admits on quota, the scheduler can place only 8 pods, and waitForPodsReady evicts the workload after 60 s and requeues it with backoff.
- When the rack is free, the requeued workload is admitted again and every pod is placed.
- Topology-aware admission counts the rack's real free GPUs, including those used by unmanaged pods, so it never admits what cannot be placed.

### S05: Topology-aware gang placement: whole jobs inside one block, slices inside one rack

**Design reference:** Topology-aware gang placement.

**What this tests.** Through one queue on the topology-aware flavor, with more quota (256 GPUs) than the 128 GPUs exist so that only placement can hold a job back, submit in order: an 8-node job that requires one block, a 4-node job that requires one block with slices of 2 nodes that each require one rack, a second 8-node job that requires one block (no block has 8 free nodes left), and a 4-node job that only prefers one rack.

Four jobs through one topology-aware queue whose quota (256 GPUs) exceeds the fleet, so only the topology can hold a job back.

![S05: Topology-aware gang placement: whole jobs inside one block, slices inside one rack](diagrams/appendix/a18-evidence/S05.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl apply -f pretrain.yaml   # 8 pods x 8 GPUs, topology=network.ds17.io/block
job.batch/pretrain created

$ kubectl apply -f finetune.yaml   # 4 pods x 8 GPUs, topology=network.ds17.io/block, slice_topology=network.ds17.io/rack, slice_size=2
job.batch/finetune created

$ kubectl apply -f pretrain-2.yaml   # 8 pods x 8 GPUs, topology=network.ds17.io/block
job.batch/pretrain-2 created

$ kubectl apply -f flex.yaml   # 4 pods x 8 GPUs, preferred=network.ds17.io/rack
job.batch/flex created

$ where each job's pods landed (node labels network.ds17.io/block and rack)
pretrain    admitted True  pods 8/8  blocks ['b1']  racks [('b1-r1', 4), ('b1-r2', 4)]
finetune    admitted True  pods 4/4  blocks ['b2']  racks [('b2-r1', 4)]
pretrain-2  admitted False pods 0/8  blocks []  racks []
flex        admitted True  pods 4/4  blocks ['b2']  racks [('b2-r2', 4)]

$ the second 8-node job's QuotaReserved condition
couldn't assign flavors to pod set main: topology "ds17-fabric" allows to fit only 4 out of 8 pod(s); all pods must fit in a single "network.ds17.io/block" topology domain. Total nodes: 16; excluded:…

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the first 8-node job runs entirely inside one block | podset-required-topology: block; Kueue picks a block with 8 free nodes and pins each pod to a node in it. |
| PASS | the sliced job runs in one block, with every rack holding a whole number of 2-node slices | podset-slice-required-topology: rack with podset-slice-size 2. |
| PASS | the second 8-node job waits, holding nothing, because no block has 8 free nodes | Required topology waits for a domain rather than spreading the job. |
| PASS | the job that only prefers a rack is admitted anyway, in the smallest domain that fits | podset-preferred-topology tries one rack and falls back to a wider domain; BestEffortFIFO lets it pass the waiting job. |

**Why you can trust this result.**

- Each pod's node is read from the API server, and its block and rack from that node's labels.
- The waiting job's reason is Kueue's own message.

**Why you get this output.**

- A required block makes Kueue pick a block with enough free nodes and pin every pod there.
- Slices of 2 nodes that each require a rack keep pairs together inside the block.
- The second 8-node job waits because neither block has 8 free nodes; the job that only prefers a rack is admitted into a wider domain, and BestEffortFIFO lets it pass.

### S06: Bin-packing against spreading: the same 64 GPUs of small jobs, and a 4-node gang after them

**Design reference:** Bin-packing to reduce fragmentation.

**What this tests.** Replay the same seeded trace of single-pod 1-, 2- and 4-GPU jobs (64 GPUs) twice, once with the spreading profile and once with the bin-packing profile of the second scheduler, through a queue on the flavor without topology so that only the scheduler decides placement. After each, read the fragmentation and submit a gang of 4 pods with 8 GPUs each.

The same seeded trace of 31 single-pod jobs (64 GPUs) runs twice through a queue without topology, once with each scheduler profile, and then a gang of four 8-GPU pods is submitted.

![S06: Bin-packing against spreading: the same 64 GPUs of small jobs, and a 4-node gang after them](diagrams/appendix/a18-evidence/S06.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl apply -f trace.yaml   # 31 single-pod jobs of 1, 2 or 4 GPUs (64 GPUs), schedulerName gpu-spread
job.batch/small-00 created
job.batch/small-01 created
job.batch/small-02 created
job.batch/small-03 created
job.batch/small-04 created
job.batch/small-05 created
job.batch/small-06 created
job.batch/small-07 created
job.batch/small-08 created
job.batch/small-09 created
job.batch/small-10 created
job.batch/small-11 created
job.batch/small-12 created
job.batch/small-13 created
job.batch/small-14 created
job.batch/small-15 created
… (15 more lines in the screenshot and in evidence/S06.txt)

$ gpu-spread: free GPUs per node, then the fragmentation report
5 4 2 4 2 3 2 4 4 5 5 4 5 5 5 5
{"free_gpus": 64, "whole_free_nodes": 0, "fragmented_share": 1.0, "largest_gang_of_8gpu_pods": 0, "pods_of_1_that_fit": 64, "pods_of_2_that_fit": 28, "pods_of_4_that_fit": 12, "pods_of_8_that_fit": 0}

$ kubectl apply -f gang-4x8.yaml   # 4 pods x 8 GPUs, schedulerName gpu-spread
job.batch/gang-4x8 created

$ gpu-spread: the gang 30 s after submission, and accounting /now for the simulated fleet (GPUs per category)
{"gang": {"running": 0, "pending": 4, "gpus_held": 0}, "categories": {"busy": 64, "fragmented": 64}}

$ kubectl apply -f trace.yaml   # 31 single-pod jobs of 1, 2 or 4 GPUs (64 GPUs), schedulerName gpu-binpack
job.batch/small-00 created
job.batch/small-01 created
job.batch/small-02 created
job.batch/small-03 created
job.batch/small-04 created
job.batch/small-05 created
job.batch/small-06 created
job.batch/small-07 created
job.batch/small-08 created
job.batch/small-09 created
job.batch/small-10 created
job.batch/small-11 created
job.batch/small-12 created
job.batch/small-13 created
job.batch/small-14 created
job.batch/small-15 created
… (15 more lines in the screenshot and in evidence/S06.txt)

$ gpu-binpack: free GPUs per node, then the fragmentation report
0 0 0 8 0 0 0 8 0 0 8 8 8 8 8 8
{"free_gpus": 64, "whole_free_nodes": 8, "fragmented_share": 0.0, "largest_gang_of_8gpu_pods": 8, "pods_of_1_that_fit": 64, "pods_of_2_that_fit": 32, "pods_of_4_that_fit": 16, "pods_of_8_that_fit": 8}

$ kubectl apply -f gang-4x8.yaml   # 4 pods x 8 GPUs, schedulerName gpu-binpack
job.batch/gang-4x8 created

$ gpu-binpack: the gang 30 s after submission, and accounting /now for the simulated fleet (GPUs per category)
{"gang": {"running": 4, "pending": 0, "gpus_held": 32}, "categories": {"busy": 96, "free": 32}}

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | spreading leaves almost no whole free node; bin-packing leaves at least 6 of 16 | MostAllocated scores the fullest node highest, so small jobs fill few nodes; LeastAllocated puts each on the emptiest node. |
| PASS | after spreading the 4-node gang cannot start, and its waiting GPUs are counted as fragmented; after packing it runs | The free GPUs are the same in total; only their arrangement decides whether an 8-GPU pod fits. |

**Measured in this run:** Whole free nodes: spreading 0, packing 8; fragmented share 1.0 vs 0.0.

**Why you can trust this result.**

- Both runs use the same trace, the same nodes and the same scheduler binary; only schedulerName differs.
- Free GPUs per node are computed from the pods the API server lists on each node.

**Why you get this output.**

- MostAllocated scores the fullest node that fits highest, so small jobs fill nodes one after another and whole nodes stay free.
- LeastAllocated scores the emptiest node highest, so every node gets a few small jobs and none stays whole; the gang's 8-GPU pods fit nowhere, and the accounting service counts those free GPUs as fragmented.

### S07: Defragmentation: drain the cheapest nodes so a waiting 4-node gang can start

**Design reference:** Defragmentation and right-sizing.

**What this tests.** Recreate S06's spread state, so the 4-node gang waits. Plan which nodes to empty with the fewest GPUs moved, drain them (the evicted small pods are recreated on other nodes), and let the gang take the whole nodes. Count the work the moved pods lost since their last checkpoint.

Packing only helps new pods. Starting from the spread layout with the gang waiting, the planner picks the nodes that are cheapest to empty, they are drained, and the gang takes them.

![S07: Defragmentation: drain the cheapest nodes so a waiting 4-node gang can start](diagrams/appendix/a18-evidence/S07.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl apply -f trace.yaml   # 31 single-pod jobs of 1, 2 or 4 GPUs (64 GPUs), schedulerName gpu-spread
job.batch/small-00 created
job.batch/small-01 created
job.batch/small-02 created
job.batch/small-03 created
job.batch/small-04 created
job.batch/small-05 created
job.batch/small-06 created
job.batch/small-07 created
job.batch/small-08 created
job.batch/small-09 created
job.batch/small-10 created
job.batch/small-11 created
job.batch/small-12 created
job.batch/small-13 created
job.batch/small-14 created
job.batch/small-15 created
… (15 more lines in the screenshot and in evidence/S07.txt)

$ kubectl apply -f gang-4x8.yaml   # 4 pods x 8 GPUs, waits for whole nodes
job.batch/gang-4x8 created

$ before: fragmentation, and the gang
{"fragmentation": {"free_gpus": 64, "whole_free_nodes": 0, "fragmented_share": 1.0, "largest_gang_of_8gpu_pods": 0, "pods_of_1_that_fit": 64, "pods_of_2_that_fit": 28, "pods_of_4_that_fit": 12, "pods…

$ the compaction plan (fewest GPUs moved first)
{"drain": ["fake-b1-r1-h1", "fake-b1-r1-h2", "fake-b1-r2-h3", "fake-b2-r1-h1"], "gpus_moved": 12, "room_elsewhere": 44, "feasible": true}

$ kubectl drain fake-b1-r1-h1 --pod-selector=ds17/gang   # evict the small pods, cordon the node
node/fake-b1-r1-h1 cordoned
evicting pod team-research/small-21-0-tfvpv
evicting pod team-research/small-04-0-hhwc8
pod/small-21-0-tfvpv evicted
pod/small-04-0-hhwc8 evicted
node/fake-b1-r1-h1 drained

$ kubectl drain fake-b1-r1-h2 --pod-selector=ds17/gang   # evict the small pods, cordon the node
node/fake-b1-r1-h2 cordoned
evicting pod team-research/small-20-0-zwwqx
evicting pod team-research/small-02-0-nxhwf
pod/small-02-0-nxhwf evicted
pod/small-20-0-zwwqx evicted
node/fake-b1-r1-h2 drained

$ kubectl drain fake-b1-r2-h3 --pod-selector=ds17/gang   # evict the small pods, cordon the node
node/fake-b1-r2-h3 cordoned
evicting pod team-research/small-22-0-bbm9v
evicting pod team-research/small-12-0-wtbt2
pod/small-12-0-wtbt2 evicted
pod/small-22-0-bbm9v evicted
node/fake-b1-r2-h3 drained

$ kubectl drain fake-b2-r1-h1 --pod-selector=ds17/gang   # evict the small pods, cordon the node
node/fake-b2-r1-h1 cordoned
evicting pod team-research/small-21-0-f5xbn
evicting pod team-research/small-16-0-rxv5w
evicting pod team-research/small-05-0-9b9m7
pod/small-21-0-f5xbn evicted
pod/small-05-0-9b9m7 evicted
pod/small-16-0-rxv5w evicted
node/fake-b2-r1-h1 drained

$ kubectl uncordon fake-b1-r1-h1 fake-b1-r1-h2 fake-b1-r2-h3 fake-b2-r1-h1   # the empty nodes take new pods again
node/fake-b1-r1-h1 already uncordoned
node/fake-b1-r1-h2 already uncordoned
node/fake-b1-r2-h3 already uncordoned
node/fake-b2-r1-h1 already uncordoned

$ after: the gang, the small jobs, fragmentation and the work lost
{"gang": {"running": 4, "pending": 0, "gpus_held": 32}, "small_pods_running": 31, "fragmentation": {"free_gpus": 32, "whole_free_nodes": 0, "fragmented_share": 1.0, "largest_gang_of_8gpu_pods": 0, "p…

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the plan empties 4 nodes and the moved GPUs fit on the other nodes | Nodes are sorted by GPUs in use; only nodes whose GPUs are all held by movable pods are candidates. |
| PASS | after the drain every small job runs again elsewhere, and the 4-node gang runs on the emptied nodes | The podFailurePolicy ignores DisruptionTarget, so the Job recreates each evicted pod. |
| PASS | compaction cost work: the moved pods lost the time since their last checkpoint | Each pod checkpoints every 600 s (ds17/checkpoint-seconds); everything since is lost. |

**Why you can trust this result.**

- The plan is computed from the pods on each node; the drain uses kubectl drain, which evicts through the Eviction API.
- Lost work is computed from each moved pod's start time and its 600 s checkpoint interval.

**Why you get this output.**

- Evicted pods get a DisruptionTarget condition, the Jobs' pod failure policy ignores it, and each Job recreates its pod on another node.
- The emptied nodes are whole again, so the gang's pods fit there.

### S08: Quotas: nominal shares, borrowing, a lending limit, and reclaim

**Design reference:** Quotas: nominal, borrowing and lending.

**What this tests.** Two teams in one cohort, 32 GPUs of nominal quota each. Vision lends at most 24 of its 32; LLM may borrow up to 32. LLM submits eight 8-GPU jobs while vision is idle, then vision submits three.

Two teams with 32 GPUs of nominal quota each in one cohort. Vision lends at most 24; LLM may borrow up to 32.

![S08: Quotas: nominal shares, borrowing, a lending limit, and reclaim](diagrams/appendix/a18-evidence/S08.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl apply -f llm-jobs.yaml   # 8 jobs x 8 GPUs = 64 GPUs; nominal 32
job.batch/llm-0 created
job.batch/llm-1 created
job.batch/llm-2 created
job.batch/llm-3 created
job.batch/llm-4 created
job.batch/llm-5 created
job.batch/llm-6 created
job.batch/llm-7 created

$ kubectl get clusterqueue cq-llm cq-vision -o custom-columns=QUEUE:.metadata.name,ADMITTED:.status.admittedWorkloads,PENDING:.status.pendingWorkloads,USAGE:.status.flavorsUsage[0].resources[0].total,BORROWED:.status.flavorsUsage[0].resources[0].borrowed
QUEUE       ADMITTED   PENDING   USAGE   BORROWED
cq-llm      7          1         7       0
cq-vision   0          0         0       0

$ the pending LLM job's QuotaReserved condition
couldn't assign flavors to pod set main: insufficient unused quota for nvidia.com/gpu in flavor fake-h100-plain, 8 more needed

$ accounting /now: free GPUs blocked by quota policy while that job waits
72

$ kubectl apply -f vision-jobs.yaml   # vision returns: 3 jobs x 8 GPUs
job.batch/vision-0 created
job.batch/vision-1 created
job.batch/vision-2 created

$ kubectl get clusterqueue cq-llm cq-vision -o custom-columns=QUEUE:.metadata.name,ADMITTED:.status.admittedWorkloads,PENDING:.status.pendingWorkloads,USAGE:.status.flavorsUsage[0].resources[0].total,BORROWED:.status.flavorsUsage[0].resources[0].borrowed
QUEUE       ADMITTED   PENDING   USAGE   BORROWED
cq-llm      5          3         5       0
cq-vision   3          0         3       0

$ LLM workloads preempted, with the reason
{"llm-4": "InCohortReclamation", "llm-5": "InCohortReclamation"}

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | LLM runs 56 GPUs: its 32 plus the 24 vision may lend; the eighth job waits although the fleet has free GPUs | borrowingLimit 32 on LLM, lendingLimit 24 on vision: 8 of vision's GPUs stay reserved for vision. |
| PASS | vision gets its 24 GPUs at once: one job from its reserved 8, two by reclaiming borrowed GPUs from LLM (InCohortReclamation) | reclaimWithinCohort: Any; LLM's usage beyond its nominal 32 was borrowed, so it is what gets preempted. |

**Why you can trust this result.**

- Usage, borrowing and preemption reasons are Kueue's own ClusterQueue and Workload status.
- The blocked-by-quota GPUs are the accounting service's count of free GPUs that fit a pending workload that is not admitted.

**Why you get this output.**

- LLM's eighth job waits because borrowing stops at vision's lending limit, although the fleet has free GPUs.
- When vision returns, its kept 8 GPUs start one job at once, and reclaimWithinCohort preempts two of LLM's borrowed jobs for the other two.

### S09: Fair share: weighted shares of borrowed capacity, and admission fair sharing inside a team

**Design reference:** Fair share: weighted shares and fairness over time.

**What this tests.** A pool queue owns 96 GPUs and runs nothing; research (weight 1) and LLM (weight 2) own none and borrow. Research submits twelve 8-GPU jobs first and takes everything; then LLM submits twelve. Then, inside one team queue with 16 GPUs and usage-based admission fair sharing, a sweep project that has been using the queue competes with a paper project that has not.

Borrowed capacity is split by weight between two teams, and inside one team a project that has used less goes first.

![S09: Fair share: weighted shares of borrowed capacity, and admission fair sharing inside a team](diagrams/appendix/a18-evidence/S09.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl apply -f research-jobs.yaml   # 12 x 8 GPUs, weight 1
job.batch/res-00 created
job.batch/res-01 created
job.batch/res-02 created
job.batch/res-03 created
job.batch/res-04 created
job.batch/res-05 created
job.batch/res-06 created
job.batch/res-07 created
job.batch/res-08 created
job.batch/res-09 created
job.batch/res-10 created
job.batch/res-11 created

$ research alone
research 96 GPUs

$ kubectl apply -f llm-jobs.yaml   # 12 x 8 GPUs, weight 2
job.batch/llm-00 created
job.batch/llm-01 created
job.batch/llm-02 created
job.batch/llm-03 created
job.batch/llm-04 created
job.batch/llm-05 created
job.batch/llm-06 created
job.batch/llm-07 created
job.batch/llm-08 created
job.batch/llm-09 created
job.batch/llm-10 created
job.batch/llm-11 created

$ kubectl get clusterqueue cq-research cq-llm -o custom-columns=QUEUE:.metadata.name,WEIGHT:.spec.fairSharing.weight,ADMITTED:.status.admittedWorkloads,WEIGHTED_SHARE:.status.fairSharing.weightedShare
QUEUE         WEIGHT   ADMITTED   WEIGHTED_SHARE
cq-research   1        4          334
cq-llm        2        8          334

$ after LLM arrives
research 32 GPUs, LLM 64 GPUs; research workloads preempted: 8 (['InCohortFairSharing'])

$ kubectl apply -f sweeps-1.yaml   # 2 x 8 GPUs, 150 s each: the sweeps project uses the queue
job.batch/sweep-0 created
job.batch/sweep-1 created

$ kubectl apply -f sweeps-2.yaml   # 4 more sweeps, queued first
job.batch/sweep-2 created
job.batch/sweep-3 created
job.batch/sweep-4 created
job.batch/sweep-5 created

$ kubectl apply -f paper.yaml   # 1 paper job, queued after them
job.batch/paper-0 created

$ kubectl get localqueue -A -o custom-columns=QUEUE:.metadata.name,NAMESPACE:.metadata.namespace,CONSUMED_GPU:.status.fairSharing.admissionFairSharingStatus.consumedResources.nvidia\.com/gpu,PENDING:.status.pendingWorkloads
QUEUE    NAMESPACE         CONSUMED_GPU   PENDING
paper    research-paper    302757124n     0
sweeps   research-sweeps   3024550576n    3

$ the jobs admitted into the two slots the first sweeps freed
paper-0, sweep-5

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | fair sharing moves the borrowed 96 GPUs to the 2:1 weights: LLM 64, research 32 (within one 8-GPU job) | A queue below its weighted share preempts workloads of a queue above it; the strategies stop when shares are as fair as the 8-GPU jobs allow. |
| PASS | when capacity frees up, the paper project's job takes one of the two freed slots, ahead of three of the four sweeps queued before it | Usage-based admission fair sharing orders pending workloads by their LocalQueue's recent usage, decayed with a 10 min half-life. |

**Why you can trust this result.**

- Admitted GPUs per team and the preemption reasons are Kueue's status; the weighted shares are in the ClusterQueues' status.
- The admission order is read from the Workloads' conditions after the two freed slots are filled.

**Why you get this output.**

- A queue below its weighted share preempts a queue above it until the shares are as close to 2:1 as 8-GPU jobs allow.
- Usage-based admission fair sharing orders pending workloads by their LocalQueue's decayed recent usage, so the paper project's job passes sweeps queued before it.

### S10: Priority, preemption and backfill: urgent work, the cost of preemption, and bounded small jobs

**Design reference:** Priority, preemption and backfill.

**What this tests.** One team queue with 32 GPUs. Four best-effort 8-GPU jobs (checkpoint every 600 s) fill it; a release job arrives. Then a 32-GPU job waits behind a 24-GPU job while an 8-GPU job with a declared 90 s runtime limit fills the gap.

Urgent work gets capacity by preemption, the cost of preemption is measured, and a small job with a declared run-time limit fills a gap.

![S10: Priority, preemption and backfill: urgent work, the cost of preemption, and bounded small jobs](diagrams/appendix/a18-evidence/S10.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl apply -f sweeps.yaml   # 4 x 8 GPUs, priority best-effort
job.batch/sweep-0 created
job.batch/sweep-1 created
job.batch/sweep-2 created
job.batch/sweep-3 created

$ kubectl apply -f rc-eval.yaml   # 1 x 8 GPUs, priority release
job.batch/rc-eval created

$ kubectl -n team-llm get workloads -o custom-columns=WORKLOAD:.metadata.name,PRIORITY:.spec.priority,ADMITTED:.status.conditions[?(@.type=='Admitted')].status,PREEMPTED:.status.conditions[?(@.type=='Preempted')].reason
WORKLOAD            PRIORITY   ADMITTED   PREEMPTED
job-rc-eval-c404d   1000       True       <none>
job-sweep-0-f7399   10         True       <none>
job-sweep-1-cf2ba   10         True       <none>
job-sweep-2-733ac   10         False      InClusterQueue
job-sweep-3-2b8d1   10         True       <none>

$ accounting: work lost to that preemption
{
 "lost_to_preemption_gpu_minutes": 10.7,
 "preemptions": [
  {
   "t": 1795823820.02433,
   "workload": "team-llm/job-sweep-2-733ac",
   "team": "llm",
   "gpus": 8,
   "ran_seconds": 80,
   "checkpoint_seconds": 600.0,
   "reason": "Preempted to accommodate a workload (UID: f5fb4c15-66e3-4455-a540-f8c7036bfbf8, JobUID: 3eae00bf-0035-465f-88fc-4ea5f02e39ca) due to prioritization in the Clust",
   "lost_gpu_minutes": 10.67
  }
 ]
}

$ kubectl apply -f eval-24.yaml   # 3 x 8 GPUs for 150 s
job.batch/eval-24 created

$ kubectl apply -f train-32.yaml   # 4 x 8 GPUs: must wait for eval-24
job.batch/train-32 created

$ kubectl apply -f probe-8.yaml   # 1 x 8 GPUs, kueue.x-k8s.io/max-exec-time-seconds: 90 (it would run 1 h)
job.batch/probe-8 created

$ 15 s later
{"train-32_admitted": false, "probe-8_admitted": true}

$ the bounded job after its limit
{"condition": "Evicted", "reason": "DeactivatedDueToMaximumExecutionTimeExceeded", "message": "The workload is deactivated due to exceeding the maximum execution time", "active": false, "pods": {"run…

$ when eval-24 finished
{"train-32": {"running": 4, "pending": 0, "gpus_held": 32}}

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the release job is admitted at once by preempting one best-effort job (InClusterQueue) | withinClusterQueue: LowerPriority; release 1000 against best-effort 10. |
| PASS | the accounting service records the preempted job's work since its last checkpoint as lost | 8 GPUs times the time since the last 600 s checkpoint boundary. |
| PASS | while the 32-GPU job waits, the bounded 8-GPU job runs in the gap | BestEffortFIFO admits a later workload that fits when the head of the queue does not. |
| PASS | Kueue stops the bounded job at its declared 90 s although it would have run an hour, so it cannot delay the big job | kueue.x-k8s.io/max-exec-time-seconds: the workload is deactivated when its admitted time passes the limit. |
| PASS | the 32-GPU job starts when the 24-GPU job finishes | Its whole request fits once eval-24's quota is released. |

**Why you can trust this result.**

- Priorities, preemption reasons and the run-time limit's effect are Kueue's Workload status.
- The lost work is the accounting service's record of the eviction.

**Why you get this output.**

- The release job preempts one best-effort job because withinClusterQueue is LowerPriority and release outranks best-effort.
- BestEffortFIFO admits the small job while the large one waits, and Kueue deactivates it when its admitted time passes the declared limit.

### S11: Measuring the scheduler: where every GPU-hour of this run went, and queue wait by job size

**Design reference:** Measuring and tuning the scheduler; Where GPU-hours go.

**What this tests.** Read the accounting service's breakdown for the whole run (S02 to S10), for the real and the simulated GPUs, per category and per team, and the queue wait of every Kueue workload by size class.

The accounting service's breakdown for the whole run, and the queue wait of every Kueue workload by job size.

![S11: Measuring the scheduler: where every GPU-hour of this run went, and queue wait by job size](diagrams/appendix/a18-evidence/S11.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl get --raw .../services/accounting:8080/proxy/breakdown?since=<S01>   # all GPUs
{
 "samples": 193,
 "gpu_minutes": 4181.7,
 "share": {
  "free": 0.425,
  "busy": 0.312,
  "blocked_by_quota": 0.208,
  "fragmented": 0.033,
  "waiting_for_gang": 0.021,
  "allocated_idle": 0.001
 },
 "gpu_minutes_by_category": {
  "free": 1776.2,
  "busy": 1306.5,
  "blocked_by_quota": 871.7,
  "fragmented": 137.7,
… (5 more lines in the screenshot and in evidence/S11.txt)

$ by team (GPU-minutes; '-' is free capacity)
-         {"free": 1776.2, "blocked_by_quota": 871.7, "fragmented": 137.7}
gpu       {"allocated_idle": 2.7, "busy": 2.0}
llm       {"waiting_for_gang": 50.7, "busy": 652.0}
nokueue   {"busy": 101.3, "waiting_for_gang": 17.7}
research  {"busy": 539.2, "waiting_for_gang": 14.7}
vision    {"waiting_for_gang": 4.0, "busy": 12.0}

$ queue wait (creation to quota reserved) by job size in GPUs: workloads, median, max, in seconds
8      n  20  median     1.5  max     9.0
12-24  n   2  median     0.0  max     0.0
32     n   5  median     0.0  max   157.0
64     n   1  median     0.0  max     0.0

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | every sampled GPU-minute of 130 GPUs is in exactly one category (total within 2% of 130 GPUs times the sampled time) | Each sample assigns every GPU of every GPU node one category. |
| PASS | each cause the design names was observed: busy, allocated but idle, waiting for a gang, fragmented, blocked by quota, and lost to preemption | S02, S03, S06, S08 and S10 each create one of them on purpose. |
| PASS | queue wait is reported for at least three size classes, from the workloads of S03 to S10 | Kueue records when each workload reserved quota; the scenarios kept those times before cleaning up. |

**Why you can trust this result.**

- The total is checked against the number of GPUs times the sampled time, so no GPU-minute is missing or counted twice.
- The waits were recorded from Kueue's own timestamps before each scenario cleaned up.

**Why you get this output.**

- Each category is present because an earlier scenario created it on purpose: S02 idle GPUs, S03 a partial gang, S06 fragments, S08 quota policy, S10 preemption.

### 6. What I did not build, and why

**Scenario.** A build is only credible if it says where it stops. These are the parts of Design 17 that this implementation does not cover, with the reason for each.

| Not built | Why |
|---|---|
| **A real 1,024-GPU fleet** | 128 of the GPUs here are simulated: KWOK nodes with no hardware, whose pods never run. Admission, preemption, fair sharing, topology placement and bin-packing are decided by the real Kueue and kube-scheduler on real API objects, so those results carry over; what does not carry over is anything a running workload does (all-reduce speed across racks, data loading, how long a real gang takes to become ready). |
| **Busy and idle on the simulated GPUs** | A simulated GPU has no activity. Each simulated pod declares one, and the accounting service uses it; only S02's real GPUs are measured by DCGM. |
| **Changing the cluster's own scheduler** | EKS manages kube-scheduler and does not expose its configuration, so the profiles run in a second scheduler. Pods that do not name it still use the default scheduler. |
| **JobSet, LeaderWorkerSet, RayJob, PyTorchJob** | Every gang here is an Indexed `batch/v1` Job; Kueue treats it as one workload with one PodSet. Multi-PodSet workloads and topology groups across PodSets are not tested. |
| **Checkpoint-aware choice of preemption victims** | Lost work is measured (S10), but Kueue chooses victims by priority and fair share, not by the time since their last checkpoint. |
| **Reservation-based backfill** | S10 shows a bounded small job running while a large one waits, and Kueue stopping it at its limit. Kueue does not reserve future capacity for the waiting job, so nothing here guarantees the large job starts at a known time. |
| **GPU partitioning (MIG, time-slicing, MPS), idle culling** | The design's right-sizing tools for small work are not built; S02 only measures the idle notebook. |
| **Trace replay of real submissions** | S06 replays one seeded synthetic trace against two profiles. There is no recorder of real submissions and no scheduler simulator. |
| **Elastic jobs and native gang scheduling in kube-scheduler** | Open questions in the design; kube-scheduler's workload-aware scheduling is off by default in 1.37 and not exposed by EKS. |

### 7. Run it yourself, and take it down

**Scenario.** The point of Terraform is that anyone can rebuild this from the repository and remove it again. These are the exact steps.

**What you need on your machine.** Terraform 1.9 or later, the AWS CLI v2 signed in to the account you want to use, Helm 3, and Python 3 with Pillow and PyYAML. `scripts/setup-tools.sh` downloads a matching `kubectl` into the project folder with a checksum check. The build needs up to 16 vCPU of on-demand G instance quota in us-east-2.

**Build.**

```text
cd implementations/ds17-gpu-scheduling-on-eks
export AWS_PROFILE=<your profile>
./scripts/up.sh
```

`up.sh` writes small local files with your account ID and your public addresses (git-ignored), builds the VPC, the cluster and the two node groups, then the platform, then the 16 simulated nodes, and waits until the scheduler and the accounting service are ready.

**Test.**

```text
python3 tests/run_scenarios.py all
```

The whole run takes about 50 minutes; most of it is waiting for timeouts, backoffs and simulated run times.

**Take it down.**

```text
./scripts/down.sh --yes
```

`down.sh` deletes the scenario jobs and the simulated nodes, destroys the platform layer, destroys the cluster layer, and then runs `verify-clean.sh`, which looks in us-east-2 for anything of this build and prints `CLEAN` when nothing is left.

**The teardown after the final run.** This is the output of `verify-clean.sh` after `down.sh` removed the build that produced the evidence above:

```text
ok         EKS clusters: none
ok         EC2 instances: none
ok         EC2 instances (Project): none
ok         Auto Scaling groups: none
ok         Launch templates: none
ok         NAT gateways: none
ok         Elastic IPs: none
ok         VPCs: none
ok         VPC endpoints: none
ok         Security groups: none
ok         EBS volumes: none
ok         CSI volumes: none
ok         CSI snapshots: none
ok         Network interfaces: none
ok         S3 buckets: none
ok         IAM roles: none
ok         CloudWatch log groups: none
info       tagging index still lists (it lags deletions): 8 item(s)

CLEAN: nothing from ds17-sched remains in this account and region.
```

**Keeping it safe in a public repository.** Terraform state, plans, kubeconfig files, local variable files and scratch evidence are git-ignored. `scripts/check-no-secrets.sh --all` blocks any file containing a credential pattern, the account ID or one of the administrator's addresses. The test harness redacts those values, and the local user name, before anything is written.

### 8. Screenshots of the AWS Console

The terminal screenshots above are rendered from recorded command output. I could not capture the AWS Console from here. If you want console pictures, these pages show the same facts. Crop out the account number and your user name in the top bar before publishing.

- EKS, Clusters: `ds17-sched`, its Compute tab with the two node groups, and its Resources tab, where the 16 simulated nodes appear next to the real ones.
- EC2, Instances: the two GPU instances and their types.
