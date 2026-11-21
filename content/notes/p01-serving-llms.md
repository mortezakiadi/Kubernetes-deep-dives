---
id: post-serving
title: "Serving LLMs on Kubernetes"
short: "Serving LLMs on Kubernetes"
---
Before the deep dives, here's the workload most of them end up hosting: a large language model behind an API. This post covers what it takes to go from a model on a laptop to a production endpoint on Kubernetes: what a model server does, which servers matter, how to deploy one by hand, and the two controllers (KServe and KubeRay) that take the manual work away.

### From a Python script to a model server

**Scenario.** A data scientist shows a demo: twenty lines of Python that load a model with the Transformers library and answer a question. The product team wants it behind an API by Friday, for 200 concurrent users. The script answers one request at a time, holds the whole GPU while it does, and has no API, no metrics and no way to scale.

![From a Python script to a model server](diagrams/posts/p01-1.svg)

**What the picture shows.** The three stages most teams go through, and what each one adds.

- **A script:** loads the model, runs one hard-coded prompt, exits. Fine for a demo; one process, no API.
- **The script wrapped in FastAPI:** now it has an endpoint, but it still handles one request at a time on the GPU, and every new feature (streaming, batching, metrics) is code you write yourself. At this point you're rebuilding a model server.
- **A model server:** a runtime built for this job. It loads the model once, exposes it over an HTTP API (usually OpenAI-compatible), and keeps the GPU busy with techniques such as **continuous batching** (new requests join a running batch between tokens) and **paged KV-cache memory** (cache memory allocated in small blocks, so many requests fit at once).
- **Memory decides what fits:** at 16-bit precision every billion parameters needs about 2 GB for the weights alone. A 7B model's weights take about 14–15 GB, a 70B model's about 140 GB, and the KV cache for active requests comes on top.

**The code.** The demo, and the same model behind a model server. The client code doesn't change when the server scales from one GPU to many replicas.

```python
# The demo: one process, one prompt, no API
from transformers import pipeline

generate = pipeline("text-generation", model="Qwen/Qwen2.5-7B-Instruct", device_map="auto")
print(generate("Explain Kubernetes in one sentence.", max_new_tokens=60)[0]["generated_text"])

# The same model behind a model server (started with: vllm serve Qwen/Qwen2.5-7B-Instruct)
from openai import OpenAI

client = OpenAI(base_url="http://qwen-7b.models.svc:8000/v1", api_key="unused")
reply = client.chat.completions.create(
    model="Qwen/Qwen2.5-7B-Instruct",
    messages=[{"role": "user", "content": "Explain Kubernetes in one sentence."}],
    stream=True,                                   # tokens arrive as they're generated
)
for chunk in reply:
    print(chunk.choices[0].delta.content or "", end="")
```

**Going deeper.**

- **Throughput comes from batching, latency from the prompt and output length.** A model server serving one user and fifty users uses almost the same GPU time per token step, which is why batching matters so much for cost.
- **OpenAI-compatible APIs are the de facto standard.** Most servers expose `/v1/chat/completions`, so applications, gateways and benchmarks work across servers without changes.

### The serving runtimes worth knowing

**Scenario.** Three teams each picked a different model server for their first project. The platform team now has to support all of them, and wants a short list of servers it will run, with a clear reason for each.

![The serving runtimes worth knowing](diagrams/posts/p01-2.svg)

**What the picture shows.** Five servers, what each is best at, and where it fits.

- **vLLM:** the most widely used open-source server, with an OpenAI-compatible API, PagedAttention and continuous batching, prefix caching, LoRA adapters and multi-GPU and multi-node serving. The default choice.
- **TGI (Hugging Face Text Generation Inference):** similar features, and it can switch backends (CUDA, TensorRT-LLM, llama.cpp, AWS Neuron for Trainium and Inferentia).
- **llama.cpp:** for laptops and edge devices, and CPU-only serving; it's where the GGUF format comes from.
- **NVIDIA NIM:** curated, pre-optimized images per model family, with a model cache on a PersistentVolume. You trade flexibility for a tested, supported stack.
- **SGLang:** strong at reusing the KV cache across requests that share a prompt prefix, which suits agents and multi-turn chats.

**Going deeper.**

- **A platform team doesn't need to master every optimization** (quantization, speculative decoding, FlashAttention). Pick an actively maintained server, and watch the settings that change the *topology*: multi-GPU and multi-node serving make the deployment stateful and need extra coordination (see [Gang scheduling](#d02-2)).
- **Standardize the interface, not the server.** If every server exposes the OpenAI-compatible API and Prometheus metrics, the platform's gateway, autoscaling and dashboards work for all of them.

### Deploying by hand

**Scenario.** The first production model is deployed with a plain Deployment. It works, until a node restarts and the pod spends 12 minutes downloading 15 GB of weights again, and then fails because the gated model needs a Hugging Face token nobody put in the cluster.

![Deploying by hand](diagrams/posts/p01-3.svg)

**What the picture shows.** Everything a single vLLM Deployment needs beyond "run this image".

- **A GPU limit:** `nvidia.com/gpu: 1`, so the pod is scheduled onto a GPU node and the device plugin assigns a GPU. See [The NVIDIA device plugin](#d06-1).
- **A toleration:** GPU nodes are usually tainted so ordinary pods stay off them; the model server tolerates the taint.
- **A Secret for gated models:** the Hugging Face token, read by the server at startup.
- **A model cache on a PersistentVolumeClaim:** the downloaded weights survive restarts, so a new pod loads from disk instead of the internet.
- **Shared memory:** a memory-backed `emptyDir` on `/dev/shm`, which multi-GPU inference uses to exchange data between processes.
- **A startup probe with a long budget:** loading weights takes minutes; without it, the liveness probe kills the pod before it's ready.

**The YAML.** A complete single-GPU deployment of vLLM with a cache, a Secret and probes.

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: model-cache
  namespace: models
spec:
  accessModes: ["ReadWriteOnce"]
  resources: { requests: { storage: 100Gi } }
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: qwen-7b
  namespace: models
spec:
  replicas: 1
  selector: { matchLabels: { app: qwen-7b } }
  template:
    metadata:
      labels: { app: qwen-7b }
    spec:
      tolerations:
      - { key: nvidia.com/gpu, operator: Exists, effect: NoSchedule }   #!
      containers:
      - name: vllm
        image: vllm/vllm-openai:v0.23.0          # pin a tested version
        args: ["--model", "Qwen/Qwen2.5-7B-Instruct", "--max-model-len", "16384"]
        env:
        - name: HF_TOKEN
          valueFrom: { secretKeyRef: { name: hf-token, key: token } }   #!
        - { name: HF_HOME, value: /cache }
        ports: [ { containerPort: 8000, name: http } ]
        resources:
          limits: { nvidia.com/gpu: 1, memory: 48Gi }   #!
          requests: { cpu: "4", memory: 32Gi }
        startupProbe:                            # weights take minutes to load #!
          httpGet: { path: /health, port: http }
          periodSeconds: 10
          failureThreshold: 90
        readinessProbe:
          httpGet: { path: /health, port: http }
        volumeMounts:
        - { name: cache, mountPath: /cache }     # survives restarts #!
        - { name: shm, mountPath: /dev/shm }
      volumes:
      - name: cache
        persistentVolumeClaim: { claimName: model-cache }
      - name: shm
        emptyDir: { medium: Memory, sizeLimit: 8Gi }
---
apiVersion: v1
kind: Service
metadata:
  name: qwen-7b
  namespace: models
spec:
  selector: { app: qwen-7b }
  ports: [ { port: 8000, targetPort: http } ]
```

**Going deeper.**

- **Multiply this by every model** and the case for a controller is obvious: each model needs the same cache, Secret, probes and tolerations, plus routing and autoscaling. That's what KServe and KubeRay provide.
- **A `ReadWriteOnce` cache works for one replica.** For several replicas, use a shared read-only volume or OCI image volumes; see [Getting model weights into pods](#post-model-data).

### KServe: separate the runtime from the model

**Scenario.** The platform team wants to upgrade vLLM across all models to fix a security issue. With hand-written Deployments, that's twenty pull requests to twenty teams. They want the server image owned in one place, and each model team to own only "which model, how many replicas".

![KServe: separate the runtime from the model](diagrams/posts/p01-4.svg)

**What the picture shows.** KServe's two layers, who owns each, and what the controller creates.

- **ServingRuntime (or ClusterServingRuntime), owned by the platform team:** the model server image, default arguments, ports and supported model formats. One change updates every model that uses it.
- **InferenceService, owned by the model team:** which model (`storageUri`), its format, resources and replicas. It refers to a runtime by format or by name.
- **What the controller creates:** a storage initializer that downloads the model, the Deployment, the Service and the route, plus autoscaling.
- **Deployment modes:** **Standard** mode creates plain Deployments and is the usual choice for large LLMs; **Knative** mode adds scale-to-zero; **ModelMesh** packs many small models together.
- **LLMInferenceService (newer):** a resource built for LLMs, with KV-cache-aware routing through the Gateway API Inference Extension, disaggregated prefill and decode, and tensor, data and expert parallelism for 7B–405B models.

**The YAML.** A runtime owned by the platform team, a model owned by its team, and the same model as an LLMInferenceService.

```yaml
apiVersion: serving.kserve.io/v1alpha1
kind: ClusterServingRuntime
metadata:
  name: vllm-platform                    # owned by the platform team #!
spec:
  supportedModelFormats:
  - { name: huggingface, autoSelect: true }
  containers:
  - name: kserve-container
    image: vllm/vllm-openai:v0.23.0      # upgrade here, once, for every model #!
    args: ["--model", "/mnt/models", "--port", "8080"]
    ports: [ { containerPort: 8080, protocol: TCP } ]
---
apiVersion: serving.kserve.io/v1beta1
kind: InferenceService
metadata:
  name: qwen-7b
  namespace: team-search                 # owned by the model team
  annotations:
    serving.kserve.io/deploymentMode: Standard
spec:
  predictor:
    minReplicas: 2
    maxReplicas: 6
    model:
      modelFormat: { name: huggingface }
      runtime: vllm-platform
      storageUri: hf://Qwen/Qwen2.5-7B-Instruct   # downloaded by the storage initializer #!
      resources:
        limits: { nvidia.com/gpu: "1" }
---
apiVersion: serving.kserve.io/v1alpha1
kind: LLMInferenceService                # the LLM-specific resource #!
metadata:
  name: qwen-7b-llm
  namespace: team-search
spec:
  model:
    uri: hf://Qwen/Qwen2.5-7B-Instruct
    name: Qwen/Qwen2.5-7B-Instruct
  replicas: 3
  router:
    route: {}
    gateway: {}
    scheduler: {}                        # managed, cache-aware endpoint picker #!
  template:
    containers:
    - name: main
      resources:
        limits: { nvidia.com/gpu: "1" }
```

**Going deeper.**

- **The LLMInferenceService API is still alpha** and changing between KServe releases, so pin your KServe version and re-check the fields on upgrade.
- **GPUs through DRA:** since KServe 0.21 a serving runtime can carry DRA resource claims, merged with each InferenceService's own settings ([0.21 release](https://kserve.github.io/website/blog/kserve-0.21-release)). Give each replica its own claim from a ResourceClaimTemplate. A single named ResourceClaim is shared, so every replica that points at it gets the same device ([Kubernetes DRA task](https://kubernetes.io/docs/tasks/configure-pod-container/assign-resources/allocate-devices-dra/)). See [Dynamic Resource Allocation](#d06-4).
- **Runtime upgrades become a rollout, not a migration:** change the ClusterServingRuntime image, and each InferenceService rolls its pods. Test a new runtime by pointing one InferenceService at a second runtime first.
- See [Workloads and node tiers](#d03-1) and [Placement, autoscaling and cost](#d03-3) for where these pods run and how they scale.

### Ray Serve and KubeRay

**Scenario.** A Python-heavy team wants to chain a retriever, a reranker and an LLM in one serving graph, scale each piece separately and change it all from Python, without writing Kubernetes YAML for each step.

![Ray Serve and KubeRay](diagrams/posts/p01-5.svg)

**What the picture shows.** Two layers of orchestration, one inside the other.

- **RayService (Kubernetes):** a custom resource managed by the KubeRay operator. It creates a RayCluster (a head pod and worker pods) and deploys a Serve application onto it, with zero-downtime upgrades.
- **Ray cluster (Ray's own scheduler):** inside the pods, Ray places tasks and actors, including Serve replicas, onto workers according to their GPU and CPU needs.
- **Serve application (Python):** each step of the graph is a deployment with its own replica count and resources, written as Python classes.
- **Two schedulers:** Kubernetes decides which nodes Ray's pods run on; Ray decides where each replica runs inside them. See [How a Pod finds its scheduler](#d02-1).

**The code.** A Serve deployment in Python, and the RayService that runs it.

```python
# app.py: a Serve application with one GPU-backed deployment
from ray import serve
from vllm import LLM, SamplingParams

@serve.deployment(ray_actor_options={"num_gpus": 1}, autoscaling_config={"min_replicas": 1, "max_replicas": 4})
class Generator:
    def __init__(self):
        self.llm = LLM(model="Qwen/Qwen2.5-7B-Instruct")

    async def __call__(self, request):
        prompt = (await request.json())["prompt"]
        out = self.llm.generate([prompt], SamplingParams(max_tokens=200))
        return {"text": out[0].outputs[0].text}

app = Generator.bind()
```

```yaml
apiVersion: ray.io/v1
kind: RayService
metadata:
  name: generator
  namespace: team-rag
spec:
  serveConfigV2: |
    applications:
    - name: generator
      import_path: app:app                 # the Python object above #!
      route_prefix: /
  rayClusterConfig:
    headGroupSpec:
      rayStartParams: { dashboard-host: "0.0.0.0" }
      template:
        spec:
          containers:
          - name: ray-head
            image: my-company/ray-vllm:2.50   # contains app.py and vLLM
            resources: { limits: { cpu: "4", memory: 16Gi } }
    workerGroupSpecs:
    - groupName: gpu
      replicas: 1
      minReplicas: 1
      maxReplicas: 4                       # Ray's autoscaler adds GPU workers #!
      rayStartParams: {}
      template:
        spec:
          containers:
          - name: ray-worker
            image: my-company/ray-vllm:2.50
            resources: { limits: { nvidia.com/gpu: 1, cpu: "8", memory: 48Gi } }
```

**Going deeper.**

- **Full control from Python, at a price:** a second orchestration layer means two places to debug, two autoscalers (Ray's and Karpenter's) and capacity that Kubernetes quotas see only as Ray's pods.
- **Good fit:** multi-step Python pipelines and teams already using Ray for training or data processing. **Less good:** a single model behind an API, where KServe or a plain Deployment is simpler.
