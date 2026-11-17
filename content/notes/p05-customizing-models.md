---
id: post-customizing
title: "Customizing models: prompts, RAG, LoRA and training jobs"
short: "Customizing models"
---
Almost nobody trains an LLM from scratch. Pre-training takes thousands of GPUs for weeks, and alignment then teaches the base model to follow instructions safely. What platform teams actually run is **post-training**: adapting an already aligned model to their domain. This post covers when training is worth it, full fine-tuning versus LoRA, serving many adapters, and running tuning jobs on Kubernetes.

### Try the cheap options first

**Scenario.** A product team asks for "our own model" so the assistant knows the company's products. Two weeks and a GPU budget later, the fine-tuned model knows last quarter's products and nothing about this quarter's. A retrieval step would have done the job in a day and stayed current.

![Try the cheap options first](diagrams/posts/p05-1.svg)

**What the picture shows.** The ladder of options, from cheapest and fastest to change, to most expensive.

- **Prompt engineering:** a system prompt that sets the scenario, the role and the boundaries. Minutes to change. It isn't a security control, because prompt injection can bypass it.
- **Context engineering and RAG:** fetch relevant documents at request time and put them in the prompt. Update the knowledge base in minutes, with no training job. See [RAG and agents on Kubernetes](#post-ai-apps).
- **LoRA fine-tuning:** teach the model a style, a format or a stable body of domain knowledge with small adapter weights. Hours of training.
- **Full fine-tuning:** change every weight, for large shifts in behaviour. Days of training and its own serving GPUs.
- **Pre-training:** thousands of GPUs for weeks. Not a platform team's job.

**Going deeper.**

- **Training becomes worth it when large, slow-changing knowledge would otherwise ride along in every prompt.** Long contexts cost GPU memory on every request, so a bank might bake lending and risk knowledge into a model and keep RAG for fresh facts.
- **Smaller and specialized often beats bigger and general:** a small model (8–16B parameters), possibly **distilled** from a large one, can match a big general model on one task at a fraction of the serving cost.

### Full fine-tuning vs. LoRA

**Scenario.** The team has 800 labelled examples of support replies in the company's tone. A full fine-tune of a 7B model needs several large GPUs, far more examples than they have, and produces a second 15 GB model to serve. They need something smaller.

![Full fine-tuning vs. LoRA](diagrams/posts/p05-2.svg)

**What the picture shows.** What each method trains, and what it costs.

- **Full fine-tuning** updates every weight. It needs hundreds of thousands of labelled examples and many GPUs (the optimizer state alone is several times the model size), risks "catastrophic forgetting" of general skills, and produces a separate full model that needs its own GPUs to serve.
- **LoRA (low-rank adaptation)** freezes the base model and trains two small matrices per adapted layer, often under 1% of the parameters. It needs only 100–1,000 good examples and finishes in hours on one or a few GPUs.
- **The adapter is small:** tens to a few hundred MB, stored and versioned separately from the base model.
- **QLoRA** loads the frozen base model in 4 bits during training, which cuts training memory further so larger models fit on smaller GPUs.
- **Newer methods** (DPO for preferences, GRPO, reward modelling) show up as multi-step pipelines with several GPU components, not a single job.

**The code.** A LoRA fine-tune with Hugging Face PEFT and TRL's `SFTTrainer`.

```python
from datasets import load_dataset
from peft import LoraConfig
from trl import SFTConfig, SFTTrainer

dataset = load_dataset("json", data_files="/data/support-replies.jsonl", split="train")  # 800 chats

peft_config = LoraConfig(
    r=16,                                   # rank: size of the adapter matrices #!
    lora_alpha=32,
    lora_dropout=0.05,
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],   # attention projections
    task_type="CAUSAL_LM",
)

trainer = SFTTrainer(
    model="Qwen/Qwen2.5-7B-Instruct",       # base weights stay frozen #!
    train_dataset=dataset,
    peft_config=peft_config,
    args=SFTConfig(
        output_dir="/checkpoints/support-tone",   # on a PVC: survives restarts
        num_train_epochs=3,
        per_device_train_batch_size=4,
        gradient_accumulation_steps=4,
        learning_rate=2e-4,
        bf16=True,
        save_steps=100,
    ),
)
trainer.train()
trainer.save_model("/checkpoints/support-tone/final")   # a ~100 MB adapter, not a 15 GB model
```

**Going deeper.**

- **Data quality beats quantity for LoRA.** 500 clean, representative examples usually beat 5,000 noisy ones. Hold out a test set, and evaluate the adapter with the same pipeline as any model (see [Running LLMs in production](#post-production)).
- **Merge or keep separate?** An adapter can be merged into the base weights for a single-purpose deployment. Keeping it separate lets one base model serve many adapters, which is the next section.

### Serving many adapters on one base model

**Scenario.** Ten teams each fine-tuned an adapter on the same 7B base model. Serving each as its own full model would take ten GPUs that sit mostly idle. They want all ten on two replicas.

![Serving many adapters on one base model](diagrams/posts/p05-3.svg)

**What the picture shows.** One base model in GPU memory, many adapters loaded beside it, and routing that knows which replica has which adapter.

- **One copy of the base weights** per replica, shared by every adapter.
- **Adapters loaded side by side:** vLLM keeps several adapters in GPU memory and applies the right one per request; the request's `model` field names the adapter.
- **Batching across adapters:** requests for different adapters run in the same batch, so the GPU stays busy even when each team's traffic is small.
- **LoRA-aware routing:** when there are more adapters than fit on one replica, the gateway sends each request to a replica that already has its adapter loaded. See [kube-proxy: iptables vs. IPVS](#d09-4) for how inference-aware routing works.

**The YAML.** A vLLM server with LoRA enabled and two adapters registered at startup (container arguments).

```yaml
args:
- --model=Qwen/Qwen2.5-7B-Instruct
- --enable-lora                                         #!
- --max-loras=8                                         # adapters in GPU memory at once
- --max-lora-rank=16
- --lora-modules
- support-tone=/adapters/support-tone                   # name=path; request with model="support-tone" #!
- legal-summary=/adapters/legal-summary
```

**Going deeper.**

- **Adapters are just files.** Package them as small OCI image layers on top of the base model, so nodes cache the base once and pull only the adapters (see [Getting model weights into pods](#post-model-data)).
- **Watch the rank and count limits:** `max-loras` and `max-lora-rank` reserve GPU memory, which comes out of the KV cache.

### Running tuning jobs with Kubeflow Trainer

**Scenario.** Data scientists submit fine-tuning jobs by SSHing into a GPU box and running `torchrun` by hand. Jobs collide, nobody knows who's using which GPU, and a crashed run is just lost. The platform team wants jobs submitted from Python, with GPUs, networking and checkpoints handled by the platform.

![Running tuning jobs with Kubeflow Trainer](diagrams/posts/p05-4.svg)

**What the picture shows.** Who owns what, and what runs.

- **The platform team defines `TrainingRuntime`s** (or cluster-wide `ClusterTrainingRuntime`s) for PyTorch, DeepSpeed, MLX and MPI: images, network settings, node placement, how the distributed environment is set up.
- **Data scientists submit `TrainJob`s** from Python with the Kubeflow SDK. Their training function is serialized into the job, so there's no image rebuild per experiment.
- **The controller turns each TrainJob into a JobSet,** with one pod per node and the distributed environment (ranks, master address) set up for `torchrun`.
- **Kueue admits the whole job or none of it,** so a 4-node job never starts with 3 nodes. See [Gang scheduling](#d02-2) and [Queueing and fair sharing](#d02-4).
- **Checkpoints go to persistent storage,** so a preempted or failed job resumes instead of starting over.

**The code.** The same job from the Python SDK, and as YAML.

```python
from kubeflow.trainer import TrainerClient, CustomTrainer

def fine_tune():
    # runs on every node under torchrun; imports inside the function
    import torch.distributed as dist
    from trl import SFTConfig, SFTTrainer
    ...                                   # the LoRA code from the previous section

client = TrainerClient()
job = client.train(
    runtime=client.get_runtime("torch-distributed"),     # defined by the platform team #!
    trainer=CustomTrainer(
        func=fine_tune,                                  # serialized into the job, no image build #!
        num_nodes=2,
        resources_per_node={"gpu": 4, "cpu": 32, "memory": "256Gi"},
        packages_to_install=["trl", "peft"],
    ),
)
client.wait_for_job_status(job)
for line in client.get_job_logs(job, follow=True):
    print(line)
```

```yaml
apiVersion: trainer.kubeflow.org/v1alpha1
kind: TrainJob
metadata:
  name: support-tone-lora
  namespace: team-support
  labels:
    kueue.x-k8s.io/queue-name: training          # all-or-nothing admission #!
spec:
  runtimeRef:
    name: torch-distributed
  trainer:
    image: registry.example.com/train/sft:2026.11
    numNodes: 2
    resourcesPerNode:
      limits: { nvidia.com/gpu: 4 }
    command: ["torchrun", "train.py", "--resume-from", "latest"]
```

**Going deeper.**

- **The runtime is where platform expertise lives:** EFA and NCCL settings, shared memory, the checkpoint path and metrics are defined once and tested, not copied into every job. See [Kubeflow Trainer](#d02-6).
- **Alternatives:** **Ray/KubeRay** (`RayJob` on a RayCluster; code ships in images or repositories), **DeepSpeed** as a training library inside any of these, and **Unsloth** for fast single-GPU fine-tuning.

### Data, network and checkpoints

**Scenario.** A 2-node fine-tune runs at half the expected speed. The GPUs aren't the bottleneck: sharded training moves weights between nodes on every layer, and the pods are talking over the ordinary pod network instead of the fast interconnect.

![Data, network and checkpoints](diagrams/posts/p05-5.svg)

**What the picture shows.** The three data paths of a multi-node tuning job.

- **Data in:** the dataset and base model are loaded at startup, ideally from storage in the same region (S3, FSx or a model image). An initializer step can download them once for all nodes.
- **GPU to GPU:** sharded training (PyTorch FSDP2) gathers and scatters weights between GPUs on every layer. Inside a node that's NVLink; between nodes it needs a fast network (EFA on AWS), or the [network](#d10-4) becomes the bottleneck.
- **Checkpoints out:** saved every N steps to a PersistentVolume or object storage, so a restart resumes from the last one.
- **The result:** the final adapter or model goes to the model registry with its training metadata.

**The YAML.** A checkpoint volume shared by the job's pods (in the TrainJob's pod template overrides or the runtime).

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: support-tone-checkpoints
  namespace: team-support
spec:
  accessModes: ["ReadWriteMany"]              # every rank writes its shard #!
  storageClassName: fsx-lustre
  resources: { requests: { storage: 200Gi } }
```

**Going deeper.**

- **Rank 0 often writes everything** in simple training scripts. For larger jobs, use sharded checkpoints so every rank writes its own part in parallel.
- **Clean up:** checkpoints of finished experiments add up fast. Keep the final one and the best one, and expire the rest.
