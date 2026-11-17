---
id: design-training-inference
title: "Design 03: Training and inference infrastructure"
short: "Training and inference infrastructure"
---
This design answers one question: *what do research, training and inference workloads actually demand from compute, network, storage and scheduling, and which platform capabilities do we build so each of them runs well on the same Kubernetes fleet?* It sits on top of [Design 02](#design-multicluster), which gives us the clusters, queues and capacity, and goes one level up: the workloads themselves.

The short version: the three workload families are different enough that one generic "GPU platform" serves none of them well. *Research* needs fast, interactive, cheap-to-waste access. *Training* needs many GPUs at once, a fast network, a fast data path and resilience to failures that will happen. *Inference* needs low, predictable latency, fast scale-out and high utilization of every GPU. The platform gives each family its own golden path (notebooks and dev pods, TrainJob, a model serving stack), built on shared building blocks: Kueue queues, Karpenter capacity, DRA devices, a tiered storage path and one set of GPU metrics.

### The use case and requirements

**Scenario.** The company now trains its own models. Thirty researchers prototype in notebooks, an LLM team runs multi-week pre-training and many fine-tuning jobs, and product teams serve a dozen models to customers, two of them too large for one node. Today every group builds its own scripts: researchers SSH into GPU boxes, training runs die and restart by hand, and inference teams over-provision because nobody trusts autoscaling. The platform team is asked to build the infrastructure all three can share.

**What the design must do.**

- **Research:** a GPU environment in minutes, with the team's data and code, that doesn't waste a whole H100 when someone goes to lunch.
- **Training:** start a 64-GPU job with one manifest, keep the GPUs busy (fast data and network), survive node failures without losing days, and checkpoint often without slowing down.
- **Inference:** meet latency targets (time to first token, time per output token) at the lowest cost per token, scale with traffic, roll out new model versions safely.
- **Between them:** a model moves from experiment to training to production through one registry and one set of evaluation gates; training and inference share GPUs over the day instead of each owning idle capacity.

**What it must avoid.**

- One cluster setup tuned for nobody: inference latency hurt by training traffic, or training jobs evicted by notebooks.
- GPUs that look "busy" in the scheduler but sit idle waiting on data, the network or a stuck rank.
- Manual recovery: a person restarting failed jobs at 3 a.m.

**Out of scope for this design:** the cluster fleet and queue topology ([Design 02](#design-multicluster)), sandboxing untrusted code ([Design 01](#design-sandboxing)), and model quality and evaluation methodology.

### Three workload families, three sets of demands

**Scenario.** In the kickoff, each group describes its needs as "GPUs, and fast". The platform team needs a sharper picture: what each workload is bottlenecked on, how long it runs, how it fails, and what it costs when it waits.

![Three workload families, three sets of demands](diagrams/designs/ds03-1.svg)

**What the picture shows.** The demands of each workload family along the dimensions that drive platform decisions.

- **Research and development:** interactive and bursty; usually one GPU or a slice of one; minutes to hours; mostly idle while a person thinks. Bottleneck: the human's waiting time. Can tolerate interruption, but hates losing notebook state.
- **Pre-training and large training:** batch; tens to thousands of GPUs at once, all-or-nothing; days to weeks. Bottleneck: GPU-to-GPU network (all-reduce) and the data and checkpoint path. One failed rank stops the whole job.
- **Fine-tuning and post-training:** batch; one to a few nodes; hours. Same shape as training, smaller, far more frequent. Bottleneck: queue wait and startup time, since it runs many times a day.
- **Online inference:** long-running services; one GPU to several nodes per replica; latency-bound. Prefill is compute-bound, decode is memory-bandwidth-bound. Bottleneck: KV-cache memory, routing and cold starts.
- **Batch inference and evaluation:** batch; throughput-bound, latency doesn't matter; ideal for Spot and for borrowed idle GPUs.

**Going deeper.**

- **The scheduling need follows from the failure model.** Training needs gang admission because a partial job is worthless. Inference needs spreading because one failed replica must not take the service down. Research needs neither, just a fast start and a limit.
- **"GPU utilization" means different things.** For training, the number that matters is goodput: the share of paid GPU time spent making progress, after failures, restarts, checkpoints and data stalls. For inference it's tokens per second per GPU at the latency target. For research it's how much of the allocated GPU time is actually used.
- **Most of the fleet's cost is training and inference; most of the people are research.** Design the capacity for the first two and the experience for the third.

### The platform at a glance

**Scenario.** The team needs one map of the capabilities to build, which workloads use each, and which ones are shared.

![The platform at a glance](diagrams/designs/ds03-2.svg)

**What the picture shows.** Five layers of platform capabilities, from what users touch to the hardware.

- **Golden paths (what users touch):** notebooks and dev pods for research, `TrainJob` for training and fine-tuning, a `ModelDeployment` template for inference, and a batch-inference job template. Each is a short manifest or an SDK call, never raw pods.
- **Workload APIs:** Kubeflow Trainer v2 (TrainJob, which runs as a JobSet), LeaderWorkerSet for multi-node model replicas, KServe or plain Deployments for single-node serving, Kubernetes Jobs for batch.
- **Scheduling and capacity:** Kueue queues and priorities, topology-aware placement, DRA for GPUs and MIG slices, Karpenter NodePools with reserved, on-demand and Spot capacity (all from [Design 02](#design-multicluster)).
- **Data and models:** a tiered storage path (S3, FSx for Lustre, local NVMe), a checkpoint service, a model registry and a weight cache close to the GPUs.
- **Observability:** GPU metrics (DCGM), training goodput and step time, serving latency and throughput, queue wait time, all labelled by team and workload.

**Going deeper.**

- **Golden paths are templates, not walls.** A team can still write a raw JobSet; the golden path is just the version with the right network settings, checkpointing, labels and queue already filled in. Most teams take it because it's faster.
- **Two cluster roles do most of the work:** training clusters (large, Capacity Blocks, EFA, topology labels) and inference clusters (multi-region, many node types, fast autoscaling). Research runs in either, on its own NodePools.

### Research: interactive environments

**Scenario.** A researcher wants to try a new attention variant on a small model. Today she waits two days for a "GPU box", then leaves it running all weekend. The platform should give her a GPU in minutes and take it back when she's not using it.

**The design.**

- **Notebooks and dev pods on demand:** a JupyterLab or VS Code server (Kubeflow Notebooks, or a simple self-service portal) in the team's namespace, with the team's datasets mounted read-only and a personal workspace volume.
- **Right-sized GPUs:** the default is a MIG slice or a small GPU (L4 or L40S class); a full H100 needs an explicit choice. Slices come through DRA ResourceClaims, so the same notebook image works on either.
- **Idle culling:** a notebook whose kernel has been idle for a set time (say 60 minutes) is stopped; its workspace volume is kept, so restarting takes minutes and loses no files.
- **A research queue:** notebooks go through Kueue at `opportunistic` priority, with a small guaranteed share per team and the ability to borrow idle GPUs. Borrowed capacity can be reclaimed by training, so researchers are told when to save.
- **One step to scale up:** from the notebook, the same code is submitted as a TrainJob with the SDK, so moving from one GPU to 16 doesn't mean rewriting anything.

**Going deeper.**

- **Time-slicing is fine inside one team's dev pool** (for example, several people's small experiments on one GPU) but never across teams, because there's no memory isolation.
- **Notebooks that run untrusted code** (downloaded repos, agent experiments) go to the sandbox platform from [Design 01](#design-sandboxing).
- **Experiment tracking** (MLflow or similar) is part of the golden path, so every run records its code version, data version and metrics without anyone having to remember.

### Training: the job lifecycle

**Scenario.** A 64-GPU fine-tuning run is submitted on Friday evening. Without help, it can wait behind a smaller job, start with half its pods, land across two network blocks, lose a node on Saturday and sit dead until Monday. The platform has to handle each of those steps automatically.

![Training: the job lifecycle](diagrams/designs/ds03-3.svg)

**What the picture shows.** One training job from submission to completion, and the component responsible at each step.

- **Submit:** the user creates a `TrainJob` that points at a platform `ClusterTrainingRuntime` (PyTorch, with EFA and NCCL settings, checkpoint paths and metrics already in it). Kubeflow Trainer turns it into a JobSet.
- **Queue and admit:** Kueue holds the job suspended until the team's queue has quota for all 64 GPUs, then admits it all at once and picks one network block through topology-aware scheduling.
- **Provision and start:** Karpenter launches nodes if needed (from Capacity Blocks first); pods pull images from a regional cache, mount data and start `torchrun`. If not all pods are running within a timeout, Kueue requeues the whole job.
- **Run and checkpoint:** the job writes checkpoints asynchronously to local NVMe, then to FSx for Lustre and S3, every N steps. Step time, throughput and GPU metrics are exported per rank.
- **Fail and resume:** a GPU error, a node loss or a preemption ends the attempt. The bad node is cordoned and replaced, the job is requeued, and it resumes from the latest checkpoint, with no human involved.
- **Finish and register:** the final checkpoint is converted and pushed to the model registry with its training metadata, which starts the evaluation pipeline.

```yaml
apiVersion: trainer.kubeflow.org/v1alpha1
kind: TrainJob
metadata:
  name: llm-sft-0412
  namespace: llm-train
  labels:
    kueue.x-k8s.io/queue-name: training          # Kueue admits it all at once #!
spec:
  runtimeRef:
    apiGroup: trainer.kubeflow.org
    kind: ClusterTrainingRuntime
    name: torch-efa-h100      # platform runtime: NCCL, EFA, checkpoints, one network block #!
  trainer:
    image: registry.example.com/llm/sft:2026.11.3
    numNodes: 8                                   # 8 nodes x 8 GPUs = 64 GPUs #!
    resourcesPerNode:
      limits: { nvidia.com/gpu: 8 }
    args: ["--config", "configs/sft-70b.yaml", "--resume-from", "latest"]
```

**Going deeper.**

- **The platform runtime is where the expertise lives.** EFA device requests, NCCL environment variables, shared-memory size, the checkpoint directory layout and the metrics sidecar are defined once in the `ClusterTrainingRuntime` and tested by the platform team, instead of being copied into every job.
- **Topology placement is worth real money.** Pods spread across network blocks spend longer in all-reduce, and every GPU in the job waits. Requiring one block for jobs that fit in one, and preferring it for larger ones, is the cheapest performance gain available. The platform runtime's pod template carries the Kueue topology annotation, so users get this without asking.
- **`--resume-from latest` is not optional.** Every training job on the platform must be restartable from a checkpoint, because preemption, Spot, Capacity Block expiry and hardware failure all end the same way.

### Training: data and checkpoints

**Scenario.** A run on 128 GPUs shows 40% GPU utilization. The cause isn't the model: the data loader reads small files straight from S3, and every checkpoint pauses training for six minutes while 1.5 TB is written. Fast GPUs are only as fast as the storage feeding them.

![Training: data and checkpoints](diagrams/designs/ds03-4.svg)

**What the picture shows.** The storage tiers between the durable copy in S3 and the GPU, and which data flows through each.

- **S3 (durable, cheapest):** datasets, final checkpoints and model artifacts. The source of truth, versioned, in the same region as the training cluster.
- **FSx for Lustre (shared, fast):** a file system linked to the S3 bucket. Datasets are loaded lazily from S3 on first read and served to every node at high throughput; checkpoints are written here and exported to S3 in the background.
- **Local NVMe (per node, fastest):** instance storage on GPU nodes. Used for the data loader's cache, for writing a checkpoint shard quickly before it's copied to shared storage, and for container image layers.
- **GPU memory:** the model, optimizer state and the current batch. Everything above exists to keep this busy.
- **Mountpoint for S3 (simple path):** for jobs that read large files sequentially, mounting S3 directly can be enough, with no file system to run.

**Going deeper.**

- **Checkpoint asynchronously and in shards.** Each rank writes its own shard (for example, with PyTorch distributed checkpointing) to local NVMe and keeps training while a background process copies it to FSx and S3. The pause drops from minutes to seconds.
- **Checkpoint frequency is a cost trade-off.** If a job of N GPUs fails on average every F hours and a checkpoint costs C minutes of pause, checkpoint often enough that lost work after a failure (half the interval, on average) costs about as much as the pauses. For large jobs that usually means every 15 to 30 minutes, not every few hours.
- **Pack small files.** Millions of small images or text files are slow on any storage. Pre-shard datasets into large files (WebDataset, Parquet, Arrow) as part of the data pipeline.
- **Keep the data in the same region as the GPUs,** which is also how [Design 02](#design-multicluster) decides where a job is dispatched.

### Training: reliability at scale

**Scenario.** On a 512-GPU run, something fails every few hours: a GPU throws an XID error, an EFA link flaps, a node's NVMe fills up. Each failure stops all 512 GPUs. The team needs failures to cost minutes, not hours.

**The design.**

- **Find bad hardware before jobs do:** new GPU nodes run a short burn-in (DCGM diagnostics, an NCCL all-reduce test across the node) before they're marked ready for training. Nodes that fail are replaced, not scheduled.
- **Detect failures while running:** the EKS node monitoring agent and DCGM report GPU, driver and network errors as node conditions; node auto repair (or a platform controller) cordons and replaces the node.
- **Restart the job, not the person:** JobSet's failure policy restarts the whole group a set number of times, and Kueue requeues it if it can't restart in place. Topology-aware scheduling can swap a failed node in the job's placement for a healthy one in the same block.
- **Detect hangs, not just crashes:** a stuck collective (one rank waiting forever) shows up as step time going to zero progress, not as a failed pod. A watchdog alerts and restarts the job when no step completes within a multiple of the normal step time.
- **Measure goodput:** report, per job and per team, the share of GPU hours that produced training progress, and break down the rest into queueing, startup, checkpoint pauses, failures and lost work.

**Going deeper.**

- **Keep a few spare nodes in large Capacity Blocks.** If a block holds 64 nodes and a job needs 60, the four spares let a failed node be replaced in minutes, without waiting for new capacity that may not exist.
- **Failures cluster.** A node that failed once is likely to fail again. Track failures per instance ID and keep repeat offenders out of the training pool.
- **Make restarts cheap:** cache images on nodes, keep the dataset warm in FSx, and load the latest checkpoint from FSx rather than S3. A restart that takes 20 minutes on 512 GPUs costs as much as many hours of a small job.

### From training to serving: registry and promotion

**Scenario.** A fine-tuned model scores well in the training team's notebook and is deployed on Friday. On Monday, customers report worse answers in Spanish: the model was never evaluated on the multilingual suite, and nobody can say exactly which checkpoint is in production.

**The design.**

- **One model registry:** every model version has an immutable ID, its weights in object storage (as Safetensors, or packaged as an OCI artifact), the training job and data versions that produced it, and its evaluation results. See [Model registries](#post-model-data) for the options.
- **Evaluation as a pipeline, not a step someone remembers:** registering a candidate starts batch evaluation jobs (quality suites, safety checks, latency and throughput benchmarks on the target GPU type) through the batch queue.
- **Promotion gates:** a model moves from `candidate` to `staging` to `production` only when its gates pass. The promotion is a Git change to the model deployment, so it's reviewed and reversible.
- **Optimize for serving before serving:** quantization, compilation and choosing tensor parallelism happen in this pipeline, and the result is evaluated again. See [Compress, then evaluate again](#post-production).

**Going deeper.**

- The serving stack never pulls weights straight from a training bucket. It pulls a promoted version from the registry, so what runs in production is always something that passed the gates.
- Keep the last two production versions warm in the weight cache, so a rollback takes a deployment change, not a 20-minute download.

### Inference: the serving stack

**Scenario.** One model fits on a single GPU and serves chat with short prompts; another is a 400B-parameter model that needs two 8-GPU nodes per replica and serves long documents. Both need to hit latency targets at the lowest cost. One generic Deployment template can't do both well.

![Inference: the serving stack](diagrams/designs/ds03-5.svg)

**What the picture shows.** The path of a request from the client to a GPU, and the choices at each layer.

- **Gateway:** a Kubernetes Gateway (Envoy-based) terminates TLS, authenticates, applies per-tenant rate limits and token budgets, and picks the model by name.
- **Inference-aware routing:** the Gateway API Inference Extension's endpoint picker (or llm-d's scheduler built on it) sends each request to the replica that's best for it: one that already holds the conversation's prefix in its KV cache, with a short queue and free KV-cache memory. This beats round-robin by a wide margin for chat and agent traffic.
- **Model servers:** vLLM (or SGLang) for most models. A model that fits on one node runs as a Deployment, one replica per node or per GPU group. A model that needs several nodes per replica runs as a LeaderWorkerSet, where each replica is a leader pod and its workers, created, scaled and restarted as a group.
- **Disaggregated serving (for large models with long prompts):** prefill and decode run in separate pools, so compute-heavy prompt processing doesn't slow down token generation. The KV cache moves from prefill to decode pods over the fast network (for example, with NIXL). Use it when prompts are long and the network can carry the KV transfers; otherwise keep prefill and decode together.
- **KV-cache offload:** hot prefixes stay in GPU memory; warm ones move to CPU memory or local NVMe (for example, with LMCache), so long shared prefixes such as system prompts aren't recomputed.

**Going deeper.**

- **Pick the parallelism to match the hardware.** Tensor parallelism inside a node (over NVLink), pipeline parallelism across nodes. Never run tensor parallelism across nodes over Ethernet.
- **Different tenants, different priorities on one model:** the inference gateway can give interactive traffic priority over batch or free-tier traffic on the same replicas, and shed the lower class first when overloaded.
- **llm-d** packages this stack (inference-aware routing, disaggregation, KV-cache management) as a Kubernetes-native framework and has been a CNCF Sandbox project since March 2026. It's a good starting point when the platform team doesn't want to assemble the pieces itself.

### Inference: autoscaling and cold starts

**Scenario.** Traffic doubles every weekday at 9 a.m. The autoscaler adds replicas, but each new replica takes 11 minutes to become ready: 4 to get a node, 2 to pull a 15 GB image, 5 to load 140 GB of weights. By the time it's ready, the morning peak is over.

**The design.**

- **Scale on serving signals:** HPA or KEDA on the number of waiting requests per replica, KV-cache utilization, or time to first token, never on CPU or GPU utilization, which stay high under any load.
- **Keep a warm floor:** each model has a minimum number of replicas on reserved capacity sized for normal traffic; autoscaling handles the peaks above it.
- **Make a new replica fast, step by step:**
  - **Node:** keep a small buffer of idle GPU nodes for large models (a low-priority placeholder pod that's evicted when a real replica needs the space), or accept Karpenter's launch time for smaller ones.
  - **Image:** pre-pull serving images on GPU nodes, or use lazy-loading images, so the image isn't on the critical path.
  - **Weights:** load from a regional cache on fast storage (FSx or local NVMe) or stream them directly into GPU memory, instead of downloading from S3 at startup.
- **Scale to zero for the long tail:** rarely used models (or LoRA adapters) are scaled to zero and loaded on demand. Serving many LoRA adapters on one base model replica is cheaper than a replica per fine-tune.

**Going deeper.**

- **Scale out across regions before scaling up within one** (see [Design 02](#design-multicluster)): shifting traffic to another region with spare replicas takes seconds.
- **Measure cold start as a metric,** broken into node, image and weights, per model. It decides how much warm capacity each model needs.
- See [Scale, start fast, route smartly](#post-production) for the model-server side of the same problem.

### Sharing GPUs between training and inference

**Scenario.** The inference fleet is sized for the daily peak, so at 3 a.m. half its GPUs are idle. The training queue, meanwhile, has two days of jobs waiting. Finance asks why the company pays for idle GPUs while work waits.

![Sharing GPUs between training and inference](diagrams/designs/ds03-6.svg)

**What the picture shows.** The same pool of GPUs over a day: inference follows traffic, and borrowed batch work fills the gap.

- **Inference has priority and a guaranteed share:** model servers run at `production` priority with quota for their peak, so they can always reclaim their GPUs.
- **Batch work borrows the gap:** batch inference, evaluation and checkpoint-friendly fine-tuning run at `opportunistic` priority through Kueue on the inference clusters' idle GPUs at night and at weekends.
- **Reclaim is fast and safe:** when inference traffic grows, the autoscaler adds replicas; Kueue preempts borrowed batch work to make room, and the batch jobs requeue from their checkpoints.
- **Only work that tolerates eviction borrows:** short evaluation jobs, batch inference with resumable progress and small fine-tunes. Multi-week pre-training stays on the training clusters' reserved capacity.

**Going deeper.**

- **Leave headroom in reclaim time.** Preempting a batch pod and starting a model server on that GPU takes as long as a cold start. Start reclaiming ahead of the traffic curve (scale on a forecast for the morning ramp), not when latency has already slipped.
- **Same node type, same images.** Borrowing only works if the batch jobs can run on the inference node types. Make the batch-inference and evaluation templates target those GPUs.
- This is the same cohort-and-borrowing mechanism as [Queues, quotas and fair sharing](#ds02-3), applied across workload families instead of teams.

### Observability: the numbers each workload lives by

**Scenario.** Every team has a dashboard, and every dashboard shows GPU utilization at 90%. Yet training is slow, inference misses its latency target at peak, and researchers wait a day for a GPU. GPU utilization alone says almost nothing.

**What each family is measured by.**

- **Research:** time to get a GPU (p50, p95), allocated vs. used GPU hours per notebook, idle-culling savings.
- **Training:** queue wait time, time to first step, step time and tokens per second per GPU, model FLOPs utilization, checkpoint pause time, failures per 1,000 GPU hours, and goodput.
- **Inference:** time to first token and time per output token (p50, p99) per model and tenant, requests and tokens per second, KV-cache utilization, prefix-cache hit rate, cold start time, cost per million tokens. See [Measure tokens, not requests](#post-observability).
- **Shared:** DCGM per GPU (utilization, memory, power, XID errors, NVLink and EFA errors), Kueue queue metrics, Karpenter capacity by type, and cost per team.

**Going deeper.**

- **SM activity is a better GPU signal than "utilization".** DCGM's GPU utilization only says a kernel was running; SM activity and tensor-core activity show how much of the GPU was doing work.
- **Every metric carries the same labels:** team, workload family, job or model, cluster. That's what lets one query answer "what did team X's training cost per useful GPU hour last month?".

### Rollout plan, trade-offs and open questions

**Scenario.** Leadership asks for the order of work that helps the most people soonest.

**Rollout.**

- **Phase 1, training reliability:** the platform `ClusterTrainingRuntime`, TrainJob through Kueue, asynchronous checkpointing to FSx and S3, node burn-in and auto repair, goodput dashboards. Training is where the most money is lost today.
- **Phase 2, the serving stack:** the gateway with inference-aware routing, vLLM templates for single-node and LeaderWorkerSet models, autoscaling on serving signals, the weight cache, the model registry with promotion gates.
- **Phase 3, research environments:** self-service notebooks with MIG slices, idle culling, the research queue and "submit as TrainJob" from the notebook.
- **Phase 4, sharing:** batch inference and evaluation borrowing inference GPUs at night; disaggregated serving for the large models that benefit.

**Trade-offs we accepted.**

- **Kubeflow Trainer and LeaderWorkerSet over custom operators:** community APIs that work with Kueue, at the cost of following their release pace (Trainer's API is still `v1alpha1`).
- **FSx for Lustre on the training path:** a file system to run and pay for, in exchange for keeping expensive GPUs fed and checkpoints fast.
- **Inference-aware routing in the gateway:** a more complex routing layer than a Kubernetes Service, for much better latency and throughput on chat and agent traffic.
- **Borrowing inference GPUs for batch work:** more preemptions and more moving parts, in exchange for recovering idle capacity every night.

**Open questions for the next review.**

- **Disaggregated serving everywhere, or only for the largest models?** It needs a fast network between prefill and decode pools and more operational care.
- **Elastic training:** frameworks that can continue with fewer nodes after a failure would reduce restart costs further, if our training code supports it.
- **Native gang scheduling** (the Workload and PodGroup APIs in kube-scheduler) for LeaderWorkerSet and TrainJob once it's stable.
- **Accelerator diversity:** adding Trainium or other accelerators means separate images, runtimes and queues per accelerator type. Is the saving worth it for our models?
