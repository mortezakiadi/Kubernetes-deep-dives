---
id: appendix-design-03-eks
title: "Appendix D: Design 03 built and tested on Amazon EKS"
short: "D. Design 03 on EKS (built and tested)"
---
This appendix describes a working, scaled-down implementation of [Design 03: Training and inference infrastructure](#design-training-inference). I built it with Terraform on Amazon EKS in one AWS account: one cluster that runs the three workload families of the design (research notebooks, distributed training, model serving), one golden path for each, one GPU pool and one queue per family, a model registry with promotion gates, an inference gateway that routes by cache contents, autoscaling on queue depth, and a way for batch work to borrow serving's idle GPUs and give them back.

The order is the one a reader needs: first the context (the problem, who the actors are and who talks to whom), then the software I implemented, the EKS topology and instances, the architecture, how it is implemented, and then the scenarios I tested, with the commands, the results, why the results can be trusted and why the system produces them.

**What "scaled down" means here.** The design has training clusters with hundreds of GPUs, fast interconnects and reserved Capacity Blocks, and inference clusters in several regions. This build has one cluster and at most six single-GPU nodes at a time (research 1, training 2, inference 3), on whichever of the T4, A10G, L4 or L40S instances the zones could give me. The training job is an 85-million-parameter model on two GPUs; the served model has 0.5 billion parameters. The mechanisms are the same ones: the same Kueue, Kubeflow Trainer, LeaderWorkerSet, vLLM and Gateway API Inference Extension components. Only the numbers are small, and section 6 lists what I did not build.

{{RESULT_SUMMARY}}

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

