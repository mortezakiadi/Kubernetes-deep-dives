---
id: design-distributed-training
title: "Design 20: Distributed training on PyTorch, Ray and Megatron with fast checkpointing and model loading"
short: "Distributed training, fast checkpoints"
---
This design answers one question: *how do we run distributed training on Kubernetes with PyTorch Distributed, Ray or Megatron, while keeping checkpoint saves from stalling training and keeping restarts and model loads short?* Each framework has its strengths: plain PyTorch with FSDP2 for most fine-tuning and mid-sized pre-training, Ray Train when training is one stage of a larger Python pipeline, and Megatron Core when a model needs tensor, pipeline, context and expert parallelism together. The platform runs all three through one path: Kueue admission ([Design 17](#design-gpu-scheduling)), framework-specific launchers, and one checkpoint and model-loading layer that they share.

The short version: *launch all three the same way, checkpoint in shards, asynchronously and in tiers, and treat loading as seriously as saving*. Every rank writes its own shard of the model and optimizer state. Training only pauses while tensors are copied to pinned host memory, and a background process writes the copy to local NVMe and then uploads it to object storage. Restarts read from the nearest tier that has a complete checkpoint, and can reshard to a new world size. Evaluation and inference load weights from parallel streams or registry-mounted volumes, not from a single slow download. Two numbers, *checkpoint stall per save* and *time from pod start to first training step*, are tracked like any other SLO.

### The use case and requirements

**Scenario.** The platform runs three kinds of training. Fine-tuning teams use PyTorch FSDP2 on 8 to 64 GPUs. An RL team runs Ray, with rollout workers, reward models and a trainer in one program. The pre-training team runs Megatron Core on 1,024 H100s for a 70B model with tensor, pipeline and data parallelism. All three complain about the same things: saving a checkpoint pauses the 1,024-GPU job for 6 minutes every hour, a restart after a node failure takes 38 minutes before the first step, and loading a 140 GB checkpoint into an evaluation job takes 20 minutes.

![Three frameworks, same pain](diagrams/designs/concepts/ds20-c1.svg)

**What the platform must deliver.**

- **One way to submit:** all three frameworks go through Kueue, with gang admission and topology-aware placement.
- **Short checkpoint stalls:** under 30 seconds of training time lost per save on the largest job.
- **Frequent checkpoints:** an interval set by failure rate (Young–Daly), not by how much the stall hurts.
- **Fast restarts:** under 8 minutes from failure to the first training step on the largest job.
- **Resharding:** resume on a different number of GPUs or a different parallel layout.
- **Fast model loading:** evaluation and inference jobs load a 70B model in under 2 minutes.
- **Durability:** a checkpoint counts only once it is complete and verified in object storage.

**Out of scope:** in-job fault tolerance and failure handling ([Design 14](#design-reliable-rl-infra)), and the scheduling policy itself ([Design 17](#design-gpu-scheduling), [Design 19](#design-scheduling-impact)).

### Choosing the framework

**Scenario.** A new team asked which framework to use for a 13B fine-tune. They had seen a Megatron recipe, a Ray example and a plain PyTorch script, and they spent a week trying all three.

![Choose by parallelism](diagrams/designs/concepts/ds20-c2.svg)

![Choosing the framework](diagrams/designs/ds20-1.svg)

**What the picture shows.** A decision guide based on the model size and the shape of the program.

- **PyTorch Distributed (torchrun + FSDP2):** the default. Sharded data parallelism with `fully_shard` covers most models up to tens of billions of parameters. It's plain PyTorch, so it's easy to debug.
- **Ray Train on KubeRay:** when training is one part of a Python program with other distributed parts, such as RL rollouts, data preprocessing or reward models. Ray starts the workers and sets up torch.distributed for you, and the training loop is still PyTorch.
- **Megatron Core:** when the model needs tensor, pipeline, context or expert parallelism to fit or to run efficiently. This usually means very large dense models, mixture-of-experts models or very long sequences. Its fused kernels and distributed optimizer give the best throughput at scale.
- **The shared layer:** all three use PyTorch Distributed Checkpoint (DCP) underneath. Megatron's `torch_dist` format builds on it, and Ray Train uploads whatever directory the training loop writes. So one checkpoint storage design serves all three.

**Going deeper.**

- **Combinations are normal:** an RL system often runs Megatron or FSDP2 trainers inside Ray actors. The framework choice is per component, not per team.
- **Pick by parallelism needs, not by fashion:** if FSDP2 fits the model with good throughput, adding tensor parallelism mostly adds complexity.

### Launching PyTorch Distributed on Kubernetes

**Scenario.** Teams wrote their own StatefulSets for torchrun, each with a different way to find the master address. One of them hard-coded the rank-0 pod name, so every restart hung when that pod moved.

![TrainJob launch](diagrams/designs/concepts/ds20-c3.svg)

![Launching PyTorch Distributed on Kubernetes](diagrams/designs/ds20-2.svg)

**What the picture shows.** One TrainJob per run, expanded by a shared runtime into a JobSet whose pods find each other through stable DNS.

- **ClusterTrainingRuntime:** maintained by the platform team. It defines the image, the torchrun launch, NCCL environment, volumes for local NVMe and pinned shared memory, and the failure policy.
- **TrainJob:** written by the team. It names the runtime, the number of nodes, GPUs per node and the command.
- **JobSet and headless Service:** created by the trainer controller, so rank 0 has a stable DNS name and torchrun's rendezvous works after restarts.
- **Kueue:** admits the whole TrainJob as one gang, with topology constraints so all nodes land in one fabric block.

**The YAML.** A team's TrainJob for a 64-GPU FSDP2 run (Kubeflow Trainer v2).

```yaml
apiVersion: trainer.kubeflow.org/v1alpha1
kind: TrainJob
metadata:
  name: sft-13b-run42
  namespace: team-finetune
  labels:
    kueue.x-k8s.io/queue-name: finetune
  annotations:
    kueue.x-k8s.io/podset-required-topology: network.example.com/fabric-block   # one block for all 8 nodes #!
spec:
  runtimeRef:
    apiGroup: trainer.kubeflow.org
    kind: ClusterTrainingRuntime
    name: torch-distributed-h100            # platform-maintained: NCCL env, /dev/shm, local NVMe, failure policy
  trainer:
    numNodes: 8
    numProcPerNode: 8
    image: registry.example.com/finetune/trainer:2026.11.3
    command: ["python", "-m", "train.sft", "--config", "configs/13b.yaml"]
    env:
    - { name: CKPT_URI, value: "s3://ckpt-finetune/sft-13b-run42" }
    - { name: CKPT_LOCAL, value: "/local-nvme/ckpt" }
    resourcesPerNode:
      limits: { nvidia.com/gpu: 8 }
```

**Going deeper.**

- **Use elastic rendezvous (c10d) even for fixed-size jobs:** torchrun's c10d backend handles a restarted pod rejoining, which a static master address doesn't.
- **Put platform defaults in the runtime:** shared memory size, NCCL timeouts, the checkpoint volume and the preStop flush hook belong in the runtime, so a hundred TrainJobs don't each get them wrong.
- **Older guides use PyTorchJob.** That is the API of Kubeflow's Training Operator v1, which Kubeflow's docs now file under "legacy v1" and replace with TrainJob and runtimes ([migration guide](https://www.kubeflow.org/docs/components/trainer/operator-guides/migration/)). For all-or-nothing placement, a runtime can ask for Volcano groups with `podGroupPolicy` ([Trainer guide](https://trainer.kubeflow.org/en/latest/operator-guides/job-scheduling/volcano.html)). Since Trainer 2.4, its `scheduling` block can instead have the controller create the Kubernetes Workload and PodGroup objects, which needs the beta gate that is off by default ([Trainer 2.4 post](https://blog.kubeflow.org/kubeflow-trainer-v2.4-release/)).

### Ray Train on KubeRay

**Scenario.** The RL team ran a long-lived RayCluster shared by everyone on the team. Jobs competed for its GPUs without Kueue seeing them, and a crashed driver left GPUs reserved for days.

![RayJob per run](diagrams/designs/concepts/ds20-c4.svg)

![Ray Train on KubeRay](diagrams/designs/ds20-3.svg)

**What the picture shows.** One RayJob per run, with its own RayCluster, admitted by Kueue and deleted when the run ends.

- **RayJob:** creates the RayCluster from `rayClusterSpec`, submits the entrypoint, and deletes the cluster when the job finishes (`shutdownAfterJobFinishes: true`, which Kueue requires).
- **Worker groups:** separate groups for trainer GPUs, rollout GPUs and CPU-only data workers, each a pod set that Kueue accounts for.
- **Ray Train:** `TorchTrainer` starts one worker per GPU, sets up the process group, and handles reporting and checkpoint upload to `storage_path`.
- **Checkpoints:** the training loop writes a DCP checkpoint to local disk, and `ray.train.report` hands the directory to Ray, which uploads it to object storage.

**The code.** A Ray Train loop that saves sharded DCP checkpoints and resumes from the latest one (Python).

```python
import os, tempfile
import ray.train
import torch.distributed.checkpoint as dcp
from ray.train import ScalingConfig, RunConfig, CheckpointConfig, Checkpoint
from ray.train.torch import TorchTrainer
from torch.distributed.checkpoint.state_dict import get_state_dict, set_state_dict

def train_loop(cfg):
    model, optim, loader = build(cfg)                     # FSDP2-wrapped model, optimizer, data loader
    start = 0
    if (ckpt := ray.train.get_checkpoint()):              # resume after a failure or preemption #!
        with ckpt.as_directory() as d:
            msd, osd = get_state_dict(model, optim)
            state = {"model": msd, "optim": osd, "step": 0}
            dcp.load(state, checkpoint_id=d)              # each rank reads only its shards
            set_state_dict(model, optim, model_state_dict=state["model"], optim_state_dict=state["optim"])
            start = state["step"]
    for step in range(start, cfg["steps"]):
        loss = train_step(model, optim, next(loader))
        if step % cfg["ckpt_every"] == 0:
            d = tempfile.mkdtemp(dir=os.environ.get("CKPT_LOCAL", "/tmp"))
            msd, osd = get_state_dict(model, optim)
            dcp.save({"model": msd, "optim": osd, "step": step}, checkpoint_id=d)
            ray.train.report({"loss": loss.item(), "step": step}, checkpoint=Checkpoint.from_directory(d))
        else:
            ray.train.report({"loss": loss.item(), "step": step})

trainer = TorchTrainer(
    train_loop, train_loop_config={"steps": 20_000, "ckpt_every": 500},
    scaling_config=ScalingConfig(num_workers=32, use_gpu=True),
    run_config=RunConfig(name="rl-policy-run7", storage_path="s3://ckpt-rl/ray",
                         checkpoint_config=CheckpointConfig(num_to_keep=3)),
)
trainer.fit()
```

**Going deeper.**

- **One cluster per job:** long-lived shared Ray clusters hide GPU usage from the scheduler. Short-lived RayJobs make each run a gang that Kueue can admit, account for and preempt.
- **Ray's upload is synchronous by default:** for large checkpoints, write with DCP's async path first (next sections) and report the directory once the local write completes.

### Megatron: mapping parallelism onto the topology

**Scenario.** The 70B run used TP=8, PP=8, DP=16. A config change put tensor-parallel groups across two nodes, so every layer's all-reduce went over InfiniBand instead of NVLink. Throughput fell by 40%.

![Megatron layout](diagrams/designs/concepts/ds20-c5.svg)

![Megatron: mapping parallelism onto the topology](diagrams/designs/ds20-4.svg)

**What the picture shows.** How Megatron's parallel groups map onto nodes, racks and fabric blocks.

- **Tensor parallelism (TP):** the most communication-heavy, so it stays inside one node over NVLink (TP ≤ 8 on 8-GPU nodes).
- **Pipeline parallelism (PP):** point-to-point activation transfers between stages. It spans nodes, ideally within one rack or leaf switch.
- **Data parallelism (DP), with the distributed optimizer:** gradient reduce-scatter and parameter all-gather once per step. It spans the fabric block.
- **Context and expert parallelism (CP, EP):** placed by communication volume. Expert all-to-all is often as heavy as TP.
- **Rank order:** Megatron assigns ranks so that TP is the innermost dimension. The platform's job is to make consecutive ranks land on the same node and the same rack. Topology-aware admission and a rank-ordered pod index do this.

**The code.** Checking that a parallel layout maps onto the hardware before launch (Python).

```python
def check_layout(world: int, tp: int, pp: int, cp: int = 1, ep: int = 1,
                 gpus_per_node: int = 8, nodes_per_rack: int = 16) -> list[str]:
    errors = []
    if world % (tp * pp * cp) != 0:
        errors.append(f"world {world} not divisible by tp*pp*cp={tp*pp*cp}")
    dp = world // (tp * pp * cp)
    if tp > gpus_per_node or gpus_per_node % tp:
        errors.append(f"TP={tp} crosses node boundaries (gpus_per_node={gpus_per_node})")   # NVLink only #!
    if ep > 1 and dp * cp % ep:
        errors.append(f"EP={ep} must divide dp*cp={dp*cp}")
    stage_nodes = tp * cp * dp // gpus_per_node                # nodes holding one pipeline stage
    if pp > 1 and stage_nodes > nodes_per_rack:
        errors.append(f"one pipeline stage spans {stage_nodes} nodes: wider than a rack")
    return errors or [f"ok: tp={tp} pp={pp} cp={cp} dp={dp} ep={ep}"]

print(check_layout(world=1024, tp=8, pp=8))      # ok: tp=8 pp=8 cp=1 dp=16 ep=1
print(check_layout(world=1024, tp=16, pp=4))     # TP=16 crosses node boundaries
```

**Going deeper.**

- **The layout is part of the checkpoint question:** Megatron's default distributed-optimizer format (`dp_reshardable`) saves and loads fast but can't change TP or PP. Changing the layout means one save in the `fully_reshardable` format (`--dist-ckpt-optim-fully-reshardable`) and switching back afterwards.
- **Measure before scaling:** run the layout for a few hundred steps on the target topology and compare tokens per second per GPU with the benchmark ([Design 15](#design-eval-infra)) before committing a month of compute.

### Anatomy of a sharded checkpoint

**Scenario.** The old checkpointing code gathered the full state on rank 0 and called `torch.save`. For the 70B model with Adam states (about 1.1 TB), rank 0 ran out of host memory, and the other 1,023 GPUs waited while one process wrote one file.

![Sharded checkpoint](diagrams/designs/concepts/ds20-c6.svg)

![Anatomy of a sharded checkpoint](diagrams/designs/ds20-5.svg)

**What the picture shows.** What a distributed checkpoint contains and who writes what.

- **Sharded state:** each rank writes the shards it owns: its slice of the parameters, the optimizer moments and the master weights. Nothing is gathered.
- **Deduplication:** replicated tensors (for example, the same shard on several data-parallel replicas) are written once. The planner spreads the writing across the replicas so no rank does more than its share.
- **Metadata:** a small file mapping every logical tensor (name, global shape, offsets) to the files and byte ranges holding it. This is what makes resharding possible.
- **Everything else needed to resume exactly:** step, learning-rate scheduler, data-loader position and random-number-generator states, saved as small per-rank or global entries.
- **Completion marker:** written last, after every rank's files and the metadata are durable. A checkpoint without it doesn't exist.

**The code.** A complete, resumable training state with DCP and the `Stateful` protocol (Python).

```python
import torch.distributed.checkpoint as dcp
from torch.distributed.checkpoint.stateful import Stateful
from torch.distributed.checkpoint.state_dict import get_state_dict, set_state_dict

class TrainState(Stateful):
    def __init__(self, model, optim, sched, loader, rng):
        self.model, self.optim, self.sched, self.loader, self.rng = model, optim, sched, loader, rng
        self.step = 0

    def state_dict(self):
        msd, osd = get_state_dict(self.model, self.optim)        # sharded DTensors, no gather #!
        return {"model": msd, "optim": osd, "sched": self.sched.state_dict(),
                "loader": self.loader.state_dict(), "rng": self.rng.state_dict(), "step": self.step}

    def load_state_dict(self, sd):
        set_state_dict(self.model, self.optim, model_state_dict=sd["model"], optim_state_dict=sd["optim"])
        self.sched.load_state_dict(sd["sched"]); self.loader.load_state_dict(sd["loader"])
        self.rng.load_state_dict(sd["rng"]); self.step = sd["step"]

state = TrainState(model, optim, sched, loader, rng)
dcp.save({"train": state}, checkpoint_id=f"/local-nvme/ckpt/step-{state.step:07d}")
```

**Going deeper.**

- **The data loader's position matters:** resuming with the right weights but restarting the data stream replays the same examples. That's a quiet bias, not a crash.
- **Megatron's equivalent:** `--ckpt-format torch_dist` builds on DCP and adds sharded-state metadata for TP, PP, EP and the distributed optimizer, so the same storage layer works for both.

### Fast checkpointing: stall only for the copy

**Scenario.** Even with sharded saving, the 1,024-GPU job paused for 6 minutes per checkpoint because each rank wrote 1.1 GB directly to a network filesystem while training waited.

![Async checkpointing](diagrams/designs/concepts/ds20-c7.svg)

![Fast checkpointing: stall only for the copy](diagrams/designs/ds20-6.svg)

**What the picture shows.** Splitting a save into a short blocking stage and a long background stage.

- **Stage (blocking):** copy the rank's shards from GPU memory to host memory. With a reused pinned-memory buffer this is a direct memory copy at tens of GB/s. For about 1.1 GB per rank it takes about a second.
- **Persist (background):** a separate process writes the host copy to local NVMe. Training continues on the GPUs at the same time.
- **Upload (background):** another background step copies the local checkpoint to object storage and writes the completion marker last.
- **Back-pressure:** at most one save in flight. If the previous save hasn't finished staging when the next is due, wait for it rather than piling up host-memory copies.
- **Process, not thread:** the background writer runs in a separate process, so Python's GIL doesn't slow the training loop.

**The code.** Asynchronous DCP saving with process-based execution and one save in flight (Python).

```python
import torch.distributed.checkpoint as dcp
from torch.distributed.checkpoint.state_dict_saver import AsyncCheckpointerType

class AsyncCheckpointer:
    def __init__(self, local_root: str, uploader):
        self.local_root, self.uploader, self.pending = local_root, uploader, None

    def maybe_save(self, step: int, state, every: int):
        if step % every:
            return
        if self.pending is not None:
            self.pending.result()                                 # back-pressure: one save in flight #!
        path = f"{self.local_root}/step-{step:07d}"
        self.pending = dcp.async_save(
            {"train": state}, checkpoint_id=path,
            async_checkpointer_type=AsyncCheckpointerType.PROCESS,   # writer outside the training process
        )
        self.pending.add_done_callback(lambda f, p=path, s=step: self.uploader.enqueue(p, s))

    def flush(self):                                              # call from the preStop / SIGTERM handler
        if self.pending is not None:
            self.pending.result()
        self.uploader.drain()
```

**Going deeper.**

- **Pinned host memory has a cost:** the staging buffer stays allocated between saves (about the size of the rank's shard). Budget it into the pod's memory request, and make sure the node's page-locked memory limit allows it.
- **Megatron:** `--async-save` with the `torch_dist` format does the same staging and background persist. Newer Megatron Core versions use the NVIDIA Resiliency Extension for the async path.

### Tiered checkpoint storage

**Scenario.** Every checkpoint went straight to a shared filesystem. With 1,024 ranks writing at once, the filesystem's metadata server became the bottleneck. Other teams' jobs slowed down every hour on the hour.

![Checkpoint tiers](diagrams/designs/concepts/ds20-c8.svg)

![Tiered checkpoint storage](diagrams/designs/ds20-7.svg)

**What the picture shows.** Three tiers with different speeds, durability and cost.

- **Tier 0, host memory (optional):** the staged copy, plus, in some designs, a copy in a peer node's memory. Fastest to restore, lost if both nodes fail.
- **Tier 1, local NVMe:** every save lands here first at several GB/s per node. Survives a process crash, lost when the node is replaced.
- **Tier 2, object storage:** every Nth checkpoint (or every checkpoint, uploaded in the background) is copied here with retries and checksums. Durable across node, rack and cluster failures.
- **Retention:** keep the last 2 on NVMe, the last 5 plus daily and milestone checkpoints in object storage, and expire the rest by lifecycle rules.
- **Restore order:** the newest *complete* checkpoint across tiers. Local NVMe if every rank still has its shards there, otherwise object storage.

**The YAML.** Runtime volumes and the uploader sidecar for tiered checkpoints (fragment of the ClusterTrainingRuntime pod template).

```yaml
spec:
  initContainers:
  - name: ckpt-uploader                       # native sidecar: starts first, stops after the trainer
    image: registry.example.com/platform/ckpt-uploader:1.8
    restartPolicy: Always
    args: ["--watch=/local-nvme/ckpt", "--dest=$(CKPT_URI)", "--parallel=32",
           "--marker=COMPLETE", "--keep-local=2"]          # upload shards, then write the marker last #!
    volumeMounts: [{ name: local-nvme, mountPath: /local-nvme }]
  containers:
  - name: node
    volumeMounts:
    - { name: local-nvme, mountPath: /local-nvme }
    - { name: dshm, mountPath: /dev/shm }
    lifecycle:
      preStop: { exec: { command: ["/opt/train/flush-checkpoint.sh"] } }   # finish in-flight save on eviction
  terminationGracePeriodSeconds: 300
  volumes:
  - name: local-nvme
    ephemeral:
      volumeClaimTemplate:
        spec: { storageClassName: local-nvme-raid0, accessModes: [ReadWriteOnce], resources: { requests: { storage: 3Ti } } }
  - name: dshm
    emptyDir: { medium: Memory, sizeLimit: 64Gi }
```

**Going deeper.**

- **Object storage likes many parallel streams:** 1,024 ranks each uploading their own files with multipart uploads use the aggregate bandwidth well. The limits to watch are request rates per prefix, so spread checkpoint keys by rank.
- **Verify before you trust:** the uploader records checksums, and a periodic job loads a sample of checkpoints in a small cluster to prove they restore ([Design 14](#design-reliable-rl-infra)).

### Fast restarts: loading, resharding and the first step

**Scenario.** After a node failure, the 70B job took 38 minutes to take its first step again: 6 minutes rescheduling, 9 minutes pulling a 22 GB image, 18 minutes reading the checkpoint from the shared filesystem, and 5 minutes compiling and warming up.

![Fast restart](diagrams/designs/concepts/ds20-c9.svg)

![Fast restarts: loading, resharding and the first step](diagrams/designs/ds20-8.svg)

**What the picture shows.** The restart timeline before and after, and what removes each part.

- **Rescheduling:** a hot-spare node or a warm reservation means Kueue re-admits the job immediately ([Design 14](#design-reliable-rl-infra)).
- **Image pull:** images pre-pulled on GPU nodes, lazy-loading snapshotters, or a node-local registry mirror. The 22 GB image is already there.
- **Checkpoint read:** the surviving 127 nodes read their shards from local NVMe. Only the replacement node reads from object storage, with many parallel range requests.
- **Peer restore:** where data-parallel replicas hold identical shards, the new node can receive its shard from a peer over the fabric, faster than from object storage.
- **Warm-up:** cached compilation artifacts (for example, a shared torch.compile or Triton cache) cut the first-step compile time.
- **Resharding:** when the job resumes on a different world size, DCP's metadata lets each new rank read the byte ranges it now owns, so no conversion job is needed.

**The code.** Choosing the restore source per rank and resuming on a different world size (Python).

```python
import os, torch, torch.distributed as dist, torch.distributed.checkpoint as dcp

def newest_complete(local_root: str, remote, required_step: int | None = None) -> str:
    """Newest checkpoint that is complete for *every* rank; prefer local NVMe when all ranks have it."""
    local = sorted(p for p in os.listdir(local_root) if p.startswith("step-"))
    have = local[-1] if local else ""
    t = torch.tensor([int(have.split("-")[1]) if have else -1], device="cuda")
    dist.all_reduce(t, op=dist.ReduceOp.MIN)                       # all ranks must have it locally #!
    remote_step = remote.latest_with_marker()
    if t.item() >= 0 and t.item() >= remote_step:
        return f"{local_root}/step-{t.item():07d}"
    return remote.uri(remote_step)                                  # read shards with parallel range GETs

def resume(state, local_root, remote):
    path = newest_complete(local_root, remote)
    dcp.load({"train": state}, checkpoint_id=path)    # works on a new world size: planner reads by offsets
    return path
```

**Going deeper.**

- **Track restart time as a budget:** schedule, pull, read, warm-up, each with a target. The slowest part changes after every fix.
- **Resharding costs read bandwidth:** a world-size change means every rank reads from many files. Prefer object storage with high concurrency over a shared filesystem for this case.

### Fast model loading for evaluation and inference

**Scenario.** Every evaluation job downloaded the 140 GB checkpoint from object storage with a single-threaded client, converted it to Hugging Face format on one CPU, then loaded it. Twenty minutes per job, and 200 evaluation jobs per week.

![Fast model loading](diagrams/designs/concepts/ds20-c10.svg)

![Fast model loading for evaluation and inference](diagrams/designs/ds20-9.svg)

**What the picture shows.** The path from a training checkpoint to a model that serves in under 2 minutes.

- **Export once:** a post-checkpoint job converts milestone checkpoints to safetensors (sharded by tensor-parallel rank if the server needs it) and records the export in the model registry. Evaluation jobs never convert.
- **Stream, don't download:** a model streamer reads tensors from object storage with many concurrent readers straight into host buffers and then GPU memory. In vLLM this is `--load-format runai_streamer`.
- **Or mount from a registry:** weights packaged as an OCI artifact and mounted with an image volume, cached on the node by the container runtime. This is useful for models that many pods load repeatedly.
- **Node-local cache:** a DaemonSet-managed cache on NVMe holds recently used models, so the second job on a node loads from local disk.
- **Pre-sharded weights:** for tensor-parallel servers, each GPU reads only its own shard (`runai_streamer_sharded`) instead of reading everything and slicing.

**The YAML.** An evaluation server that streams pre-sharded weights from object storage (vLLM).

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: eval-70b-step120k, namespace: eval }
spec:
  replicas: 1
  selector: { matchLabels: { app: eval-70b } }
  template:
    metadata: { labels: { app: eval-70b } }
    spec:
      serviceAccountName: eval-reader                     # read-only access to the export bucket
      containers:
      - name: vllm
        image: vllm/vllm-openai:v0.32.0
        args:
        - s3://model-exports/llm-70b/step-120000-tp8
        - --tensor-parallel-size=8
        - --load-format=runai_streamer_sharded             # each GPU streams only its own shard #!
        - --model-loader-extra-config={"concurrency":32}
        resources: { limits: { nvidia.com/gpu: 8 } }
```

**Going deeper.**

- **Loading time is bandwidth arithmetic:** 140 GB in 2 minutes needs about 1.2 GB/s sustained. A single stream rarely does that, and 32 parallel streams from object storage usually do. Measure the node's NIC and the bucket's limits first.
- **Version the export, not just the checkpoint:** the registry records which training checkpoint, which conversion code and which sharding produced each export, so evaluation results trace back to exact weights ([Design 15](#design-eval-infra)).

### Measuring checkpoint and loading performance

**Scenario.** After the async change, the team said checkpoints were "basically free". A month later, a model-size increase doubled host-memory staging time, and nobody noticed for three weeks.

![Measure and tune](diagrams/designs/concepts/ds20-c11.svg)

![Measuring checkpoint and loading performance](diagrams/designs/ds20-10.svg)

**What the picture shows.** The metrics that make checkpointing and loading visible.

- **Checkpoint stall:** training time blocked per save (the stage step plus any back-pressure wait), per job. The SLO is under 30 seconds on the largest job.
- **Background duration:** time to local completion and to durable upload. If upload takes longer than the save interval, saves queue up.
- **Recovery point:** the age of the newest durable checkpoint. It bounds how much work a total failure loses.
- **Restart time:** from failure detection to first step, broken down by phase.
- **Model load time:** from pod start to ready, by model size and load method.
- **Goodput:** productive training time divided by wall-clock time, net of stalls, restarts and lost work ([Design 14](#design-reliable-rl-infra)).

**The code.** Choosing the checkpoint interval from measured stall time and failure rate (Young–Daly) and reporting the expected overhead (Python).

```python
import math

def checkpoint_plan(stall_s: float, mtbf_h: float, restart_s: float) -> dict:
    """stall_s: training time lost per save; mtbf_h: job-level mean time between failures."""
    mtbf_s = mtbf_h * 3600
    interval = math.sqrt(2 * stall_s * mtbf_s)                 # Young–Daly optimum #!
    save_overhead = stall_s / interval
    expected_rework = (interval / 2 + restart_s) / mtbf_s      # half an interval lost per failure, plus restart
    return {"interval_min": round(interval / 60, 1),
            "save_overhead_pct": round(100 * save_overhead, 2),
            "failure_overhead_pct": round(100 * expected_rework, 2),
            "goodput_ceiling_pct": round(100 * (1 - save_overhead - expected_rework), 1)}

print(checkpoint_plan(stall_s=360, mtbf_h=4, restart_s=38 * 60))   # before: sync saves, slow restarts
print(checkpoint_plan(stall_s=20,  mtbf_h=4, restart_s=7 * 60))    # after: async saves, fast restarts
```

**Going deeper.**

- **The interval follows the stall:** with a 6-minute stall the best interval is about 54 minutes. With a 20-second stall it's about 13 minutes, so each failure costs about a quarter as much rework.
- **Alert on trends, not just thresholds:** stall time growing with model size or with host-memory pressure is the early warning.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds20-c12.svg)

- **Phase 1, one launch path (weeks 1–4):** ClusterTrainingRuntimes for torchrun and Megatron, RayJob templates, all through Kueue with topology-aware gangs.
- **Phase 2, sharded and async checkpoints (weeks 4–8):** DCP everywhere, async process-based saves, completion markers, and stall and recovery-point metrics.
- **Phase 3, tiers and restarts (weeks 8–12):** local NVMe tier, the uploader sidecar, restore-from-nearest-tier, pre-pulled images, compile caches and the restart-time breakdown.
- **Phase 4, model loading (weeks 10–14):** export jobs, the model registry, streamed and sharded loading, and node caches for evaluation and inference.

**Trade-offs we accepted.**

- **More host memory per node** for pinned staging buffers.
- **Local NVMe can lose the newest checkpoint** when a node dies before upload. The recovery point is the last durable checkpoint, not the last save.
- **Megatron's fast checkpoint format isn't reshardable.** Changing the layout costs one slower save.
- **Exports duplicate storage:** milestone checkpoints exist in training format and serving format.

**Open questions.**

- **In-memory checkpointing:** how far can peer replication in host memory cut the recovery point before object-storage upload becomes the only durability guarantee?
- **Checkpoint-free recovery:** can fault-tolerant training (replica groups that continue without a failed member) replace most restarts ([Design 14](#design-reliable-rl-infra))?
- **One format for training and serving:** could exports be avoided if serving engines read DCP checkpoints directly?
- **Cross-cluster restarts:** what's the fastest way to resume a job in another cluster or cloud ([Design 18](#design-multicloud-fleet)) when the newest checkpoint is in the wrong region?
