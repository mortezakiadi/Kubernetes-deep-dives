---
id: appendix-design-17-eks
title: "Appendix R: Design 17 built and tested on Amazon EKS"
short: "R. Design 17 on EKS (built and tested)"
---
This appendix describes a working, scaled-down implementation of [Design 17: Scheduling that keeps accelerators busy](#design-gpu-scheduling). I built it with Terraform on a standard Amazon EKS cluster in one AWS account: Kueue for admission (quotas in cohorts, borrowing and lending, fair sharing, priorities, the pods-ready timeout and topology-aware admission), a second kube-scheduler with a bin-packing and a spreading GPU profile for placement, a GPU-hour accounting service that puts every GPU into one category every 10 seconds, two real GPUs measured with DCGM, and a simulated fleet of 128 GPUs on which the scheduling behaviour is tested.

The order is the one a reader needs: first the context, then the software I implemented, the EKS topology and instances, the architecture, how it is implemented, and then the scenarios I tested, with the commands, the results, why the results can be trusted and why the system produces them. Section 6 says plainly what I did not build. (Appendices M to Q are kept for Designs 12 to 16, which are not built yet.)

**What "scaled down" and "simulated" mean here.** The design is about a 1,024-GPU cluster of 128 nodes with 8 H100s each. This build has two real GPUs and a simulated fleet of 16 nodes with 8 GPUs each, 128 in all, in 2 network blocks of 2 racks of 4 hosts. The simulated nodes are Kubernetes Node objects run by KWOK: no machine stands behind them, their GPUs do not exist, and pods bound to them are never started, only moved through their lifecycle (ready, then complete after the run time set on each pod). That is enough for this design, because everything it is about happens in the control plane: Kueue admits, preempts and orders real Workload objects, the real kube-scheduler binary scores and binds real Pod objects to those nodes, and the accounting service reads the same API. What a simulated GPU cannot show is whether it is busy, so the real GPUs are used for exactly that (S02), and simulated pods declare their activity.

{{RESULT_SUMMARY}}

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

{{CODE_SIZE}}

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

