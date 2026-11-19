---
id: appendix-design-02-eks
title: "Appendix C: Design 02 built and tested on Amazon EKS"
short: "C. Design 02 on EKS (built and tested)"
---
This appendix describes a working, scaled-down implementation of [Design 02: Operating CPU and GPU clusters as one compute platform](#design-multicluster). I built it with Terraform on Amazon EKS in one AWS account: a hub cluster and three worker clusters (two CPU, one GPU), one front door for batch jobs, fair sharing between three teams, GPU capacity that is bought only when a queued job needs it, and walls between teams. I tested every claim of the design that this scale can test, recorded the evidence, and removed it again with one command. Every command, output and pass or fail result in section 5 is copied from the recorded test run, not written from memory.

The order is the one a reader needs: first the context (the problem, who the actors are and who talks to whom), then the software I implemented, the EKS topology and instances, the architecture, how it is implemented, and then the scenarios I tested with the commands, the results, why the results can be trusted and why the system produces them.

**What "scaled down" means here.** The design has a hub, CPU clusters, large GPU training clusters with Capacity Blocks and EFA, and inference clusters in several regions, with hundreds of GPUs. This build has one hub, two CPU workers of four vCPUs each, and one GPU worker that can run up to two single-GPU nodes on-demand and two on Spot, all in one region. The mechanisms are the same ones: the same Kueue and MultiKueue, the same quota model, the same node-pool rules. Only the numbers are small, and section 6 lists what I did not build.

{{RESULT_SUMMARY}}

### Context: the problem, the actors and who talks to whom

**Scenario.** A company has grown several Kubernetes clusters by accident. Each has its own quotas and its own chat channel for "who is using the GPUs?" A training job waits for days in one cluster while reserved GPUs sit idle in another, and two teams have started buying their own capacity. Leadership asks the platform team to make it one platform. A researcher should be able to submit a job once, without knowing which cluster has room, who owns the GPUs or how the machines get bought. This appendix builds a small version of that platform and tests whether it behaves that way.

Five terms are used throughout.

- **Team.** A group that owns a share of the fleet. The test has three: `llm`, `vision` and `research`. Each has a namespace in every cluster, a guaranteed share, and limits on how much it lends and borrows.
- **Job.** A batch job such as a training run. It is what a team submits.
- **Hub cluster.** The front door. Teams submit jobs here and nowhere else. It holds each team's share of the whole fleet, decides which worker to offer a job to, and runs no user pods.
- **Worker clusters.** The clusters that run the jobs: `cpu-a` (On-Demand), `cpu-b` (Spot) and `gpu`. Each is a complete cluster with its own queues.
- **Queue manager and node pools.** The queue manager is Kueue, which runs on every cluster. On the hub it chooses a worker. On a worker it decides whether a job may start. A node pool says which machines may be bought, and buys one only when an admitted pod needs it.

![The path of one job: team, hub, worker, node pool and AWS](diagrams/appendix/a03-5.svg)

**What the picture shows.** The path of one job from the team to AWS. The dashed box is not built here: teams submit with `kubectl`, and I did not build a portal.

**The path of one job.**

1. A team member submits a job to the hub, into the team's own queue.
2. The hub's queue manager checks the team's share of the whole fleet (layer 1). The job is held, with no pods created, until the share allows it.
3. The hub offers the job to the workers that suit it, CPU or GPU. Each of them gets a copy.
4. Each worker checks its own queue for that team (layer 2): the quota, its own or borrowed, and room for all of the job's pods at once. The first worker that can admit the job wins, and the hub deletes the other copies.
5. The winning worker starts the pods and its scheduler places them. If no node fits, the worker's node pool asks AWS for one. GPU nodes exist only while a job needs them.
6. The job runs. Its pods can read their own team's data in S3 and no other team's.
7. The hub copies the job's status back, so the team sees it on the hub. When the job ends, the copy in the worker is deleted and the empty nodes are removed.

![One job as calls between components](diagrams/appendix/a03-6.svg)

**What the picture shows.** One CPU job as calls between the components. Time runs from top to bottom. Each arrow is one call and its number matches the list below. Boxes on a line are decisions a component makes. Dashed arrows are replies.

**Who talks to whom, and why.**

1. **Team to Hub.** The team creates a Job in its own namespace with a queue label. *Why:* there is one front door, and the team never needs to know which cluster will run the job. A pod created directly on the hub is refused (S02).
2. **Hub, layer 1.** The hub's queue manager reserves quota in the team's fleet-wide queue and keeps the Job suspended. *Why:* quota is decided before any pod exists. A job larger than the team's fleet share waits here, and its status says so (S03).
3. **Hub to the worker that will win.** The hub copies the job's Workload to a candidate worker over the private network, using the narrow identity it holds for that worker. *Why:* the hub does not need to know in advance which worker has room. It only offers work to workers whose connection is up (S06, S09).
4. **Hub to the other worker.** The same copy goes to the other candidate. *Why:* whichever worker can admit the job first should win (S06).
5. **Worker, layer 2.** The worker's queue manager checks the team's queue in this cluster: its own share plus what it may borrow, and whether all of the job's pods fit at once. *Why:* it is all or nothing, so a job never starts three of four pods and holds resources while waiting for the fourth (S04). A team's borrowed capacity can be taken back by its owner (S05).
6. **Worker to Hub.** The worker reports that it admitted the job. *Why:* the hub has to know which worker won.
7. **Hub to the other worker.** The hub deletes the other copy (S06).
8. **Worker to kube-scheduler.** The job is released, its pods are created and the scheduler places them (layer 3). *Why:* pods exist only for work that already has quota (S03).
9. **kube-scheduler to Node pool.** A pod that fits on no node waits, and the node pool (layer 4) sees it. *Why:* capacity is bought only for admitted work. A GPU pod carries the toleration its flavor adds, which is the only way onto the tainted GPU nodes (S07).
10. **Node pool to AWS.** The pool launches an instance of a type it allows. *Why:* the pools have limits and start empty. The GPU worker may use two GPUs On-Demand and two on Spot, and Auto Mode picks whichever allowed family has capacity (S07).
11. **AWS to Worker.** The node joins the cluster and the pods run.
12. **Worker to Hub.** The hub watches the job in the worker and copies its status back. *Why:* the team never needs credentials for a worker (S02).
13. **Hub to Team.** The team sees the status on the hub.
14. **Hub to Worker.** After the final status copy, the hub deletes the job in the worker (S02).
15. **Node pool to AWS.** The empty node is removed, after one minute for CPU nodes and two minutes for GPU nodes. *Why:* idle capacity should not cost money (S07).

Three rules decide what a job gets.

- **Where it runs:** the first worker that can admit the whole job (S06).
- **When it runs:** each team has a guaranteed share, can borrow what others are not using, and gets it back when the owner asks. Borrowed work is preempted and requeued (S05).
- **What capacity it gets:** the flavors in the team's queue, in order. `llm` gets On-Demand GPU first and Spot after, `research` gets Spot only, and a GPU can be reached only through a queue (S07).

![Components view: where each piece runs and who calls whom](diagrams/appendix/a03-7.svg)

**What the picture shows.** The components view. The hub holds the front door, the fleet-wide queues and one narrow credential per worker. Each worker is a full cluster with its own queue manager, team namespaces and node pools. AWS sits underneath: the instances the node pools buy, and one bucket with a prefix per team.

| Caller | Callee | Why | What enforces it |
|---|---|---|---|
| Team | Hub API server | Submit a job to the team's queue | A namespace per team with Pod Security, a resource quota and a network policy (S08). The hub refuses any pod in a team namespace (S02). |
| Hub queue manager | Each worker's API server | Copy jobs, watch their status, delete the copies | One kubeconfig Secret per worker, holding a narrow identity limited to Jobs, pods and Kueue's own objects. Private endpoint inside the VPC (S01, S09). |
| Hub queue manager | Fleet queues | Layer 1 quota for each team | The hub's queues and the workers' queues are generated from one team table, so the hub's quota is the sum of the workers' (S01). |
| Worker queue manager | Team queues in the worker | Layer 2: admit the whole job, borrow, preempt | The same table (S04, S05). |
| Worker | kube-scheduler | Place the pods of an admitted job | Kueue manages every Job and Pod in a team namespace. A pod with no queue is held by a scheduling gate (S07). |
| Node pool | EC2 | Buy and remove nodes | Pool limits. GPU pools start empty, are limited to two GPUs each and are tainted (S07). |
| Job pod | S3 | Read the team's own data | One role per team limited to its prefix, assumed through EKS Pod Identity in every worker (S08). |
| Team's pods | Another team's pods | Nothing | A network policy admits traffic only from the same namespace (S08). |

The sections that follow build on this picture. Section 1 lists each component and what I wrote or reused, section 2 shows the topology, section 3 shows the architecture and the four layers, and section 5 tests each promise.

### 1. The software components I implemented

**Scenario.** The design draws a hub, worker clusters, queues and node pools. To test it, each of those has to be real software that I can start, call and push to its limits. Nothing is left as "assume this exists".

| # | Component | What it is | Where it runs | Reused or written |
|---|---|---|---|---|
| 1 | **Hub cluster** | A small EKS Auto Mode cluster that runs the queue manager and no user work. A policy refuses any pod in a team namespace. | Cluster `ds02-fleet-hub` | Written (Terraform and policy) |
| 2 | **Kueue 0.20.7 and MultiKueue** | The job queueing system. On the hub it is the manager: it holds the fleet-wide quota, picks workers and mirrors status. On each worker it is the local admission controller. The same pinned version runs on all four clusters. | All four clusters | Reused (Kubernetes SIG Scheduling) |
| 3 | **Worker clusters** | Two CPU workers (`cpu-a` on On-Demand capacity, `cpu-b` on Spot) and one GPU worker. Each is a standalone EKS Auto Mode cluster with its own queues. | Clusters `ds02-fleet-cpu-a`, `-cpu-b`, `-gpu` | Written (Terraform) |
| 4 | **The team table** | One table of team shares (CPU and GPU, guaranteed, lendable, borrowable). Terraform generates every queue on the hub and every worker from it, so the hub's quota is always the sum of the workers'. | `terraform/20-platform/teams.tf` | Written |
| 5 | **Queues and flavors** | Per team: a CPU and a GPU ClusterQueue on the hub, matching queues on the workers, a LocalQueue per namespace, ResourceFlavors that map to node pools, and three priority classes (`production`, `standard`, `opportunistic`). | Hub and workers | Written (generated) |
| 6 | **Node pools** | Auto Mode NodePools: `cpu-ondemand`, `cpu-spot`, `gpu-ondemand`, `gpu-spot`. The GPU pools are tainted, limited to two GPUs each, and start empty. | Workers | Written |
| 7 | **Dispatch wiring** | A kubeconfig Secret per worker on the hub, each holding a narrowly scoped identity; a MultiKueueCluster per worker; MultiKueueConfigs; and the AdmissionChecks the hub's queues use. | Hub | Written |
| 8 | **Tenant walls** | A namespace per team in every cluster with the Pod Security `restricted` profile, a resource quota, and a network policy that admits traffic only from the same namespace. | All clusters | Written |
| 9 | **Team identity and data** | One S3 bucket with a prefix per team, an IAM role per team limited to its prefix, and an EKS Pod Identity association per team in every worker. | AWS and workers | Written |
| 10 | **Side-door guards** | Kueue manages every Job and Pod in a team namespace, so one with no queue is held. The hub refuses pods outright. GPU nodes are tainted. | All clusters | Configured |
| 11 | **Fleet status tool** | A script that reads the Workloads and ClusterQueues of all four clusters and says, for each job, which layer is holding it. | `scripts/fleet-status.py` | Written |
| 12 | **Scenario runner and screenshot renderer** | Runs the tests, records commands and output, redacts private values and draws terminal-style screenshots. | `tests/` | Written |

In total I wrote about 1,200 lines of Terraform, 600 lines of test code and a 80-line status tool. There is no application code: Design 02 is about wiring existing schedulers together correctly, so the work is configuration, and the proof is behaviour.

**Going deeper.**

- **One module, four clusters.** A single Terraform module installs Kueue, creates the namespaces, queues and walls, and (for workers) creates the identity the hub uses. It is called once per cluster, so the clusters cannot drift apart.
- **Where each secret lives.** Each worker's access token is created in that worker and copied into a Secret in the hub's Kueue namespace and nowhere else. Team roles can read only their own S3 prefix.
- **Why the hub has no worker pool.** MultiKueue does not support the manager being one of its own workers, and keeping the hub small and boring means a noisy job can never take down scheduling for everyone.

### 2. EKS topology and instances

**Scenario.** Isolation and cost both depend on where things run. The topology has to give each cluster its own failure domain, keep GPU capacity at zero when idle, and let the hub reach the workers without opening their APIs to the internet.

![Fleet topology and instances](diagrams/appendix/a03-2.svg)

**What the picture shows.** One VPC, two Availability Zones, four independent EKS clusters. Each worker's nodes are bought by its own node pools.

| Item | What I built | Why |
|---|---|---|
| **Clusters** | Four EKS Auto Mode clusters on Kubernetes 1.37, from one Terraform resource written once | The design keeps the fleet within one minor version so a job that runs in one worker runs in all. |
| **API access** | Private endpoint inside the VPC; public endpoint only for the administrator's `/32`. The workers' security groups admit HTTPS from the VPC so the hub can reach them privately. | The hub dispatches over the private network. No worker API is open to the internet. |
| **Platform nodes** | The built-in Auto Mode `general-purpose` pool on every cluster (small `c6a` instances in my run; S01 prints the exact list). It runs Kueue and the add-ons. | One or two small nodes per cluster, removed by Auto Mode when not needed. |
| **CPU node pools** | `cpu-ondemand` on cpu-a and `cpu-spot` on cpu-b: `c` or `m` family, generation 6 or later, 2 or 4 vCPUs, a 16-vCPU limit, empty nodes removed after one minute | A ResourceFlavor's node label selects the pool, so quota and capacity line up. |
| **GPU node pools** | `gpu-ondemand` and `gpu-spot` on the GPU worker. Each may use `xlarge` instances of the `g6` (NVIDIA L4), `g5` (A10G), `g6e` or `g4dn` (T4) families, one GPU per node, limited to two GPUs, tainted `nvidia.com/gpu:NoSchedule`, empty nodes removed after two minutes | GPU nodes exist only while an admitted job needs them. Auto Mode picks whichever family has capacity: in my first attempt `g6` had none in the zone and a `g4dn` node with a Tesla T4 was launched instead. S07 prints the instance type and GPU model it got. |
| **Drivers** | None to install: EKS Auto Mode includes the NVIDIA drivers and device plugin. | Fewer moving parts per cluster. |
| **Network** | `10.30.0.0/16`, a public subnet and a private subnet in each of two zones, one NAT gateway | One NAT keeps cost down. A production build uses one per zone. |
| **IAM** | One cluster role (five AWS-managed EKS policies), one node role (`WorkerNodeMinimalPolicy` and `ContainerRegistryPullOnly`), and one role per team for S3 through Pod Identity | No role on a node can touch S3, and no team role can touch another team's prefix. |
| **State** | Terraform state is local and git-ignored | The repository is public; state holds identifiers. |

**Going deeper.**

- **What Auto Mode does for me.** It provisions and patches the nodes, runs the network plugin and DNS, and includes the GPU drivers. My NodePools only say which nodes are allowed.
- **Cost.** Four control planes, a handful of small nodes, one NAT gateway, and GPU nodes only for the minutes a test needs them. The whole stack exists for hours, and `down.sh` removes it.
- **Not Capacity Blocks.** The design buys large training runs as reserved Capacity Blocks. I have no reservation to use, so the first flavor is On-Demand and the second is Spot.

### 3. Architecture

**Scenario.** A researcher submits a training job. She should not need to know which cluster has room, who owns the GPUs, or how the nodes get bought. A new engineer asks what happens between "submit" and "running".

![One hub, three workers, one front door](diagrams/appendix/a03-1.svg)

**What the picture shows.** Teams submit to the hub (1). The hub's Kueue holds each team's share of the whole fleet and decides which worker to offer a job to (2, 3). The hub refuses to run anything itself and holds only narrow credentials for the workers (4). Each worker is a complete Kueue cluster with its own queues and node pools (5). Identity and data (6) and the Terraform that makes it all (7) sit underneath.

![One job's path through the four layers](diagrams/appendix/a03-3.svg)

**The four layers.** Every batch job passes four decisions, each made by a different component, and each can be the reason a job waits.

1. **Layer 1, the hub's Kueue:** does the team have quota in the fleet-wide queue, and which workers should be offered the job?
2. **Layer 2, the worker's Kueue:** does the team's queue in this worker have quota (its own or borrowed), and can the whole job be admitted at once?
3. **Layer 3, kube-scheduler:** which node and which device takes each pod?
4. **Layer 4, the node pool:** a pod that cannot be placed makes Auto Mode buy a node of the right kind.

![Quotas, borrowing and reclaim on one worker](diagrams/appendix/a03-4.svg)

**The quota model.** Each team has a guaranteed share (nominal quota), can lend part of it (lending limit) and can borrow from the others up to a cap (borrowing limit). A team that asks for its own share takes it back from whoever borrowed it.

### 4. How it is implemented

**Scenario.** A reader wants to rebuild this, or to know which line of configuration produces which behaviour. This section maps the design to files.

**The folder** (`implementations/ds02-multicluster-compute/` in the repository):

- `terraform/10-fleet/`: the VPC, subnets, NAT gateway, IAM roles, and the four EKS Auto Mode clusters.
- `terraform/20-platform/`: Kueue on every cluster (one reusable module), the team table, queues, node pools, the hub's MultiKueue wiring, team identities and the hub policy.
- `scripts/`: `up.sh`, `down.sh`, `verify-clean.sh`, `fleet-status.py`, `setup-tools.sh`, and a secret scanner that blocks a commit containing a credential.
- `tests/`: the scenario runner, the harness and the screenshot renderer.
- `docs/`: the generator for this appendix and its diagrams.

**Two Terraform layers.** The first layer creates the clusters. The second talks to them, so it has to run after the first. Both refuse to run against any account except the one named in a local, git-ignored file.

**What the second layer does, in order.**

1. Installs Kueue 0.20.7 on all four clusters from one module, with the same configuration: whole-job admission, `waitForPodsReady`, fair sharing, and management of every Job and Pod in team namespaces.
2. Turns on NetworkPolicy enforcement (Auto Mode ships it off) and creates each team's namespace, quota and network policy.
3. Creates the priority classes, ResourceFlavors, ClusterQueues, LocalQueues and NodePools, all generated from the team table.
4. On each worker, creates the narrow identity the hub will use and reads its token.
5. On the hub, stores each worker's kubeconfig as a Secret, and creates the MultiKueueClusters, MultiKueueConfigs and AdmissionChecks.
6. Creates the S3 bucket, the team roles and the Pod Identity associations, and the hub's admission policy.

**The control-by-control map.**

| Design control | Implemented as | Verified by |
|---|---|---|
| One front door for batch work | Hub LocalQueues with a MultiKueue admission check | S02 |
| The hub runs no user workloads | ValidatingAdmissionPolicy on the hub; MultiKueue keeps hub jobs suspended | S02 |
| Every wait has one owner | Four layers, each with its own status; `fleet-status.py` reads them | S03, S10 |
| Gang scheduling | Kueue admits all pods or none; the job stays suspended until then | S04 |
| Guaranteed, borrowable, reclaimable shares | One cohort per kind of capacity, with nominal, lending and borrowing limits and `reclaimWithinCohort: Any` | S05 |
| Dispatch to the right cluster | `MultiKueueConfig` lists, AllAtOnce dispatch, first worker to admit wins | S06 |
| Reserved before on-demand before Spot | Flavors listed in order in each queue; a team's queue lists only the flavors it may use | S07 |
| GPU capacity bought only when needed | NodePools that start empty, with limits and a short consolidation delay | S07 |
| Close the side doors | `manageJobsWithoutQueueName`, a taint on GPU nodes, a toleration added only by the flavor | S07 |
| Tenant isolation | Namespace, Pod Security, quota, network policy per team; Pod Identity roles per prefix | S08 |
| Narrow credentials for the hub | A ServiceAccount per worker with a ClusterRole limited to Jobs and Workloads | S01, S09 |
| Failure modes | A worker lost, the hub's controller down | S09 |
| One place to see the fleet | `fleet-status.py` over Workloads and ClusterQueues | S10 |

**Where reality differed from the design.** These are findings, confirmed on the live fleet.

- **The AWS CLI prefers `AWS_REGION` over `AWS_DEFAULT_REGION`.** If your shell sets the first, commands go to the wrong region. `up.sh` and the test harness set both.
- **Two ClusterQueues per team on the hub, not one.** A MultiKueue admission check applies to a queue's whole workload, so CPU and GPU work, which go to different workers, need different queues. I made one queue per team per kind of capacity (`llm-cpu`, `llm-gpu`).
- **The hub's narrow identity must also be allowed to watch pods.** With Kueue's pod integration switched on, the hub watches each worker's pods, and without that permission the connection stays down (`pods is forbidden`). I added pods to the narrow role, as Kueue's own setup guide does. It still cannot touch anything outside Jobs, pods and Kueue's own objects.
- **A job borrows another team's on-demand GPU before using its own Spot one, by default.** The default is to stop at the first flavor that can borrow. I set `whenCanBorrow: TryNextFlavor` on the GPU queues so a team uses its own Spot share first, which is the cost behaviour the design asks for.
- **GPU capacity is not guaranteed, even in a large region.** `g6` instances had no capacity in the zones I could use at the time. Listing four GPU families in the NodePool let Auto Mode launch a `g4dn` node instead. A real platform would use reservations.
- **A fresh node's log endpoint is not ready for a few seconds.** `kubectl logs` against a new node first returns `tls: internal error`, so the tests wait for the job to finish and then retry.
- **Tearing Kueue down can hang.** Kueue puts finalizers on its queues and flavors. Once its controller is removed nothing clears them, and deleting Kueue's custom resource definitions waits forever. In my teardown I had to clear them by hand. `down.sh` now clears them repeatedly while the platform layer is destroyed. I have not re-run that new version of the teardown, because the environment was already gone by then.
- **Quota groups cannot overlap.** A queue's resource groups cannot cover the same resource twice, so GPU jobs' CPU and memory must be in the GPU queue's group.

### 5. The scenarios I tested

**Scenario.** The design makes specific promises. I turned each into a scenario that runs real commands against the live fleet and decides pass or fail from the output, with no human judgement in the loop.

**How to read this section.**

- `--context hub`, `cpu-a`, `cpu-b` or `gpu` selects the cluster the command runs against. Commands that start with `scripts/fleet-status.py` read all four.
- "Layer 1" and "layer 2" are the hub's and the workers' admission decisions, as in the picture above.
- The screenshots are terminal windows **rendered from the recorded text of the commands that ran**, after removing private values. They are faithful renderings of terminal output, not photographs of a screen. The account number and the administrator's address are replaced by placeholders in every file and picture, and a scanner blocks any commit that contains them.
- A check passes only if the recorded output satisfies it. If a scenario crashes, it fails and says why.

The tests ran from a clean build, after `up.sh` created the whole fleet from nothing.

