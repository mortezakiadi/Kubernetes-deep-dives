---
id: post-model-data
title: "Getting model weights into pods"
short: "Getting model weights into pods"
---
LLM weights range from a few GB (Mistral 7B is about 14 GB) to nearly a terabyte (Llama 3.1 405B is about 750 GB). Moving that much data into pods quickly and cheaply is a storage, network and startup-time problem. This post covers the formats, the registries, and four ways to get weights to a model server.

### What's in a model file

- **Weight-only formats** (PyTorch `.pt`/`.pth`, TensorFlow checkpoints, NumPy) need the original code to rebuild the network.
- **"Mostly self-contained" formats** carry more:
  - **Safetensors** is safe to load (unlike pickle, it can't execute code), memory-maps tensors without copies, and shards large models into many files with an index.
  - **GGUF** packs quantized weights plus tokenizer data into one file for llama.cpp and vLLM.
  - **ONNX** carries the graph but not the tokenizer.

None is fully self-contained yet. `config.json` and `tokenizer.json` still travel alongside the weights.
→ See [The build pipeline](#d13-2).

### Model registries

A registry tracks versions, metadata, lineage, stage (experiment → staging → production) and access, but usually stores only *pointers* to the weights in object storage. Common choices are the Hugging Face Hub (public), MLflow, the Kubeflow Model Registry, and plain OCI registries.

### Four ways to get weights into a pod

| Approach | How | Good for | Watch out for |
|---|---|---|---|
| Init-container copy | Download (S3, HTTP, HF) into an `emptyDir` before the server starts | Fastest reads, single replica | Slow startup, one copy per pod per node |
| Shared PersistentVolume | One copy on NFS/EFS/Ceph, mounted `ReadOnlyMany` by every replica | Many replicas, models managed outside the cluster | Every read crosses the network; struggles at hundreds of replicas |
| Modelcar | Model in an OCI image, run as a sidecar; the server reads it through a `/proc` symlink | Fine-tuned models sharing base layers | Needs `shareProcessNamespace` (a security concern), startup races |
| OCI image volume | Mount the model image directly as a read-only volume (beta in Kubernetes 1.35) | The clean, native future | Feature gates and runtime versions still required |

→ See [StorageClass, PV and PVC](#d14-1), [Storage and StatefulSet mechanics](#d08-2) and [Storage (cost)](#d12-3).

### Why OCI images fit models well

Container images are layered, cached per node and content-addressed. Put a base model in one layer and LoRA adapters on top, and ten fine-tuned variants share one copy of the base on each node. The CNCF **ModelPack** spec is standardizing this. Pre-pulling or prefetching model images keeps scale-up predictable.
→ See [Networking (cost)](#d12-2).

### My rule of thumb

Pick the access method per tier. Use PersistentVolumes in development for easy updates and OCI images in production for speed and repeatability. Always measure *time from pod scheduled to model ready*, because that is where large-model autoscaling breaks down.
