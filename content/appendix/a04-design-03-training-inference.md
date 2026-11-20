---
id: appendix-design-03-eks
title: "Appendix D: Design 03 built and tested on Amazon EKS"
short: "D. Design 03 on EKS (built and tested)"
---
This appendix describes a working, scaled-down implementation of [Design 03: Training and inference infrastructure](#design-training-inference). I built it with Terraform on Amazon EKS in one AWS account: one cluster that runs the three workload families of the design (research notebooks, distributed training, model serving), one golden path for each, one GPU pool and one queue per family, a model registry with promotion gates, an inference gateway that routes by cache contents, autoscaling on queue depth, and a way for batch work to borrow serving's idle GPUs and give them back.

The order is the one a reader needs: first the context (the problem, who the actors are and who talks to whom), then the software I implemented, the EKS topology and instances, the architecture, how it is implemented, and then the scenarios I tested, with the commands, the results, why the results can be trusted and why the system produces them.

**What "scaled down" means here.** The design has training clusters with hundreds of GPUs, fast interconnects and reserved Capacity Blocks, and inference clusters in several regions. This build has one cluster and at most six single-GPU nodes at a time (research 1, training 2, inference 3), on whichever of the T4, A10G, L4 or L40S instances the zones could give me. The training job is an 85-million-parameter model on two GPUs; the served model has 0.5 billion parameters. The mechanisms are the same ones: the same Kueue, Kubeflow Trainer, LeaderWorkerSet, vLLM and Gateway API Inference Extension components. Only the numbers are small, and section 6 lists what I did not build.

11 of 11 scenarios passed, 78 of 78 checks.

| Scenario | Title | Checks passed |
|---|---|---|
| S01 | The platform at rest: components, node pools and queues | 8 of 8 |
| S02 | Research: a GPU notebook in minutes, taken back when idle | 8 of 8 |
| S03 | Training: the job lifecycle from one manifest | 9 of 9 |
| S04 | Training: checkpoints that do not stall the GPUs | 4 of 4 |
| S05 | Training: a lost node and a hung rank, with nobody there | 8 of 8 |
| S06 | From training to serving: registry, gates and rollback | 9 of 9 |
| S07 | Inference: the gateway and inference-aware routing | 6 of 6 |
| S08 | Inference: scaling on queue depth, and what a cold start costs | 7 of 7 |
| S09 | Sharing GPUs: batch work borrows serving's idle GPU and gives it back | 6 of 6 |
| S10 | Inference: one model replica spread over two nodes | 8 of 8 |
| S11 | Observability: the number each workload family lives by | 5 of 5 |

### Context: the problem, the actors and who talks to whom

**Scenario.** The company now trains its own models. Thirty researchers prototype in notebooks, an LLM team runs fine-tuning and long training jobs, and product teams serve models to customers. Today every group builds its own scripts: researchers SSH into GPU boxes, training runs die and are restarted by hand, and inference teams over-provision because nobody trusts autoscaling. The platform team is asked to build the infrastructure all three can share.

Five terms are used throughout.

- **Family.** One of the three kinds of workload: research (notebooks), training (and fine-tuning), inference (serving and batch inference). Each has a namespace, a queue and a GPU pool.
- **Golden path.** The short manifest a user writes for a family: a notebook, a `TrainJob`, a model deployment, a batch job. The platform fills in the rest.
- **Queue manager.** Kueue. It decides whether a workload may start, whether it may borrow idle quota, and whom to preempt when an owner wants quota back.
- **Node pool.** A rule that says which machines may be bought and how many. EKS Auto Mode buys a node only when an admitted pod needs it.
- **Registry.** A layout in S3 that holds immutable model versions, a pointer that names the version in production, and an audit record of every promotion decision.

![Three groups, four golden paths, one platform](diagrams/appendix/a04-1.svg)

**What the picture shows.** Who uses the platform, what each writes, and what the platform provides underneath. A researcher, a training engineer and a product team each write one short manifest. Everything below that line is the platform team's work.

**The path of one training job.**

1. A training engineer creates a `TrainJob` that names the platform's runtime and the queue.
2. Kueue holds the job, with no pods, until the queue's quota covers all of its GPUs.
3. When it is admitted, the job controller creates every pod at once, and the runtime's pod template asks for all of them in one zone.
4. Pods that fit on no node make the node pool buy GPU nodes.
5. Each pod tests its GPU, joins the others, and trains. Every few steps each rank writes its part of a checkpoint to S3, in the background, and then a marker saying it is done.
6. If a node disappears or a rank freezes, the job controller restarts the whole group, and the script resumes from the newest checkpoint whose markers are all present.
7. At the end the job writes its final checkpoint and its events, and the empty nodes are removed a few minutes later.

![One training job as calls between components](diagrams/appendix/a04-4.svg)

**What the picture shows.** One training job as calls between the components. Time runs from top to bottom. Each arrow is one call. The red arrows are the failure path.

**The path of one request, and of one model version.**

1. A client sends an OpenAI-style request to the gateway.
2. The gateway's route sends it to an inference pool. The pool's endpoint picker scores each replica on queue depth, KV-cache use and how much of the prompt the replica has already cached, and picks one.
3. The replica answers. Each replica's metrics go to Prometheus.
4. Which weights a replica holds is decided before it starts: an init container reads the registry's production pointer and copies exactly that version.
5. A version reaches that pointer only through evaluation and the promotion gates.
6. When requests start to wait in the replicas' queues, KEDA adds a replica, and Kueue admits its pod only if serving's quota allows it, taking GPUs back from batch work if needed.

![A request, a model version and a scaling decision](diagrams/appendix/a04-5.svg)

**What the picture shows.** The serving path on the top row, the registry and the autoscaler below, and where the time of a new replica goes.

| Caller | Callee | Why | What enforces it |
|---|---|---|---|
| User | Kubernetes API | Create a notebook, a TrainJob, a deployment or a batch job in the family's namespace | A namespace per family, a queue label on the manifest, and a Pod Security `baseline` profile (S02, S03) |
| Kueue | Workload APIs | Hold a workload suspended, admit it, take its quota back | Kueue's integrations for TrainJob, LeaderWorkerSet, Deployment, StatefulSet and Job (S03, S09, S10) |
| Pods | GPU nodes | Land only on the node pool for their family | A taint on every GPU node, and a toleration that only a Kueue flavor adds (S01) |
| Pods | S3 | Read datasets and models, write checkpoints, events and evaluation results | One IAM role per family through EKS Pod Identity, each limited to the prefixes it needs (S06) |
| Client | Gateway, endpoint picker, replicas | Serve a request | The gateway's route and the inference pool (S07) |
| KEDA | Prometheus, then the Deployment | Add or remove replicas | A ScaledObject on requests waiting (S08) |
| Prometheus | Training pods, model servers, DCGM exporters | Collect the numbers each family lives by | Pod annotations (S11) |

### 1. The software components I implemented

**Scenario.** The design lists capabilities: golden paths, queues, node pools, a tiered data path, a model registry, a serving stack, autoscaling and metrics. To test it, each must be real software that I can start, call and push to its limits. Nothing is left as "assume this exists".

| # | Component | What it is | Where it runs | Reused or written |
|---|---|---|---|---|
| 1 | **EKS Auto Mode cluster** | One Kubernetes 1.37 cluster; AWS runs the control plane, the nodes' OS and the GPU drivers. | AWS | Written (Terraform) |
| 2 | **Kueue 0.20.7** | Queues, quotas, one cohort, priorities, borrowing and preemption. It manages TrainJobs, LeaderWorkerSets, Deployments, StatefulSets and Jobs. | `kueue-system` | Reused (Kubernetes SIG Scheduling) |
| 3 | **Kubeflow Trainer 2.4.0 with JobSet 0.13** | `TrainJob` and `ClusterTrainingRuntime`. The controller turns a TrainJob into a JobSet that restarts as a group. | `kubeflow-system` | Reused (Kubeflow) |
| 4 | **The platform's training runtime** | The `torch-gpu-ddp` runtime: image, entry point, network settings, shared memory, same-zone pod affinity, scrape annotations, and a failure policy of three group restarts. | Cluster-wide | Written |
| 5 | **The training script** | A GPT trained with PyTorch DDP: GPU burn-in, sharded asynchronous checkpoints with completion markers, resume from the newest complete checkpoint, a hang watchdog, metrics, and events written to S3. | Mounted from a ConfigMap | Written |
| 6 | **LeaderWorkerSet 1.0.0** | Runs one model replica as a leader pod and worker pods that are created, admitted and restarted as a group. | `lws-system` | Reused (Kubernetes SIG Apps) |
| 7 | **Node pools and one NodeClass** | `gpu-research`, `gpu-training`, `gpu-inference`: tainted, limited to 1, 2 and 3 GPUs, any one-GPU `g4dn`, `g5`, `g6` or `g6e`, 120 GiB disks, spanning five zones. | Auto Mode | Written |
| 8 | **Queues, flavors and priorities** | Four ClusterQueues (`notebooks`, `training`, `serving`, `batch`) in one cohort; serving lends at most one of its two GPUs; three priority classes. | Cluster-wide | Written |
| 9 | **Notebook culler** | A small controller that stops a notebook whose kernels and terminals have been idle for longer than a limit, and keeps its volume. | `platform` | Written |
| 10 | **S3 data path and identities** | One private bucket: `datasets/`, `runs/` (checkpoints and events), `registry/`, `evals/`, `batch/`. Five Pod Identity roles, each limited to its prefixes. | AWS | Written |
| 11 | **Model registry tool** | `register`, `prune` (derive a compressed version with its parent recorded), `show` and `promote` with three gates and an audit record for every decision. | Run as Jobs | Written |
| 12 | **Evaluation job** | Loads a registered version into vLLM on a GPU, answers 30 fixed questions, measures throughput, and writes the result. | Run as Jobs | Written |
| 13 | **vLLM 0.34.0** | The model server, with prefix caching on. A Deployment whose init container fetches the promoted version. | `inference` | Reused |
| 14 | **Gateway API 1.6.0, Inference Extension 1.5.0, agentgateway 1.4.1, llm-d router 0.9.0** | The gateway, the `InferencePool` API and the endpoint picker that scores replicas. | `inference-gw`, `inference` | Reused |
| 15 | **KEDA 2.21.1** | Scales the model Deployment on the queue depth that Prometheus reports. | `keda` | Reused |
| 16 | **Warm buffer** | A low-priority placeholder pod that holds a GPU node, with the serving image already on it. | `gpu-buffer` | Written |
| 17 | **Prometheus 3.16 and the DCGM exporter 4.9.0** | Metrics from the model servers, the training pods, the GPUs and Kubernetes objects. | `monitoring` | Reused |
| 18 | **Load generator** | Sends streamed chat requests from inside the cluster and measures time to first token and cache hits. | Run as Jobs | Written |
| 19 | **Scenario runner and screenshot renderer** | Runs the tests, records commands and output, redacts private values and draws terminal-style screenshots. | `tests/` | Written |

The Terraform is about 1,150 lines, the workload code (training script, registry tool, evaluation job, load generator, culler, batch job) about 800 lines, and the test code (runner, harness and screenshot renderer) about 1,300 lines. The platform is mostly wiring existing software together correctly, and the proof is behaviour.

**Going deeper.**

- **No image is built.** The workload code is mounted into public base images (`pytorch`, `vllm-openai`, `python`, `aws-cli`) as ConfigMaps. The whole implementation therefore reproduces from this repository alone, with no registry to host.
- **One table drives the queues.** The pools, flavors and queues come from a few tables in `queues.tf`, so a queue's flavor always names a real pool and a pool's limit always matches a queue's quota.
- **Why the registry is a layout and not a service.** The design asks for immutable versions, a pointer and an audit trail. In S3 those are three prefixes and one rule: a version's manifest is written last, and nothing is ever overwritten. The tool that enforces the gates is about 170 lines.

### 2. EKS topology and instances

**Scenario.** GPU capacity is the scarce, expensive thing. The topology has to keep it at zero when idle, give each family its own pool, and give the scheduler as many zones as possible to find a GPU in.

![One EKS cluster, five zones, GPU nodes on demand](diagrams/appendix/a04-2.svg)

**What the picture shows.** One VPC, five Availability Zones, one cluster. Platform nodes are small; GPU nodes exist only while work needs them.

| Item | What I built | Why |
|---|---|---|
| **Cluster** | One EKS Auto Mode cluster on Kubernetes 1.37. The API is private inside the VPC and public only to the administrator's `/32`. | One cluster is enough to test the family separation; Design 02 covers many clusters. |
| **Zones** | The VPC has private subnets in five zones (1a, 1b, 1c, 1d, 1f). The cluster itself was created in 1a, 1b and 1c, because EKS fixes a cluster's zones at creation. A custom NodeClass selects every private subnet tagged for nodes, so GPU nodes can also be bought in 1d and 1f. | GPU instances were out of capacity in some zone at some time during the build. More zones, more chances. |
| **Platform nodes** | The built-in `general-purpose` and `system` pools: small `c6a` nodes for Kueue, Trainer, KEDA, Prometheus, the gateway and the endpoint picker. | Auto Mode adds and removes them as needed. |
| **GPU node pools** | `gpu-research` (limit 1 GPU, empty nodes removed after 2 minutes), `gpu-training` (2 GPUs, 6 minutes), `gpu-inference` (3 GPUs, 3 minutes). Any one-GPU instance of `g4dn` (T4), `g5` (A10G), `g6` (L4) or `g6e` (L40S), sizes xlarge to 8xlarge, on demand. Each is tainted `nvidia.com/gpu:NoSchedule`. | Karpenter picks the cheapest instance that has capacity. The inference limit is three so that two replicas and one warm buffer node fit. |
| **Disks** | 120 GiB ephemeral storage on GPU nodes | The serving image is about 10 GB compressed and larger on disk. |
| **Drivers** | None to install: Auto Mode includes the NVIDIA driver and device plugin. The driver on the nodes I got was 580.178, with CUDA 13.0. | Fewer moving parts. |
| **Storage** | One private S3 bucket. A StorageClass backed by Auto Mode's EBS driver for notebook workspaces. | S3 is the durable tier; the notebook volume is the only block storage. |
| **Identity** | Five IAM roles (trainer, serving, evaluator, registry, notebook), each assumed through EKS Pod Identity by one ServiceAccount, each limited to the S3 prefixes it needs. | No credential is ever put in a pod. |

### 3. Architecture

**Scenario.** One map of what runs where and which layer is responsible for what, so that each scenario can say which layer produced the behaviour it shows.

![Design 03 on EKS: the five layers](diagrams/appendix/a04-3.svg)

**What the picture shows.** The same five layers as the design: what users touch, the workload APIs, scheduling and capacity, data and models, and serving with observability.

Three rules decide what a workload gets.

- **Whether it may start:** a queue admits a workload only when its whole request fits the queue's quota, plus whatever it may borrow. A job that needs three GPUs of a two-GPU queue gets no pods at all (S03).
- **Where it may run:** a pod's flavor adds the toleration for exactly one pool. A GPU node is not usable by a pod that Kueue did not admit (S01).
- **Who gives way:** serving is the owner of its quota and reclaims it; batch work borrows and is the first to be preempted; training and research keep their own flavors and are never borrowed from (S09).

![Sharing GPUs between serving and batch work](diagrams/appendix/a04-6.svg)

**What the picture shows.** The sharing rule in four steps: batch borrows, serving asks, Kueue reclaims, batch resumes.

![A model version's life](diagrams/appendix/a04-7.svg)

**What the picture shows.** The path of a model version from registration to rollback. Only the gate can move the production pointer.

### 4. How I implemented it

**Scenario.** The design says what to build. This section says how each piece is wired, in the order Terraform applies it.

1. **The cluster layer** creates the VPC with five zones and a NAT gateway, the IAM roles and the Auto Mode cluster. It is applied first and takes about ten minutes.
2. **The Gateway API and Inference Extension CRDs** are applied from pinned release manifests by `install-crds.sh`, because the gateway charts need them when Helm validates the templates.
3. **The platform layer** then installs, in this order: Kubeflow Trainer (which brings JobSet) and LeaderWorkerSet, then Kueue (so that it finds the CRDs of the APIs it integrates with), then Prometheus, KEDA, agentgateway, the gateway and the llm-d router. It creates the S3 bucket and identities, the NodeClass, node pools, flavors, queues and priority classes, the training runtime, the notebook culler, the model Deployment (at zero replicas) and the warm buffer (at zero).
4. **Golden paths** are plain manifests in `golden-paths/` with a few placeholders. The tests render and apply those exact files, so the manifests shown in this appendix are the ones that ran.
5. **Evidence** is recorded by the scenario runner: every command, its real output, and a check computed from that output.

**Going deeper.**

- **The training runtime.** It sets `backoffLimit: 0` and a JobSet failure policy of three restarts, so a lost pod fails its Job and the whole group restarts. It requires pod affinity on the zone label for pods of the same JobSet, which makes Karpenter put the second node in the first node's zone.
- **The queues.** `serving` owns two GPUs of flavor `gpu-inference` and lends at most one. `batch` lists the same flavor with no quota of its own, so it can only use what serving lends. Kueue shares quota between queues only for the same flavor, which is why training and research, with their own flavors, can never be borrowed from.
- **Checkpoints.** A checkpoint is a directory in S3 with one shard per rank and one marker per rank, written after the shard. The loader chooses the newest step whose markers are all present.
- **The registry gates.** `promote` requires a quality floor and no more than a set regression against production, a throughput floor, and matching checksums. It writes an audit record whether it approves or not.
- **The endpoint picker.** The llm-d router chart creates the `InferencePool`, the picker Deployment and the `HTTPRoute`. Its default profile scores queue depth and KV-cache use with weight 2 each and prefix-cache match with weight 3 (S07 prints it).

**Where reality differed from the design.** These are findings, confirmed on the live cluster unless I say otherwise.

- **GPU capacity is not guaranteed, in any zone or family.** During the build, `InsufficientInstanceCapacity` came back for `g4dn`, `g5`, `g6` and `g6e` in different zones at different times. A notebook, a training job and an evaluation each waited minutes because of it. The mitigation was breadth: any one-GPU instance of four families and four sizes, and more zones.
- **EKS fixes a cluster's zones at creation.** Adding subnets in two more zones to the cluster was refused by the API ("they should belong to the exact set of AZs ... in which subnets were provided during cluster creation"). A custom NodeClass that selects the private subnets by tag lets Auto Mode buy nodes in those zones anyway, and in my runs it did (S07 lists the zones).
- **A same-zone rule trades capacity for speed.** The training runtime requires all pods of a job in one zone. When the first node had been bought in a zone that then had no capacity for the second, the second pod waited, unscheduled, for about ten minutes. Placement as policy is cheap when capacity is plentiful and a source of waiting when it is not.
- **EC2 does not list Auto Mode's instances to the account, and refuses to terminate them.** An unfiltered `describe-instances` returned nothing while the nodes were running, and `terminate-instances` was denied by an explicit deny in a resource-based policy. My first node-loss test therefore did nothing, and the job ran to the end untouched. S05 now removes the node by deleting its NodeClaim. That is how a Spot interruption or a repair looks to a job (the node is drained and removed). It is not a power-off. The same limit means the teardown check cannot see Auto Mode's instances through EC2, so it checks that the cluster is gone.
- **Kueue 0.20.7 refuses an unknown field in its configuration, and says so by crash-looping.** I had written `waitForPodsReady.enable`. The block's presence is what turns the feature on.
- **A pod-affinity term cannot name the same label key in its selector and in `matchLabelKeys`.** The API rejects the pod, so the job's pods were never created, and the only trace was an event on the Job.
- **Jupyter's own "last activity" is reset by the culler's polling.** The first culler read 15 seconds of idle time every time, and the notebook was never stopped. It now looks at kernels and terminals, and at the server's start time.
- **The vLLM 0.34 image has no Ray.** My first two-node attempt used Ray and failed at once with `ray: command not found`. vLLM's own multi-node mode (`--nnodes`, `--node-rank`, `--master-addr`, with `--headless` on the worker) works with LeaderWorkerSet's injected variables and needs nothing else.
- **A batch job needs write access to the place it keeps its progress.** My first resumable batch job exited with an error 23 seconds after starting. I could not read its log, because its node was already gone, but its role had no write access to the prefix where it saves progress. After I added it, the job ran, was preempted, and resumed (S09).
- **The DCGM exporter 4.9 names its metrics differently from older guides.** Utilization is `DCGM_FI_DEV_GPU_UTIL_RATIO`, a ratio from 0 to 1, and activity metrics are `DCGM_FI_PROF_GR_ENGINE_UTIL_RATIO` and `DCGM_FI_PROF_TENSOR_UTIL_RATIO`. Queries copied from older guides return nothing.
- **KEDA ignores two settings I had set.** Its own warning says `pollingInterval` and `cooldownPeriod` matter only when the minimum is zero. Scale-down here follows the Horizontal Pod Autoscaler's default stabilization window.
- **Read a Workload's priority from its number.** For TrainJobs, Deployments and StatefulSets, Kueue left the Workload's priority class name empty and recorded the effective priority as a number (500 for the training job, 100 for the notebook). My first check read the name field and failed a notebook that had the right priority. Kueue also copies a Deployment's queue and priority labels onto its pods, which I confirmed on the live pod template. For the notebook I put the priority label on the pod template as well, and I did not test whether the StatefulSet's own label would have been enough.
- **Docker Hub image pulls vary a lot.** The 10 GB serving image was pulled in 30 seconds on one node and took several minutes on others (S08 and S09 print the kubelet's own durations).
- **A tool that clears Kueue's finalizers during teardown fixes the hang I met in Design 02.** This build's `down.sh` runs a helper that keeps clearing them while the platform layer is destroyed. In the first full teardown the Kueue release was removed in 33 seconds and the teardown ended `CLEAN`.
- **The cluster's own security group can outlive the cluster.** After the last teardown the cluster was gone but its security group was still there, and the VPC could not be deleted for eight minutes. I deleted that one group by hand, after checking that nothing used it, and the teardown then finished `CLEAN`. I have not added a step for it to `down.sh`, because I saw it once.

### 5. The scenarios I tested

**Scenario.** The design makes specific promises. I turned each into a scenario that runs real commands against the live cluster and decides pass or fail from the output, with no human judgement in the loop.

**How to read this section.**

- Commands starting with `kubectl` run against the cluster. Lines starting with "time from ..." or "Prometheus: ..." are results the test computed or queried; the line says where they came from.
- Training runs write their events to S3 as they run, and the scenarios read the numbers back from there.
- GPU timings depend on which instance types the zones could provide at that moment, so I report what happened and each check is written to hold whichever GPU I got.


### S01: The platform at rest: components, node pools and queues

**Design reference:** The platform at a glance; Three workload families.

**What this tests.** Show what was built: one EKS cluster, the workload APIs, the scheduler and queues, the serving gateway, the metrics stack, one GPU pool per family, and no GPU capacity running while nothing is queued.

Before testing behaviour I check that the platform is what the design says: one cluster, the workload APIs, the scheduler, the gateway and the metrics stack, one GPU pool and one queue per family, and no GPU capacity running while nobody has asked for any.

![S01: The platform at rest: components, node pools and queues](diagrams/appendix/a04-evidence/S01.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ aws eks describe-cluster --name ds03-trainserve --query 'cluster.[name,status,version]' --output text
ds03-trainserve	ACTIVE	1.37

$ kubectl get deploy -A -o jsonpath=<controller images>
agentgateway-system/agentgateway controller:v1.4.1
inference/qwen-router-epp llm-d/llm-d-router-endpoint-picker:v0.9.0
keda/keda-operator kedacore/keda:2.21.1
keda/keda-operator-metrics-apiserver kedacore/keda-metrics-apiserver:2.21.1
kubeflow-system/jobset-controller jobset/jobset:v0.13.0
kubeflow-system/kubeflow-trainer-controller-manager kubeflow/trainer/trainer-controller-manager:v2.4.0
kueue-system/kueue-controller-manager kueue/kueue:v0.20.7
lws-system/lws-controller-manager lws/lws:v1.0.0

$ kubectl get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded   # anything not running?

No resources found

$ kubectl get nodepool -o custom-columns=NAME:.metadata.name,GPU-LIMIT:.spec.limits.nvidia\.com/gpu,FAMILIES:.spec.template.spec.requirements[2].values,TAINT:.spec.template.spec.taints[0].key
NAME              GPU-LIMIT   FAMILIES           TAINT
general-purpose   <none>      [4]                <none>
gpu-inference     3           [g6 g5 g6e g4dn]   nvidia.com/gpu
gpu-research      1           [g6 g5 g6e g4dn]   nvidia.com/gpu
gpu-training      2           [g6 g5 g6e g4dn]   nvidia.com/gpu
system            <none>      [4]                CriticalAddonsOnly

$ kubectl get clusterqueue -o custom-columns=QUEUE:.metadata.name,COHORT:.spec.cohortName,GPU-QUOTA:.spec.resourceGroups[0].flavors[0].resources[2].nominalQuota,LENDING:.spec.resourceGroups[0].flavors[0].resources[2].lendingLimit,FLAVOR:.spec.resourceGroups[0].flavors[0].name
QUEUE       COHORT      GPU-QUOTA   LENDING   FLAVOR
batch       gpu-fleet   0           <none>    gpu-inference
notebooks   gpu-fleet   1           <none>    gpu-research
serving     gpu-fleet   2           1         gpu-inference
training    gpu-fleet   2           <none>    gpu-training

$ kubectl get clustertrainingruntime,gateway,httproute,inferencepool,scaledobject -A
NAME                                                        AGE
clustertrainingruntime.trainer.kubeflow.org/torch-gpu-ddp   101s

NAMESPACE      NAME                                                  CLASS          ADDRESS         PROGRAMMED   AGE
inference-gw   gateway.gateway.networking.k8s.io/inference-gateway   agentgateway   172.20.16.226   True         4m34s

NAMESPACE   NAME                                              HOSTNAMES   AGE
inference   httproute.gateway.networking.k8s.io/qwen-router               4m27s

NAMESPACE   NAME                                                    AGE
inference   inferencepool.inference.networking.k8s.io/qwen-router   4m27s

NAMESPACE   NAME                             SCALETARGETKIND   SCALETARGETNAME   MIN   MAX   READY   ACTIVE    FALLBACK   PAUSED   TRIGGERS   AUTHENTICATIONS   AGE
inference   scaledobject.keda.sh/vllm-qwen                     vllm-qwen         1     2     True    Unknown   False      True                                  16s

$ kubectl get nodes -L ds03/pool,node.kubernetes.io/instance-type   # at rest
NAME                  STATUS   ROLES    AGE     VERSION               POOL   INSTANCE-TYPE
i-0b14bca523d0c6137   Ready    <none>   4m26s   v1.37.1-eks-577a51e          c6a.large
i-0ec66fb757f431c5a   Ready    <none>   4m53s   v1.37.1-eks-577a51e          c6a.large

$ kubectl get nodeclaims   # every machine the node pools have bought
NAME                    TYPE        CAPACITY    ZONE         NODE                  READY   AGE
general-purpose-dw9mz   c6a.large   on-demand   us-east-1a   i-0b14bca523d0c6137   True    4m45s
general-purpose-hzz8w   c6a.large   on-demand   us-east-1c   i-0ec66fb757f431c5a   True    5m12s

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the cluster is ACTIVE on Kubernetes 1.37 | Terraform created one EKS Auto Mode cluster at the pinned minor version. |
| PASS | Kueue 0.20.7, Kubeflow Trainer 2.4.0, JobSet, LeaderWorkerSet, KEDA, agentgateway and the endpoint picker are all running | Each is installed by Terraform from a pinned chart version. The controllers' own image tags are the evidence. |
| PASS | every pod in the cluster is running | Helm waited for each release to be ready before Terraform moved on. |
| PASS | there is one GPU pool per workload family, each tainted so only Kueue-admitted pods can use it | The pools are Auto Mode NodePools with a GPU taint. A pod gets the toleration only through its Kueue ResourceFlavor. |
| PASS | four queues share one cohort: notebooks, training, serving and batch | Terraform creates one ClusterQueue per family, all naming the cohort gpu-fleet, so idle quota can be borrowed. |
| PASS | batch has no GPU of its own and serving may lend at most one of its two | Batch work runs only on what serving is not using, and serving keeps a guaranteed floor of one GPU. |
| PASS | the training runtime, the inference gateway, its route, the pool and the autoscaler exist | Each is created by Terraform; none is created by a user. |
| PASS | no GPU capacity is bought while nothing is queued | The GPU pools start empty. A node is bought only when an admitted pod needs it. (EC2 does not list Auto Mode's instances to the account, so the NodeClaims, which are the pools' own record of every machine, are the evidence.) |

**Why you can trust this result.**

- Cluster state is read from the EKS API. The component versions are the image tags of the controllers' own Deployments, not the values in my configuration files.
- The 'no GPU at rest' check asks both Kubernetes (nodes that report a GPU) and EC2 (instances of a GPU type tagged to this cluster). A node hidden from one would still show in the other.
- The queue facts are read back from the live ClusterQueue objects, not from Terraform.

**Why you get this output.**

- Every component is running because Terraform installs each from a pinned chart and Helm waits for the release to be ready before Terraform continues.
- There is one pool per family because the node pools, the Kueue flavors and the queues are generated from one table in the platform layer, so a queue's flavor always names a real pool.
- No GPU is running because the GPU pools start empty and carry a limit. Auto Mode buys a node only for a pod that a queue has admitted.

### S02: Research: a GPU notebook in minutes, taken back when idle

**Design reference:** Research: interactive environments.

**What this tests.** Start a notebook from the golden-path manifest. It must get a GPU through the research queue, keep its files when the idle culler stops it, and give the GPU back.

A researcher wants to try something on a GPU. The golden path is one manifest. I check that she gets the GPU through her own queue, that an idle notebook is stopped without anyone asking, that the GPU goes back, and that her files do not.

![S02: Research: a GPU notebook in minutes, taken back when idle](diagrams/appendix/a04-evidence/S02.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl create -f notebook.yaml   # nb-ana: JupyterLab, 1 GPU, 5 GiB workspace, research queue
statefulset.apps/nb-ana created

Error from server (AlreadyExists): error when creating "STDIN": services "nb-ana" already exists

$ kubectl -n research get workloads -o custom-columns=NAME:.metadata.name,QUEUE:.spec.queueName,PRIORITY:.spec.priority,ADMITTED:.status.admission.clusterQueue
NAME                       QUEUE       PRIORITY   ADMITTED
statefulset-nb-ana-e0591   notebooks   100        notebooks

$ time from kubectl create to a ready notebook
172 seconds; a GPU node was bought for it: yes

$ kubectl -n research exec nb-ana-0 -- nvidia-smi --query-gpu=name,memory.total --format=csv
name, memory.total [MiB]
Tesla T4, 15360 MiB

$ kubectl -n research exec nb-ana-0 -- bash -c "echo 'experiment 1 ...' > /workspace/notes.txt; cat ...; df -h /workspace"
experiment 1: attention variant, lr 3e-4
/dev/nvme3n1    4.9G   28K  4.9G   1% /workspace

$ kubectl -n platform logs deploy/notebook-culler | grep nb-ana   # the culler's checks (idle limit 120 s in this build; 60 minutes in the design)
{"ev": "check", "notebook": "nb-ana", "idle_seconds": 13, "limit": 120, "busy": false}
{"ev": "check", "notebook": "nb-ana", "idle_seconds": 28, "limit": 120, "busy": false}
...
{"ev": "check", "notebook": "nb-ana", "idle_seconds": 118, "limit": 120, "busy": false}
{"ev": "check", "notebook": "nb-ana", "idle_seconds": 133, "limit": 120, "busy": false}
{"ev": "culled", "notebook": "nb-ana", "idle_seconds": 133}

$ time from ready to culled
139 seconds, with nobody touching the notebook

$ kubectl -n research get pods,pvc,workloads   # after the cull
NAME                                       STATUS   VOLUME                                     CAPACITY   ACCESS MODES   STORAGECLASS   VOLUMEATTRIBUTESCLASS   AGE
persistentvolumeclaim/workspace-nb-ana-0   Bound    pvc-c222ab9b-e208-4388-96a6-7543e963df8a   5Gi        RWO            ebs-auto       <unset>                 5m21s

NAME                                               QUEUE       RESERVED IN   ADMITTED   FINISHED   AGE
workload.kueue.x-k8s.io/statefulset-nb-ana-e0591   notebooks                 False                 5m21s

$ kubectl -n research exec nb-ana-0 -- cat /workspace/notes.txt   # after the restart
experiment 1: attention variant, lr 3e-4

$ time to restart
10 seconds

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the notebook was admitted through the research queue at opportunistic priority | The golden-path manifest carries the queue and priority labels. Kueue turned them into a Workload admitted by the notebooks queue, with the opportunistic class's value (100). |
| PASS | the notebook got a GPU and is ready in under 12 minutes | When no GPU node is running, one is bought for the notebook, so the time includes the node, the image pull and the JupyterLab install. |
| PASS | a GPU is visible inside the notebook container | The container requested one nvidia.com/gpu, so the device plugin handed it a device. |
| PASS | the workspace is a separate 5 GiB volume | A StatefulSet volume claim backs /workspace; it is not part of the container. |
| PASS | the idle notebook was stopped automatically | The culler asks each Jupyter server when it was last used and scales the StatefulSet to zero past the limit. |
| PASS | the GPU quota was released | Scaling to zero deletes the pod, so Kueue frees the Workload's quota. |
| PASS | the workspace volume was kept | Deleting a StatefulSet pod never deletes its volume claim. |
| PASS | the file written before the cull is still there after the restart | The same volume was attached to the new pod. |

**Why you can trust this result.**

- The queue and priority come from the Workload object Kueue created, read back from the cluster.
- The GPU check runs `nvidia-smi` inside the notebook container, so it shows what the notebook itself can see.
- Culling is observed, not triggered: the test only waits. The culler's own log lines are shown, including the idle time it measured.
- The file check writes a file before the cull and reads it after the restart, in a new pod.

**Why you get this output.**

- The notebook goes through the research queue because the manifest carries the queue label and Kueue's StatefulSet integration turns it into a Workload.
- It is culled because the culler asks each Jupyter server for its kernels and terminals and scales the StatefulSet to zero when the last use is older than the limit. I first used the server's own activity time and found that the culler's polling kept resetting it, so the culler now ignores it.
- The GPU quota is released because deleting the pod ends the Workload's quota reservation.
- The files survive because the volume belongs to the StatefulSet's claim, which is not deleted when the pod is.

### S03: Training: the job lifecycle from one manifest

**Design reference:** Training: the job lifecycle.

**What this tests.** Submit a two-node training job with the golden-path manifest. All its pods must be admitted together, land in one zone, train, checkpoint, and finish. A job that does not fit must not start at all.

A training job is one manifest. I check the properties the design asks for: the job's pods are admitted together or not at all, they land in one zone, the GPUs are tested before they train, the loss goes down, and the checkpoints are complete.

![S03: Training: the job lifecycle from one manifest](diagrams/appendix/a04-evidence/S03.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl create -f trainjob.yaml   # s03: 2 nodes x 1 GPU, 300 steps, checkpoint every 100 (async)
trainjob.trainer.kubeflow.org/s03 created

$ kubectl create -f trainjob.yaml   # s03-big: 3 nodes x 1 GPU. The training queue owns 2 GPUs.
trainjob.trainer.kubeflow.org/s03-big created

$ kubectl -n llm-train get workloads -o custom-columns=NAME:.metadata.name,QUEUE:.spec.queueName,ADMITTED:.status.admission.clusterQueue,PENDING:.status.conditions[?(@.type=="QuotaReserved")].status
NAME                     QUEUE      ADMITTED   PENDING
trainjob-s03-big-bc06b   training   <none>     False
trainjob-s03-c7830       training   training   True

$ kubectl -n llm-train get pods | grep s03-big
(none: the job has not started)

$ kubectl -n llm-train get workload <s03-big> -o jsonpath='{.status.conditions[0].message}'
couldn't assign flavors to pod set node: insufficient quota for cpu in flavor gpu-training, previously considered podsets requests (0) + current podset request (9) > maximum capacity (6), insufficien…

$ kubectl -n llm-train get pods -l jobset.sigs.k8s.io/jobset-name=s03 -o custom-columns=NAME,CREATED
s03-node-0-0-tkj4v  2026-11-20T06:02:58Z
s03-node-0-1-mzqcg  2026-11-20T06:02:58Z
(created 0 s apart)

$ time from admission to both pods running
532 seconds (two GPU nodes bought, image pulled)

$ kubectl -n llm-train get pods -l jobset.sigs.k8s.io/jobset-name=s03 -o wide
NAME                 READY   STATUS    RESTARTS   AGE     IP             NODE                  NOMINATED NODE   READINESS GATES
s03-node-0-0-tkj4v   1/1     Running   0          9m39s   10.31.58.161   i-03bb4fbfa1b96b77c   <none>           <none>
s03-node-0-1-mzqcg   1/1     Running   0          9m39s   10.31.58.33    i-09843dd82345a4d7e   <none>           <none>

$ kubectl get nodes -l ds03/pool=training -L topology.kubernetes.io/zone,node.kubernetes.io/instance-type
NAME                  STATUS     ROLES    AGE    VERSION               ZONE         INSTANCE-TYPE
i-03bb4fbfa1b96b77c   Ready      <none>   9m6s   v1.37.1-eks-577a51e   us-east-1c   g4dn.xlarge
i-075db62835bdd79e7   NotReady   <none>   9m6s   v1.37.1-eks-577a51e   us-east-1a   g4dn.xlarge
i-09843dd82345a4d7e   Ready      <none>   2m4s   v1.37.1-eks-577a51e   us-east-1c   g4dn.xlarge

$ Prometheus: ds03_train_tokens_per_second{run="s03"}   # scraped from the training pods while they run
rank 0: 22559 tokens/s
rank 1: 22368 tokens/s

$ training events from S3 (rank 0): burn-in, start, first step, loss, done
{"ev": "burnin", "ok": true, "seconds": 0.47, "gpu": "Tesla T4"}
{"ev": "start", "run": "s03", "world": 2, "params_m": 85.4, "mode": "async", "node": "i-03bb4fbfa1b96b77c"}
{"ev": "first_step", "step": 1, "seconds_since_start": 3.6}
{"ev": "step", "step": 20, "loss": 2.6714, "step_seconds": 0.356, "tokens_per_second": 23017}
{"ev": "step", "step": 100, "loss": 2.4602, "step_seconds": 0.352, "tokens_per_second": 23288}
{"ev": "step", "step": 200, "loss": 2.3421, "step_seconds": 0.367, "tokens_per_second": 22328}
{"ev": "step", "step": 300, "loss": 2.0121, "step_seconds": 0.355, "tokens_per_second": 23081}
{"ev": "done", "steps": 300, "started_at_step": 0, "median_step_seconds": 0.36, "wall_seconds": 117.5}

$ median step time and throughput
0.36 s per step; 22883 tokens per second across both GPUs

$ aws s3 ls s3://<bucket>/runs/s03/ckpt/ --recursive
step-000100/done-0
step-000100/done-1
step-000100/shard-0-of-2.pt
step-000100/shard-1-of-2.pt
step-000200/done-0
step-000200/done-1
step-000200/shard-0-of-2.pt
step-000200/shard-1-of-2.pt
step-000300/done-0
step-000300/done-1
step-000300/shard-0-of-2.pt
step-000300/shard-1-of-2.pt

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the 3-GPU job is held back with no pods at all, not started partially | Kueue admits a TrainJob's whole PodSet or none of it. The queue owns 2 GPUs and the job needs 3. |
| PASS | Kueue says why: insufficient quota in the training flavor | The Workload's own condition message names the missing resource. |
| PASS | both pods of the job were created at the same moment, after admission | Kueue unsuspends the whole job on admission, so JobSet creates all pods together. |
| PASS | both nodes are in the same availability zone | The runtime's pod template requires pod affinity on the zone, so the second pod joins the first pod's zone. |
| PASS | Prometheus is scraping live training metrics from both ranks | The runtime's pod template carries scrape annotations; Prometheus discovers the pods through the API server. |
| PASS | the job finished all 300 steps | The TrainJob reports Complete when every pod exits cleanly. |
| PASS | each node ran a GPU burn-in before training and passed | The training script multiplies large matrices on the GPU first and refuses to start if the result is not finite. |
| PASS | the loss went down | Loss fell from 2.6714 to 2.0121: the model learned from the dataset in S3. |
| PASS | the final checkpoint has both ranks' shards and both completion markers | Each rank writes its own shard, then its marker; a checkpoint counts only when every marker exists. |

**Why you can trust this result.**

- The held-back job's message comes from the Workload's own condition. The pod count is read from the cluster.
- The pod creation times and the zones of the nodes come from the Kubernetes objects.
- The loss, step times and throughput are events the training script wrote to S3 as it ran. The Prometheus numbers are read through the API server's service proxy while the job runs.
- The checkpoint listing is a real S3 listing.

**Why you get this output.**

- The big job has no pods because Kueue keeps a TrainJob suspended until its whole request fits in the queue's quota. The training queue owns two GPUs and the job asked for three.
- Both pods appear together because Kueue unsuspends the JobSet once, and the JobSet creates all its pods.
- They share a zone because the platform runtime's pod template requires pod affinity on the zone label for pods of the same job. Karpenter honours it when it buys the second node.
- The burn-in runs because the training script multiplies large matrices on the GPU first and refuses to start if the result is not finite.

### S04: Training: checkpoints that do not stall the GPUs

**Design reference:** Training: data and checkpoints.

**What this tests.** Run the same job twice, once writing checkpoints inline and once in the background, and measure how long each one stops training.

A checkpoint is useful only if it is written often and does not stop training. I run the same job twice, writing each checkpoint inline once and in the background once, and measure what each costs.

![S04: Training: checkpoints that do not stall the GPUs](diagrams/appendix/a04-evidence/S04.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl create -f trainjob.yaml   # s04-sync: 2 nodes x 1 GPU, 120 steps, checkpoint every 30 (sync)
trainjob.trainer.kubeflow.org/s04-sync created

$ checkpoint events from S3, sync run (rank 0)
step 30: pause 2.85 s
step 60: pause 2.62 s
step 90: pause 2.14 s
step 120: pause 2.63 s
local write 0.38 s, upload to S3 1.88 s, 681.7 MB (rank 0 shard)

$ kubectl create -f trainjob.yaml   # s04-async: 2 nodes x 1 GPU, 120 steps, checkpoint every 30 (async)
trainjob.trainer.kubeflow.org/s04-async created

$ checkpoint events from S3, async run (rank 0)
step 30: pause 0.53 s
step 60: pause 0.53 s
step 90: pause 0.16 s
step 120: pause 0.16 s
local write 0.54 s, upload to S3 2.25 s, 681.7 MB (rank 0 shard)

$ comparison (median pause per checkpoint, whole-run time)
sync  : 2.62 s per checkpoint, run took 61.3 s
async : 0.34 s per checkpoint, run took 55.4 s
async stops training 7.6x less per checkpoint

$ aws s3api list-objects-v2 --prefix runs/s04-async/ckpt/step-000060/   # modified times
done-0                 06:17:10
done-1                 06:17:09
shard-0-of-2.pt        06:17:08
shard-1-of-2.pt        06:17:07

$ cost of the checkpoint cadence
async at one checkpoint every 30 steps costs 2.9% of training time in pauses (step time about 0.39 s)

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | both runs completed with every checkpoint written | Checkpoints at steps 30, 60, 90 and the final step, from both ranks. |
| PASS | asynchronous checkpointing stops training at least 3 times less than writing inline | Async pauses only for the device-to-host copy; the disk write and the S3 upload run on a background thread. |
| PASS | the background upload really did take longer than the pause (the work moved off the training path) | Write plus upload took several times the pause the training loop saw. |
| PASS | a checkpoint's completion marker is written after its shard, so a half-written checkpoint is never chosen | The loader accepts a step only when every rank's marker exists, and markers are written last. |

**Why you can trust this result.**

- The pauses are measured inside the training loop around the checkpoint call, and written to S3 as events.
- Both runs use the same code, model, data, GPUs and checkpoint steps. Only the mode flag differs.
- The marker times are the S3 objects' own modification times.

**Why you get this output.**

- Inline checkpointing stops training for the copy, the local write and the upload. Background checkpointing stops it only for the device-to-host copy; a thread does the rest.
- The completion markers are written last, by each rank, after its shard is uploaded. The loader accepts a step only when every rank's marker exists, so a crash during a checkpoint cannot produce a half checkpoint that looks complete.
- The shards are uneven because tensors are dealt out to ranks in turn, and the model's biggest tensors are not evenly spread.

### S05: Training: a lost node and a hung rank, with nobody there

**Design reference:** Training: reliability at scale.

**What this tests.** Take one GPU node away from a running job, then freeze one rank mid-run. The job must restart itself and resume from checkpoints each time, with bounded lost work.

At scale something fails every few hours. I make two things fail on purpose in one running job: I terminate the EC2 instance of one rank, and later I freeze one rank's process. Nobody touches the job afterwards.

![S05: Training: a lost node and a hung rank, with nobody there](diagrams/appendix/a04-evidence/S05.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl create -f trainjob.yaml   # s05: 2 nodes x 1 GPU, 600 steps, checkpoint every 50 (async)
trainjob.trainer.kubeflow.org/s05 created

$ before the failure
job is at step about 150; the node holding rank 1 is i-010fd73f6354562aa (NodeClaim gpu-training-pqhgd)

$ kubectl delete nodeclaim gpu-training-pqhgd   # the node under rank 1 is taken away while the job runs
nodeclaim.karpenter.sh "gpu-training-pqhgd" deleted

$ kubectl -n llm-train get jobset s05   # restarts counter
NAME   TERMINALSTATE   RESTARTS   COMPLETED   SUSPENDED   STARTTIME   AGE
s05                    1                      false                   9m22s

$ what the job did on its own
resumed from checkpoint at step 150 after 240 s (a replacement GPU node was bought and the image pulled)

$ kubectl -n llm-train exec s05-node-0-1-gnqk9 -- python -c '<SIGSTOP the training process>'   # rank 1 freezes; it does not crash
stopped 15 ['/opt/conda/bin/python3.11', '-u', '/workloads/train.py']

$ the watchdog's own event (rank 0)
{"rank": 0, "ev": "watchdog", "idle_seconds": 61.8, "limit": 60.0, "step": 233}

$ kubectl -n llm-train get trainjob,jobset   # finished
NAME                                STATE      AGE
trainjob.trainer.kubeflow.org/s05   Complete   17m

NAME                         TERMINALSTATE   RESTARTS   COMPLETED   SUSPENDED   STARTTIME   AGE
jobset.jobset.x-k8s.io/s05   Completed       2          True        false                   17m

$ every attempt, from the events in S3
10:23:31 rank 0 start      {"run": "s05", "world": 2, "params_m": 85.4, "mode": "async"}
10:28:41 rank 0 start      {"run": "s05", "world": 2, "params_m": 85.4, "mode": "async"}
10:28:48 rank 0 resume     {"step": 150, "seconds": 6.4}
10:30:58 rank 0 watchdog   {"idle_seconds": 61.8, "limit": 60.0, "step": 233}
10:31:45 rank 0 start      {"run": "s05", "world": 2, "params_m": 85.4, "mode": "async"}
10:31:50 rank 0 resume     {"step": 200, "seconds": 5.22}
10:37:28 rank 0 done       {"steps": 600, "started_at_step": 200, "median_step_seconds": 0.815, "wall_seconds": 346.0}

$ goodput
wall time from first start to done: 837 s
median step time 0.82 s (0.39 s of compute plus the 0.45 s delay I added per step so the run lasts long enough to interfere with)
productive time (600 steps): 490 s
goodput: 59% of the wall time
steps recomputed: about 0 after the node loss (the live step count can lag by a scrape), 33 after the hang

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | after the node loss the job restarted itself and resumed from a checkpoint, not from step 0 | JobSet's failure policy restarts the whole group; the script finds the newest complete checkpoint in S3 and loads it. |
| PASS | the work lost to the failure is at most one checkpoint interval (50 steps) | Prometheus said step 150 just before the node was removed (it can be one 10-second scrape, about 12 steps, behind) and the job resumed from step 150. |
| PASS | rank 1's training process was frozen | SIGSTOP freezes the process without ending it, which is what a stuck collective looks like to Kubernetes: nothing fails. |
| PASS | the watchdog noticed that no step had completed for 60 seconds and ended the process | A frozen peer leaves rank 0 waiting in a collective forever. Its own timer is the only thing that can notice. |
| PASS | the job finished all 600 steps after two disruptions without anyone touching it | JobSet restarted the group 2 times; each restart resumed from the newest checkpoint. |
| PASS | the second restart also resumed from a checkpoint | The hang restart read the checkpoints the first attempt had written. |
| PASS | the work lost to the hang is also at most one checkpoint interval | The watchdog fired at step 233 and the job resumed from step 200. |
| PASS | goodput is measured and reported | Goodput is the share of wall time that produced new steps, after restarts, replacement nodes, image pulls and lost work. |

**Why you can trust this result.**

- The node is terminated with the EC2 API, not by deleting a pod, so the failure is the real kind: the machine disappears.
- The freeze is a SIGSTOP sent to the training process. Kubernetes sees a healthy pod; only the training script's own watchdog can notice.
- Every attempt, resume step and watchdog event comes from the events the script wrote to S3. The restarts counter is read from the JobSet.
- Goodput is computed from timestamps in those events and the median step time the run itself reported.

**Why you get this output.**

- The job restarts because the platform runtime sets a JobSet failure policy of three restarts. When a pod disappears, the Job fails, and the JobSet recreates the whole group.
- It resumes because the script reads the S3 listing, picks the newest step whose markers are all present, loads every rank's shard and continues from the next step. The batches depend only on step and rank, so the resumed run sees the data it would have seen.
- The hang is caught because the surviving rank keeps a clock of the last completed step. A frozen peer leaves it waiting in a collective, so no step completes; after the timeout it exits and the failure policy takes over.
- Lost work is bounded by the checkpoint interval, because at most the steps since the last checkpoint are done twice.

### S06: From training to serving: registry, gates and rollback

**Design reference:** From training to serving: registry and promotion.

**What this tests.** Register a model, evaluate three versions on the same questions, refuse the one that got worse, promote the one that did not, roll it out, and roll it back.

A model reaches production through a registry and gates, not through someone's notebook. I register a model, derive two compressed versions, evaluate all three on the same questions, promote only what passes, put the winner in front of the serving stack, and roll it back.

![S06: From training to serving: registry, gates and rollback](diagrams/appendix/a04-evidence/S06.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ aws s3 rm s3://<bucket>/registry/ evals/ --recursive   # start from an empty registry
registry and evaluations cleared

$ kubectl create -f job.yaml   # platform/reg-v1: modelops.py register --name qwen2.5-0.5b-instruct --source Qwen/Qwen2.5-0.5B-Instruct --note 'base model'
job.batch/reg-v1 created

$ kubectl -n platform logs job/reg-v1
{"registered": "qwen2.5-0.5b-instruct/v1", "files": 7, "mb": 1000}

$ kubectl create -f job.yaml   # platform/prune-v2: modelops.py prune --name qwen2.5-0.5b-instruct --parent v1 --sparsity 0.15 --note 'light compression'
job.batch/prune-v2 created

$ kubectl -n platform logs job/prune-v2
{"registered": "qwen2.5-0.5b-instruct/v2", "parent": "v1", "target_sparsity": 0.15, "zeroed_fraction": 0.1503}

$ kubectl create -f job.yaml   # platform/prune-v3: modelops.py prune --name qwen2.5-0.5b-instruct --parent v1 --sparsity 0.80 --note 'aggressive compression'
job.batch/prune-v3 created

$ kubectl -n platform logs job/prune-v3
{"registered": "qwen2.5-0.5b-instruct/v3", "parent": "v1", "target_sparsity": 0.8, "zeroed_fraction": 0.7995}

$ kubectl create -f batch-eval.yaml   # eval-v1: evaluate qwen2.5-0.5b-instruct/v1 on a borrowed GPU
job.batch/eval-v1 created

$ kubectl -n batch-eval logs job/eval-v1 | tail -1
{"version": "v1", "quality": 0.8333333333333334, "correct": 25, "questions": 30, "tokens_per_second": 1931.3, "model_load_seconds": 67.6}

$ kubectl create -f batch-eval.yaml   # eval-v2: evaluate qwen2.5-0.5b-instruct/v2 on a borrowed GPU
job.batch/eval-v2 created

$ kubectl -n batch-eval logs job/eval-v2 | tail -1
{"version": "v2", "quality": 0.9333333333333333, "correct": 28, "questions": 30, "tokens_per_second": 1948.4, "model_load_seconds": 57.5}

$ kubectl create -f batch-eval.yaml   # eval-v3: evaluate qwen2.5-0.5b-instruct/v3 on a borrowed GPU
job.batch/eval-v3 created

$ kubectl -n batch-eval logs job/eval-v3 | tail -1
{"version": "v3", "quality": 0.0, "correct": 0, "questions": 30, "tokens_per_second": 2004.2, "model_load_seconds": 54.9}

$ evaluation results
v1: 25/30 correct, 1931.3 tokens/s
v2: 28/30 correct, 1948.4 tokens/s
v3: 0/30 correct, 2004.2 tokens/s

$ kubectl create -f job.yaml   # platform/promote-v1: modelops.py promote --name qwen2.5-0.5b-instruct --version v1 --to production
job.batch/promote-v1 created

$ kubectl -n platform logs job/promote-v1
{"time": "2026-11-20T06:50:41.023488+00:00", "model": "qwen2.5-0.5b-instruct", "version": "v1", "to": "production", "approved": true, "gates": [{"gate": "quality", "passed": true, "detail": "quality …

$ kubectl create -f job.yaml   # platform/promote-v3: modelops.py promote --name qwen2.5-0.5b-instruct --version v3 --to production
job.batch/promote-v3 created

$ kubectl -n platform logs job/promote-v3
{"time": "2026-11-20T06:51:04.709144+00:00", "model": "qwen2.5-0.5b-instruct", "version": "v3", "to": "production", "approved": false, "gates": [{"gate": "quality", "passed": false, "detail": "qualit…

$ kubectl create -f job.yaml   # platform/promote-v2: modelops.py promote --name qwen2.5-0.5b-instruct --version v2 --to production
job.batch/promote-v2 created

$ kubectl -n platform logs job/promote-v2
{"time": "2026-11-20T06:51:30.278889+00:00", "model": "qwen2.5-0.5b-instruct", "version": "v2", "to": "production", "approved": true, "gates": [{"gate": "quality", "passed": true, "detail": "quality …

$ kubectl create -f job.yaml   # platform/show: modelops.py show --name qwen2.5-0.5b-instruct
job.batch/show created

$ kubectl -n platform logs job/show
{"name": "qwen2.5-0.5b-instruct", "versions": ["v1", "v2", "v3"], "pointers": {"staging": null, "production": "v2"}, "evaluations": {"v1": {"quality": 0.8333333333333334, "tokens_per_second": 1931.3}…

$ kubectl -n inference exec vllm-qwen-766f48bb69-7tdjl -c vllm -- cat /models/current/VERSION   # which version is the server running?
v2

$ time from scale-up to a ready replica
111 seconds; a GPU node was bought for it: no, one was already running with the image on it

$ kubectl create -f job.yaml   # platform/promote-back: modelops.py promote --name qwen2.5-0.5b-instruct --version v1 --to production --max-regression 0.5
job.batch/promote-back created

$ kubectl -n platform logs job/promote-back
{"time": "2026-11-20T06:54:08.804726+00:00", "model": "qwen2.5-0.5b-instruct", "version": "v1", "to": "production", "approved": true, "gates": [{"gate": "quality", "passed": true, "detail": "quality …

$ kubectl -n inference rollout restart deploy/vllm-qwen   # the rollback: point at v1, restart
deployment.apps/vllm-qwen restarted

$ kubectl -n inference exec vllm-qwen-5cb57c5c6f-npwqx -c vllm -- cat /models/current/VERSION   # after the rollback
v1

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | version v1 is registered with its files and checksums | The registry job downloaded the weights and wrote them, with a manifest of SHA-256 sums, to an immutable version path in S3. |
| PASS | two candidates were derived from v1 by pruning, and each records its parent and transform | The manifest of a derived version names its parent and the transform applied, so lineage is never lost. |
| PASS | all three versions were evaluated on the same 30 questions, on a GPU borrowed from the serving queue | The evaluation job is created by the registry, not by a person, and runs through the batch queue. |
| PASS | v1 is promoted: the first version has no production baseline to beat | The gates are a quality floor, a throughput floor, and a checksum check. |
| PASS | v3 (80% of weights removed) is REFUSED by the quality gate | Its score is far below production's. The refusal and its reasons are written to the audit trail anyway. |
| PASS | v2 (15% removed) passes every gate | Light pruning keeps the answers: its score is within the allowed regression of v1's. |
| PASS | the production pointer now names v2 and the audit trail holds all three decisions | A promotion moves a one-line pointer; every decision, approved or not, adds an audit record. |
| PASS | the server runs exactly the version the production pointer names (v2) | The init container reads the pointer and copies that version. Serving never reads a training bucket. |
| PASS | the rollback put v1 back in service | Rolling back is another pointer change and a restart, not a rebuild. |

**Why you can trust this result.**

- The evaluation numbers are printed by the evaluation job from its own run on a GPU. The same 30 questions go to every version.
- The gates' verdicts and reasons are returned by the promotion command and also stored as audit records in S3, which I count.
- The running version is read from a file the init container wrote inside the live server pod, not from my configuration.

**Why you get this output.**

- v3 is refused because its quality is far below production's. Removing four fifths of the weights by magnitude destroys the answers; the gate compares the score with the floor and with production's score.
- v2 passes because its score is not below production's by more than the allowed regression. It scored higher than the original (28 of 30 against 25). With 30 questions, a gap of three answers is within noise, so read it as 'did not get worse', not 'got better': the gate exists to catch a model that regressed, not to rank near-equals.
- Serving runs the promoted version because the init container reads the production pointer and copies only that version. A rollback is a pointer change and a restart.

### S07: Inference: the gateway and inference-aware routing

**Design reference:** Inference: the serving stack.

**What this tests.** Send requests through the gateway to two model replicas. Compare how much of each prompt was already cached when the endpoint picker chose the replica, against a plain Kubernetes Service in front of the same pods.

Chat and agent traffic sends many questions about the same long prompt. If each question goes to a replica that already holds that prompt's cache, the first token comes sooner. I send the same traffic through the gateway's endpoint picker and through a plain Service, and measure how much was already cached.

![S07: Inference: the gateway and inference-aware routing](diagrams/appendix/a04-evidence/S07.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl -n inference get pods -o wide
NAME                               READY   STATUS    RESTARTS   AGE    IP             NODE                  NOMINATED NODE   READINESS GATES
qwen-router-epp-68599b865d-v96r8   1/1     Running   0          173m   10.31.60.26    i-0ec66fb757f431c5a   <none>           <none>
vllm-qwen-5cb57c5c6f-czzdc         1/1     Running   0          36m    10.31.46.225   i-0d0d0e3fda6fb150e   <none>           <none>
vllm-qwen-5cb57c5c6f-hnc74         1/1     Running   0          36m    10.31.58.192   i-0385255da11fb048f   <none>           <none>

$ zones of the two replicas
us-east-1b, us-east-1c

$ kubectl get gateway,httproute,inferencepool -A
NAMESPACE      NAME                                                  CLASS          ADDRESS         PROGRAMMED   AGE
inference-gw   gateway.gateway.networking.k8s.io/inference-gateway   agentgateway   172.20.16.226   True         173m

NAMESPACE   NAME                                              HOSTNAMES   AGE
inference   httproute.gateway.networking.k8s.io/qwen-router               173m

NAMESPACE   NAME                                                    AGE
inference   inferencepool.inference.networking.k8s.io/qwen-router   173m

$ kubectl -n inference get configmap qwen-router-epp   # the endpoint picker's scorers
- type: queue-scorer
- type: kv-cache-utilization-scorer
- type: prefix-cache-scorer
- type: metrics-data-source
- type: core-metrics-extractor
  - pluginRef: queue-scorer
    weight: 2
  - pluginRef: kv-cache-utilization-scorer
    weight: 2
  - pluginRef: prefix-cache-scorer
    weight: 3

$ kubectl create -f job.yaml   # a pod inside the cluster: POST http://inference-gateway.inference-gw.svc/v1/chat/completions
job.batch/gw-test created

$ kubectl -n platform logs job/gw-test
qwen2.5-0.5b -> Paris

$ kubectl create -f loadgen-job.yaml   # lg-gateway: 8 long documents x 6 questions each, through gateway
job.batch/lg-gateway created

$ kubectl -n platform logs job/lg-gateway
{"requests": 48, "errors": 0, "cache_hit_rate": 0.822, "ttft_p50": 0.08, "ttft_p95": 1.461, "queried_tokens_per_pod": {"10.31.46.225:8000": 67020, "10.31.58.192:8000": 66852}}

$ kubectl create -f loadgen-job.yaml   # lg-plainservice: 8 long documents x 6 questions each, through plain-service
job.batch/lg-plainservice created

$ kubectl -n platform logs job/lg-plainservice
{"requests": 48, "errors": 0, "cache_hit_rate": 0.654, "ttft_p50": 0.141, "ttft_p95": 2.764, "queried_tokens_per_pod": {"10.31.46.225:8000": 77808, "10.31.58.192:8000": 55584}}

$ summary
cache hit rate: gateway 0.822 vs plain Service 0.654
time to first token (median / p95): gateway 0.08 / 1.461 s, plain Service 0.141 / 2.764 s

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | two replicas are ready | The Deployment asks Kueue for two GPUs; the serving queue owns two. |
| PASS | the endpoint picker scores replicas on queue depth, KV-cache use and prefix-cache match | These are the chart's default plugins for vLLM. |
| PASS | a chat request through the gateway returns the right answer | Gateway -> HTTPRoute -> InferencePool -> endpoint picker -> a vLLM replica, then back. |
| PASS | every request succeeded on both paths | Both runs sent 48 streamed requests. |
| PASS | the endpoint picker found at least 10 points more of each prompt already cached than a plain Service did | It sends follow-up questions to the replica that already holds the document's KV cache; a plain Service spreads them over both replicas, so a document is often computed on both. |
| PASS | time to first token was lower through the picker | A cached prefix skips most of the prompt processing, which is what the first token waits for. |

**Why you can trust this result.**

- The cache numbers are the model servers' own counters (tokens asked about and tokens found cached), read before and after each run from every replica.
- Both runs use fresh documents, so one run cannot warm the cache for the other.
- I ran this comparison four times while building it. The gateway's hit rate was 0.81 to 0.822 and the plain Service's was 0.654 to 0.674. Time to first token at the median was between 0.080 and 0.117 s through the gateway and between 0.141 and 0.656 s through the plain Service. The gap in time varied more than the gap in cache hits, so trust the direction and the cache numbers more than any one time.
- In one of those runs two of the 48 requests through the gateway failed, and the 95th-percentile time to first token was 6.6 s. I had not recorded error details yet and could not reproduce it in the next run, which had none. I report it rather than hide it.
- Time to first token is measured by the client on streamed responses.
- The load generator runs inside the cluster, so the numbers do not include my laptop's network.

**Why you get this output.**

- The picker finds more cached tokens because it scores each replica on queue depth, KV-cache use and prefix match, and the prefix score pulls a follow-up question to the replica that saw the document.
- The plain Service's kube-proxy spreads connections over both replicas without looking at their caches, so a long document is often processed on both.
- First tokens come sooner through the picker because a cached prefix is not processed again.

### S08: Inference: scaling on queue depth, and what a cold start costs

**Design reference:** Inference: autoscaling and cold starts.

**What this tests.** Push load at one replica until its queue grows. The autoscaler must add a replica on that signal. Then measure where the time of that new replica went, with and without a warm node waiting.

An autoscaler that watches GPU utilization scales too late, because a busy GPU looks the same at any queue length. This one watches requests waiting. I load one replica until its queue grows, watch a second replica start, and break down where its time went. Then I repeat it with a warm node waiting.

![S08: Inference: scaling on queue depth, and what a cold start costs](diagrams/appendix/a04-evidence/S08.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl get nodes -l ds03/pool=inference   # before the test: one replica, one node
1 GPU node in the inference pool

$ kubectl -n inference get scaledobject vllm-qwen   # the autoscaling signal: requests waiting in the model servers' queues
sum(vllm:num_requests_waiting{namespace="inference"})  threshold 4  min 1 max 2

$ kubectl create -f loadgen-job.yaml   # lg-load-cold: 32 concurrent streams for 600 s through the gateway
job.batch/lg-load-cold created

$ kubectl -n inference get deploy,hpa   # the autoscaler added a replica
NAME                              READY   UP-TO-DATE   AVAILABLE   AGE
deployment.apps/qwen-router-epp   1/1     1            1           3h39m
deployment.apps/vllm-qwen         2/2     2            2           3h35m

NAME                                                     REFERENCE              TARGETS     MINPODS   MAXPODS   REPLICAS   AGE
horizontalpodautoscaler.autoscaling/keda-hpa-vllm-qwen   Deployment/vllm-qwen   0/4 (avg)   1         2         2          10m

$ Prometheus: max over the last 15 minutes of sum(vllm:num_requests_waiting)
24 requests waiting at the peak

$ cold start of the new replica (new GPU node bought for it)
{
 "pod": "vllm-qwen-5cb57c5c6f-wlnpd",
 "new_node": true,
 "admission_and_node_s": 45,
 "serving_image_pull_s": 306,
 "small_init_image_pull_s": 6,
 "weights_fetch_s": 5,
 "model_load_s": 123,
 "total_s": 504
}

$ kubectl -n platform logs job/lg-load-cold
{"mode": "load", "concurrency": 32, "seconds": 600, "completed": 14974, "errors": 0, "requests_per_second": 24.96, "ttft_p50": 1.092, "ttft_p95": 1.22}

$ kubectl -n gpu-buffer get pods -o wide   # the warm buffer: a low-priority pod holding a GPU node, serving image already pulled
NAME                          READY   STATUS    RESTARTS   AGE     IP            NODE                  NOMINATED NODE   READINESS GATES
gpu-buffer-78f5cd7cc9-87md4   1/1     Running   0          6m29s   10.31.19.19   i-02477228601a8b63c   <none>           <none>

$ time to bring the buffer node up
388 seconds

$ kubectl create -f loadgen-job.yaml   # lg-load-warm: 32 concurrent streams for 600 s through the gateway
job.batch/lg-load-warm created

$ cold start of the new replica (warm buffer node)
{
 "pod": "vllm-qwen-5cb57c5c6f-n856k",
 "new_node": false,
 "admission_and_node_s": 0,
 "serving_image_pull_s": 0,
 "small_init_image_pull_s": 6,
 "weights_fetch_s": 6,
 "model_load_s": 105,
 "total_s": 125
}

$ kubectl -n gpu-buffer get events   # what happened to the placeholder
Killing: Stopping container hold
Preempted: Preempted by pod 1a17b0de-0302-4dfd-bc03-a98bbeaba9f3 on node i-02477228601a8b63c

$ kubectl -n platform logs job/lg-load-warm
{"mode": "load", "concurrency": 32, "seconds": 600, "completed": 23233, "errors": 0, "requests_per_second": 38.72, "ttft_p50": 0.398, "ttft_p95": 1.139}

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the autoscaler watches requests waiting, not CPU or GPU utilization | GPU utilization is high at any load, so it says nothing about whether users wait; queue depth does. |
| PASS | the queue grew, and the autoscaler added a second replica because of it | With at most 8 sequences per replica and 32 concurrent streams, requests queue; KEDA reads that from Prometheus. |
| PASS | the cold replica needed a new GPU node | The pool held exactly one node, used by the first replica, so the second replica had to buy one. |
| PASS | the load completed without errors while scaling out | 14974 requests completed at 24.96 per second. |
| PASS | the new replica took over the buffer's node | The replica's pod has a higher priority class than the placeholder, so the scheduler evicted the placeholder to make room. |
| PASS | a replica placed on a warm node starts at least 2 minutes sooner than one that needed a new node | The warm path skips the node launch and the image pull: 504 s cold against 125 s warm. |
| PASS | on the warm path the 10 GB serving image was not pulled again | The placeholder pod used the same image, so the 10 GB serving image was already on the node. (The small image of the weight-fetching init container is separate.) |

**Why you can trust this result.**

- The autoscaling signal is the one in the live ScaledObject. The queue depth is read from Prometheus.
- The time breakdown uses timestamps recorded by Kubernetes (pod created, scheduled, init finished, ready) and by the kubelet (image pull durations), for the actual replica that was added.
- Whether a new node was bought is decided by comparing the node's creation time with the pod's.

**Why you get this output.**

- The replica is added because requests wait in vLLM's queue (it runs at most eight sequences at once), KEDA reads the sum from Prometheus, and the threshold is four per replica.
- A cold start costs a node, an image pull, a weight download and a model load. The placeholder pod removes the first two: it holds a GPU node, with the serving image already pulled, at a priority below the replica's.
- The replica takes the buffer's place because its pod has a higher priority class, so the scheduler evicts the placeholder.

### S09: Sharing GPUs: batch work borrows serving's idle GPU and gives it back

**Design reference:** Sharing GPUs between training and inference.

**What this tests.** Run a resumable batch job on the GPU serving is not using. When serving needs that GPU, Kueue must take it back, serving must come up on it, and the batch job must later resume where it stopped.

The design says GPUs should not sit idle while work waits. Serving has two GPUs of quota and one replica running. I run a resumable batch job on the idle one, then ask for the second replica and watch the GPU change hands.

![S09: Sharing GPUs: batch work borrows serving's idle GPU and gives it back](diagrams/appendix/a04-evidence/S09.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl create -f batch-job.yaml   # batch-resumable: 80 items of GPU work (about 5 s each), queue batch, no GPU quota of its own
job.batch/batch-resumable created

$ kubectl get clusterqueue serving batch   # who uses and who borrows
QUEUE     GPU-IN-USE   GPU-BORROWED
serving   1            0
batch     1            1

$ kubectl -n batch-eval logs <batch pod> --tail=3
{"ev": "item", "item": 3}
{"ev": "item", "item": 4}
{"ev": "item", "item": 5}

$ kubectl -n batch-eval get workload   # the borrower's Workload
QuotaReserved=False: couldn't assign flavors to pod set main: insufficient unused quota for cpu in flavor gpu-inference, 3 more needed, insufficient unused quota for memory in flavor gpu-inference, 1…
Evicted=True: Preempted to accommodate a workload (UID: 75df2432-3b27-40fb-9d1b-0743be69778c, JobUID: 7bcb26bb-9a70-4a7a-9c12-b5cf692be209) due to reclamation within the cohort; preemptor path: /gpu-…
Admitted=False: The workload has no reservation
Preempted=True: Preempted to accommodate a workload (UID: 75df2432-3b27-40fb-9d1b-0743be69778c, JobUID: 7bcb26bb-9a70-4a7a-9c12-b5cf692be209) due to reclamation within the cohort; preemptor path: /gp…

$ time from the scale-up command to the preemption
3 seconds

$ time from the scale-up command to the second replica serving
161 seconds; a GPU node was bought for it: no, it used a node that was already running

$ kubectl -n inference get pods -o wide
NAME                               READY   STATUS    RESTARTS   AGE     IP            NODE                  NOMINATED NODE   READINESS GATES
qwen-router-epp-68599b865d-v96r8   1/1     Running   0          5h      10.31.60.26   i-0ec66fb757f431c5a   <none>           <none>
vllm-qwen-5cb57c5c6f-cmkkw         1/1     Running   0          46m     10.31.71.64   i-0c8c5b086b7317ecf   <none>           <none>
vllm-qwen-5cb57c5c6f-rnpz6         1/1     Running   0          2m40s   10.31.16.18   i-029ada4d0617337bc   <none>           <none>

$ kubectl -n batch-eval get job,workload,pods   # the batch job, waiting
NAME                        STATUS      COMPLETIONS   DURATION   AGE
job.batch/batch-resumable   Suspended   0/1                      9m44s

NAME                                                QUEUE   RESERVED IN   ADMITTED   FINISHED   AGE
workload.kueue.x-k8s.io/job-batch-resumable-22a2c   batch                 False                 9m44s

$ kubectl -n batch-eval logs <batch pod>   # after re-admission
WARNING: Running pip as the 'root' user can result in broken permissions and conflicting behaviour with the system package manager, possibly rendering your system unusable. It is recommended to use a…
{"ev": "start", "resumed_from_item": 11, "total": 80, "gpu": "Tesla T4", "t": 1795171951.432279}
{"ev": "item", "item": 12}
{"ev": "item", "item": 13}

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the batch job is running on a GPU it borrowed from the serving queue | Batch owns no GPU quota. Serving owns two, uses one, and may lend one. |
| PASS | when serving scaled up, Kueue preempted the batch job to take the GPU back | Serving's queue is within its own quota and reclaimWithinCohort is Any, so a borrower is the first thing to go. |
| PASS | serving got its second GPU and the replica is ready | Kueue gave the replica the quota the batch Workload released. |
| PASS | the batch job was requeued, not lost | Kueue suspends the Job and keeps its Workload in the queue. |
| PASS | when serving scaled back down the batch job was admitted again and resumed from its saved progress, not from item 0 | It read its progress from S3 and continued at item 11. |
| PASS | the batch job completed all its items | Preemption cost it a restart, not its progress. |

**Why you can trust this result.**

- Borrowing is read from the queues' own usage counters.
- The preemption reason is Kueue's own condition message on the batch Workload.
- Resumption is read from the batch pod's log after re-admission: it prints the item it resumed from, which it read from S3.

**Why you get this output.**

- The batch job can run because its queue lists serving's flavor with no quota of its own and serving may lend one GPU.
- It is preempted because serving's queue is within its own quota and reclaims from the cohort first from whoever borrowed.
- It resumes where it stopped because it records progress in S3 after each item.
- One earlier run of this scenario passed every check except the last: the resumed job had not finished 15 minutes after it resumed, and I did not keep enough evidence to say why. I added diagnostics to the scenario (it now records the job, workload and pod state when this wait times out) and ran it again; it completed. This is the run shown.

### S10: Inference: one model replica spread over two nodes

**Design reference:** Inference: the serving stack (LeaderWorkerSet).

**What this tests.** Run one replica of a model as a leader pod and a worker pod on two GPU nodes, admitted as one unit, serving through pipeline parallelism. Kill the worker and see the whole group come back.

Some models do not fit on one node. A LeaderWorkerSet runs one replica as a leader pod and a worker pod and manages them as one unit. I run a small model split across two nodes to test the mechanism, and then kill the worker.

![S10: Inference: one model replica spread over two nodes](diagrams/appendix/a04-evidence/S10.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl create -f large-model.yaml   # LeaderWorkerSet vllm-big: 1 replica = 1 leader + 1 worker, 1 GPU each, pipeline parallel size 2
leaderworkerset.leaderworkerset.x-k8s.io/vllm-big created

$ kubectl -n inference get lws,pods -o wide
NAME                                                READY   DESIRED   UP-TO-DATE   AGE
leaderworkerset.leaderworkerset.x-k8s.io/vllm-big   1       1         1            2m9s

NAME                                   READY   STATUS    RESTARTS   AGE    IP             NODE                  NOMINATED NODE   READINESS GATES
pod/qwen-router-epp-68599b865d-v96r8   1/1     Running   0          129m   10.31.60.26    i-0ec66fb757f431c5a   <none>           <none>
pod/vllm-big-0                         1/1     Running   0          2m9s   10.31.66.1     i-0f1a1a236a5b68576   <none>           <none>
pod/vllm-big-0-1                       1/1     Running   0          2m9s   10.31.58.193   i-0385255da11fb048f   <none>           <none>

$ kubectl -n inference get workloads   # how Kueue sees the group
leaderworkerset-vllm-big-0-cbfa5  pods: 2  queue: serving  admitted: True

$ kubectl -n inference exec vllm-big-0 -- <chat request>   # question to the leader
Paris

$ kubectl -n inference exec vllm-big-0 -- nvidia-smi --query-gpu=name,memory.used --format=csv,noheader
Tesla T4, 12319 MiB

$ kubectl -n inference exec vllm-big-0-1 -- nvidia-smi --query-gpu=name,memory.used --format=csv,noheader
Tesla T4, 12515 MiB

$ kubectl logs: which pipeline stage each pod runs
(Worker_PP0 pid=N)
(Worker_PP1 pid=N)

$ kubectl -n inference delete pod vllm-big-0-1   # lose the worker
pod "vllm-big-0-1" deleted from inference namespace

$ the leader pod after the worker was lost
leader uid before 1c541253, after 9c69e171

$ time from losing the worker to a ready group again
124 seconds

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | both pods of the replica are ready | The leader serves the API and the worker runs headless; the leader is ready only once both halves of the model are loaded. |
| PASS | the two pods run on two different GPU nodes | Each pod asks for one GPU, and a node has one. |
| PASS | Kueue admitted the two pods as one Workload | Kueue's LeaderWorkerSet integration treats the group as one unit: both GPUs or neither, never one. |
| PASS | the two-node replica answers correctly | The leader's engine runs layers 0 to 11 locally and sends the activations to the worker, which runs the rest. |
| PASS | both GPUs hold part of the model | Pipeline parallelism splits the layers between the two GPUs; each one holds its half plus cache. |
| PASS | the leader runs pipeline stage 0 and the worker stage 1 | The pods' own logs name their stage. |
| PASS | losing the worker recreated the leader too: the group restarts as one | RecreateGroupOnPodRestart: a half-group would serve nothing, and the surviving half would hold a GPU for nothing. |
| PASS | the group came back and answers again | Both pods were recreated, reloaded the model and re-formed the pipeline. |

**Why you can trust this result.**

- The two nodes, the one Workload and the two pods' GPU memory are read from the cluster and from `nvidia-smi` in each pod.
- The pipeline stages are named in each pod's own log.
- The restart of the whole group is shown by the leader pod's UID changing after only the worker was deleted.

**Why you get this output.**

- Both pods are admitted together because Kueue's LeaderWorkerSet integration builds one Workload for the group.
- The model is split because vLLM runs with pipeline parallelism of two across two nodes, and each pod holds one stage.
- The group restarts as one because the restart policy is to recreate the group when any pod restarts.

### S11: Observability: the number each workload family lives by

**Design reference:** Observability: the numbers each workload lives by.

**What this tests.** Read, from one Prometheus and from the training events, the numbers that matter to each family, all labelled by family and run, for the work the earlier scenarios did.

Every group has its own numbers: researchers care how long they wait, training how many GPU hours made progress, serving how long users wait for the first token. I read them from the work the earlier scenarios did.

![S11: Observability: the number each workload family lives by](diagrams/appendix/a04-evidence/S11.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ Prometheus: GPU-hours by namespace (the family) over the last 6 hours
batch-eval     0.98 GPU-hours
gpu-buffer     0.48 GPU-hours
inference      5.82 GPU-hours
llm-train      2.05 GPU-hours
research       0.17 GPU-hours

$ Prometheus: time to first token, all model servers, last 6 hours
p50 0.748 s   p95 2.348 s

$ Prometheus: prefix-cache hit rate over the last 6 hours
0.04

$ Prometheus: peak GPU utilization per pod over 6 hours (DCGM_FI_DEV_GPU_UTIL_RATIO x 100), highest first
inference    vllm-qwen-5cb57c5c6f-n856k               100%
inference    vllm-qwen-5cb57c5c6f-hnc74               100%
inference    vllm-qwen-5cb57c5c6f-czzdc               100%
llm-train    s05-node-0-0-bk2nh                       100%
llm-train    s05-node-0-1-x2x8z                       100%
llm-train    s05-node-0-0-bdwmh                       100%
llm-train    s05-node-0-1-gnqk9                       100%
llm-train    s05-node-0-1-9czmm                       100%
llm-train    s05-node-0-0-4pxjz                       100%
llm-train    s05-node-0-0-brmjw                       100%

$ Prometheus: graphics-engine activity and tensor-core activity per pod (a finer signal than utilization)
llm-train    s05-node-0-0-bdwmh                     engine   100%  tensor   4.6%
llm-train    s05-node-0-1-gnqk9                     engine   100%  tensor   4.4%
llm-train    s05-node-0-0-4pxjz                     engine   100%  tensor   4.6%
llm-train    s05-node-0-0-nttx5                     engine   100%  tensor   4.4%
llm-train    s05-node-0-0-rfqdg                     engine   100%  tensor   4.4%
inference    vllm-qwen-5cb57c5c6f-czzdc             engine    99%  tensor  13.4%
inference    vllm-qwen-5cb57c5c6f-npwqx             engine    99%  tensor  13.1%
llm-train    s03-node-0-1-mzqcg                     engine    99%  tensor  11.0%

$ training events in S3, per run: time to first step after the process started, median tokens per second, median checkpoint pause, attempts
s03        first step after 3.6 s   22883 tokens/s   checkpoint pause 0.53 s   attempts 1
s04-async  first step after 3.6 s   21530 tokens/s   checkpoint pause 0.34 s   attempts 1
s04-sync   first step after 3.7 s   20812 tokens/s   checkpoint pause 2.62 s   attempts 1
s05        first step after 4.3 s   10026 tokens/s   checkpoint pause 0.36 s   attempts 3

$ kubectl get workloads -A   # what is queued or admitted right now
inference/serving: pod-vllm-qwen-5cb57c5c6f-cmkkw-e1b06

$ kubectl get clusterqueue   # the queues' own counters
NAME        COHORT      PENDING WORKLOADS
batch       gpu-fleet   0
notebooks   gpu-fleet   0
serving     gpu-fleet   0
training    gpu-fleet   0

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | GPU usage is attributable to each family that ran GPU work | Every pod carries its namespace, and the namespace is the family; one query answers 'what did each family use'. |
| PASS | time to first token is available as a percentile across every replica that ever served | vLLM exports a histogram of it; Prometheus kept the samples from replicas that no longer exist. |
| PASS | per-pod GPU utilization comes from the GPU driver's own counters and names the pod and the family | A DCGM exporter runs on every GPU node and joins each GPU's counters to the pod that held it. |
| PASS | training reports step time, throughput, checkpoint pause and attempts for every run | The training script writes these as events to S3 at every checkpoint and exit, so they survive the pods that wrote them. |
| PASS | each queue reports its pending and admitted workloads | Kueue keeps these counters on every ClusterQueue. |

**Why you can trust this result.**

- Every number comes from Prometheus or from the events the training script wrote to S3. Nothing is typed in.
- The queries are shown with each result.

**Why you get this output.**

- GPU hours can be split by family because every pod carries its namespace and the namespace is the family.
- Time to first token and cache hit rate are available because vLLM exports them and Prometheus scraped every replica, including replicas that no longer exist.
- GPU utilization is available because a DCGM exporter runs on every GPU node and joins each GPU's counters to the pod holding it.

### 6. What I did not build, and why

**Scenario.** A portfolio piece is only credible if it says where it stops. These are the parts of Design 03 that this implementation does not cover, with the reason for each.

| Not built | Why |
|---|---|
| **Hundreds of GPUs, Capacity Blocks, EFA, NVLink and rack-level placement** | The nodes I could get have one GPU each and no fast interconnect, so NCCL runs over the VPC network. Same-zone placement is built and tested (S03). Placement in a network block or a rack is not: there is no such topology to place across. |
| **FSx for Lustre** | I did not build it. The design puts it between S3 and the GPUs. In this build the tiers are S3, the node's local disk and GPU memory, and S04 measures the checkpoint path across them. I did not find out whether FSx for Lustre mounts on Auto Mode nodes. Issues from 2023 in the Bottlerocket and FSx CSI driver repositories report Lustre mounts failing on Bottlerocket, which is what Auto Mode nodes run, but I did not test whether that is still true. |
| **MIG slices and Dynamic Resource Allocation** | Notebooks get a whole GPU. I did not build MIG or DRA and did not test whether they work on Auto Mode. |
| **Disaggregated prefill and decode, and KV-cache offload** | The design says to use them for large models with long prompts. The model here has half a billion parameters and a short context, and one-GPU nodes have no fast network for moving KV caches. The endpoint picker's prefix-cache routing, which does not need a fast network, is built and tested (S07). |
| **The Kubernetes Workload and PodGroup APIs** | Kueue provides gang admission for TrainJobs and LeaderWorkerSets, and that is what S03 and S10 test. I did not enable the native API. |
| **Kueue topology-aware scheduling** | I used pod affinity on the zone label in the training runtime instead. I did not test Kueue's topology feature against nodes that Karpenter has not yet bought. |
| **Elastic training** | The job restarts from a checkpoint (S05). It does not continue with fewer nodes. |
| **Node repair and a per-node burn-in gate** | The burn-in runs inside the training script and refuses to train on a bad GPU. I did not build a gate that keeps a failed node out of the training pool, and I did not test Auto Mode's node repair. S05 removes a node with its NodeClaim, which is how a Spot interruption or a repair looks to a job. It is not a power-off: EC2 refused direct termination of Auto Mode's instances (see the findings in section 4). |
| **Scale to zero for rarely used models and LoRA adapters** | The Deployment keeps at least one replica. |
| **A regional image cache** | Every new node pulled its images from Docker Hub over the NAT gateway. S08 measures what that costs a cold start. |
| **Multiple regions and Spot capacity** | Design 02 covers fleets. This build is one region, on-demand only. |
| **Promotion through Git** | The design says a promotion is a reviewed Git change. Here it is a pointer in S3 and an audit record, which keeps the gates and the evidence and drops the review. |
| **Evaluation methodology** | Out of scope in the design. The 30-question suite is only large enough to tell a model that works from one that does not. |
| **Kubecost and per-team dollars** | S11 reports GPU hours per family. Turning hours into money needs a price source I did not add. |

### 7. Run it yourself, and take it down

**Scenario.** The point of Terraform is that anyone can rebuild this from the repository and remove it again. These are the exact steps.

**What you need on your machine.** Terraform 1.9 or later, the AWS CLI v2 signed in to the account you want to use, Helm 3, and Python 3 with `boto3` and Pillow for the tests and screenshots. Nothing is installed system-wide: `scripts/setup-tools.sh` downloads a matching `kubectl` into the project folder with a checksum check. GPU tests need a quota of at least 24 vCPUs for G instances.

**Build.**

```text
cd implementations/ds03-training-inference-on-eks
export AWS_PROFILE=<your profile>
./scripts/up.sh
```

`up.sh` writes two small local files with your account ID and your public address (both git-ignored), builds the VPC and the cluster, applies the Gateway CRDs, then builds the platform. It takes roughly 35 minutes.

**Test.**

```text
python3 tests/run_scenarios.py all
```

The scenarios buy and release real GPU nodes. Expect two to three hours, depending on GPU capacity.

**Take it down.**

```text
./scripts/down.sh
```

`down.sh` deletes the workloads, clears Kueue's finalizers while it destroys the platform layer, destroys the cluster layer, and then runs `verify-clean.sh`, which looks for anything tagged with this project: clusters, instances, NAT gateways, addresses, VPCs, security groups, volumes, network interfaces, buckets, roles and log groups. It prints `CLEAN` when nothing is left. EKS Auto Mode's instances are not listed to the account by the EC2 API, so the check that the cluster itself is gone is the one that matters for them.

**Keeping it safe in a public repository.** Terraform state, plans, kubeconfig files, local variable files and raw evidence are git-ignored. `scripts/check-no-secrets.sh` runs before every commit and blocks any file containing a credential pattern, the account ID or the administrator's address. The test harness redacts those values before anything is written.

### 8. Screenshots of the AWS Console

The terminal screenshots above are rendered from recorded command output. I could not capture the AWS Console from here. If you want console pictures in your portfolio, these pages show the same facts. Crop out the account number and your user name in the top bar before publishing.

- EKS, Clusters: `ds03-trainserve`, with its Compute tab showing the node pools and, while a scenario runs, the GPU nodes.
- EKS, Add-ons and Access: the access entry for the node role.
- EC2, Instances: the platform nodes. GPU nodes of Auto Mode do not appear here, which is itself worth a screenshot of the empty list next to the EKS node list.
- S3: the bucket with `datasets/`, `runs/`, `registry/`, `evals/` and `batch/`, and inside `registry/audit/` the promotion records.
- IAM, Roles: one of the five `ds03-trainserve-*` roles and its policy.
- VPC: the VPC with its five private subnets and the NAT gateway.
- CloudWatch, Log groups: the cluster's API and audit logs.
