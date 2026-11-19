---
id: appendix-design-02-eks
title: "Appendix C: Design 02 built and tested on Amazon EKS"
short: "C. Design 02 on EKS (built and tested)"
---
This appendix describes a working, scaled-down implementation of [Design 02: Operating CPU and GPU clusters as one compute platform](#design-multicluster). I built it with Terraform on Amazon EKS in one AWS account: a hub cluster and three worker clusters (two CPU, one GPU), one front door for batch jobs, fair sharing between three teams, GPU capacity that is bought only when a queued job needs it, and walls between teams. I tested every claim of the design that this scale can test, recorded the evidence, and removed it again with one command. Every command, output and pass or fail result in section 5 is copied from the recorded test run, not written from memory.

The order is the one a reader needs: first the context (the problem, who the actors are and who talks to whom), then the software I implemented, the EKS topology and instances, the architecture, how it is implemented, and then the scenarios I tested with the commands, the results, why the results can be trusted and why the system produces them.

**What "scaled down" means here.** The design has a hub, CPU clusters, large GPU training clusters with Capacity Blocks and EFA, and inference clusters in several regions, with hundreds of GPUs. This build has one hub, two CPU workers of four vCPUs each, and one GPU worker that can run up to two single-GPU nodes on-demand and two on Spot, all in one region. The mechanisms are the same ones: the same Kueue and MultiKueue, the same quota model, the same node-pool rules. Only the numbers are small, and section 6 lists what I did not build.

10 of 10 scenarios passed, 46 of 46 checks.

| Scenario | Title | Checks passed |
|---|---|---|
| S01 | The fleet: four clusters made from one definition | 6 of 6 |
| S02 | One front door, and the hub runs nothing | 6 of 6 |
| S03 | Why is my job pending? One answer per layer | 4 of 4 |
| S04 | Gang scheduling: all of a job, or none of it | 4 of 4 |
| S05 | Fair sharing: borrow idle capacity, give it back when asked | 4 of 4 |
| S06 | Dispatch: a full worker is skipped, and the job runs in the other | 2 of 2 |
| S07 | GPUs: only through the queue, from the right capacity, scaled from zero | 9 of 9 |
| S08 | Tenant isolation: network and identity | 4 of 4 |
| S09 | Failure drills: a worker lost, and the hub down | 5 of 5 |
| S10 | Observability: the fleet in one place | 2 of 2 |

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


### S01: The fleet: four clusters made from one definition

**Design reference:** Why several clusters, and how to cut them; The architecture at a glance.

**What this tests.** Show a hub and three workers created from one Terraform definition, on the same Kubernetes minor version, running the same pinned Kueue, with the hub connected to every worker and no GPU capacity running at rest.

Before testing behaviour I check that the fleet is what the design says: several clusters made from one definition, on one Kubernetes version, running one pinned queue controller, with the hub connected to every worker and no GPU capacity running when nobody asked for it.

![S01: The fleet: four clusters made from one definition](diagrams/appendix/a03-evidence/S01.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ for c in hub cpu-a cpu-b gpu: aws eks describe-cluster ...
ds02-fleet-hub	ACTIVE	1.37	manager
ds02-fleet-cpu-a	ACTIVE	1.37	cpu-worker
ds02-fleet-cpu-b	ACTIVE	1.37	cpu-worker
ds02-fleet-gpu	ACTIVE	1.37	gpu-worker

$ kubectl --context <each> -n kueue-system get deploy kueue-controller-manager -o jsonpath=<image>
hub    registry.k8s.io/kueue/kueue:v0.20.7
cpu-a  registry.k8s.io/kueue/kueue:v0.20.7
cpu-b  registry.k8s.io/kueue/kueue:v0.20.7
gpu    registry.k8s.io/kueue/kueue:v0.20.7

$ kubectl --context hub get multikueuecluster,multikueueconfig,admissioncheck
NAME                                     CONNECTED   AGE
multikueuecluster.kueue.x-k8s.io/cpu-a   True        62s
multikueuecluster.kueue.x-k8s.io/cpu-b   True        62s
multikueuecluster.kueue.x-k8s.io/gpu     True        62s

NAME                                          AGE
multikueueconfig.kueue.x-k8s.io/cpu-workers   61s
multikueueconfig.kueue.x-k8s.io/gpu-workers   61s

NAME                                         AGE
admissioncheck.kueue.x-k8s.io/dispatch-cpu   61s
admissioncheck.kueue.x-k8s.io/dispatch-gpu   61s

$ kubectl --context hub get clusterqueue   # a team's fleet-wide share: one queue per team and kind of capacity
NAME           COHORT      PENDING WORKLOADS
llm-cpu        cpu-fleet   0
llm-gpu        gpu-fleet   0
research-cpu   cpu-fleet   0
research-gpu   gpu-fleet   0
vision-cpu     cpu-fleet   0
vision-gpu     gpu-fleet   0

$ kubectl --context gpu get nodes -L ds02/pool,node.kubernetes.io/instance-type   # at rest
NAME                  STATUS   ROLES    AGE    VERSION               POOL   INSTANCE-TYPE
i-0bf02de64f7a3aaac   Ready    <none>   2m1s   v1.37.1-eks-577a51e          c6a.large

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | all four clusters are ACTIVE on the same Kubernetes version | One Terraform resource, written once, creates all four clusters, so they cannot differ in version. |
| PASS | the cluster role labels are manager, cpu-worker (x2) and gpu-worker | Each cluster carries the role it was created for. The platform layer reads it to decide what to install. |
| PASS | every cluster runs the same Kueue version | A single pinned chart version is installed on every cluster by the same Terraform module. |
| PASS | the hub is connected to all three workers | The hub holds one kubeconfig Secret per worker, each with a narrowly scoped identity, and MultiKueue reports each connection Active. |
| PASS | no GPU node is running at rest (GPU capacity is bought only when a queued job needs it) | GPU NodePools start empty. Nodes are created only for pods that Kueue has admitted. |
| PASS | the hub has no node pool for user work | The hub only has the platform's built-in pool. Team capacity lives in the workers. |

**Why you can trust this result.**

- Cluster state is read from the EKS API, the Kueue image from each cluster's own Deployment, and the connections from the hub's own MultiKueueCluster objects. None of it is read back from my configuration files.
- The 'no GPU at rest' check looks at the capacity every node reports, so a GPU node hidden under another name would still be counted.

**Why you get this output.**

- The four clusters match because they are one Terraform resource written once and repeated by a map. The role label on each comes from that map.
- Kueue is identical everywhere because one module installs it with one pinned chart version on every cluster.
- The hub reports each worker Active because Terraform gave every worker a narrow identity, stored that identity's kubeconfig in the hub, and created a MultiKueueCluster for it.
- No GPU node is running because the GPU NodePools start empty and their limits are set in the NodePool, so nothing is bought until an admitted job needs it.

### S02: One front door, and the hub runs nothing

**Design reference:** Sending work to the right cluster; The architecture at a glance.

**What this tests.** Submit a job once to the hub. It must run in a worker, report its status back to the hub, leave no pod on the hub, and be refused if someone tries to run a pod on the hub directly.

This scenario is the user's view of the platform: submit once to one place. The job has to run somewhere else, report back, and leave nothing running on the hub. I also try to cheat by running a pod on the hub directly.

![S02: One front door, and the hub runs nothing](diagrams/appendix/a03-evidence/S02.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl --context hub -n llm run rogue --image=python:3.12-slim   # a pod that passes every security check, run on the hub directly

The pods "rogue" is invalid: : ValidatingAdmissionPolicy 'hub-runs-no-user-pods' with binding 'hub-runs-no-user-pods' denied request: The hub cluster runs no user workloads. Submit a Job to your Loca…

$ kubectl --context hub create -f job.yaml   # llm/front-door: queue cpu, 2 pod(s) x cpu 1
job.batch/front-door created

$ kubectl --context hub -n llm get workload,job   # the hub's single view of the job
NAME                                           QUEUE   RESERVED IN   ADMITTED   FINISHED   AGE
workload.kueue.x-k8s.io/job-front-door-649bf   cpu     llm-cpu       True                  2s

NAME                   STATUS    COMPLETIONS   DURATION   AGE
job.batch/front-door   Running   0/2           2s         3s

$ kubectl --context hub -n llm get pods   # nothing runs on the hub

No resources found in llm namespace.

$ kubectl --context cpu-a -n llm get pods -o wide   # the pods are here
NAME               READY   STATUS    RESTARTS   AGE   IP       NODE     NOMINATED NODE   READINESS GATES
front-door-9lq7l   0/1     Pending   0          7s    <none>   <none>   <none>           <none>
front-door-p9snc   0/1     Pending   0          7s    <none>   <none>   <none>           <none>

$ kubectl --context hub -n llm get job front-door   # status mirrored back
NAME         STATUS     COMPLETIONS   DURATION   AGE
front-door   Complete   2/2           73s        79s

$ kubectl --context cpu-a -n llm get jobs   # the remote copy is cleaned up

No resources found in llm namespace.

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | a pod created directly on the hub is refused | A ValidatingAdmissionPolicy on the hub denies every pod in a team namespace. |
| PASS | the hub chose a worker for the job | The job's Workload on the hub records where it was sent: cpu-a. |
| PASS | no pod exists on the hub | The hub keeps a suspended copy of the job. Pods exist only in the worker that admitted it. |
| PASS | the job's pods are running in that worker | MultiKueue created the Job in the winning worker, linked to the Workload that worker admitted. |
| PASS | the job completed and its status is visible on the hub | The hub watches the remote job and copies its status. The user never needs worker credentials. |
| PASS | the remote copy was deleted when the job finished | After the final status sync the hub deletes the remote objects. |

**Why you can trust this result.**

- The worker's name comes from the hub's own Workload record, and I then look for the pods in exactly that worker.
- The 'no pod on the hub' check lists pods in the team's namespace on the hub. An empty list is a measured result, not an assumption.
- The cleanup check looks in the worker after the job finished, so a leftover copy would fail the test.

**Why you get this output.**

- The direct pod is refused because the hub carries a ValidatingAdmissionPolicy that denies every pod in a team namespace.
- The hub has no pods because MultiKueue keeps the job suspended there. Only the worker that admitted the Workload runs it.
- The status is visible on the hub because MultiKueue watches the remote job and copies its status back.
- The remote copy disappears because the hub deletes the objects it created in the worker after the final status sync.

### S03: Why is my job pending? One answer per layer

**Design reference:** Four layers of scheduling decisions.

**What this tests.** Create the two situations that make a job wait (no fleet quota at the hub, no room in any worker) and show that each is explained by a different layer, in one line.

A researcher asks why her job has been pending for an hour. The design's answer is that every wait has one owner. I create the two ways to wait: too big for the team's fleet-wide share, and within the fleet share but too big for any single worker, and read each reason from the system that holds the job.

![S03: Why is my job pending? One answer per layer](diagrams/appendix/a03-evidence/S03.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl --context hub create -f job.yaml   # vision/too-big-for-fleet: queue cpu, 7 pod(s) x cpu 1
job.batch/too-big-for-fleet created

$ kubectl --context hub create -f job.yaml   # llm/too-big-for-any-worker: queue cpu, 6 pod(s) x cpu 1
job.batch/too-big-for-any-worker created

$ scripts/fleet-status.py workloads   # every workload in the fleet, and the layer holding it
CLUSTER  NAMESPACE WORKLOAD                           STATE
hub      llm       job-too-big-for-any-worker-bd12c   LAYER 1 pending: quota reserved at the hub, waiting for a worker to admit. No worker cluster reserved quota: cpu-a (ExceedsMaxQuota): couldn't as…
hub      vision    job-too-big-for-fleet-4a048        LAYER 1 pending: no fleet quota. couldn't assign flavors to pod set main: insufficient quota for cpu in flavor fleet-cpu, previously considered
cpu-a    llm       job-too-big-for-any-worker-bd12c   LAYER 2 pending: couldn't assign flavors to pod set main: insufficient quota for cpu in flavor cpu-ondemand, previously considered podset
cpu-b    llm       job-too-big-for-any-worker-bd12c   LAYER 2 pending: couldn't assign flavors to pod set main: insufficient quota for cpu in flavor cpu-spot, previously considered podsets re

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the job larger than the team's fleet-wide quota waits at LAYER 1 (hub quota) | The hub's ClusterQueue for the team caps the team at its fleet share plus what it may borrow. |
| PASS | the job larger than any worker's queue waits at LAYER 1, with quota reserved and no worker admitting it | The hub's quota allows it, so it is offered to the workers, and none of them has a queue big enough. |
| PASS | the same job is waiting at LAYER 2 in the workers it was offered to | Each worker's own Kueue holds the copy and says why it cannot admit it. |
| PASS | neither job has created a single pod | Admission comes before placement. Pods do not exist for work that has no quota. |

**Why you can trust this result.**

- The table is produced by a script that reads the Workload objects of every cluster and prints the condition message each Kueue wrote. I do not write the reasons.
- The pod count is read from all four clusters. Zero is a measured value.

**Why you get this output.**

- The first job waits at the hub because the team's fleet-wide queue has quota for fewer CPUs than the job asks for.
- The second job gets a quota reservation at the hub (the team's fleet share is large enough) and is then offered to the workers. Each worker's queue can hold at most the team's share there plus what it may borrow, which is less than the job needs, so every copy waits at layer 2.
- No pods exist because Kueue keeps a job suspended until the whole job is admitted.

### S04: Gang scheduling: all of a job, or none of it

**Design reference:** Four layers of scheduling decisions (admission before placement).

**What this tests.** Submit a 4-pod job to a worker that can hold only 3 of the pods. The job must wait whole, with no pods created, instead of starting 3 and holding resources for the 4th.

Gang scheduling is the property that a distributed job starts all at once or not at all. Without it, a 4-pod job on a cluster with room for 3 starts 3 pods, which then hold resources and wait for the 4th. I test the property directly on one worker.

![S04: Gang scheduling: all of a job, or none of it](diagrams/appendix/a03-evidence/S04.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl --context cpu-a create -f job.yaml   # vision/gang-of-4: queue cpu, 4 pod(s) x cpu 1
job.batch/gang-of-4 created

$ kubectl --context cpu-a -n vision get workload,pods
NAME                                          QUEUE   RESERVED IN   ADMITTED   FINISHED   AGE
workload.kueue.x-k8s.io/job-gang-of-4-79bb3   cpu                   False                 18s

$ kubectl --context cpu-a -n vision get workload -o jsonpath=<the QuotaReserved message>
couldn't assign flavors to pod set main: insufficient quota for cpu in flavor cpu-ondemand, previously considered podsets requests (0) + current podset request (4) > maximum capacity (3)

$ kubectl --context cpu-a -n vision delete job gang-of-4
job.batch "gang-of-4" deleted from vision namespace

$ kubectl --context cpu-a create -f job.yaml   # vision/gang-of-3: queue cpu, 3 pod(s) x cpu 1
job.batch/gang-of-3 created

$ kubectl --context cpu-a -n vision get workload   # a 3-pod job fits and is admitted whole
NAME                  QUEUE   RESERVED IN   ADMITTED   FINISHED   AGE
job-gang-of-3-c3d21   cpu     vision-cpu    True                  3s

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the 4-pod job is not admitted | The vision queue on this worker can use 3 CPUs at most (1 of its own plus 2 borrowed), and the job needs 4. |
| PASS | the job's pod count is zero while it waits | Kueue keeps the job suspended. The Job controller creates no pods until the whole gang is admitted. |
| PASS | the reason names the missing quota | The Workload's condition is the one-line answer to 'why is it pending'. |
| PASS | a 3-pod job fits the same queue and is admitted | Same queue, same worker: the only difference is the size of the gang. |

**Measured in this run:** This test talks to the worker directly, so the result is the worker's own decision.

**Why you can trust this result.**

- The test talks to the worker's own Kueue, so the decision is that worker's and not the hub's.
- The pod list is read from the worker's API after the job has had time to start. If pods existed, the check would fail.
- A control job one pod smaller is admitted in the same queue, so a bug that blocks everything would show.

**Why you get this output.**

- The 4-pod job waits because the team's queue on this worker can use 3 CPUs at most: 1 of its own and 2 it may borrow.
- No pods are created because the Job is created suspended and Kueue unsuspends it only once all of its pods fit.
- The 3-pod job is admitted because 3 CPUs are within what the queue may use.

### S05: Fair sharing: borrow idle capacity, give it back when asked

**Design reference:** Queues, quotas and fair sharing.

**What this tests.** One team borrows what others are not using. When an owner comes back for its share, the borrower is preempted and requeued, and the owner runs.

Static per-team pools leave capacity idle. The design's answer is a cohort: each team has a guaranteed share, can borrow what others are not using, and gets it back when it asks. I run exactly that sequence: a borrower takes idle capacity, then the owner comes back.

![S05: Fair sharing: borrow idle capacity, give it back when asked](diagrams/appendix/a03-evidence/S05.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl --context cpu-a get clusterqueue   # llm 2, vision 1, research 1 CPU, one cohort
NAME           COHORT      PENDING WORKLOADS
llm-cpu        cpu-fleet   0
research-cpu   cpu-fleet   0
vision-cpu     cpu-fleet   0

$ kubectl --context cpu-a create -f job.yaml   # research/research-run: queue cpu, 3 pod(s) x cpu 1
job.batch/research-run created

$ scripts/fleet-status.py queues   # research uses 3 CPUs: its own 1 plus 2 borrowed
CLUSTER  QUEUE          PENDING ADMITTED  USAGE (flavor: resource used / nominal, borrowed)
hub      llm-cpu              0        0  fleet-cpu: cpu 0/4 (borrowed 0)
hub      llm-gpu              0        0  fleet-gpu: cpu 0/6 (borrowed 0); fleet-gpu: gpu 0/2 (borrowed 0)
hub      research-cpu         0        0  fleet-cpu: cpu 0/2 (borrowed 0)
hub      research-gpu         0        0  fleet-gpu: cpu 0/6 (borrowed 0); fleet-gpu: gpu 0/2 (borrowed 0)
hub      vision-cpu           0        0  fleet-cpu: cpu 0/2 (borrowed 0)
hub      vision-gpu           0        0  fleet-gpu: cpu 0/3 (borrowed 0); fleet-gpu: gpu 0/1 (borrowed 0)
cpu-a    llm-cpu              0        0  cpu-ondemand: cpu 0/2 (borrowed 0)
cpu-a    research-cpu         0        1  cpu-ondemand: cpu 3/1 (borrowed 2)
cpu-a    vision-cpu           0        0  cpu-ondemand: cpu 0/1 (borrowed 0)
cpu-b    llm-cpu              0        0  cpu-spot: cpu 0/2 (borrowed 0)
cpu-b    research-cpu         0        0  cpu-spot: cpu 0/1 (borrowed 0)
cpu-b    vision-cpu           0        0  cpu-spot: cpu 0/1 (borrowed 0)
gpu      llm-gpu              0        0  gpu-ondemand: cpu 0/3 (borrowed 0); gpu-ondemand: gpu 0/1 (borrowed 0); gpu-spot: cpu 0/3 (borrowed 0); gpu-spot: gpu 0/1 (borrowed 0)
gpu      research-gpu         0        0  gpu-spot: cpu 0/6 (borrowed 0); gpu-spot: gpu 0/2 (borrowed 0)
gpu      vision-gpu           0        0  gpu-ondemand: cpu 0/3 (borrowed 0); gpu-ondemand: gpu 0/1 (borrowed 0)

$ kubectl --context cpu-a create -f job.yaml   # llm/llm-owner: queue cpu, 2 pod(s) x cpu 1
job.batch/llm-owner created

$ scripts/fleet-status.py workloads   # the owner is admitted; the borrower has been evicted and requeued
CLUSTER  NAMESPACE WORKLOAD                           STATE
cpu-a    llm       job-llm-owner-79e8f                running (admitted)
cpu-a    research  job-research-run-095b9             LAYER 2 pending: couldn't assign flavors to pod set main: insufficient unused quota for cpu in flavor cpu-ondemand, 1 more needed

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | research was admitted with 3 CPUs although its own share is 1 | The cohort lets a team borrow what others lend, up to the borrowing limit. Two CPUs came from llm and vision. |
| PASS | llm, asking for its own share, was admitted | llm's share is guaranteed, so Kueue reclaims it from whoever is borrowing it. |
| PASS | research's borrowing workload was preempted | reclaimWithinCohort is Any, so a queue within its nominal quota may preempt workloads that are borrowing from the cohort. |
| PASS | the preempted job was requeued, not lost | Preempted Workloads go back to the queue and run again when capacity returns. |

**Measured in this run:** Kueue's own words for the preemption: 'Preempted to accommodate a workload (UID: a0f1cde0-d7ac-4eec-ae90-1cedd84d911b, JobUID: 141d7f52-642a-4b6c-b248-992cd7bb5511) due to reclamation within the cohort; preemptor path: /cpu-fleet/llm-cpu; '

**Why you can trust this result.**

- Usage and borrowed amounts are read from each ClusterQueue's own status.
- The preemption is read from the borrower's Workload conditions, and the message is Kueue's own.
- The workload still exists after preemption, so the check for 'requeued, not lost' is not vacuous.

**Why you get this output.**

- Research is admitted with 3 CPUs because the cohort lets it borrow up to its borrowing limit, from queues that lend. llm lends one CPU and vision lends one.
- llm is admitted because it is asking for its own guaranteed share and the queue allows preemption of borrowers (`reclaimWithinCohort: Any`).
- Research's job is preempted and requeued because it was running on borrowed quota, which is the first thing Kueue takes back.

### S06: Dispatch: a full worker is skipped, and the job runs in the other

**Design reference:** Sending work to the right cluster.

**What this tests.** Fill one CPU worker for a team, then submit that team's job to the hub. The hub offers it to both workers and the one with room must win.

The hub offers a job to several workers and the first to admit wins. I make one worker unable to take a team's job and show that the other one does.

![S06: Dispatch: a full worker is skipped, and the job runs in the other](diagrams/appendix/a03-evidence/S06.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl --context cpu-a create -f job.yaml   # llm/fill-cpu-a: queue cpu, 4 pod(s) x cpu 1
job.batch/fill-cpu-a created

$ scripts/fleet-status.py queues   # cpu-a's llm queue is full (4 of 4 CPUs)
CLUSTER  QUEUE          PENDING ADMITTED  USAGE (flavor: resource used / nominal, borrowed)
hub      llm-cpu              0        0  fleet-cpu: cpu 0/4 (borrowed 0)
hub      llm-gpu              0        0  fleet-gpu: cpu 0/6 (borrowed 0); fleet-gpu: gpu 0/2 (borrowed 0)
hub      research-cpu         0        0  fleet-cpu: cpu 0/2 (borrowed 0)
hub      research-gpu         0        0  fleet-gpu: cpu 0/6 (borrowed 0); fleet-gpu: gpu 0/2 (borrowed 0)
hub      vision-cpu           0        0  fleet-cpu: cpu 0/2 (borrowed 0)
hub      vision-gpu           0        0  fleet-gpu: cpu 0/3 (borrowed 0); fleet-gpu: gpu 0/1 (borrowed 0)
cpu-a    llm-cpu              0        1  cpu-ondemand: cpu 4/2 (borrowed 2)
cpu-a    research-cpu         0        0  cpu-ondemand: cpu 0/1 (borrowed 0)
cpu-a    vision-cpu           0        0  cpu-ondemand: cpu 0/1 (borrowed 0)
cpu-b    llm-cpu              0        0  cpu-spot: cpu 0/2 (borrowed 0)
cpu-b    research-cpu         0        0  cpu-spot: cpu 0/1 (borrowed 0)
cpu-b    vision-cpu           0        0  cpu-spot: cpu 0/1 (borrowed 0)
gpu      llm-gpu              0        0  gpu-ondemand: cpu 0/3 (borrowed 0); gpu-ondemand: gpu 0/1 (borrowed 0); gpu-spot: cpu 0/3 (borrowed 0); gpu-spot: gpu 0/1 (borrowed 0)
gpu      research-gpu         0        0  gpu-spot: cpu 0/6 (borrowed 0); gpu-spot: gpu 0/2 (borrowed 0)
gpu      vision-gpu           0        0  gpu-ondemand: cpu 0/3 (borrowed 0); gpu-ondemand: gpu 0/1 (borrowed 0)

$ kubectl --context hub create -f job.yaml   # llm/needs-3: queue cpu, 3 pod(s) x cpu 1
job.batch/needs-3 created

$ kubectl --context hub -n llm get workload -o custom-columns=<name, chosen cluster, nominated clusters>
NAME                CLUSTER   NOMINATED
job-needs-3-e9a38   cpu-b     <none>

$ kubectl --context cpu-a -n llm get workloads   # only the job that fills it; no copy of needs-3
NAME                   QUEUE   RESERVED IN   ADMITTED   FINISHED   AGE
job-fill-cpu-a-067eb   cpu     llm-cpu       True                  20s

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the job was sent to cpu-b, the worker with room | Both workers were offered the job. cpu-a's llm queue had no room, so only cpu-b could admit it (chosen: cpu-b). |
| PASS | the full worker holds no copy of the job any more | As soon as one worker admits, the hub deletes the copies in the others. |

**Why you can trust this result.**

- The setup is verified before the test: the usage table shows the first worker's queue full.
- The chosen cluster is read from the hub's Workload. The cleanup check reads the other worker's Workloads.

**Why you get this output.**

- The job goes to cpu-b because the hub copied it to both CPU workers and cpu-a's queue for that team was full, so only cpu-b could admit it.
- cpu-a holds no copy afterwards because the hub deletes the copies in the workers that did not win.

### S07: GPUs: only through the queue, from the right capacity, scaled from zero

**Design reference:** Inside a GPU cluster; Autoscaling: where capacity comes from; Multi-tenant isolation (side doors).

**What this tests.** Run GPU jobs through the hub, check which kind of capacity each team's job got, check that the GPU nodes appear only when needed and disappear when idle, and try to get a GPU without going through the queue.

GPUs are the scarce, expensive resource, so the design controls them four ways: through queues, by flavor, by scaling from zero, and by closing every side door. This scenario runs real GPU jobs and then tries to get a GPU without permission.

![S07: GPUs: only through the queue, from the right capacity, scaled from zero](diagrams/appendix/a03-evidence/S07.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl --context gpu get nodes -L ds02/pool   # before: no GPU node
NAME                  STATUS   ROLES    AGE   VERSION               POOL
i-0bf02de64f7a3aaac   Ready    <none>   12m   v1.37.1-eks-577a51e   

$ aws ec2 describe-instances <GPU instances of the GPU worker, running>   # the same fact, from EC2
GPU instances running: 0

$ kubectl --context hub create -f job.yaml   # llm/llm-train-a: queue gpu, 1 pod(s) x cpu 2, gpu 1
job.batch/llm-train-a created

$ kubectl --context hub create -f job.yaml   # llm/llm-train-b: queue gpu, 1 pod(s) x cpu 2, gpu 1
job.batch/llm-train-b created

$ kubectl --context hub create -f job.yaml   # research/research-train: queue gpu, 1 pod(s) x cpu 2, gpu 1
job.batch/research-train created

$ kubectl --context gpu get workloads -A -o <flavor each job was given>
llm-train-a      -> gpu-ondemand
llm-train-b      -> gpu-spot
research-train   -> gpu-spot

$ kubectl --context gpu get nodes -L ds02/pool,node.kubernetes.io/instance-type   # GPU nodes created for the admitted jobs
NAME                  STATUS   ROLES    AGE   VERSION               POOL           INSTANCE-TYPE
i-001871d8b881e6fa1   Ready    <none>   13s   v1.37.1-eks-577a51e   gpu-ondemand   g4dn.xlarge
i-0bf02de64f7a3aaac   Ready    <none>   16m   v1.37.1-eks-577a51e                  c6a.large

$ kubectl --context gpu -n llm logs -l job-name=llm-train-a   # what the job saw
GPU 0: Tesla T4 (UUID: GPU-3532a20f-4653-9fd0-c1f0-07830fb1f1e1)

$ kubectl --context gpu create -f pod.yaml   # a bare pod asking for a GPU in a team namespace, with no queue label
pod/bypass-queue created

$ kubectl --context gpu -n llm get pod bypass-queue
NAME           READY   STATUS            RESTARTS   AGE
bypass-queue   0/1     SchedulingGated   0          22s

$ kubectl --context gpu -n default create -f pod.yaml   # the same, in a namespace Kueue does not manage
pod/bypass-namespace created

$ kubectl --context gpu -n default get pod bypass-namespace
NAME               READY   STATUS    RESTARTS   AGE
bypass-namespace   0/1     Pending   0          61s

$ kubectl --context gpu -n default get events   # why it is not scheduled
0/2 nodes are available: 2 Insufficient nvidia.com/gpu. preemption: 0/2 nodes are available: 1 No preemption victims found for incoming pod, 1 Preemption is not helpful for scheduling.
Pod should schedule on: nodeclaim/gpu-ondemand-mbr2z
NodeClaim gpu-ondemand-mbr2z nominated for this pod failed to launch: creating nodeclaim, creating instance, insufficient capacity, with fleet error(s), InsufficientInstanceCapacity: We currently do …

$ kubectl --context gpu get nodes -L ds02/pool   # after the jobs ended
NAME                  STATUS   ROLES    AGE     VERSION               POOL
i-001871d8b881e6fa1   Ready    <none>   2m46s   v1.37.1-eks-577a51e   gpu-ondemand
i-0bf02de64f7a3aaac   Ready    <none>   19m     v1.37.1-eks-577a51e   

$ aws ec2 describe-instances <GPU instances of the GPU worker, running>   # after the jobs ended
GPU instances running: 0

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | there is no GPU instance before any job is submitted | GPU NodePools scale from zero. |
| PASS | the first llm job got on-demand GPU capacity (the first flavor in its queue) | A job tries the flavors of its queue in order and takes the first with room. |
| PASS | the second llm job, with the on-demand share used up, got Spot | llm owns one on-demand and one Spot GPU. The second job falls through to the next flavor. |
| PASS | the research job got Spot, the only flavor its queue lists | Research's queue does not include on-demand, so an experiment can never buy it. |
| PASS | GPU nodes were created, from a GPU instance family the pool allows, in the pools that match the flavors | The flavor's node label selects the NodePool, so quota and capacity line up. |
| PASS | the job saw a real GPU | The device plugin gave the pod one GPU, and nvidia-smi inside it names the device. |
| PASS | a GPU pod with no queue label is held by a scheduling gate and never runs | Kueue manages every pod in a team namespace. Without a queue it adds a scheduling gate that nothing removes. |
| PASS | a GPU pod outside the queues stays Pending, and no node was bought for it | GPU nodes are tainted. The pod has no toleration, and the built-in pool has no GPU instance types, so nothing can satisfy it. |
| PASS | every GPU instance was terminated after the jobs ended (scale to zero) | The NodePool removes empty nodes two minutes after the last pod leaves, and Auto Mode terminates the instance. |

**Measured in this run:** Waiting for the GPU nodes to be removed once they are empty.

**Why you can trust this result.**

- The flavor each job received is read from the Workload that admitted it in the GPU worker.
- The jobs print `nvidia-smi -L` from inside the pod, so the GPU is shown by the driver, not assumed from a resource request.
- The side-door tests create the forbidden pods for real and read their phase, their scheduling gate and the node list before and after.
- The final check waits for the GPU nodes to disappear. It fails if they do not.

**Why you get this output.**

- The first llm job gets on-demand capacity and the second gets Spot because llm's queue lists flavors in that order and owns one of each.
- Research gets Spot because its queue does not list the on-demand flavor. That is the 'weekend experiment cannot buy on-demand' rule.
- Which GPU model a job gets depends on which allowed instance family had capacity when the node was bought, so the test checks for a GPU and does not hard-code one. In my first attempt the `g6` family had no capacity and Auto Mode launched a `g4dn` node.
- GPU nodes appear only after a job is admitted because the NodePools start empty and only admitted pods carry the toleration the flavor adds.
- A bare GPU pod in a team namespace is held because Kueue manages every pod in a team namespace and gates the ones with no queue. In an unmanaged namespace it stays Pending because no pool can satisfy it: GPU nodes are tainted and the platform pool has no GPU instance types.
- The nodes disappear because the NodePool removes empty nodes after two minutes.

### S08: Tenant isolation: network and identity

**Design reference:** Multi-tenant isolation.

**What this tests.** Show that one team's pods cannot reach another team's pods, and that one team's job cannot read another team's data, while each can use its own.

Teams share clusters, so one team must not be able to see another's pods or read another's data. I test the network wall and the identity wall, each with a positive control so a block cannot be mistaken for a broken network.

![S08: Tenant isolation: network and identity](diagrams/appendix/a03-evidence/S08.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl --context cpu-a create -f job.yaml   # vision/vision-server: queue cpu, 1 pod(s) x cpu 1
job.batch/vision-server created

$ kubectl --context cpu-a create -f job.yaml   # llm/llm-probe: queue cpu, 1 pod(s) x cpu 1
job.batch/llm-probe created

$ kubectl --context cpu-a create -f job.yaml   # vision/vision-probe: queue cpu, 1 pod(s) x cpu 1
job.batch/vision-probe created

$ kubectl --context cpu-a -n llm logs job/llm-probe   # from team llm to team vision's server
blocked: URLError

$ kubectl --context cpu-a -n vision logs job/vision-probe   # from team vision to its own server
reachable: 200

$ kubectl --context cpu-a create -f job.yaml   # llm/llm-own-data: queue cpu, 1 pod(s) x cpu 1
job.batch/llm-own-data created

$ kubectl --context cpu-a create -f job.yaml   # llm/llm-other-data: queue cpu, 1 pod(s) x cpu 1
job.batch/llm-other-data created

$ kubectl --context cpu-a -n llm logs job/llm-own-data   # aws s3 cp s3://<bucket>/llm/dataset.txt -
dataset owned by team llm

$ kubectl --context cpu-a -n llm logs job/llm-other-data   # aws s3 cp s3://<bucket>/vision/dataset.txt -
download failed: s3://ds02-fleet-data-b8ecd20d/vision/dataset.txt to - An error occurred (403) when calling the HeadObject operation: Forbidden

$ kubectl --context cpu-a -n llm get resourcequota,networkpolicy
NAME                        REQUEST                              LIMIT   AGE
resourcequota/team-limits   count/jobs.batch: 3/30, pods: 0/20           22m

NAME                                           POD-SELECTOR   AGE
networkpolicy.networking.k8s.io/isolate-team   <none>         22m

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | a pod in team llm cannot reach team vision's server | Each team namespace has a policy that admits traffic only from its own namespace. The probe's own output says it was blocked. |
| PASS | a pod in team vision can reach its own team's server | The same policy allows pods in the same namespace. The block is deliberate, not a broken network. |
| PASS | a team's job reads its own S3 prefix | EKS Pod Identity gave the job's ServiceAccount a role limited to that team's prefix. |
| PASS | the same job is refused another team's prefix | The role's policy names only the team's own prefix. The refusal comes from IAM, not from the cluster. |

**Measured in this run:** A server runs in team vision's namespace on cpu-a at 10.30.28.97.

**Why you can trust this result.**

- Each probe is a real job that runs in a team namespace and reports what it got. The server runs in another team's namespace on the same worker.
- The same-namespace probe is the positive control. If the network were simply down it would fail too.
- The identity test uses the AWS CLI inside the job, so the answer (data or AccessDenied) is IAM's own.

**Why you get this output.**

- Cross-team traffic is dropped because each team namespace has a policy that admits ingress only from its own namespace.
- Same-namespace traffic works because that policy allows it.
- A team's job reads its own prefix because EKS Pod Identity associates the team's ServiceAccount with a role whose policy lists only that prefix. Another team's prefix is refused by IAM.

### S09: Failure drills: a worker lost, and the hub down

**Design reference:** Failure modes and operations.

**What this tests.** Cut the hub off from one worker, and check that new jobs still run. Take the hub's queue controller down, and check that a running job is not affected.

The design's failure table says how the fleet behaves when a worker or the hub is lost. I cut the hub off from one worker, and I take the hub's queue controller away.

![S09: Failure drills: a worker lost, and the hub down](diagrams/appendix/a03-evidence/S09.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl --context hub -n kueue-system patch secret cpu-a-kubeconfig   # simulate losing cpu-a: its credentials stop working
secret/cpu-a-kubeconfig patched

$ kubectl --context hub get multikueuecluster
NAME    CONNECTED   AGE
cpu-a   False       22m
cpu-b   True        22m
gpu     True        22m

$ kubectl --context hub create -f job.yaml   # llm/during-outage: queue cpu, 1 pod(s) x cpu 1
job.batch/during-outage created

$ kubectl --context hub -n kueue-system patch secret cpu-a-kubeconfig   # restore the credentials
secret/cpu-a-kubeconfig patched

$ kubectl --context hub create -f job.yaml   # llm/long-run: queue cpu, 1 pod(s) x cpu 1
job.batch/long-run created

$ kubectl --context hub -n kueue-system scale deploy/kueue-controller-manager --replicas=0
deployment.apps/kueue-controller-manager scaled

$ kubectl --context hub create -f job.yaml   # a new submission while the hub's Kueue is down

Error from server (InternalError): error when creating "STDIN": Internal error occurred: failed calling webhook "mjob.kb.io": failed to call webhook: Post "https://kueue-webhook-service.kueue-system.…

$ kubectl --context cpu-a -n llm get pods   # the running job, hub still down
NAME             READY   STATUS    RESTARTS   AGE
long-run-bpvnl   1/1     Running   0          30s

$ kubectl --context hub -n kueue-system scale deploy/kueue-controller-manager --replicas=1
deployment.apps/kueue-controller-manager scaled

$ kubectl --context hub -n kueue-system rollout status deploy/kueue-controller-manager --timeout=180s
Waiting for deployment "kueue-controller-manager" rollout to finish: 0 of 1 updated replicas are available...
deployment "kueue-controller-manager" successfully rolled out

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the hub marks cpu-a inactive | MultiKueue checks every worker connection and reports it on the MultiKueueCluster. |
| PASS | a new job still runs, in the other CPU worker | The hub offers work only to workers that are active. It chose cpu-b. |
| PASS | cpu-a rejoins when its credentials return | No manual step is needed: the connection is retried. |
| PASS | the job already running in the worker is not affected by the hub being down | Once dispatched, a job runs entirely inside its worker. The hub is not in the data path. |
| PASS | a new submission is refused while the hub's queue controller is down | Kueue's admission webhook is part of the controller. Only new batch submissions are affected; nothing already running is. |

**Why you can trust this result.**

- A worker is 'lost' by breaking the credentials the hub holds for it, the real way a worker becomes unreachable to the hub. The original value is restored afterwards.
- The job during the outage is dispatched for real, and the cluster it ran in is read from the Workload.
- The job that survives the hub outage is observed in the worker while the hub's controller has zero replicas.

**Why you get this output.**

- The hub marks the worker inactive because MultiKueue tests each connection and records the result on the MultiKueueCluster.
- New work goes to the other worker because the hub offers jobs only to active workers.
- A running job is unaffected by the hub being down because, once dispatched, it runs entirely inside the worker. The hub is not in the data path.

### S10: Observability: the fleet in one place

**Design reference:** Observability, cost and chargeback.

**What this tests.** Run a few jobs for different teams and show the fleet-wide view: where each job went, how much of each queue is used and borrowed.

Everyone claims to be GPU-starved and finance says utilization is low. The design needs one place to see both. I run a few jobs and read the fleet-wide view.

![S10: Observability: the fleet in one place](diagrams/appendix/a03-evidence/S10.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl --context hub create -f job.yaml   # llm/mixed-llm: queue cpu, 2 pod(s) x cpu 1
job.batch/mixed-llm created

$ scripts/fleet-status.py workloads
CLUSTER  NAMESPACE WORKLOAD                           STATE
hub      llm       job-mixed-llm-75307                running (admitted in cpu-a)
hub      research  job-mixed-research-9d9bc           running (admitted in cpu-b)
hub      vision    job-mixed-vision-82f54             running (admitted in cpu-a)
cpu-a    llm       job-mixed-llm-75307                running (admitted)
cpu-a    vision    job-mixed-vision-82f54             running (admitted)
cpu-b    research  job-mixed-research-9d9bc           running (admitted)

$ scripts/fleet-status.py queues
CLUSTER  QUEUE          PENDING ADMITTED  USAGE (flavor: resource used / nominal, borrowed)
hub      llm-cpu              0        1  fleet-cpu: cpu 2/4 (borrowed 0)
hub      llm-gpu              0        0  fleet-gpu: cpu 0/6 (borrowed 0); fleet-gpu: gpu 0/2 (borrowed 0)
hub      research-cpu         0        1  fleet-cpu: cpu 2/2 (borrowed 0)
hub      research-gpu         0        0  fleet-gpu: cpu 0/6 (borrowed 0); fleet-gpu: gpu 0/2 (borrowed 0)
hub      vision-cpu           0        1  fleet-cpu: cpu 1/2 (borrowed 0)
hub      vision-gpu           0        0  fleet-gpu: cpu 0/3 (borrowed 0); fleet-gpu: gpu 0/1 (borrowed 0)
cpu-a    llm-cpu              0        1  cpu-ondemand: cpu 2/2 (borrowed 0)
cpu-a    research-cpu         0        0  cpu-ondemand: cpu 0/1 (borrowed 0)
cpu-a    vision-cpu           0        1  cpu-ondemand: cpu 1/1 (borrowed 0)
cpu-b    llm-cpu              0        0  cpu-spot: cpu 0/2 (borrowed 0)
cpu-b    research-cpu         0        1  cpu-spot: cpu 2/1 (borrowed 1)
cpu-b    vision-cpu           0        0  cpu-spot: cpu 0/1 (borrowed 0)
gpu      llm-gpu              0        0  gpu-ondemand: cpu 0/3 (borrowed 0); gpu-ondemand: gpu 0/1 (borrowed 0); gpu-spot: cpu 0/3 (borrowed 0); gpu-spot: gpu 0/1 (borrowed 0)
gpu      research-gpu         0        0  gpu-spot: cpu 0/6 (borrowed 0); gpu-spot: gpu 0/2 (borrowed 0)
gpu      vision-gpu           0        0  gpu-ondemand: cpu 0/3 (borrowed 0); gpu-ondemand: gpu 0/1 (borrowed 0)

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | every job is listed with the cluster it runs in | The hub's Workload records the chosen cluster, so one query answers 'where did it go'. |
| PASS | every queue in the fleet shows pending, admitted and usage | Each ClusterQueue reports its own usage and borrowed amounts. |

**Why you can trust this result.**

- The tables are produced from each Kueue's own objects by a script in the repository, and the output is shown whole.

**Why you get this output.**

- Every job shows its worker because the hub's Workload records the chosen cluster.
- Every queue shows its pending count, admitted count and borrowed amount because each ClusterQueue keeps those in its status.

### 6. What I did not build, and why

**Scenario.** A portfolio piece is only credible if it says where it stops. These are the parts of Design 02 that this implementation does not cover, with the reason for each.

| Not built | Why |
|---|---|
| **More than one region** | The design spans us-east-1 and eu-west-1. The mechanism (a MultiKueueConfig per region, with data locality chosen through the queue a team submits to) is the same as the one tested here, but a second region would double the networking and cost without testing anything new. |
| **Hundreds of GPUs, Capacity Blocks, EFA, NVLink and topology-aware scheduling** | I have no reservation to use, and two single-GPU nodes have no network topology to schedule across. The flavor order, limits and scale-from-zero rules are tested; the topology rules are not. |
| **The Incremental dispatcher** | It matters when one queue can send a job to several GPU workers in preference order. I have one GPU worker, so the default dispatcher (offer to all, first to admit wins) is the one tested. |
| **Dynamic Resource Allocation (DRA) and MIG slices** | The NVIDIA DRA driver is not supported by EKS Auto Mode in the documentation I checked, and the design itself keeps the device plugin where DRA is not needed. Jobs here request whole GPUs with `nvidia.com/gpu`. |
| **Argo CD and ApplicationSets** | The design uses GitOps to install the same add-ons on every cluster by role. Terraform plays that role here: one module, called for each cluster, with the team table as the single source of truth. The consequence is the same (no drift); the tool differs. |
| **Dedicated nodes or a dedicated cluster for a regulated tenant** | The shared tier is built and tested. The other two tiers are more node pools and more clusters of the same kind. |
| **Inference clusters and HPA or KEDA scaling** | Design 02 is the batch and training platform. Inference is the subject of Design 03. |
| **Kubecost, DCGM metrics and a metrics store** | The fleet status script reads the same Kueue objects the dashboards would. Per-team cost and GPU utilization need a metrics pipeline that I did not build. |
| **Checkpointing** | The design says preempted jobs must checkpoint. The test jobs are `sleep` and `nvidia-smi`, so there is nothing to checkpoint; the requeue itself is what is tested. |

### 7. Run it yourself, and take it down

**Scenario.** The point of Terraform is that anyone can rebuild this from the repository and remove it again. These are the exact steps.

**What you need on your machine.** Terraform 1.9 or later, the AWS CLI v2 signed in to the account you want to use, Helm 3, and Python 3 with Pillow for the screenshots. Nothing is installed system-wide: `scripts/setup-tools.sh` downloads a matching `kubectl` into the project folder with a checksum check. A GPU test needs a quota of at least four vCPUs for G instances.

**Build.**

```text
cd implementations/ds02-multicluster-compute
export AWS_PROFILE=<your profile>
./scripts/up.sh
```

`up.sh` writes two small local files with your account ID and your public address (both git-ignored), builds the four clusters and the platform, and prints the state of the MultiKueue connections. It takes roughly 25 to 30 minutes.

**Test.**

```text
python3 tests/run_scenarios.py all
```

**Take it down.**

```text
./scripts/down.sh
```

`down.sh` deletes running jobs, destroys the platform layer, destroys the fleet layer, and then runs `verify-clean.sh`, which looks for anything tagged with this project: clusters, instances, NAT gateways, addresses, VPCs, security groups, volumes, network interfaces, buckets, roles and log groups. It prints `CLEAN` when nothing is left. The Resource Groups tagging index can lag deletions by up to an hour, so it is shown for information only.

**Keeping it safe in a public repository.** Terraform state, plans, kubeconfig files, local variable files and raw evidence are git-ignored. `scripts/check-no-secrets.sh` runs before every commit and blocks any file containing a credential pattern, the account ID or the administrator's address. The test harness redacts those values before anything is written.

### 8. Screenshots of the AWS Console

The terminal screenshots above are rendered from recorded command output. I could not capture the AWS Console from here. If you want console pictures in your portfolio, these pages show the same facts. Crop out the account number and your user name in the top bar before publishing.

- EKS, Clusters: the four `ds02-fleet-*` clusters with the same Kubernetes version.
- EKS, a worker's Compute tab: the node pools, and during S07 the `gpu-ondemand` and `gpu-spot` nodes.
- EC2, Instances: a GPU node while a job runs (`g4dn.xlarge` in my final run).
- S3: the bucket with its three team prefixes.
- IAM, Roles: one team role and its policy.
- VPC: the VPC with its two private subnets and the NAT gateway.
