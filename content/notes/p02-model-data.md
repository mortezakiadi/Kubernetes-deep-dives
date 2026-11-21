---
id: post-model-data
title: "Getting model weights into pods"
short: "Getting model weights into pods"
---
LLM weights range from a few GB (Mistral 7B is about 14 GB) to nearly a terabyte (Llama 3.1 405B is about 800 GB at 16-bit). Moving that much data into pods quickly and cheaply is a storage, network and startup-time problem. This post covers the formats, the registries, four ways to get weights to a model server with a working example of each, and how to measure whether it's fast enough.

### What's in a model file

**Scenario.** A team downloads a model from Hugging Face and finds 30 files: four `.safetensors` shards, an index, a `config.json`, two tokenizer files and a README. They copy only the shards into their image, and the server fails at startup. Knowing which files a server needs, and which format is safe, comes before any storage decision.

![What's in a model file](diagrams/posts/p02-1.svg)

**What the picture shows.** A typical model directory, and how the common formats compare.

- **Weight shards:** large models are split into many `model-0000N-of-0000M.safetensors` files, with `model.safetensors.index.json` mapping each tensor to its shard.
- **`config.json`:** the architecture (layers, hidden size, attention heads). The server needs it to build the network before loading weights.
- **Tokenizer files:** `tokenizer.json` and `tokenizer_config.json` (including the chat template). Without them the server can't turn text into tokens.
- **Weight-only formats** (PyTorch `.pt`/`.pth`, TensorFlow checkpoints, NumPy) need the original code to rebuild the network. PyTorch's pickle-based files can also execute code when loaded.
- **"Mostly self-contained" formats:** **Safetensors** is safe to load (it can't execute code), memory-maps tensors without copies and shards large models. **GGUF** packs quantized weights plus tokenizer data into one file for llama.cpp (vLLM can load it too). **ONNX** carries the graph but not the tokenizer.

**The code.** Download only the files a server needs, and check the size before you plan storage.

```python
from huggingface_hub import HfApi, snapshot_download

repo = "Qwen/Qwen2.5-7B-Instruct"
info = HfApi().model_info(repo, files_metadata=True)
total = sum(f.size or 0 for f in info.siblings if f.rfilename.endswith(".safetensors"))
print(f"weights: {total / 1e9:.1f} GB")            # plan PVC and image sizes from this

path = snapshot_download(
    repo,
    allow_patterns=["*.safetensors", "*.json", "tokenizer*"],   # skip READMEs, .bin duplicates
    local_dir="/models/qwen2.5-7b",
)
```

**Going deeper.**

- **None of the formats is fully self-contained yet:** `config.json` and the tokenizer files always travel with the weights. Treat the whole directory as the unit you version and copy.
- **Refuse pickle in production.** Only load Safetensors (or GGUF) from sources you don't control, and scan model artifacts in the same pipeline as images. See [The build pipeline](#d13-2).

### Model registries

**Scenario.** Three fine-tuned versions of the same model are in an S3 bucket called `final`, `final-v2` and `final-v2-really`. Nobody can say which one is in production, which dataset trained it or whether it passed evaluation.

![Model registries](diagrams/posts/p02-2.svg)

**What the picture shows.** What a registry stores, and what it only points to.

- **Metadata in the registry:** name, version, owner, training job and dataset (lineage), evaluation results and the stage or alias (`candidate`, `staging`, `production`).
- **Weights in object storage:** the registry usually stores only a *pointer* (an S3 URI or an OCI reference) to the files, not the files themselves.
- **Deployments refer to an alias, not a path:** "the production version of `support-assistant`", resolved to a concrete URI at deploy time, so promotion is a registry change, not a manifest edit.
- **Common choices:** the Hugging Face Hub (public models), MLflow, the Kubeflow Model Registry, and plain OCI registries when models are packaged as images.

**The code.** Register a version with its lineage, and promote it by moving an alias (MLflow shown).

```python
import mlflow
from mlflow import MlflowClient

client = MlflowClient()
mv = mlflow.register_model(
    model_uri="s3://models/support-assistant/run-0412/",       # pointer, not a copy #!
    name="support-assistant",
    tags={"train_job": "llm-sft-0412", "dataset": "tickets-2026-10", "base": "Qwen2.5-7B"},
)
client.set_model_version_tag("support-assistant", mv.version, "eval.multilingual", "pass")
client.set_registered_model_alias("support-assistant", "production", mv.version)   # promotion
```

**Going deeper.**

- **Promote through Git as well as the registry:** the alias tells you which version is "production"; a reviewed change to the serving manifest makes it so. Both together give you an audit trail and a rollback.
- **Keep evaluation results next to the version,** so "did this model pass?" is answered by the registry, not by someone's notebook.

### Four ways to get weights into a pod

**Scenario.** The serving team has one model with 2 replicas in development and another with 60 replicas in production that scales up every morning. One approach can't be right for both.

![Four ways to get weights into a pod](diagrams/posts/p02-3.svg)

**What the picture shows.** Four paths from storage to the model server, and where each one pays its cost.

- **Init-container copy:** download from S3, HTTP or Hugging Face into an `emptyDir` before the server starts. Fastest reads once loaded, but every pod downloads its own copy, every time.
- **Shared PersistentVolume:** one copy on a network file system (EFS, FSx, NFS, Ceph), mounted read-only by every replica. Easy to update, but every load crosses the network, and hundreds of replicas starting at once can saturate it.
- **Modelcar:** the model packaged in an OCI image and run as a sidecar; the server reads it through a `/proc` symlink. Image caching per node, but it needs `shareProcessNamespace` and has startup races.
- **OCI image volume:** mount the model image directly as a read-only volume. Image caching per node, no sidecar. On by default since Kubernetes 1.35.

| Approach | Good for | Watch out for |
|---|---|---|
| Init-container copy | Fastest reads, single replica | Slow startup, one copy per pod per node |
| Shared PersistentVolume | Many replicas, models managed outside the cluster | Network reads; struggles at hundreds of replicas |
| Modelcar | Fine-tuned models sharing base layers | `shareProcessNamespace` (a security concern), startup races |
| OCI image volume | The clean, native path | Needs a recent kubelet and container runtime |

**Going deeper.**

- **The four YAML examples below** are each the minimum for that pattern, ready to adapt. See [StorageClass, PV and PVC](#d14-1), [Storage and StatefulSet mechanics](#d08-2) and [Storage (cost)](#d12-3) for the storage background.

### Pattern 1 and 2: init-container copy and a shared volume

**Scenario.** In development, the team wants to try a new model version by changing one line, without building anything. A download at startup, or a shared volume they can copy files onto, is the simplest path.

**Init-container copy.** An init container downloads the model from S3 into an `emptyDir`; the server starts only when the copy is complete.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: qwen-7b-dev
spec:
  replicas: 1
  selector: { matchLabels: { app: qwen-7b-dev } }
  template:
    metadata:
      labels: { app: qwen-7b-dev }
    spec:
      serviceAccountName: model-reader          # Pod Identity role: read s3://models/
      initContainers:
      - name: fetch-model
        image: amazon/aws-cli:2.31.0
        args: ["s3", "sync", "s3://models/qwen2.5-7b/", "/models/qwen2.5-7b/"]   #!
        volumeMounts: [ { name: models, mountPath: /models } ]
      containers:
      - name: vllm
        image: vllm/vllm-openai:v0.23.0
        args: ["--model", "/models/qwen2.5-7b"]
        resources: { limits: { nvidia.com/gpu: 1 } }
        volumeMounts: [ { name: models, mountPath: /models, readOnly: true } ]
      volumes:
      - name: models
        emptyDir: { sizeLimit: 40Gi }           # node-local disk, gone with the pod #!
```

**Shared volume.** One copy on EFS, mounted `ReadOnlyMany` by every replica.

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: models-shared
spec:
  accessModes: ["ReadOnlyMany"]                 # every replica mounts the same copy #!
  storageClassName: efs-sc
  resources: { requests: { storage: 500Gi } }
---
# in the Deployment's pod spec:
#   containers[0].args: ["--model", "/models/qwen2.5-7b"]
#   volumes:
#   - name: models
#     persistentVolumeClaim: { claimName: models-shared, readOnly: true }
```

**Going deeper.**

- **Throughput, not capacity, is the limit of shared volumes.** Twenty replicas each reading 15 GB at startup is 300 GB through one file system. Use a file system with enough provisioned throughput (FSx for Lustre for large fleets), or stagger rollouts.
- **The init-container pattern pays the download on every restart.** Add a node-local cache (a `hostPath` or local PV per node) only if you accept the operational cost of managing it.
- **KServe's node-local cache does that management for you, with limits.** It is off until an admin enables it. A node group names the nodes and their local disk, and a LocalModelCache names the model, which KServe then downloads to every node in the group ahead of time. An InferenceService uses the copy only when its storage URI matches the cache's source URI exactly. The cache removes the download, not the time to load weights into the GPU ([KServe model cache](https://kserve.github.io/website/docs/model-serving/generative-inference/modelcache/localmodel)). For LLMInferenceService, KServe 0.21 added cached LoRA adapters ([0.21 release](https://kserve.github.io/website/blog/kserve-0.21-release)).

### Pattern 3 and 4: models as OCI images

**Scenario.** In production, ten fine-tuned variants of the same 7B base model run on the same nodes. Copying the full base model for each variant wastes 140 GB per node and minutes per startup. Images already solve "share layers, cache per node, version by digest".

![Pattern 3 and 4: models as OCI images](diagrams/posts/p02-4.svg)

**What the picture shows.** Why model images fit the problem.

- **Layers are content-addressed:** the base model is one layer; each LoRA adapter is a small layer on top. Ten variants share one copy of the base on each node.
- **Cached per node:** the kubelet pulls a layer once per node; the next pod on that node starts without downloading it.
- **Versioned by digest:** the deployment names an exact model by its digest, so what runs is exactly what passed evaluation.
- **Image volume (pattern 4):** the model image is mounted read-only into the pod; the server reads files directly from it. No sidecar, no shared process namespace.
- **Modelcar (pattern 3):** for older clusters or runtimes without image volume support, KServe runs the model image as a sidecar and links its files into the server container.

**The YAML.** An image volume, and the KServe modelcar equivalent.

```yaml
# Pattern 4: OCI image volume (Kubernetes 1.35+)
apiVersion: apps/v1
kind: Deployment
metadata:
  name: support-assistant
spec:
  replicas: 6
  selector: { matchLabels: { app: support-assistant } }
  template:
    metadata:
      labels: { app: support-assistant }
    spec:
      containers:
      - name: vllm
        image: vllm/vllm-openai:v0.23.0
        args: ["--model", "/models"]
        resources: { limits: { nvidia.com/gpu: 1 } }
        volumeMounts:
        - { name: model, mountPath: /models, readOnly: true }
      volumes:
      - name: model
        image:                                   # the model is an image #!
          reference: registry.example.com/models/support-assistant@sha256:4f1c...   #!
          pullPolicy: IfNotPresent
---
# Pattern 3: KServe modelcar (works where image volumes aren't available)
apiVersion: serving.kserve.io/v1beta1
kind: InferenceService
metadata:
  name: support-assistant
spec:
  predictor:
    model:
      modelFormat: { name: huggingface }
      storageUri: oci://registry.example.com/models/support-assistant:2026.11.1   #!
      resources: { limits: { nvidia.com/gpu: "1" } }
```

**Going deeper.**

- **Build model images with one file per layer group:** base weights in one layer, adapters and config in later layers, so a new fine-tune only adds a small layer. The CNCF **ModelPack** specification is standardizing how models are packaged as OCI artifacts.
- **Pre-pull for predictable scale-up:** a DaemonSet that pulls the production model image on every GPU node (or a node image with it baked in) takes the download out of the scale-up path. See [Networking (cost)](#d12-2).
- **Large images need registry and node tuning:** parallel layer pulls, enough root volume on GPU nodes, and a registry or pull-through cache in the same region.

### Measure time to ready

**Scenario.** Autoscaling adds replicas at 9 a.m., but they're ready at 9:14, after the peak has passed. Nobody knew where the 14 minutes went, so nobody could fix them.

![Measure time to ready](diagrams/posts/p02-5.svg)

**What the picture shows.** The stages between "pod created" and "serving traffic", and which of the patterns above shortens each.

- **Node:** if no GPU node is free, Karpenter launches one. Minutes. Fixed by warm capacity, not by storage.
- **Server image pull:** the vLLM image is several GB. Fixed by pre-pulling or a node image that includes it.
- **Weights:** download (init copy), network reads (shared volume) or a local image layer (image volume). Usually the largest stage for big models.
- **Load into GPU memory:** reading weights from disk into the GPU, then building the KV cache. Faster from local NVMe than from network storage.
- **Warm-up:** compiling kernels and capturing CUDA graphs. Can be reduced with cached compilation artifacts.

**The YAML.** A recording rule that turns pod conditions into a "time to ready" metric per model (using kube-state-metrics).

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: model-startup
spec:
  groups:
  - name: model-startup
    rules:
    - record: model:pod_time_to_ready_seconds
      expr: |
        kube_pod_status_ready_time{namespace="models"}
          - on (namespace, pod) kube_pod_created{namespace="models"}   #!
```

**Going deeper.**

- **My rule of thumb:** pick the access method per tier. Use PersistentVolumes in development for easy updates and OCI images in production for speed and repeatability. Always measure *time from pod scheduled to model ready*, because that is where large-model autoscaling breaks down.
- **Track the stages separately** (node launch, image pull from kubelet events, model load from the server's own log or metrics), so you know which fix to apply.
