---
id: post-serving
title: "Serving LLMs on Kubernetes"
short: "Serving LLMs on Kubernetes"
---
Before the deep dives, here's the workload most of them end up hosting: a large language model behind an API. This post covers what it takes to go from a model on a laptop to a production endpoint on Kubernetes.

### From a Python script to a model server

Loading a model with the Transformers library and calling it works for a demo, but it is one process, one hard-coded prompt and no API. Wrapping it in FastAPI is the next step, and at that point you are rebuilding a **model server**: a runtime that loads the model, exposes it over REST or gRPC, and is tuned for throughput and latency. Memory is the first constraint: at 16-bit precision, a 7B model's weights alone take about 14–15 GB and a 70B model's about 140 GB, plus room for the KV cache.

### The serving runtimes worth knowing

- **vLLM:** the most widely used open-source server, with an OpenAI-compatible API, PagedAttention and continuous batching.
- **TGI (Hugging Face):** similar, and can switch backends (CUDA, TensorRT-LLM, llama.cpp, AWS Neuron for Trainium/Inferentia).
- **llama.cpp:** for laptops and edge devices; it is where the GGUF format comes from.
- **NVIDIA NIM:** curated, pre-optimized images per model family, with a model cache on a PersistentVolume.
- **SGLang:** strong at reusing the KV cache across requests that share a prompt prefix (agents, multi-turn chats).

A platform team doesn't need to master every inference optimization (quantization, speculative decoding, FlashAttention). Pick an actively maintained server and watch the settings that change the *topology*: multi-GPU and multi-node serving make the deployment stateful and need extra coordination.

### Deploying by hand

A plain Deployment for vLLM already needs a GPU limit, a toleration for tainted GPU nodes, a Secret for gated models, and a PVC as a model cache so a restart doesn't re-download 100 GB. Multiply that by every model and the case for a controller is obvious.
→ See [The NVIDIA device plugin](#d06-1).

### KServe: separate the runtime from the model

KServe splits **ServingRuntime** (the model server image and defaults, owned by the platform team) from **InferenceService** (one model, owned by data scientists), so runtime upgrades and model releases can move at their own pace.

- **Standard mode** creates plain Deployments, which is the usual choice for large LLMs.
- **Knative mode** adds scale-to-zero.
- **ModelMesh** packs many small models together.

The newer **LLMInferenceService** adds KV-cache-aware routing, disaggregated serving and tensor/data/expert parallelism for 7B–405B models.
→ See [Workloads and node tiers](#d03-1) and [Placement, autoscaling and cost](#d03-3).

### Ray Serve and KubeRay

Ray defines its own cluster (a head node plus workers) and its own scheduling, and KubeRay's **RayService** maps it onto Kubernetes. Python teams get full control from code, at the cost of a second orchestration layer on top of Kubernetes.
→ See [How a Pod finds its scheduler](#d02-1).
