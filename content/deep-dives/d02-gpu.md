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
- Two labeling tools often run next to it: Node Feature Discovery and GPU Feature Discovery. They describe the GPUs but don't hand them out; see [How GPUs are advertised](#d06-2).
- A minimal vLLM Deployment on such a node still needs a GPU limit, a toleration for the GPU taint, and a PVC as a model cache, so restarts don't re-download the weights.
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
- **Pod A, pin a model (Option 1):** a `nodeSelector` on `nvidia.com/gpu.product` set to the A100 model. Only the p4de passes.
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

![Dynamic Resource Allocation](diagrams/sections/d06-4.svg)

**What the picture shows.** The DRA objects, who creates each one, and how a claim turns into one specific GPU in a container.

- **DeviceClass (from the driver):** `gpu.nvidia.com` names a category of devices. The NVIDIA DRA driver creates it when installed (plus `mig.nvidia.com` for MIG slices).
- **ResourceSlice (from the driver, per node):** lists every device on the node with its attributes, such as product name and architecture, and its capacity, such as memory.
- **ResourceClaimTemplate (you write it):** describes the device a pod needs: a device class plus a CEL selector such as "memory at least 40Gi".
- **ResourceClaim (Kubernetes creates it):** one claim per pod, made from the template when the pod is created and deleted with the pod.
- **Pod:** lists the claim under `spec.resourceClaims` and gives it to a container with `resources.claims`; there's no `nvidia.com/gpu` count.
- **kube-scheduler (DynamicResources plugin):** filters out nodes with no free matching device, picks a node and a device, and writes that allocation into the claim.
- **kubelet and the DRA driver's kubelet plugin:** prepare exactly the allocated device on the node and expose it to the container.

**Going deeper.**

- Dynamic Resource Allocation (stable since Kubernetes 1.34) lets a pod describe the device it needs, and the driver picks a match at scheduling time.

```yaml
# DRA: describe the GPU you need; the DRA driver finds a match.
# The device class and attribute names come from the driver (here NVIDIA's).
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
              expression: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("40Gi")) >= 0 #!
---
apiVersion: v1
kind: Pod
metadata:
  name: inference-dra
spec:
  resourceClaims:
  - name: gpu
    resourceClaimTemplateName: gpu-40gb-plus #!
  containers:
  - name: server
    image: vllm/vllm-openai:latest
    resources:
      claims: #!
      - name: gpu                    # no nvidia.com/gpu count: the claim is the request
```

```yaml
# DRA: describe the GPU you need; the DRA driver finds a match.
# The device class and attribute names come from the driver (here NVIDIA's).
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
              expression: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("40Gi")) >= 0 #!
---
apiVersion: v1
kind: Pod
metadata:
  name: inference-dra
spec:
  resourceClaims:
  - name: gpu
    resourceClaimTemplateName: gpu-40gb-plus #!
  containers:
  - name: server
    image: vllm/vllm-openai:latest
    resources:
      claims: #!
      - name: gpu                    # no nvidia.com/gpu count: the claim is the request
```

> **Note: what this example does, line by line**
>
> Together, the two manifests ask for "any NVIDIA GPU with at least 40 GiB of memory". Notice there is no `nvidia.com/gpu: 1` anywhere in this YAML: that line belongs to the older device-plugin way used in the [GPU labels example](#gpu-labels-yaml) (Option 1 and Option 2), not to DRA.
>
> **Part 1: the `ResourceClaimTemplate` describes the GPU you want**
>
> - **`name: gpu-40gb-plus`:** the name the pod uses to point at this request.
> - **`requests: - name: gpu`:** "I need one device, and I'll call it `gpu`."
> - **`exactly:`:** allocate exactly one device that matches. DRA can also list alternatives ("this, or else that"); here there's only one option.
> - **`deviceClassName: gpu.nvidia.com`:** only look at NVIDIA GPUs. The NVIDIA DRA driver creates this class when you install it.
> - **The `cel` selector:** a small filter in CEL (Common Expression Language) that reads "this device's memory compared with 40Gi is 0 or more", that is, at least 40 GiB. An A10G (24 GB) is filtered out; an A100 (40 or 80 GB) or an H100 (80 GB) qualifies. The `memory` attribute is published by NVIDIA's driver, which is why the names depend on the driver.
>
> **Part 2: the Pod uses it**
>
> - **`spec.resourceClaims`:** create a claim for this pod from the `gpu-40gb-plus` template. Each pod gets its own ResourceClaim (named something like `inference-dra-gpu-x7k2p`), which is deleted with the pod. That's why it's a *template*: ten replicas get ten separate claims instead of fighting over one GPU.
> - **`containers[].resources.claims: - name: gpu`:** give the `server` container access to that device. In a pod with several containers, you choose which ones see the GPU.
> - **No GPU count under `resources.limits`:** the container lists `claims` instead. The claim *is* the request, so there is nothing to count.
>
> **What happens when you apply it**
>
> 1. **The claim is created:** Kubernetes makes the pod's ResourceClaim from the template.
> 2. **The scheduler filters and allocates:** its DynamicResources plugin checks the devices each node publishes (as ResourceSlices, written by the driver) and filters out nodes with no free NVIDIA GPU of 40 GiB or more. Then it picks a node and records the exact GPU it chose in the claim.
> 3. **The kubelet prepares the device:** on that node, the kubelet asks the driver's kubelet plugin to set up that GPU and expose it to the `server` container.
> 4. **The server starts:** vLLM starts and sees exactly one GPU that's big enough.
>
> **Note: how do you install DRA?**
>
> You don't install DRA itself. The API (`ResourceClaim`, `ResourceClaimTemplate`, `DeviceClass`) is built into Kubernetes 1.34 and later. What you install is a **DRA driver** for your hardware: it advertises the devices on each node and allocates the one a claim asks for. For NVIDIA GPUs that's the NVIDIA DRA driver, installed with Helm:
>
> ```bash
> helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
> helm repo update
> helm install nvidia-dra-driver-gpu nvidia/nvidia-dra-driver-gpu \
>   --create-namespace --namespace nvidia-dra-driver-gpu \
>   --set resources.computeDomains.enabled=false \
>   --set gpuResourcesEnabledOverride=true    # turn on GPU allocation
>
> # check that the driver pods are running
> kubectl get pods -n nvidia-dra-driver-gpu
>
> # the driver creates the DeviceClass the claim above refers to
> kubectl get deviceclass gpu.nvidia.com
> ```
>
> Things to check before you rely on it:
>
> - **Kubernetes version:** NVIDIA asks for 1.34.2 or later.
> - **Chart name:** from v0.4.0 the driver moved to `kubernetes-sigs` and the chart is called `dra-driver-nvidia-gpu`; use the name that matches your version.
> - **Don't run two allocators:** if the NVIDIA GPU Operator is installed, use v25.10.0 or later with its device plugin turned off, and label the GPU nodes (for example `nvidia.com/dra-kubelet-plugin=true`) so the driver runs only there.
> - **Old-style requests aren't DRA:** a pod that asks for `nvidia.com/gpu: 1` under `resources.limits` still goes through the device plugin path, even with a DRA driver installed.
> - **On EKS:** Bottlerocket nodes aren't supported by the NVIDIA DRA driver; Karpenter needs `settings.enableDRA: true` (alpha); and support for EKS Auto Mode was announced in November 2026 for Kubernetes 1.36+, so check the current AWS docs.

- **DRA compared with node labels and the device plugin.** [Option 2 in the GPU labels example](#gpu-labels-yaml) gets the same "at least 40 GB" result without DRA. It works in three steps: the **node** carries a label with its per-GPU memory (`nvidia.com/gpu.memory: "81920"` in the GPU node manifest at the top of that example, added automatically by GPU Feature Discovery); Option 2 filters on that label with node affinity (`Gt 40000`); then it asks the device plugin for `nvidia.com/gpu: 1`, which only counts GPUs. DRA instead describes the **device** itself, so the scheduler checks each GPU's own properties. For one big GPU both work. The four cases below are where DRA goes further.

> **Where DRA goes further: four cases**
>
> - **When GPUs on a node differ:** a node label describes the node as one thing, so a node with one 80 GB and one 24 GB GPU can't be described honestly. DRA checks every GPU separately, so asking for two big GPUs only matches if *both* are big:
>
>     ```yaml
>     apiVersion: resource.k8s.io/v1
>     kind: ResourceClaimTemplate
>     metadata:
>       name: two-big-gpus
>     spec:
>       spec:
>         devices:
>           requests:
>           - name: gpus
>             exactly:
>               deviceClassName: gpu.nvidia.com
>               count: 2                          # two devices... #!
>               selectors:
>               - cel:                            # ...and each one must pass this check
>                   expression: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("40Gi")) >= 0 #!
>     ```
>
> - **When several devices must fit together:** a GPU and the network card it sends data through should sit on the same PCIe root, so traffic doesn't cross the CPU. Both drivers publish the standard attribute `resource.kubernetes.io/pcieRoot`, and `matchAttribute` requires the two devices to share it. On EKS the network card is an EFA device, offered by the dranet driver:
>
>     ```yaml
>     apiVersion: resource.k8s.io/v1
>     kind: ResourceClaimTemplate
>     metadata:
>       name: gpu-plus-efa
>     spec:
>       spec:
>         devices:
>           requests:
>           - name: gpu
>             exactly:
>               deviceClassName: gpu.nvidia.com
>           - name: efa
>             exactly:
>               deviceClassName: efa.networking.k8s.aws   # from the dranet driver
>           constraints:
>           - requests: ["gpu", "efa"]                   # these two devices... #!
>             matchAttribute: resource.kubernetes.io/pcieRoot   # ...must share a PCIe root #!
>     ```
>
>     The same constraint with `requests: ["gpus"]` and `count: 2` keeps two GPUs on the same PCIe root.
>
> - **When pods share a GPU:** create one `ResourceClaim` by name (not a template) and point several pods at it, so they all get the same GPU. How they share it is a driver setting; here NVIDIA's time-slicing (it may need to be turned on in the driver):
>
>     ```yaml
>     apiVersion: resource.k8s.io/v1
>     kind: ResourceClaim                # one claim, not a template
>     metadata:
>       name: shared-gpu
>     spec:
>       devices:
>         requests:
>         - name: gpu
>           exactly:
>             deviceClassName: gpu.nvidia.com
>         config:
>         - requests: ["gpu"]
>           opaque:                      # passed to the driver as-is
>             driver: gpu.nvidia.com
>             parameters:
>               apiVersion: resource.nvidia.com/v1beta1
>               kind: GpuConfig
>               sharing:
>                 strategy: TimeSlicing #!
>     ---
>     apiVersion: v1
>     kind: Pod
>     metadata:
>       name: notebook-a                 # notebook-b is the same, with its own name
>     spec:
>       resourceClaims:
>       - name: gpu
>         resourceClaimName: shared-gpu  # by name, so every pod gets the same GPU #!
>       containers:
>       - name: jupyter
>         image: jupyter/base-notebook:latest
>         resources:
>           claims:
>           - name: gpu
>     ```
>
> - **When you want a fallback:** `firstAvailable` lists options in order of preference, and the scheduler uses the first one it can satisfy. Here: one Hopper GPU, or else two Ampere GPUs:
>
>     ```yaml
>     apiVersion: resource.k8s.io/v1
>     kind: ResourceClaimTemplate
>     metadata:
>       name: h100-or-two-a100
>     spec:
>       spec:
>         devices:
>           requests:
>           - name: gpu
>             firstAvailable:                 # try these in order #!
>             - name: one-hopper
>               deviceClassName: gpu.nvidia.com
>               selectors:
>               - cel:
>                   expression: device.attributes["gpu.nvidia.com"].architecture == "Hopper"
>             - name: two-ampere              # only if no Hopper GPU is free
>               deviceClassName: gpu.nvidia.com
>               count: 2
>               selectors:
>               - cel:
>                   expression: device.attributes["gpu.nvidia.com"].architecture == "Ampere"
>     ```
>
>     The application has to cope with either result (one GPU or two), for example by reading how many GPUs it was given at startup.

### Different GPUs in one server, and MIG

**Scenario.** A research lab runs on-prem servers that mix GPU models, and wants to split one 80 GB H100 so several notebooks can share it. Node labels, the device plugin's count and DRA each see these servers differently, and only some of them can tell the GPUs apart.

![Different GPUs in one server, and MIG](diagrams/sections/d06-5.svg)

**What the picture shows.** Two servers, and what each advertising method reports for them.

- **The mixed server:** one A100 (80 GB) and one T4 (16 GB) in the same machine. Cloud GPU instances always have identical GPUs, but on-prem and lab servers can mix models.
- **Labels on the mixed server:** a node label holds one value, so `nvidia.com/gpu.product` and `nvidia.com/gpu.memory` can't describe both GPUs honestly.
- **Count on the mixed server:** `nvidia.com/gpu: 2`. A pod asking for one GPU can get either the A100 or the T4.
- **DRA on the mixed server:** each GPU is its own entry, so a claim for "at least 40Gi" can only get the A100.
- **The MIG server:** MIG (Multi-Instance GPU) splits one A100 or H100 into isolated slices, each with its own memory and compute. Here an 80 GB H100 is split into one `3g.40gb`, one `2g.20gb` and two `1g.10gb` slices.
- **MIG with the device plugin:** the "single" strategy needs all slices on a node to be the same and still calls them `nvidia.com/gpu`; the "mixed" strategy publishes one count per size, such as `nvidia.com/mig-1g.10gb: 2`. Either way the layout is set in advance.
- **MIG with DRA:** each slice is its own device under the `mig.nvidia.com` class, with a `profile` attribute and its own memory, so a claim can ask for a profile or a minimum size.

**Going deeper.**

- Changing a MIG layout with the GPU Operator: label the node with the layout you want, for example `nvidia.com/mig.config=all-1g.10gb`, and the operator's MIG manager reconfigures the GPU. Pods using the GPU must be moved off first.
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

