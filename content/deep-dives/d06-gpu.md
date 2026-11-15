---
id: d-gpu
title: "How GPUs become schedulable"
short: "Kubernetes and GPU"
---
### The NVIDIA device plugin

**Scenario.** A team adds GPU nodes to their Kubernetes cluster and deploys a pod that requests `nvidia.com/gpu: 1`. The pod stays `Pending` with "Insufficient nvidia.com/gpu", even though the nodes clearly have GPUs installed. Kubernetes can't see them yet.

![The NVIDIA device plugin](diagrams/sections/d06-1.svg)

**What the picture shows.** How the NVIDIA device plugin makes GPUs visible to Kubernetes, in numbered steps.

- **Installing the plugin:** we add a module to the cluster setup (Terraform) that installs the NVIDIA device plugin for Kubernetes.
- **Step 1:** the plugin runs on every GPU worker node as a **DaemonSet**.
- **Step 2:** on each node, the plugin registers itself with the **kubelet**, which reports the node's GPUs to the **API server** as `nvidia.com/gpu`. From then on GPUs are a countable resource the scheduler can use. This is the missing piece in the scenario.
- **Steps 3 and 4:** when the scheduler places a pod that requests a GPU, the pod is bound to that worker node.
- **Step 5:** the kubelet on that node tells the plugin how many GPUs the pod needs, and the plugin assigns specific GPU devices to the container.

**Going deeper.**

- Two discovery layers come first.
- **Node Feature Discovery** labels basic hardware facts (PCI vendor and class), and **GPU Feature Discovery** adds GPU details (model, memory, family, CUDA version).
- The device plugin then does four jobs:

- it discovers devices;
- it allocates them exclusively to containers;
- it monitors their health;
- it advertises them as `nvidia.com/gpu` for scheduling.

- A minimal vLLM Deployment on such a node still needs a GPU limit, a toleration for the GPU taint, and a PVC as a model cache, so restarts don't re-download the weights.
- Plugins expose counts, not topology.
- That gap is why topology labels, Dynamic Resource Allocation and topology-aware schedulers exist.
