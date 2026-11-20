---
id: d-gpu
title: "How GPUs become schedulable"
short: "Kubernetes and GPU"
---
### The NVIDIA device plugin

**Scenario.** A team adds GPU nodes to their Kubernetes cluster and deploys a pod that requests `nvidia.com/gpu: 1`. The pod stays `Pending` with "Insufficient nvidia.com/gpu", even though the nodes clearly have GPUs installed. Kubernetes can't see them yet.

![The NVIDIA device plugin](diagrams/sections/d06-1.svg)

**What the picture shows.** How the NVIDIA device plugin turns physical GPUs into a count the scheduler can use, in numbered steps.

- **AWS cloud and the GPU instance:** an EC2 GPU instance (here a p4d.24xlarge with 8 A100s) gives the worker node its physical GPUs. The NVIDIA driver must already be on the node; the EKS-optimized accelerated AMIs include it.
- **Control plane:** the API server stores each node's status, including its GPU count; kube-scheduler checks that count the same way it checks CPU and memory.
- **Step 1, the plugin registers:** the device plugin runs on every GPU node as a DaemonSet. It finds the node's GPUs and registers with the kubelet over a local socket.
- **Step 2, the kubelet reports the count:** the kubelet adds `nvidia.com/gpu: 8` to the node's capacity and allocatable. From then on GPUs are a countable resource. This is the missing piece in the scenario: without the plugin the node never reports any `nvidia.com/gpu`, so every node fails the fit check.
- **Step 3, a pod asks for a GPU:** the user or CI deploys a pod with `nvidia.com/gpu: 1` under `resources.limits`.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: inference
spec:
  containers:
  - name: inference
    image: vllm/vllm-openai:latest
    resources:
      limits:
        nvidia.com/gpu: 1    # schedulable only after the device plugin registers GPUs #!
```

- **Step 4, the scheduler subtracts and binds:** a node fits if enough of its GPUs are still unreserved. The scheduler picks one and binds the pod to it.
- **Step 5, the kubelet asks for a device:** on that node, the kubelet asks the plugin to allocate one GPU. This happens on the node, not in the control plane.
- **Step 6, the container gets the GPU:** the plugin returns the device files and `NVIDIA_VISIBLE_DEVICES`, and the container starts with exactly that GPU. The scheduler never knew which GPU it would be, only that one was free.

**Going deeper.**

> **Installing it.** Each GPU node first needs the NVIDIA driver and container toolkit; the EKS-optimized accelerated AMIs already include them. Then install the plugin with Helm:
>
> ```bash
> helm repo add nvdp https://nvidia.github.io/k8s-device-plugin
> helm repo update
> helm upgrade -i nvdp nvdp/nvidia-device-plugin \
>   --namespace nvidia-device-plugin --create-namespace
> ```
>
> On EKS Auto Mode the plugin is already built in. The NVIDIA GPU Operator is the all-in-one alternative: it installs the driver, toolkit, device plugin, NFD and GPU Feature Discovery together.
>
> **Check (device plugin):** `kubectl get nodes -o custom-columns=NAME:.metadata.name,GPUS:.status.allocatable.nvidia\.com/gpu`

```yaml
# What the NODE shows (written by the kubelet; see it with kubectl get node -o yaml)
apiVersion: v1
kind: Node
metadata:
  name: gpu-node-1
status:
  capacity:
    nvidia.com/gpu: "8"              # reported by the device plugin #!
  allocatable:
    nvidia.com/gpu: "8"              # what the scheduler counts against
---
# What the POD writes: just a number
apiVersion: v1
kind: Pod
metadata:
  name: train
spec:
  containers:
  - name: trainer
    image: my-company/trainer:2.0
    resources:
      limits:
        nvidia.com/gpu: 1            # "any one free GPU on the node" #!
```

- The device plugin does four jobs:
  - it discovers the devices;
  - it advertises them as `nvidia.com/gpu` for scheduling;
  - it allocates them exclusively to containers;
  - it monitors their health, and an unhealthy GPU is taken out of the node's allocatable count.
- **Two labeling tools usually run next to it:** on a typical GPU node there are three separate NVIDIA-related pods. All three are DaemonSets (one pod per GPU node), and the NVIDIA GPU Operator usually installs them together:

| Tool | What it does | What it produces | Do you need it? |
|---|---|---|---|
| **Device plugin** | Counts the GPUs, and **gives one to a container** when a pod asks | A count in the node's capacity (`nvidia.com/gpu: 8`), plus the actual GPU device inside the container | **Always.** It's the only one that hands out GPUs; without it pods can't get one at all. |
| **Node Feature Discovery** | Scans the hardware | Generic labels such as `feature.node.kubernetes.io/pci-10de.present=true` ("an NVIDIA card is here") | **Only as a helper:** GPU Feature Discovery needs it to write its labels, and the GPU Operator uses it to find which nodes need the GPU driver and plugin. |
| **GPU Feature Discovery** | Reads the GPU details | Labels such as `nvidia.com/gpu.product=…A100…` and `nvidia.com/gpu.memory=81920` | **Only when your GPUs differ** and pods must pick a model or size, *and* nothing else describes the GPU already. Often unnecessary on EKS (see below). |

- **They describe the GPUs; only the device plugin hands them out:** Node Feature Discovery and GPU Feature Discovery only write *information* onto the Node object as labels, like a sign on the door saying "this room has A100s with 80 GB". The device plugin is the only one that *gives* a GPU to a container: when a pod asks for `nvidia.com/gpu: 1`, the kubelet asks the device plugin for a GPU, and the plugin hands one over.
- **Why it matters:** a pod that only uses the labels (`nodeSelector: nvidia.com/gpu.product: …A100…`) lands on an A100 node but **gets no GPU**. It still needs `nvidia.com/gpu: 1` so the device plugin hands one over. Labels choose the node; the device plugin gives the GPU. More on the labels in [How GPUs are advertised](#d06-2).
- **Why not just the device plugin?** Its count treats every GPU the same: a 16 GB T4 and an 80 GB H100 are both "1". So with the device plugin alone, a pod can ask for "one GPU" but not "an A100" or "at least 40 GB". That's the gap the labels fill.
- **What you actually need:**
  - **one GPU type everywhere:** the device plugin alone; every GPU is the same, so "one GPU" is all a pod needs to say;
  - **several GPU types:** the device plugin plus a way to label the model, either GPU Feature Discovery (which brings Node Feature Discovery) or labels you already have;
  - **with the GPU Operator:** you get all three by default, whether you need them or not.
- **On EKS you may not need GPU Feature Discovery:** every node already has `node.kubernetes.io/instance-type`, and on AWS the instance type tells you the GPU (a p4de.24xlarge has 8 × A100 80 GB, a g5 has A10Gs). With Karpenter you also get `karpenter.k8s.aws/instance-gpu-name` and `instance-gpu-memory`. A `nodeSelector` or affinity on those picks the GPU model without GFD. GFD earns its place on on-prem or mixed hardware, where nothing else describes the GPU.
- A minimal vLLM Deployment on such a node still needs a GPU limit, a toleration for the GPU taint, and a PVC as a model cache, so restarts don't re-download the weights. The `Recreate` strategy matters too: the cache is a single-node volume, so a rolling update would start the new pod while the old one still holds it, and the new pod would wait.

```yaml
# 1. The model cache: survives pod restarts, so weights aren't re-downloaded
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: model-cache
spec:
  accessModes: ["ReadWriteOnce"]           # one node at a time (an EBS volume on EKS)
  storageClassName: gp3
  resources:
    requests:
      storage: 100Gi
---
# 2. The inference server
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vllm
spec:
  replicas: 1
  strategy:
    type: Recreate                         # RWO volume: the old pod must let go first
  selector:
    matchLabels: {app: vllm}
  template:
    metadata:
      labels: {app: vllm}
    spec:
      tolerations:
      - key: nvidia.com/gpu                # allowed onto the tainted GPU nodes #!
        operator: Exists
        effect: NoSchedule
      containers:
      - name: server
        image: vllm/vllm-openai:latest      # pin a version in production
        args: ["--model", "Qwen/Qwen2.5-7B-Instruct", "--port", "8000"]
        env:
        - name: HF_HOME                    # Hugging Face downloads go to the cache volume
          value: /models #!
        ports:
        - containerPort: 8000
        resources:
          limits:
            nvidia.com/gpu: 1              # the device plugin hands over one GPU #!
          requests:
            cpu: "4"
            memory: 24Gi
        volumeMounts:
        - name: model-cache
          mountPath: /models
        - name: shm                        # vLLM uses shared memory between processes
          mountPath: /dev/shm
        readinessProbe:                    # no traffic until the model is loaded
          httpGet: {path: /health, port: 8000}
          initialDelaySeconds: 60
          periodSeconds: 10
      volumes:
      - name: model-cache
        persistentVolumeClaim:
          claimName: model-cache #!
      - name: shm
        emptyDir:
          medium: Memory
          sizeLimit: 8Gi
```

- Plugins expose counts, not topology: the scheduler doesn't know which GPUs share an NVLink or a PCIe switch. That gap is why topology labels, [Dynamic Resource Allocation](#d06-4) and topology-aware schedulers ([topology bin-packing](#d02-3)) exist.

### How GPUs are advertised

**Scenario.** A platform team must make sure a new inference service lands only on GPUs with at least 40 GB of memory. Reading the docs, they find five things that all seem to "advertise GPUs" to Kubernetes: the device plugin, Node Feature Discovery, GPU Feature Discovery, Karpenter's labels and DRA. Which one does what, which ones do they actually need, and which one decides the GPU a pod gets?

![How GPUs are advertised](diagrams/sections/d06-2.svg)

**What the picture shows.** The three kinds of advertising side by side, and what each one lets the scheduler do.

- **Labels (left column):** Node Feature Discovery, GPU Feature Discovery and the cloud/Karpenter labels write facts about the GPUs on the **Node** object. Pods match them with a `nodeSelector` or node affinity. They only help choose a node; nothing is reserved.
- **Count (middle column):** the device plugin publishes `nvidia.com/gpu: 8` in the node's capacity. Pods ask for a number, and the scheduler reserves against that count like CPU or memory.
- **DRA (right column):** a DRA driver publishes every device as its own entry in a ResourceSlice, with its attributes. Pods bring a ResourceClaim that describes the device they need, and the scheduler matches it against individual devices.
- **Bottom row, who picks the exact GPU:** with labels, nobody; with a count, the device plugin picks one when the container starts; with DRA, the scheduler picks it and records it in the claim.
- **The classic setup uses two of them together:** labels to choose the node, plus the count to reserve a GPU on it. DRA replaces both with one mechanism.

**Going deeper.**

- How a node tells Kubernetes about its GPUs is the main thing that sets DRA apart. There are two kinds of advertising, and before DRA they came from different tools:
  - **Labels** are information about the node ("this node has A100s with 80 GB"). They only help you *choose a node*, with a `nodeSelector` or node affinity.
  - **A count** is something the scheduler reserves ("this node has 8 GPUs, 3 are free"). The **device plugin** publishes it, and a pod asks for it with `nvidia.com/gpu: 1`. The scheduler only picks the node; the device plugin then hands over *any* free GPU on it.
  - **DRA** replaces both with one entry per **device** (a ResourceSlice) that carries that device's own attributes. The scheduler matches a claim against individual GPUs and records exactly which one the pod gets.

| Mechanism | Kind | What it publishes | How a pod uses it |
|---|---|---|---|
| **Device plugin** (NVIDIA; also AMD `amd.com/gpu`, AWS Neuron `aws.amazon.com/neuron`) | **Count** | A count in the node's capacity, e.g. `nvidia.com/gpu: 8` | `resources.limits` |
| **Node Feature Discovery** | **Label** | Generic hardware labels, e.g. `feature.node.kubernetes.io/pci-10de.present` (10de = NVIDIA) | `nodeSelector` / node affinity |
| **GPU Feature Discovery** (NVIDIA, installed with the GPU Operator; needs NFD running) | **Label** | Node labels with GPU details: model, memory, count | `nodeSelector` / node affinity |
| **Cloud and Karpenter labels** | **Label** | Labels from the instance type: `node.kubernetes.io/instance-type`, `karpenter.k8s.aws/instance-gpu-name`, `instance-gpu-memory`, `instance-gpu-count` | Node affinity, or NodePool requirements |
| **DRA driver** | **DRA** (per device) | One ResourceSlice entry per device, with its attributes | A `ResourceClaim` with CEL selectors |

- A few things follow from that table. GPU Feature Discovery's `nvidia.com/gpu.count` label is still only a label: the scheduler reserves against the device plugin's `nvidia.com/gpu` count, never against it. GPU Feature Discovery builds on Node Feature Discovery, adding GPU details to its generic "there's an NVIDIA card" label. Karpenter's labels are known *before* a node exists, so they can make Karpenter launch the right instance; GPU Feature Discovery can only label a node once it's running. DRA doesn't need any of these labels, though they can still be used alongside it (for example to keep a node pool for one GPU model). And NVIDIA's DCGM exporter, often seen next to these, only reports GPU usage and health for monitoring; the scheduler never reads it.

**How each mechanism works, step by step.** The same cluster in each picture: a control plane on the left, a GPU node on the right, numbered arrows explained underneath. Picture 1, the device plugin, is the [previous section](#d06-1).

![2. Node Feature Discovery: generic hardware labels](diagrams/extra/adv-nfd.svg)

> **Installing it.** NFD has its own Helm chart:
>
> ```bash
> helm install nfd oci://registry.k8s.io/nfd/charts/node-feature-discovery \
>   --namespace node-feature-discovery --create-namespace
> ```
>
> Skip this if you use the GPU Operator, or the device plugin chart with `gfd.enabled=true`: both install NFD for you.
>
> **Check (NFD):** `kubectl get node gpu-node-1 --show-labels | tr ',' '\n' | grep feature.node`

```yaml
# What the NODE shows (labels written by the NFD master)
apiVersion: v1
kind: Node
metadata:
  name: gpu-node-1
  labels:
    # The GPU Operator configures NFD to label by vendor only:
    feature.node.kubernetes.io/pci-10de.present: "true" #!
    # NFD's own default labels by class + vendor (0302 = 3D controller):
    # feature.node.kubernetes.io/pci-0302_10de.present: "true"
---
# What the POD writes: "any node with an NVIDIA card"
apiVersion: apps/v1
kind: DaemonSet                      # a typical user: per-GPU-node agents
metadata:
  name: gpu-monitor
spec:
  selector:
    matchLabels: {app: gpu-monitor}
  template:
    metadata:
      labels: {app: gpu-monitor}
    spec:
      nodeSelector: #!
        feature.node.kubernetes.io/pci-10de.present: "true" #!
      containers:
      - name: exporter
        image: my-company/gpu-monitor:1.0
```

![3. GPU Feature Discovery: GPU details as node labels](diagrams/extra/adv-gfd.svg)

> **Installing it.** GPU Feature Discovery now ships in the same repository and Helm chart as the device plugin, so you turn it on there. The chart also installs NFD for you unless you say you already have it:
>
> ```bash
> helm upgrade -i nvdp nvdp/nvidia-device-plugin \
>   --namespace nvidia-device-plugin --create-namespace \
>   --set gfd.enabled=true
> # NFD already installed separately? add: --set nfd.enabled=false
> ```
>
> With the GPU Operator, GPU Feature Discovery is on by default.
>
> **Check (GFD):** `kubectl get node gpu-node-1 --show-labels | tr ',' '\n' | grep nvidia.com/gpu`

```yaml
# What the NODE shows (labels written by GPU Feature Discovery via NFD)
apiVersion: v1
kind: Node
metadata:
  name: gpu-node-1
  labels:
    nvidia.com/gpu.product: NVIDIA-A100-SXM4-80GB
    nvidia.com/gpu.memory: "81920"   # per-GPU memory, in MiB #!
    nvidia.com/gpu.family: ampere
    nvidia.com/gpu.count: "8"        # only a label: nothing counts against it
---
# What the POD writes: pick a node by label, then still ask for a GPU
apiVersion: v1
kind: Pod
metadata:
  name: inference
spec:
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
        - matchExpressions:
          - key: nvidia.com/gpu.memory #!
            operator: Gt #!
            values: ["40000"]
  containers:
  - name: server
    image: vllm/vllm-openai:latest
    resources:
      limits:
        nvidia.com/gpu: 1            # the device plugin still allocates the GPU
```

![4. Cloud and Karpenter labels: known before the node exists](diagrams/extra/adv-karpenter.svg)

> **Installing it.** `node.kubernetes.io/instance-type` needs nothing: the kubelet sets it on every node. The `karpenter.k8s.aws/...` labels come from Karpenter, which you install with Helm once its IAM role and interruption queue exist (Karpenter's getting-started guide creates them):
>
> ```bash
> helm upgrade --install karpenter oci://public.ecr.aws/karpenter/karpenter \
>   --version "${KARPENTER_VERSION}" --namespace kube-system \
>   --set settings.clusterName="${CLUSTER_NAME}" \
>   --set settings.interruptionQueue="${CLUSTER_NAME}" \
>   --wait
> ```
>
> Then apply an `EC2NodeClass` and a `NodePool` like the one below. On EKS Auto Mode, Karpenter is built in and run by AWS, and the same labels use the `eks.amazonaws.com/` prefix instead (for example `eks.amazonaws.com/instance-gpu-name`).
>
> **Check (Karpenter):** `kubectl get nodes -L karpenter.k8s.aws/instance-gpu-name,karpenter.k8s.aws/instance-gpu-memory`

```yaml
# A Karpenter NodePool that may launch big-GPU instances
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: gpu
spec:
  template:
    spec:
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: default
      requirements:
      - key: karpenter.k8s.aws/instance-gpu-name #!
        operator: In
        values: ["a100", "h100"]
      - key: karpenter.k8s.aws/instance-gpu-memory   # per GPU, in MiB #!
        operator: Gt
        values: ["40000"]
      taints:
      - key: nvidia.com/gpu
        effect: NoSchedule
---
# What the POD writes: affinity on the same labels Karpenter will set
apiVersion: v1
kind: Pod
metadata:
  name: inference
spec:
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
        - matchExpressions:
          - key: karpenter.k8s.aws/instance-gpu-memory #!
            operator: Gt
            values: ["40000"]
  tolerations:
  - key: nvidia.com/gpu
    operator: Exists
    effect: NoSchedule
  containers:
  - name: server
    image: vllm/vllm-openai:latest
    resources:
      limits:
        nvidia.com/gpu: 1
```

![5. DRA: one entry per device](diagrams/extra/adv-dra.svg)

> **Installing it.** DRA itself is built into Kubernetes 1.34 and later; you install a DRA driver for your hardware. For NVIDIA GPUs:
>
> ```bash
> helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
> helm repo update
> helm install nvidia-dra-driver-gpu nvidia/nvidia-dra-driver-gpu \
>   --create-namespace --namespace nvidia-dra-driver-gpu \
>   --set resources.computeDomains.enabled=false \
>   --set gpuResourcesEnabledOverride=true
> ```
>
> Don't run the classic device plugin on the same nodes. Version requirements, the chart's new name and EKS caveats are in "How do you install DRA?" in the [Dynamic Resource Allocation](#d06-4) section.
>
> **Check (DRA):** `kubectl get deviceclass` and `kubectl get resourceslices`

```yaml
# What the DRIVER publishes (one ResourceSlice per node, one entry per GPU)
apiVersion: resource.k8s.io/v1
kind: ResourceSlice
metadata:
  name: gpu-node-1-gpu.nvidia.com-x8k2p   # generated name
spec:
  driver: gpu.nvidia.com
  nodeName: gpu-node-1
  pool:
    name: gpu-node-1
    generation: 1
    resourceSliceCount: 1
  devices:
  - name: gpu-0 #!
    attributes:
      productName: {string: NVIDIA A100-SXM4-80GB}
      architecture: {string: Ampere} #!
    capacity:
      memory: {value: 80Gi} #!
  - name: gpu-1
    # ... one entry for each of the 8 GPUs
---
# What the POD writes: a claim that describes the device (full example below)
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: gpu-40gb-plus
spec:
  spec:
    devices:
      requests:
      - name: gpu
        exactly:
          deviceClassName: gpu.nvidia.com #!
          selectors:
          - cel:
              expression: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("40Gi")) >= 0
```

### Steering pods to the right GPU

**Scenario.** Three teams share a cluster with T4, A10G, A100 and H100 nodes. The batch team wants exactly A100s, the inference team wants "at least 40 GB, Hopper if possible", and the web team's ordinary pods keep landing on expensive GPU nodes.

![Steering pods to the right GPU](diagrams/sections/d06-3.svg)

**What the picture shows.** Four GPU nodes with their labels and taint, and how three different pods are filtered and scored against them.

- **The four GPU nodes:** GPU Feature Discovery labels each one with its model, memory and family: a g4dn (T4, about 15 GB, Turing), a g5 (A10G, about 23 GB, Ampere), a p4de (A100, 80 GB, Ampere) and a p5 (H100, 80 GB, Hopper).
- **The taint on every GPU node:** `nvidia.com/gpu=present:NoSchedule` keeps away any pod without a matching toleration. This is the fix for the web team.
- **Pod A, pin a model (Option 1):** a `nodeSelector` with `nvidia.com/gpu.product: NVIDIA-A100-SXM4-80GB`. `nvidia.com/gpu.product` is the label's key ("which GPU model?") and `NVIDIA-A100-SXM4-80GB` is the value it must equal. A `nodeSelector` is an exact match, so only the p4de passes: the g4dn, g5 and p5 have the same key with other values (`Tesla-T4`, `NVIDIA-A10G`, `NVIDIA-H100-80GB-HBM3`), and the CPU node has no such label. Even a p4d wouldn't match, since its A100s are the 40 GB model (`NVIDIA-A100-SXM4-40GB`).
- **Pod B, a range plus a preference (Option 2):** a required rule `nvidia.com/gpu.memory` greater than 40000 removes the g4dn and the g5; a preferred rule for the `hopper` family adds points to the p5, so the p5 wins when it has room and the p4de is the fallback.
- **Pod C, a web pod:** it has no toleration, so every GPU node is filtered out and it runs on a regular CPU node.
- **The GPU request:** pods A and B still ask for `nvidia.com/gpu: 1`. The labels only choose the node; the count reserves a GPU on it.

**Going deeper.**

- `nvidia.com/gpu: 1` is purely resource-based, so every GPU counts the same: a 16 GB T4 and an 80 GB A100 are both "1".

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: inference
spec:
  containers:
  - name: server
    image: vllm/vllm-openai:latest
    resources:
      limits:
        nvidia.com/gpu: 1      # "any one GPU": a 16 GB T4 and an 80 GB A100 both count as 1 #!
```

- A `nodeSelector` on GPU labels pins an exact model, node affinity can say "at least 40 GB, prefer Hopper", and a taint on GPU nodes keeps everything else off them.

<div id="gpu-labels-yaml" class="anchor"></div>

```yaml
# The GPU NODE: labels (added automatically by NVIDIA GPU Feature Discovery,
# part of the GPU Operator) and a taint (set in the node group / NodePool).
# Shown trimmed: a real node has many more labels.
apiVersion: v1
kind: Node
metadata:
  name: gpu-node-1
  labels:
    nvidia.com/gpu.product: NVIDIA-A100-SXM4-80GB   # the exact model #!
    nvidia.com/gpu.memory: "81920"                  # per-GPU memory, in MiB #!
    nvidia.com/gpu.family: ampere                   # GPU generation #!
    nvidia.com/gpu.count: "8"
spec:
  taints:
  - key: nvidia.com/gpu
    value: present
    effect: NoSchedule                              # keeps non-GPU pods off #!
---
# Option 1: pin an exact GPU model with nodeSelector
apiVersion: v1
kind: Pod
metadata:
  name: inference-a100
spec:
  nodeSelector:
    nvidia.com/gpu.product: NVIDIA-A100-SXM4-80GB #!
  tolerations:                       # GPU nodes are usually tainted
  - key: nvidia.com/gpu
    operator: Exists
    effect: NoSchedule
  containers:
  - name: server
    image: vllm/vllm-openai:latest
    resources:
      limits:
        nvidia.com/gpu: 1
---
# Option 2: "at least 40 GB, prefer Hopper" with node affinity
apiVersion: v1
kind: Pod
metadata:
  name: inference-big-gpu
spec:
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
        - matchExpressions:
          - key: nvidia.com/gpu.memory #!
            operator: Gt #!
            values: ["40000"]        # more than ~40 GB #!
      preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 50
        preference:
          matchExpressions:
          - key: nvidia.com/gpu.family #!
            operator: In
            values: ["hopper"] #!
  tolerations:
  - key: nvidia.com/gpu
    operator: Exists
    effect: NoSchedule
  containers:
  - name: server
    image: vllm/vllm-openai:latest
    resources:
      limits:
        nvidia.com/gpu: 1
```

- In Option 2, node affinity only picks a node with big GPUs; `nvidia.com/gpu: 1` is what actually reserves a GPU there. Leave it out and the scheduler counts no GPU for the pod, so the container gets none, or, on nodes that trust the image's `NVIDIA_VISIBLE_DEVICES=all` setting, quietly uses GPUs other pods have reserved.

### Dynamic Resource Allocation

**Scenario.** The inference team wants "any NVIDIA GPU with at least 40 GB" without depending on node labels, and the training team needs each worker's GPU on the same PCIe root as its network card. Neither can be said with `nvidia.com/gpu: 1`.

**The words you will meet.** DRA adds a few Kubernetes objects. If you know storage, the idea will feel familiar: a pod doesn't name a disk, it makes a *claim* for one of some *class*, and Kubernetes finds a match. DRA does the same for devices. Kubernetes' own [DRA documentation](https://kubernetes.io/docs/concepts/scheduling-eviction/dynamic-resource-allocation/) makes that comparison with storage classes and PersistentVolumeClaims. The other two storage matches in the table are our own comparison.

| Word | What it is | Who creates it | Storage comparison |
|---|---|---|---|
| **ResourceSlice** | The list of devices on one node, each with its facts: product name, architecture, memory. | The DRA driver, per node | PersistentVolume (our comparison) |
| **DeviceClass** | A named category of devices, such as `gpu.nvidia.com`. A request says which class it wants. | The device driver's side (NVIDIA ships its classes in the driver's Helm chart); a cluster admin can add more | A StorageClass |
| **CEL** (Common Expression Language) | A one-line rule that is true or false for each device, such as "memory is at least 40Gi". It narrows the devices down. A class has one, and your claim may add one. **Why it is needed:** a count or a node label can only say "one GPU" or "this node has A100s", while a rule can test each device's own facts. | The driver or an admin, inside a class. You, optionally, inside a claim | n/a |
| **ResourceClaimTemplate** | A recipe for a request: which class, an optional CEL rule, how many devices (an exact count, one by default; the only other mode is "all matching devices"). A request has no "shared" or "immediate" setting. | You | volumeClaimTemplates in a StatefulSet (our comparison) |
| **ResourceClaim** | One actual request, made from the recipe. The scheduler records which device it picked in the claim. | Kubernetes, one per pod | A PersistentVolumeClaim |

![If you know storage, you already know the shape of DRA](diagrams/extra/dra-storage-analogy.svg)

The storage side as YAML, for comparison:

```yaml
# STORAGE: a class, and a StatefulSet that makes one claim per pod
apiVersion: storage.k8s.io/v1
kind: StorageClass                   # "a named kind of disk" #!
metadata:
  name: gp3
provisioner: ebs.csi.aws.com
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: db
spec:
  replicas: 3
  # (selector, serviceName and the pod template are left out here)
  volumeClaimTemplates:              # a recipe: every pod gets its own claim #!
  - metadata:
      name: data
    spec:
      storageClassName: gp3          # which class to ask for
      accessModes: ["ReadWriteOnce"]
      resources:
        requests:
          storage: 100Gi             # how much
```

**Step 1. A device and its facts: the ResourceSlice.** The driver on each node publishes a ResourceSlice: one entry per device, with its attributes (descriptive facts such as `productName`, `architecture` and `type`) and its capacity (amounts such as `memory`). Every DRA decision is made from these facts, and the names depend on the driver. `kubectl get resourceslices -o yaml` shows the real ones on your cluster. A device reports a little less than its advertised size (a T4 sold as 16 GB shows about 15 GB, [NVIDIA forum](https://forums.developer.nvidia.com/t/nvidia-t4-has-only-15g-of-memory/173099)), so read the real value there before you choose a threshold. Here is the ResourceSlice of one illustrative node, gpu-node-1, a lab server with four different GPUs (trimmed, illustrative values):

```yaml
apiVersion: resource.k8s.io/v1
kind: ResourceSlice
metadata:
  name: gpu-node-1-gpu.nvidia.com-x8k2p
spec:
  driver: gpu.nvidia.com
  nodeName: gpu-node-1
  pool: {name: gpu-node-1, generation: 1, resourceSliceCount: 1}
  devices:                           # four GPUs, one entry each
  - name: gpu-0
    attributes: {type: {string: gpu}, productName: {string: Tesla T4}, architecture: {string: Turing}}
    capacity: {memory: {value: 15Gi}}
  - name: gpu-1
    attributes: {type: {string: gpu}, productName: {string: NVIDIA A10G}, architecture: {string: Ampere}}
    capacity: {memory: {value: 23Gi}}
  - name: gpu-2
    attributes: {type: {string: gpu}, productName: {string: NVIDIA A100-SXM4-80GB}, architecture: {string: Ampere}}
    capacity: {memory: {value: 80Gi}}
  - name: gpu-3
    attributes: {type: {string: gpu}, productName: {string: NVIDIA H100 80GB HBM3}, architecture: {string: Hopper}}
    capacity: {memory: {value: 80Gi}}
```

**Step 2. CEL: which devices exactly.** CEL stands for Common Expression Language, a small language for writing true-or-false rules. DRA needs it because a count or a node label can only say "one GPU" or "this node has A100s". A rule can say "memory at least 40Gi" about each device, and the scheduler tests it against the facts in the ResourceSlices. It runs the rule on every device and keeps only those where it comes out true. `device.attributes[...]` reads an entry's attributes and `device.capacity[...]` reads its capacity, so the rules use the names from Step 1. Three rules, from simple to combined:

- `device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("40Gi")) >= 0` reads "memory is at least 40Gi". `quantity("40Gi")` turns the text into an amount Kubernetes can compare, and `compareTo(...) >= 0` means "greater than or equal".
- `device.attributes["gpu.nvidia.com"].architecture == "Hopper"` reads "the architecture is Hopper". Attributes are descriptive facts such as the product name or architecture; capacity holds amounts such as memory.
- Join two rules with `&&` ("both must be true"), for example Hopper and at least 40Gi.

The picture applies the three rules to the four GPUs of gpu-node-1 from Step 1:

![CEL is a filter: one node, three rules](diagrams/extra/dra-cel-filter.svg)

**Step 3. DeviceClass: a named category of devices.** `gpu.nvidia.com` means "an NVIDIA GPU". A class is itself a CEL rule that a device must pass before a request can use it: NVIDIA's says "the device is published by the driver `gpu.nvidia.com` and its type is `gpu`", which is how the class keeps only GPUs. The class comes with NVIDIA's DRA driver chart ([source](https://github.com/NVIDIA/k8s-dra-driver-gpu/blob/main/deployments/helm/dra-driver-nvidia-gpu/templates/deviceclass-gpu.yaml)), so installing the driver creates it, while the ResourceSlices are published by the running driver. You usually don't write a class, you pick one, and `kubectl get deviceclass` lists what your cluster has. Here is NVIDIA's class (trimmed):

```yaml
apiVersion: resource.k8s.io/v1
kind: DeviceClass
metadata:
  name: gpu.nvidia.com
spec:
  selectors:
  - cel:                             # the class's own rule, shipped by the driver's chart
      expression: "device.driver == 'gpu.nvidia.com' && device.attributes['gpu.nvidia.com'].type == 'gpu'"
```

All four devices in the ResourceSlice of Step 1 pass this rule, because each is published by that driver with type `gpu`.

**Step 4. Your request: the ResourceClaimTemplate.** A template is a recipe for a request: which class, an optional CEL rule of your own, and how many devices (one by default). A device must pass two filters before it can be given to your pod: the class's rule from Step 3 (FILTER 1) and the template's own rule (FILTER 2). The API requires both, so the numbers are only labels for reading. This template asks for an NVIDIA GPU with at least 40Gi of memory:

```yaml
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate          # a recipe: class + optional CEL rule #!
metadata:
  name: gpu-40gb-plus
spec:
  spec:
    devices:
      requests:
      - name: gpu
        exactly:
          deviceClassName: gpu.nvidia.com      # FILTER 1 comes in here: the class's rule from Step 3 is inherited by this request #!
          selectors:
          - cel:                               # FILTER 2: your claim's own rule (optional). A device must pass both #!
              expression: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("40Gi")) >= 0
```

On gpu-node-1 from Step 1, FILTER 1 keeps all four GPUs and FILTER 2 keeps the A100 (`gpu-2`) and the H100 (`gpu-3`). What each line means:

- **`name: gpu-40gb-plus`:** the name the pod uses to point at this request.
- **`requests: - name: gpu`:** "I need one device, and I'll call it `gpu`."
- **`exactly:`:** allocate exactly one device that matches. DRA can also list alternatives ("this, or else that"); here there's only one option.
- **`deviceClassName: gpu.nvidia.com`:** only look at NVIDIA GPUs. This is where FILTER 1 comes in.
- **The `cel` selector:** FILTER 2, the rule from Step 2 that reads "this device's memory compared with 40Gi is 0 or more", that is, at least 40 GiB.

**You do not have to write any CEL.** A template (or claim) that names only the class gets every device the class allows. Its own rule is optional and can only narrow further: the API requires a device to satisfy the class's selectors and the request's selectors, and an administrator can even create an empty class if users may ask for anything. This template has FILTER 1 only:

```yaml
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: any-nvidia-gpu
spec:
  spec:
    devices:
      requests:
      - name: gpu
        exactly:
          deviceClassName: gpu.nvidia.com      # every device the class allows is a candidate: all four GPUs of gpu-node-1
```

**The three rules from Step 2 as templates.** Each template holds one rule as FILTER 2, with the GPUs of gpu-node-1 that it keeps:

```yaml
# Rule 1: memory is at least 40Gi  -> keeps gpu-2, gpu-3
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: rule-1-memory
spec:
  spec:
    devices:
      requests:
      - name: gpu
        exactly:
          deviceClassName: gpu.nvidia.com
          selectors:
          - cel:
              expression: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("40Gi")) >= 0 #!
---
# Rule 2: architecture is Hopper  -> keeps gpu-3
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: rule-2-hopper
spec:
  spec:
    devices:
      requests:
      - name: gpu
        exactly:
          deviceClassName: gpu.nvidia.com
          selectors:
          - cel:
              expression: device.attributes["gpu.nvidia.com"].architecture == "Hopper" #!
---
# Rule 3: both, joined with &&  -> keeps gpu-3
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: rule-3-both
spec:
  spec:
    devices:
      requests:
      - name: gpu
        exactly:
          deviceClassName: gpu.nvidia.com
          selectors:
          - cel:
              expression: |-
                device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("40Gi")) >= 0 &&
                device.attributes["gpu.nvidia.com"].architecture == "Hopper" #!
```

**Step 5. Why the class checks the device type.** The class's rule from Step 3 has a second part: the type must be `gpu`. This part exists because of MIG. MIG (Multi-Instance GPU) cuts one physical GPU into small, isolated slices. The last section of this deep dive explains it in full ([Different GPUs in one server, and MIG](#d06-5)). The GPU Operator's MIG manager creates the slices. The NVIDIA DRA driver then finds them and lists each slice as its own device, next to the whole GPUs ([NVIDIA's DRA driver install notes](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/26.3/dra-intro-install.html)). So one driver lists two kinds of device: whole GPUs and slices.

The picture compares two devices on the same node: a whole GPU on the left and a small slice of a GPU on the right. They look almost identical, with the same product name and the same architecture. Only the type differs, "gpu" on the left and "mig" on the right. In the top row, your claim asks only for "Hopper". Both devices are Hopper, so both qualify, and the orange box warns that your pod could be handed the small slice by mistake. In the bottom row, the class's rule is added, and it says the type must be "gpu". The slice's type is "mig", so the class removes it, and that is the grey box. Only the whole GPU is left, which is what you wanted.

![Why the class checks the device type](diagrams/extra/dra-class-type-test.svg)

**The same idea on a whole node.** Here is a whole node with four devices: a T4, an A100, an H100, and a slice of a second H100. We call the node gpu-node-2, and it is made up for this example. A real node can mix whole GPUs with GPUs that are split into slices. Your claim asks for a Hopper GPU, from the class for whole GPUs. The picture checks each device against both filters:

![One node, four devices, two filters](diagrams/extra/dra-two-filters-node.svg)

The same node as YAML. The comments say which filter each device passes:

```yaml
# What the driver publishes (trimmed, illustrative values)
apiVersion: resource.k8s.io/v1
kind: ResourceSlice
metadata:
  name: gpu-node-2-gpu.nvidia.com-x8k2p
spec:
  driver: gpu.nvidia.com
  nodeName: gpu-node-2
  pool: {name: gpu-node-2, generation: 1, resourceSliceCount: 1}
  devices:
  - name: gpu-0                      # passes FILTER 1, fails FILTER 2 (Turing, not Hopper)
    attributes: {type: {string: gpu}, productName: {string: Tesla T4}, architecture: {string: Turing}}
  - name: gpu-1                      # passes FILTER 1, fails FILTER 2 (Ampere, not Hopper)
    attributes: {type: {string: gpu}, productName: {string: NVIDIA A100-SXM4-80GB}, architecture: {string: Ampere}}
  - name: gpu-2                      # passes both filters: the only candidate #!
    attributes: {type: {string: gpu}, productName: {string: NVIDIA H100 80GB HBM3}, architecture: {string: Hopper}}
  - name: mig-0                      # would pass FILTER 2, but fails FILTER 1: its type is mig #!
    attributes: {type: {string: mig}, profile: {string: 1g.10gb}, productName: {string: NVIDIA H100 80GB HBM3}, architecture: {string: Hopper}}
---
# FILTER 1: the class, from the driver's chart
apiVersion: resource.k8s.io/v1
kind: DeviceClass
metadata:
  name: gpu.nvidia.com
spec:
  selectors:
  - cel:
      expression: "device.driver == 'gpu.nvidia.com' && device.attributes['gpu.nvidia.com'].type == 'gpu'" #!
---
# FILTER 2: your claim's own rule, on top of the class
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: one-hopper-gpu
spec:
  spec:
    devices:
      requests:
      - name: gpu
        exactly:
          deviceClassName: gpu.nvidia.com            # brings in FILTER 1
          selectors:
          - cel:
              expression: device.attributes["gpu.nvidia.com"].architecture == "Hopper" #!
```

Only `gpu-2` is left. Take the claim's rule away and `gpu-2` is still allowed, but so are `gpu-0` and `gpu-1`, since the class only asks for whole NVIDIA GPUs. Take the class away and `mig-0` would pass the Hopper rule, which is what the class's type check prevents.

**Your own category.** Anyone can define another category by writing another rule. Kubernetes' own documentation gives the example of a cost-optimized class and a high-performance class ([DRA docs](https://kubernetes.io/docs/concepts/scheduling-eviction/dynamic-resource-allocation/)). This class is ours, not something NVIDIA ships. It admits only Hopper GPUs, so the template that uses it needs no CEL of its own:

```yaml
# Example (ours): an admin defines a category of their own, Hopper GPUs only
apiVersion: resource.k8s.io/v1
kind: DeviceClass
metadata:
  name: hopper-gpu
spec:
  selectors:
  - cel:
      expression: "device.driver == 'gpu.nvidia.com' && device.attributes['gpu.nvidia.com'].type == 'gpu' && device.attributes['gpu.nvidia.com'].architecture == 'Hopper'" #!
---
# A claim for that category needs no CEL of its own
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: one-hopper
spec:
  spec:
    devices:
      requests:
      - name: gpu
        exactly:
          deviceClassName: hopper-gpu #!
```

**Step 6. The pod that uses the template.** The pod names the template from Step 4, and its container asks for the claim that Kubernetes makes from it:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: inference-dra
spec:
  resourceClaims:
  - name: gpu
    resourceClaimTemplateName: gpu-40gb-plus     # "make me a claim from this template" #!
  containers:
  - name: server
    image: vllm/vllm-openai:latest
    resources:
      claims:
      - name: gpu                                # this container gets what the claim allocates #!
```

- **`spec.resourceClaims`:** create a claim for this pod from the `gpu-40gb-plus` template. Each pod gets its own ResourceClaim (named something like `inference-dra-gpu-x7k2p`), which is deleted with the pod. That's why it's a *template*: ten replicas get ten separate claims instead of fighting over one GPU.
- **`containers[].resources.claims: - name: gpu`:** give the `server` container access to that device. In a pod with several containers, you choose which ones see the GPU.
- **No GPU count under `resources.limits`:** the container lists `claims` instead. The claim *is* the request, so there is nothing to count. Notice there is no `nvidia.com/gpu: 1` anywhere in these manifests: that line belongs to the older device-plugin way used in the [GPU labels example](#gpu-labels-yaml) (Option 1 and Option 2), not to DRA.

**Step 7. The claim Kubernetes makes for the pod.** When the pod is created, Kubernetes copies the template's spec into a new ResourceClaim for that pod. Once the scheduler has picked a device, the claim looks like this (the status is written by the scheduler):

```yaml
apiVersion: resource.k8s.io/v1
kind: ResourceClaim                  # one real request #!
metadata:
  name: inference-dra-gpu-x7k2p      # generated name
spec:                                # copied unchanged from the template gpu-40gb-plus
  devices:
    requests:
    - name: gpu
      exactly:
        deviceClassName: gpu.nvidia.com            # brings in FILTER 1, the class's rule
        selectors:
        - cel:                                     # FILTER 2 came along in the copy
            expression: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("40Gi")) >= 0
status:
  allocation:
    devices:
      results:
      - request: gpu
        driver: gpu.nvidia.com
        pool: gpu-node-1
        device: gpu-2                # a device that passed both filters (the A100): the scheduler's pick #!
  reservedFor:                       # who uses the claim: the pod inference-dra #!
  - resource: pods
    name: inference-dra
    uid: 7c1f6b2e-0a4d-4f4e-9b0a-2f6d5e8a1c33   # illustrative
```

What happens when you apply the pod and the template:

1. **The claim is created:** Kubernetes makes the pod's ResourceClaim from the template.
2. **The scheduler filters and allocates:** its DynamicResources plugin checks the devices each node publishes (as ResourceSlices, written by the driver) and filters out nodes with no free NVIDIA GPU of 40 GiB or more. Then it picks a node and records the exact GPU it chose in the claim.
3. **The kubelet prepares the device:** on that node, the kubelet asks the driver's kubelet plugin to set up that GPU and expose it to the `server` container. The plugin answers with the GPU's CDI names, and the container runtime uses them to add the GPU to the container (see the CDI point under Going deeper).
4. **The server starts:** vLLM starts and sees exactly one GPU that's big enough.

**Step 8. Template or claim?** A claim you write by hand is one request that several pods can share, and it outlives any pod. A template gives every pod its own claim, made when the pod starts and removed with it. Ten replicas then get ten separate GPUs instead of sharing one.

![Template or claim: who gets which GPU](diagrams/extra/dra-template-vs-claim.svg)

The same two cases as YAML. Left: one claim you write, and three pods that name it. Right: one template, and three pods that name it. The only line that differs in the pods is the one that names the claim or the template.

```yaml
# A. A ResourceClaim you write by hand: all three pods share it
apiVersion: resource.k8s.io/v1
kind: ResourceClaim                  # one request, written by you #!
metadata:
  name: shared-gpu
spec:
  devices:
    requests:
    - name: gpu
      exactly:
        deviceClassName: gpu.nvidia.com
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: notebooks-shared
spec:
  replicas: 3
  selector:
    matchLabels: {app: notebooks-shared}
  template:
    metadata:
      labels: {app: notebooks-shared}
    spec:
      resourceClaims:
      - name: gpu
        resourceClaimName: shared-gpu          # all 3 pods name the same claim #!
      containers:
      - name: jupyter
        image: jupyter/base-notebook:latest
        resources:
          claims:
          - name: gpu
```

```yaml
# B. A ResourceClaimTemplate: every pod gets its own claim
# (the template gpu-40gb-plus is the one from Step 4)
apiVersion: apps/v1
kind: Deployment
metadata:
  name: notebooks-own-gpu
spec:
  replicas: 3
  selector:
    matchLabels: {app: notebooks-own-gpu}
  template:
    metadata:
      labels: {app: notebooks-own-gpu}
    spec:
      resourceClaims:
      - name: gpu
        resourceClaimTemplateName: gpu-40gb-plus   # Kubernetes makes one claim per pod #!
      containers:
      - name: jupyter
        image: jupyter/base-notebook:latest
        resources:
          claims:
          - name: gpu
```

**Check (claims):** `kubectl get resourceclaims`. With A you see one claim, `shared-gpu`, and it stays after the pods are deleted. With B you see three claims with generated names, one per pod, and each disappears with its pod. In A, the claim's allocation records where its device is available (a node selector in the claim's status, [API reference](https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/resource-claim-v1/)), so the three pods run where that device is.

**Is sharing a claim like sharing a CPU?** Roughly, yes. Pods that share a claim get access to the same GPU, a bit like programs sharing a CPU. But the claim only gives access. How the pods actually share the GPU is a setting of the driver. With NVIDIA's time-slicing, the pods' workloads interleave on the GPU, and NVIDIA's docs say there is no memory or fault isolation between them ([NVIDIA's time-slicing docs](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/gpu-sharing.html)). In NVIDIA's DRA driver, time-slicing is still in alpha ([AWS's EKS guide](https://docs.aws.amazon.com/eks/latest/userguide/device-management-nvidia-dra-device-plugin.html)). With MIG, each pod gets its own isolated slice with its own memory instead. These are separate pods, not threads of one program.

**Step 9. Putting it together.** The driver publishes the devices (ResourceSlices) and the categories (DeviceClasses). You write a template that names a class and, if you want, adds a CEL rule. The pod points at the template, Kubernetes turns it into a claim, and the scheduler matches the claim against the ResourceSlices. Dynamic Resource Allocation (stable since Kubernetes 1.34) lets a pod describe the device it needs, and the driver picks a match at scheduling time.

![Dynamic Resource Allocation](diagrams/sections/d06-4.svg)

**Going deeper, by topic.**

- **How the chosen GPU gets into the container (CDI):** DRA does two jobs: the scheduler chooses the GPU, and then that exact GPU has to reach the container. The second job uses CDI (Container Device Interface), an open standard for container runtimes, not a Kubernetes feature. A CDI entry is a named device, and it lists what the runtime must change when it creates the container: device files, mounts, hooks and environment variables ([CDI tutorial](https://github.com/cncf-tags/container-device-interface/blob/main/TUTORIAL.md)). The driver's kubelet plugin prepares the allocated GPU and answers the kubelet with its CDI names ([DRA kubelet API](https://github.com/kubernetes/kubelet/blob/master/pkg/apis/dra/v1/api.proto)), and the container runtime (containerd or CRI-O) applies the matching entries from the CDI files on the node, so the container starts with that GPU's device files and driver libraries in place. This is a requirement for DRA: NVIDIA lists a container runtime that supports CDI, and is set to use it for DRA-allocated devices, as a prerequisite of its DRA driver ([NVIDIA docs](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/dra-intro-install.html)).

![How the chosen GPU gets into the container (CDI)](diagrams/extra/dra-cdi-flow.svg)

- **How do you install DRA?** You don't install DRA itself. The API (`ResourceClaim`, `ResourceClaimTemplate`, `DeviceClass`) is built into Kubernetes 1.34 and later. What you install is a **DRA driver** for your hardware: it advertises the devices on each node and allocates the one a claim asks for. For NVIDIA GPUs that's the NVIDIA DRA driver, installed with Helm:

```bash
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
helm repo update
helm install nvidia-dra-driver-gpu nvidia/nvidia-dra-driver-gpu \
  --create-namespace --namespace nvidia-dra-driver-gpu \
  --set resources.computeDomains.enabled=false \
  --set gpuResourcesEnabledOverride=true    # turn on GPU allocation

# check that the driver pods are running
kubectl get pods -n nvidia-dra-driver-gpu

# the driver's chart creates the DeviceClass gpu.nvidia.com (Step 3)
kubectl get deviceclass gpu.nvidia.com
```

- **Before you rely on it:** check these points.
  - **Kubernetes version:** NVIDIA asks for 1.34.2 or later.
  - **Container runtime:** it must support CDI and be set to use it for DRA-allocated devices (see the CDI point).
  - **Chart name:** from v0.4.0 the driver moved to `kubernetes-sigs` and the chart is called `dra-driver-nvidia-gpu`; use the name that matches your version.
  - **Don't run two allocators:** if the NVIDIA GPU Operator is installed, use v25.10.0 or later with its device plugin turned off, and label the GPU nodes (for example `nvidia.com/dra-kubelet-plugin=true`) so the driver runs only there.
  - **Old-style requests aren't DRA:** a pod that asks for `nvidia.com/gpu: 1` under `resources.limits` still goes through the device plugin path, even with a DRA driver installed.
  - **Kueue quotas:** if you use Kueue, a quota on `nvidia.com/gpu` does not count devices that pods claim through DRA unless you map them. See [counting DRA devices in Kueue quotas](#d02-4).
  - **On EKS:** Bottlerocket nodes work with the NVIDIA DRA driver once you turn off the NVIDIA device plugin built into the Bottlerocket NVIDIA images (this needs Bottlerocket 1.63.0 or later, per [AWS](https://docs.aws.amazon.com/eks/latest/userguide/device-management-nvidia-mig.html)). Karpenter 1.15 or later needs `settings.enableDRA: true` (alpha; [Karpenter docs](https://karpenter.sh/docs/tasks/dra/)). EKS Auto Mode includes the NVIDIA and EFA DRA drivers on Kubernetes 1.36 and later, as [AWS announced in November 2026](https://aws.amazon.com/about-aws/whats-new/2026/11/amazon-eks-auto-mode-karpenter-dra/); you create the DeviceClass objects yourself. Some AWS pages written earlier, including the 1.37 release notes, still say Auto Mode doesn't support the NVIDIA DRA driver, so check the current AWS docs.

- **How DRA differs from the device plugin:** the table compares the two. In short, CDI does not change how DRA finds a GPU. It changes how the found GPU reaches the container: through a standard file the runtime reads, instead of a setting that NVIDIA's runtime has to interpret.

| | Device plugin | DRA |
|---|---|---|
| **What the scheduler knows** | A count of free GPUs per node | Each GPU's own facts (ResourceSlice) and which one a claim got |
| **Who picks the exact GPU** | The plugin, when the kubelet asks at container start. The scheduler never knows which one. | The scheduler, and it records the pick in the claim |
| **How the container gets the GPU** | The plugin's reply lists device files and a setting that names the GPU. NVIDIA's container runtime reads that setting. This is the plugin's default in its README for version 0.17.1. The plugin can use CDI instead, and the GPU Operator has done so by default since 25.10. | Always CDI: the driver returns CDI names and the runtime applies them |
| **What the runtime must support** | Nothing special by default. CDI is optional. | CDI, required |

- **When GPUs on a node differ:** a node label describes the node as one thing, so a node with one 80 GB and one 24 GB GPU can't be described honestly. DRA checks every GPU separately, so asking for two big GPUs only matches if *both* are big:

```yaml
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: two-big-gpus
spec:
  spec:
    devices:
      requests:
      - name: gpus
        exactly:
          deviceClassName: gpu.nvidia.com
          count: 2                          # two devices... #!
          selectors:
          - cel:                            # ...and each one must pass this check
              expression: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("40Gi")) >= 0 #!
```

- **When several devices must fit together:** a GPU and the network card it sends data through should sit on the same PCIe root, so traffic doesn't cross the CPU. Both drivers publish the standard attribute `resource.kubernetes.io/pcieRoot`, and `matchAttribute` requires the two devices to share it. On EKS the network card is an EFA device, offered by the dranet driver. The same constraint with `requests: ["gpus"]` and `count: 2` keeps two GPUs on the same PCIe root. The picture shows the idea, and the YAML after it asks for a GPU plus an EFA device:

![When several devices must fit together](diagrams/extra/dra-pcie-root.svg)

```yaml
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: gpu-plus-efa
spec:
  spec:
    devices:
      requests:
      - name: gpu
        exactly:
          deviceClassName: gpu.nvidia.com
      - name: efa
        exactly:
          deviceClassName: efa.networking.k8s.aws   # from the dranet driver
      constraints:
      - requests: ["gpu", "efa"]                   # these two devices... #!
        matchAttribute: resource.kubernetes.io/pcieRoot   # ...must share a PCIe root #!
```

- **When pods share a GPU:** create one `ResourceClaim` by name (not a template) and point several pods at it, so they all get the same GPU. How they share it is a driver setting; here NVIDIA's time-slicing (it may need to be turned on in the driver). Time-slicing means the pods take turns on the GPU. There is no memory or compute isolation between them ([AWS docs](https://docs.aws.amazon.com/eks/latest/userguide/device-management-nvidia.html)); MIG is what isolates. A claim made from a *template* is a separate claim for every pod, so template replicas share a GPU only when the driver's **consumable shares** are on. That is an alpha feature in NVIDIA's driver, and the Kubernetes side is on by default from 1.36 ([NVIDIA docs](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/dra-intro-install.html)). Without it, each replica gets a whole GPU. Here is one claim and one of the pods that share it:

```yaml
apiVersion: resource.k8s.io/v1
kind: ResourceClaim                # one claim, not a template
metadata:
  name: shared-gpu
spec:
  devices:
    requests:
    - name: gpu
      exactly:
        deviceClassName: gpu.nvidia.com
    config:
    - requests: ["gpu"]
      opaque:                      # passed to the driver as-is
        driver: gpu.nvidia.com
        parameters:
          apiVersion: resource.nvidia.com/v1beta1
          kind: GpuConfig
          sharing:
            strategy: TimeSlicing #!
---
apiVersion: v1
kind: Pod
metadata:
  name: notebook-a                 # notebook-b is the same, with its own name
spec:
  resourceClaims:
  - name: gpu
    resourceClaimName: shared-gpu  # by name, so every pod gets the same GPU #!
  containers:
  - name: jupyter
    image: jupyter/base-notebook:latest
    resources:
      claims:
      - name: gpu
```

- **When you want a fallback:** `firstAvailable` lists options in order of preference, and the scheduler uses the first one it can satisfy. The application has to cope with either result (one GPU or two), for example by reading how many GPUs it was given at startup. Here is one Hopper GPU, or else two Ampere GPUs:

```yaml
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: h100-or-two-a100
spec:
  spec:
    devices:
      requests:
      - name: gpu
        firstAvailable:                 # try these in order #!
        - name: one-hopper
          deviceClassName: gpu.nvidia.com
          selectors:
          - cel:
              expression: device.attributes["gpu.nvidia.com"].architecture == "Hopper"
        - name: two-ampere              # only if no Hopper GPU is free
          deviceClassName: gpu.nvidia.com
          count: 2
          selectors:
          - cel:
              expression: device.attributes["gpu.nvidia.com"].architecture == "Ampere"
```

### Different GPUs in one server, and MIG

**Scenario.** A research lab runs on-prem servers that mix GPU models, and wants to split one 80 GB H100 so several notebooks can share it. Node labels, the device plugin's count and DRA each see these servers differently, and only some of them can tell the GPUs apart.

**First, what MIG is.** MIG (Multi-Instance GPU) is a feature built into NVIDIA GPUs from the Ampere generation on, such as the A100 and H100 ([NVIDIA GPU Operator docs](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/gpu-operator-mig.html)). It cuts one physical GPU into isolated slices, up to seven on an A100 or H100, each with its own memory and its own share of the compute units. Kubernetes can then hand each slice to a different pod.

A slice is named by its size, for example `3g.40gb`: the number before the `g` is how many of the GPU's seven compute slices it gets, and the number before `gb` is its memory in GB.

![How to read a MIG slice name](diagrams/extra/mig-slice-names.svg)

Now the same lab server, seen through each advertising method:

![Different GPUs in one server, and MIG](diagrams/sections/d06-5.svg)

**What the picture shows.** Two servers, and what each advertising method reports for them.

- **The mixed server:** one A100 (80 GB) and one T4 (16 GB) in the same machine. Cloud GPU instances always have identical GPUs, but on-prem and lab servers can mix models.
- **Labels on the mixed server:** a node label holds one value, so `nvidia.com/gpu.product` and `nvidia.com/gpu.memory` can't describe both GPUs honestly.
- **Count on the mixed server:** `nvidia.com/gpu: 2`. A pod asking for one GPU can get either the A100 or the T4.
- **DRA on the mixed server:** each GPU is its own entry, so a claim for "at least 40Gi" can only get the A100.
- **The MIG server:** one 80 GB H100 cut into four slices, named as in the slice-name picture: one `3g.40gb`, one `2g.20gb` and two `1g.10gb`. No slice is a whole GPU, and each one is advertised on its own.
- **MIG is not another way to advertise GPUs.** It changes what the GPU is, and the mechanisms you already know then report slices instead of whole GPUs:
  - **Count (device plugin):** each slice is counted and handed to one container, like a GPU. The `single` strategy calls every slice `nvidia.com/gpu` and needs them all the same size; the `mixed` strategy gives each size its own count, such as `nvidia.com/mig-1g.10gb: 2`. Either way the layout is set in advance.
  - **Labels (GPU Feature Discovery):** with `mixed`, the node gets labels per slice size, so pods can still choose a node by what it holds. With `single`, the product label gains the slice profile instead.
  - **DRA:** each slice is its own device under the `mig.nvidia.com` class, with a `profile` attribute and its own memory, so a claim can ask for a profile or a minimum size. The class's device-type check, explained in Step 5 of the Dynamic Resource Allocation section, is what keeps these slices out of whole-GPU claims.
  - **Reserving works the same:** labels choose the node, and the count or the claim reserves one slice on it. The difference is that the unit is a slice.

**Going deeper.**

- **Do you need to install anything? Yes, on the Kubernetes side.** The card has MIG built in, but Kubernetes sees no slices until something creates them and something else advertises them. These are separate jobs with separate pieces:

| Job | Who does it | Needed? |
|---|---|---|
| **Cut the GPU into slices** (switch MIG on, create the layout) | **MIG Manager**, part of the GPU Operator. NVIDIA's `mig-parted` tool does the same by hand on a host. | **Always.** The device plugin and DRA only advertise slices that already exist (the alpha DynamicMIG feature is the exception). |
| **Advertise the slices as a count** (`nvidia.com/mig-1g.10gb: 2`) | **Device plugin**, with a MIG strategy | The classic path. |
| **Advertise each slice as a device** (class `mig.nvidia.com`) | **DRA driver**, with the device plugin turned off | The DRA path, instead of the device plugin. |
| **Label the slices** (`nvidia.com/mig-1g.10gb.count`, `.memory`) | **GPU Feature Discovery** | Optional. Only if pods must choose a node by slice size. |
| **Mark the node as having an NVIDIA card** | **Node Feature Discovery** | A helper only. GPU Feature Discovery builds on it. |

> **Installing it (device plugin path).** Nothing is installed on the card itself. You need a MIG-capable GPU (on AWS, MIG is available on the P-family instance types with A100 and H100 GPUs, per [AWS's MIG on EKS guide](https://docs.aws.amazon.com/eks/latest/userguide/device-management-nvidia-mig.html)) and the NVIDIA GPU Operator, which installs the driver, device plugin, NFD and GFD. Install it with a MIG strategy, which also deploys the MIG manager, then label a node with the layout you want:
>
> ```bash
> helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
> helm repo update
> helm install --wait --generate-name \
>   -n gpu-operator --create-namespace \
>   nvidia/gpu-operator \
>   --set mig.strategy=mixed
>
> # "balanced" on an 80 GB H100: one 3g.40gb, one 2g.20gb, two 1g.10gb
> kubectl label node gpu-node-1 nvidia.com/mig.config=all-balanced --overwrite
> ```
>
> The strategy only decides how slices are advertised; it does not switch MIG on. The MIG manager does that when it sees the layout label: it stops the operator's GPU pods on that node, turns MIG mode on, cuts the slices and restarts them. Nothing else may be using the GPUs while it runs, so move pods that use them off first, and some cloud nodes need a reboot (see NVIDIA's GPU Operator MIG page). To change the layout later, label the node again, for example `nvidia.com/mig.config=all-1g.10gb`. Pin a chart version in production.
>
> **With DRA instead:** install the Operator with its device plugin turned off and the DRA driver on, as in the Dynamic Resource Allocation section, and set the layout with the same node label. Then restart the DRA driver pod so it discovers the slices the MIG manager created.
>
> **Check (MIG):** `kubectl get node gpu-node-1 --show-labels | tr ',' '\n' | grep mig`, and wait for `nvidia.com/mig.config.state=success`.

- Newer versions of NVIDIA's DRA driver can also create MIG slices on demand for a claim and remove them when the claim is released (an alpha feature called DynamicMIG), instead of relying on a fixed layout.
- Asking for a MIG slice, with the device plugin and with DRA:

```yaml
# Device plugin, "mixed" MIG strategy: ask for a slice size by resource name
apiVersion: v1
kind: Pod
metadata:
  name: notebook-small
spec:
  containers:
  - name: jupyter
    image: jupyter/base-notebook:latest
    resources:
      limits:
        nvidia.com/mig-1g.10gb: 1 #!
---
# DRA: ask for a MIG slice by profile
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: small-mig-slice
spec:
  spec:
    devices:
      requests:
      - name: mig
        exactly:
          deviceClassName: mig.nvidia.com #!
          selectors:
          - cel:
              expression: device.attributes["gpu.nvidia.com"].profile == "1g.10gb" #!
---
apiVersion: v1
kind: Pod
metadata:
  name: notebook-small-dra
spec:
  resourceClaims:
  - name: mig
    resourceClaimTemplateName: small-mig-slice
  containers:
  - name: jupyter
    image: jupyter/base-notebook:latest
    resources:
      claims:
      - name: mig
```

