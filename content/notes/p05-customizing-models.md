---
id: post-customizing
title: "Customizing models: prompts, RAG, LoRA and training jobs"
short: "Customizing models"
---
Almost nobody trains an LLM from scratch. Pre-training takes thousands of GPUs for weeks, and alignment then teaches the base model to follow instructions safely. What platform teams actually run is **post-training**: adapting an already aligned model to their domain. The first question is whether you need to train at all.

### 1. Try the cheap options first

- **Prompt engineering:** a system prompt that sets the scenario, the role and the boundaries. It is not a security control, because prompt injection can bypass it.
- **Context engineering and RAG:** fetch relevant documents at request time and put them in the prompt. Update the knowledge base in minutes, with no training job.

Training becomes worth it when large, slow-changing knowledge would otherwise ride along in every prompt. Long contexts cost GPU memory on every request, so a bank might bake lending and risk knowledge into a model and keep RAG for fresh facts. A small, specialized model (8–16B parameters), possibly **distilled** from a large one, can match a big general model at a fraction of the serving cost.

### 2. Full fine-tuning vs. LoRA

- **Full fine-tuning** updates every weight. It needs hundreds of thousands of labelled examples and many GPUs, risks "catastrophic forgetting", and produces a separate model that needs its own GPUs to serve.
- **Parameter-efficient fine-tuning (PEFT)**, mainly **LoRA**, freezes the base model and trains small adapter matrices, often under 1% of the parameters. It needs only 100–1,000 examples and finishes in hours. At serving time one base model can host many adapters (vLLM loads them side by side), which is why [LoRA-aware routing](#d09-4) matters. QLoRA adds quantization to cut training memory further.

Hugging Face TRL's `SFTTrainer` handles both, and newer methods (DPO, GRPO, reward modelling) show up as multi-step pipelines with several GPU components.

### 3. Running tuning jobs on Kubernetes

- Even small fine-tunes usually span several GPUs and often several nodes. Sharded training (PyTorch FSDP2) moves weights between GPUs on every layer, so the [network](#d10-4) becomes the bottleneck.
- **Kubeflow Trainer** separates the roles:
  - the platform team defines `TrainingRuntime`s (PyTorch, DeepSpeed, MLX, MPI);
  - data scientists submit `TrainJob`s from Python, where their function is serialized into the job, so there is no image rebuild per experiment;
  - the controller turns them into a **JobSet**.
- Alternatives: **Ray/KubeRay** (`RayJob` on a RayCluster; code ships in images or repos), **DeepSpeed** as a training library, and **Unsloth** for fast single-GPU fine-tuning.
- Checkpoints must go to persistent storage, and multi-node jobs need [gang scheduling](#d02-2) and [quota](#d02-4).
